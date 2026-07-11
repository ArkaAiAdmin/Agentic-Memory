"""SSO JWT signing/verification, key management, token lifecycle.

Tests the core cryptographic operations in ``infra/authlib_sso.py``:

- KeyManager: generate, get, get_active, revoke, list_keys, public_jwk_set
- sign_token / verify_token round-trip (RSA-256, lifetime, claims)
- Error paths: revoked key, unknown kid, malformed token, expired token
- verify_oidc_id_token against a synthetic IdP JWKS

All tests use an in-memory or temp-file SQLite DB bootstrapped with
the production schema (incl. migration 047 — idem_token_key table).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

INSTALL_DIR = os.environ.get(
    "MEMORY_INSTALL_ROOT",
    str(Path.home() / ".config" / "agentic-memory"),
)
sys.path.insert(0, str(INSTALL_DIR))

import infra.memory_common as memory_common  # noqa: E402
from _fixtures import bootstrap_temp_db_clean  # noqa: E402


def _init_db(db_path: Path) -> None:
    bootstrap_temp_db_clean(db_path)


class TestKeyManager(unittest.TestCase):
    """KeyManager tests — generate, retrieve, revoke, list."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sso_jwt_km_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _conn(self):
        import sqlite3
        return sqlite3.connect(str(self.db_path))

    def test_generate_creates_key_with_kid(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            kid = KeyManager.generate(conn)
            self.assertTrue(kid, "kid should be non-empty")
            self.assertIsInstance(kid, str)
            row = conn.execute(
                "SELECT kid, public_jwk, private_jwk, revoked_at FROM idem_token_key WHERE kid = ?",
                (kid,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row[3], "new key should not be revoked")
            pub = json.loads(row[1])
            self.assertIn("n", pub)
            self.assertIn("e", pub)
        finally:
            conn.close()

    def test_get_returns_key(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            kid = KeyManager.generate(conn)
            key = KeyManager.get(conn, kid)
            self.assertIsNotNone(key)
            self.assertEqual(key["kid"], kid)
            self.assertIn("public_jwk", key)
            self.assertIn("private_jwk", key)
        finally:
            conn.close()

    def test_get_unknown_kid_returns_none(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            key = KeyManager.get(conn, "nonexistent-kid")
            self.assertIsNone(key)
        finally:
            conn.close()

    def test_get_active_generates_when_empty(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            key = KeyManager.get_active(conn)
            self.assertIsNotNone(key)
            self.assertIn("kid", key)
            self.assertIsNone(key.get("revoked_at"))
        finally:
            conn.close()

    def test_get_active_returns_latest_non_revoked(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            k1 = KeyManager.generate(conn)
            time.sleep(1.1)
            k2 = KeyManager.generate(conn)
            active = KeyManager.get_active(conn)
            self.assertEqual(active["kid"], k2, "should return most recent")
        finally:
            conn.close()

    def test_generate_multiple_keys_are_unique(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            kids = {KeyManager.generate(conn) for _ in range(3)}
            self.assertEqual(len(kids), 3)
        finally:
            conn.close()

    def test_revoke_sets_revoked_at(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            kid = KeyManager.generate(conn)
            result = KeyManager.revoke(conn, kid)
            self.assertTrue(result)
            key = KeyManager.get(conn, kid)
            self.assertIsNotNone(key["revoked_at"])
        finally:
            conn.close()

    def test_revoke_nonexistent_returns_false(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            result = KeyManager.revoke(conn, "no-such-kid")
            self.assertFalse(result)
        finally:
            conn.close()

    def test_list_keys_returns_all(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            k1 = KeyManager.generate(conn)
            time.sleep(0.01)
            k2 = KeyManager.generate(conn)
            keys = KeyManager.list_keys(conn)
            kids = [k["kid"] for k in keys]
            self.assertIn(k1, kids)
            self.assertIn(k2, kids)
        finally:
            conn.close()

    def test_public_jwk_set_excludes_revoked(self):
        from infra.authlib_sso import KeyManager
        conn = self._conn()
        try:
            k1 = KeyManager.generate(conn)
            k2 = KeyManager.generate(conn)
            KeyManager.revoke(conn, k1)
            jwks = KeyManager.public_jwk_set(conn)
            self.assertEqual(len(jwks["keys"]), 1)
            self.assertEqual(jwks["keys"][0]["kid"], k2)
        finally:
            conn.close()


class TestSignVerifyToken(unittest.TestCase):
    """sign_token / verify_token round-trip and error paths."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sso_jwt_sv_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _conn(self):
        import sqlite3
        return sqlite3.connect(str(self.db_path))

    def test_sign_and_verify_roundtrip(self):
        from infra.authlib_sso import sign_token, verify_token
        conn = self._conn()
        try:
            token, kid = sign_token(conn, {"sub": "user123", "provider": "okta"})
            self.assertIsInstance(token, str)
            self.assertTrue(token.startswith("eyJ"), "should be a JWT (starts with base64 header)")
            claims = verify_token(conn, token)
            self.assertEqual(claims["sub"], "user123")
            self.assertEqual(claims["provider"], "okta")
            self.assertEqual(claims["iss"], "agentic-memory")
            self.assertEqual(claims["aud"], "agentic-memory-api")
            self.assertIn("exp", claims)
            self.assertIn("iat", claims)
        finally:
            conn.close()

    def test_sign_with_custom_claims_and_expiry(self):
        from infra.authlib_sso import sign_token, verify_token
        conn = self._conn()
        try:
            token, kid = sign_token(
                conn, {"sub": "admin", "role": "superuser"},
                expires_in=60, issuer="test-iss", audience="test-aud",
            )
            claims = verify_token(
                conn, token, issuer="test-iss", audience="test-aud",
            )
            self.assertEqual(claims["role"], "superuser")
            self.assertEqual(claims["iss"], "test-iss")
            self.assertEqual(claims["aud"], "test-aud")
        finally:
            conn.close()

    def test_verify_revoked_key_token_raises(self):
        from infra.authlib_sso import KeyManager, sign_token, verify_token, SsoAuthError
        conn = self._conn()
        try:
            token, kid = sign_token(conn, {"sub": "u1", "provider": "okta"})
            KeyManager.revoke(conn, kid)
            with self.assertRaises(SsoAuthError):
                verify_token(conn, token)
        finally:
            conn.close()

    def test_verify_unknown_kid_raises(self):
        from infra.authlib_sso import verify_token, SsoAuthError
        conn = self._conn()
        try:
            # Sign with a key then delete from DB
            from infra.authlib_sso import sign_token
            token, kid = sign_token(conn, {"sub": "u1", "provider": "okta"})
            conn.execute("DELETE FROM idem_token_key WHERE kid = ?", (kid,))
            conn.commit()
            with self.assertRaises(SsoAuthError):
                verify_token(conn, token)
        finally:
            conn.close()

    def test_verify_malformed_token_raises(self):
        from infra.authlib_sso import verify_token, SsoAuthError
        conn = self._conn()
        try:
            with self.assertRaises(SsoAuthError):
                verify_token(conn, "not.a.jwt")
        finally:
            conn.close()

    def test_verify_expired_token_raises(self):
        from infra.authlib_sso import sign_token, verify_token, SsoAuthError
        conn = self._conn()
        try:
            # Sign with 0-second expiry — token expires at the current second.
            # With leeway=0 the token is rejected as expired; if leeway
            # were >0 a near-boundary token could slip through.
            token, kid = sign_token(
                conn, {"sub": "u1", "provider": "okta"},
                expires_in=0,
            )
            with self.assertRaises(SsoAuthError):
                verify_token(conn, token)
        finally:
            conn.close()

    def test_sign_with_revoked_key_raises(self):
        from infra.authlib_sso import KeyManager, sign_token, SsoConfigError
        conn = self._conn()
        try:
            kid = KeyManager.generate(conn)
            KeyManager.revoke(conn, kid)
            with self.assertRaises(SsoConfigError):
                sign_token(conn, {"sub": "u1", "provider": "okta"}, kid=kid)
        finally:
            conn.close()


class TestVerifyOidcIdToken(unittest.TestCase):
    """verify_oidc_id_token against a synthetic IdP JWKS."""

    def _generate_idp_jwks(self):
        """Create a synthetic RSA key pair and return (jwks, private_key, kid)."""
        from authlib.jose import JsonWebKey
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwk = JsonWebKey.import_key(priv_pem)
        priv_dict = jwk.as_dict(is_private=True)
        pub_dict = jwk.as_dict(is_private=False)
        kid = priv_dict.get("kid", "test-kid-1")
        pub_dict["kid"] = kid
        priv_dict["kid"] = kid
        jwks = {"keys": [pub_dict]}
        return jwks, priv_dict, kid

    def _sign_id_token(self, claims: dict, private_key: dict) -> str:
        from authlib.jose import JsonWebKey, JsonWebToken

        jwt = JsonWebToken(["RS256"])
        token = jwt.encode(
            {"alg": "RS256", "kid": private_key["kid"], "typ": "JWT"},
            claims,
            JsonWebKey.import_key(private_key),
        )
        return token.decode("utf-8") if isinstance(token, bytes) else token

    def test_verify_valid_id_token(self):
        jwks, priv_key, kid = self._generate_idp_jwks()
        from infra.authlib_sso import verify_oidc_id_token

        token = self._sign_id_token(
            {
                "sub": "ext-user-1",
                "email": "user@example.com",
                "iss": "https://idp.example.com",
                "aud": "my-client",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            },
            priv_key,
        )
        claims = verify_oidc_id_token(
            token, jwks,
            issuer="https://idp.example.com",
            audience="my-client",
        )
        self.assertEqual(claims["sub"], "ext-user-1")
        self.assertEqual(claims["email"], "user@example.com")

    def test_verify_id_token_bad_issuer_raises(self):
        jwks, priv_key, kid = self._generate_idp_jwks()
        from infra.authlib_sso import verify_oidc_id_token, SsoAuthError

        token = self._sign_id_token(
            {
                "sub": "ext-user-1",
                "iss": "https://wrong-issuer.com",
                "aud": "my-client",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            },
            priv_key,
        )
        with self.assertRaises(SsoAuthError):
            verify_oidc_id_token(
                token, jwks,
                issuer="https://idp.example.com",
                audience="my-client",
            )

    def test_verify_id_token_expired_raises(self):
        jwks, priv_key, kid = self._generate_idp_jwks()
        from infra.authlib_sso import verify_oidc_id_token, SsoAuthError

        token = self._sign_id_token(
            {
                "sub": "ext-user-1",
                "iss": "https://idp.example.com",
                "aud": "my-client",
                "exp": int(time.time()) - 60,
                "iat": int(time.time()) - 120,
            },
            priv_key,
        )
        with self.assertRaises(SsoAuthError):
            verify_oidc_id_token(
                token, jwks,
                issuer="https://idp.example.com",
                audience="my-client",
            )

    def test_verify_id_token_wrong_kid_raises(self):
        jwks, priv_key, kid = self._generate_idp_jwks()
        from infra.authlib_sso import verify_oidc_id_token, SsoAuthError

        # Modify kid in token header to not match any JWK
        token = self._sign_id_token(
            {
                "sub": "ext-user-1",
                "iss": "https://idp.example.com",
                "aud": "my-client",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            },
            priv_key,
        )
        # Replace kid in the JWK set
        for k in jwks["keys"]:
            k.pop("kid", None)
        jwks["keys"][0]["kid"] = "different-kid"

        with self.assertRaises(SsoAuthError):
            verify_oidc_id_token(
                token, jwks,
                issuer="https://idp.example.com",
                audience="my-client",
            )

    def test_verify_id_token_empty_jwks_raises(self):
        from infra.authlib_sso import verify_oidc_id_token, SsoAuthError

        with self.assertRaises(SsoAuthError):
            verify_oidc_id_token("fake.token.here", {"keys": []})

    def test_jwt_unverified_header_parses(self):
        from infra.authlib_sso import _jwt_unverified_header
        import base64, json

        header = {"alg": "RS256", "kid": "test-kid", "typ": "JWT"}
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
        token = f"{header_b64}.payload.sig"
        parsed = _jwt_unverified_header(token)
        self.assertEqual(parsed["kid"], "test-kid")
        self.assertEqual(parsed["alg"], "RS256")

    def test_jwt_unverified_header_malformed_raises(self):
        from infra.authlib_sso import _jwt_unverified_header, SsoAuthError

        with self.assertRaises(SsoAuthError):
            _jwt_unverified_header("not-a-valid-base64!!")

    def test_select_jwk_without_keys_dict(self):
        """_select_jwk handles a single key dict (not a JWKS wrapper)."""
        from infra.authlib_sso import _select_jwk
        from authlib.jose import JsonWebKey
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwk = JsonWebKey.import_key(priv_pem)
        pub_dict = jwk.as_dict(is_private=False)
        result = _select_jwk(pub_dict, None)
        self.assertIn("kty", result)


if __name__ == "__main__":
    unittest.main()
