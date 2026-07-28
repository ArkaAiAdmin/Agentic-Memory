"""SSO IdP metadata cache + SsoSession + SAML parsing tests.

Tests the following from ``infra/authlib_sso.py``:

- IdPMetadataCache: get, put, fetch (HTTP mocked), parse_saml_sso_url
- SsoSession.authorization_url for OIDC and SAML
- SsoSession.parse_callback for OIDC (JWT) and SAML (XML assertion)
- parse_saml_response: structural validation, NameID, attributes,
  expiry checks
- verify_saml_signature: closed-fail without pyxmlsec + lxml

All DB-backed tests use a temp file with the prod schema (incl.
migration 047 — sso_idp_cache table).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

INSTALL_DIR = os.environ.get(
    "MEMORY_INSTALL_ROOT",
    str(Path.home() / ".config" / "agentic-memory"),
)
sys.path.insert(0, str(INSTALL_DIR))

from _fixtures import bootstrap_temp_db_clean  # noqa: E402


def _init_db(db_path: Path) -> None:
    bootstrap_temp_db_clean(db_path)


# ---------------------------------------------------------------------------
# Sample SAML response XML (unsigned, valid-time-window)
# ---------------------------------------------------------------------------

_SAML_ASSERTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<saml2p:Response xmlns:saml2p="urn:oasis:names:tc:SAML:2.0:protocol"
                 xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion"
                 ID="_abc123" Version="2.0" IssueInstant="2026-07-10T00:00:00Z">
  <saml2:Issuer>https://idp.example.com</saml2:Issuer>
  <saml2p:Status>
    <saml2p:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </saml2p:Status>
  <saml2:Assertion ID="_assertion1" IssueInstant="2026-07-10T00:00:00Z">
    <saml2:Issuer>https://idp.example.com</saml2:Issuer>
    <saml2:Subject>
      <saml2:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">
        user@example.com
      </saml2:NameID>
      <saml2:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
        <saml2:SubjectConfirmationData NotOnOrAfter="2099-12-31T23:59:59Z"/>
      </saml2:SubjectConfirmation>
    </saml2:Subject>
    <saml2:Conditions NotBefore="2020-01-01T00:00:00Z" NotOnOrAfter="2099-12-31T23:59:59Z">
      <saml2:AudienceRestriction>
        <saml2:Audience>https://sp.example.com</saml2:Audience>
      </saml2:AudienceRestriction>
    </saml2:Conditions>
    <saml2:AttributeStatement>
      <saml2:Attribute Name="email" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
        <saml2:AttributeValue>user@example.com</saml2:AttributeValue>
      </saml2:Attribute>
      <saml2:Attribute Name="displayName" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
        <saml2:AttributeValue>Jane Doe</saml2:AttributeValue>
      </saml2:Attribute>
    </saml2:AttributeStatement>
  </saml2:Assertion>
</saml2p:Response>"""


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# SAML metadata XML (IdP SSO descriptor)
# ---------------------------------------------------------------------------

_SAML_METADATA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
                  entityID="https://idp.example.com">
  <IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
                         Location="https://idp.example.com/sso"/>
    <SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
                         Location="https://idp.example.com/sso-post"/>
  </IDPSSODescriptor>
</EntityDescriptor>"""


# =====================================================================
# IdPMetadataCache tests
# =====================================================================

class TestIdPMetadataCache(unittest.TestCase):
    """IdPMetadataCache: get, put, fetch, parse_saml_sso_url."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sso_cache_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _conn(self):
        import sqlite3
        return sqlite3.connect(str(self.db_path))

    def test_put_and_get_roundtrip(self):
        from infra.authlib_sso import IdPMetadataCache
        conn = self._conn()
        try:
            IdPMetadataCache.put(conn, "test-idp", "<xml>hello</xml>")
            result = IdPMetadataCache.get(conn, "test-idp")
            self.assertEqual(result, "<xml>hello</xml>")
        finally:
            conn.close()

    def test_get_missing_returns_none(self):
        from infra.authlib_sso import IdPMetadataCache
        conn = self._conn()
        try:
            result = IdPMetadataCache.get(conn, "nonexistent")
            self.assertIsNone(result)
        finally:
            conn.close()

    def test_put_replaces_existing(self):
        from infra.authlib_sso import IdPMetadataCache
        conn = self._conn()
        try:
            IdPMetadataCache.put(conn, "idp-1", "<xml>v1</xml>")
            IdPMetadataCache.put(conn, "idp-1", "<xml>v2</xml>")
            result = IdPMetadataCache.get(conn, "idp-1")
            self.assertEqual(result, "<xml>v2</xml>")
        finally:
            conn.close()

    @patch("infra.authlib_sso.requests.get")
    def test_fetch_returns_from_cache_on_second_call(self, mock_get):
        from infra.authlib_sso import IdPMetadataCache
        conn = self._conn()
        try:
            mock_get.side_effect = [
                MagicMock(text="<xml>remote</xml>", raise_for_status=lambda: None),
            ]
            result1 = IdPMetadataCache.fetch(
                conn, "idp-1", "https://idp.example.com/metadata", force=False,
            )
            self.assertEqual(result1, "<xml>remote</xml>")
            # Second call should hit cache, not HTTP
            result2 = IdPMetadataCache.fetch(
                conn, "idp-1", "https://idp.example.com/metadata", force=False,
            )
            self.assertEqual(result2, "<xml>remote</xml>")
            self.assertEqual(mock_get.call_count, 1)
        finally:
            conn.close()

    @patch("infra.authlib_sso.requests.get")
    def test_fetch_force_ignores_cache(self, mock_get):
        from infra.authlib_sso import IdPMetadataCache
        conn = self._conn()
        try:
            mock_get.side_effect = [
                MagicMock(text="<xml>v1</xml>", raise_for_status=lambda: None),
                MagicMock(text="<xml>v2</xml>", raise_for_status=lambda: None),
            ]
            IdPMetadataCache.fetch(conn, "idp-1", "https://idp.example.com/metadata", force=False)
            result2 = IdPMetadataCache.fetch(
                conn, "idp-1", "https://idp.example.com/metadata", force=True,
            )
            self.assertEqual(result2, "<xml>v2</xml>")
            self.assertEqual(mock_get.call_count, 2)
        finally:
            conn.close()

    @patch("infra.authlib_sso.requests.get")
    def test_fetch_http_error_raises(self, mock_get):
        from infra.authlib_sso import IdPMetadataCache
        conn = self._conn()
        try:
            mock_get.side_effect = Exception("HTTP 500")
            with self.assertRaises(Exception):
                IdPMetadataCache.fetch(
                    conn, "idp-1", "https://idp.example.com/metadata", force=True,
                )
        finally:
            conn.close()

    def test_parse_saml_sso_url_http_redirect(self):
        from infra.authlib_sso import IdPMetadataCache

        url = IdPMetadataCache.parse_saml_sso_url(_SAML_METADATA_XML)
        self.assertEqual(url, "https://idp.example.com/sso")

    def test_parse_saml_sso_url_no_sso_raises(self):
        from infra.authlib_sso import IdPMetadataCache, SsoConfigError

        empty_xml = """<?xml version="1.0"?><EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
          entityID="https://idp.example.com"/>"""
        with self.assertRaises(SsoConfigError):
            IdPMetadataCache.parse_saml_sso_url(empty_xml)


# =====================================================================
# SsoSession tests
# =====================================================================

class TestSsoSessionOIDC(unittest.TestCase):
    """SsoSession with OIDC provider config."""

    def setUp(self):
        from infra.authlib_sso import SsoProviderConfig

        self.config = SsoProviderConfig(
            name="test-oidc",
            kind="oidc",
            client_id="client-123",
            client_secret="secret-456",
            authorize_url="https://idp.example.com/oauth2/authorize",
            token_url="https://idp.example.com/oauth2/token",
            jwks_url="https://idp.example.com/oauth2/certs",
            issuer="https://idp.example.com",
            scopes="openid email profile",
        )

    def test_authorization_url_builds_correctly(self):
        from infra.authlib_sso import SsoSession

        session = SsoSession(self.config)
        url = session.authorization_url(
            redirect_uri="https://app.example.com/callback",
            state="random-state-abc",
        )
        self.assertIn("client_id=client-123", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fapp.example.com%2Fcallback", url)
        self.assertIn("state=random-state-abc", url)
        self.assertIn("response_type=code", url)
        self.assertIn("scope=openid+email+profile", url)
        self.assertIn("nonce=random-state-abc", url)

    @patch("infra.authlib_sso.requests.post")
    def test_parse_callback_with_code_exchange(self, mock_post):
        """OIDC callback with 'code' triggers token exchange."""
        from infra.authlib_sso import SsoSession

        # Generate a valid id_token signed by a synthetic IdP key
        from joserfc.jwk import import_key
        from joserfc import jwt as jose_jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        idp_key = import_key(priv_pem)
        priv_dict = idp_key.as_dict(private=True)
        pub_dict = idp_key.as_dict(private=False)
        kid = priv_dict.get("kid", "idp-key-1")
        pub_dict["kid"] = kid

        id_token_val = jose_jwt.encode(
            {"alg": "RS256", "kid": kid, "typ": "JWT"},
            {
                "sub": "ext-user-1",
                "email": "user@example.com",
                "name": "Jane Doe",
                "iss": "https://idp.example.com",
                "aud": "client-123",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            },
            import_key(priv_dict),
            algorithms=["RS256"],
        )
        id_token_str = (
            id_token_val.decode("utf-8")
            if isinstance(id_token_val, bytes)
            else id_token_val
        )

        mock_post.return_value = MagicMock(
            json=lambda: {"id_token": id_token_str},
            raise_for_status=lambda: None,
        )

        session = SsoSession(self.config)
        identity = session.parse_callback(
            code="auth-code-xyz",
            jwks={"keys": [pub_dict]},
        )
        self.assertEqual(identity.provider, "test-oidc")
        self.assertEqual(identity.external_sub, "ext-user-1")
        self.assertEqual(identity.email, "user@example.com")
        self.assertEqual(identity.display_name, "Jane Doe")

    @patch("infra.authlib_sso.requests.post")
    def test_parse_callback_code_exchange_no_id_token(self, mock_post):
        """OIDC callback where token endpoint omits id_token."""
        from infra.authlib_sso import SsoSession, SsoAuthError

        mock_post.return_value = MagicMock(
            json=lambda: {"access_token": "at-xxx"},
            raise_for_status=lambda: None,
        )

        session = SsoSession(self.config)
        with self.assertRaises(SsoAuthError):
            session.parse_callback(code="auth-code-xyz")

    def test_parse_callback_with_direct_id_token(self):
        """OIDC callback with direct id_token (no code exchange)."""
        from infra.authlib_sso import SsoSession
        from joserfc.jwk import import_key
        from joserfc import jwt as jose_jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        idp_key = import_key(priv_pem)
        priv_dict = idp_key.as_dict(private=True)
        pub_dict = idp_key.as_dict(private=False)
        kid = priv_dict.get("kid", "idp-key-1")
        pub_dict["kid"] = kid

        id_token_val = jose_jwt.encode(
            {"alg": "RS256", "kid": kid, "typ": "JWT"},
            {
                "sub": "ext-user-2",
                "email": "user2@example.com",
                "iss": "https://idp.example.com",
                "aud": "client-123",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            },
            import_key(priv_dict),
            algorithms=["RS256"],
        )
        id_token_str = (
            id_token_val.decode("utf-8")
            if isinstance(id_token_val, bytes)
            else id_token_val
        )

        session = SsoSession(self.config)
        identity = session.parse_callback(
            id_token=id_token_str,
            jwks={"keys": [pub_dict]},
        )
        self.assertEqual(identity.external_sub, "ext-user-2")
        self.assertEqual(identity.email, "user2@example.com")


class TestSsoSessionSAML(unittest.TestCase):
    """SsoSession with SAML provider config."""

    def setUp(self):
        from infra.authlib_sso import SsoProviderConfig

        self.config = SsoProviderConfig(
            name="test-saml",
            kind="saml",
            sso_url="https://idp.example.com/sso",
            entity_id="https://sp.example.com/saml",
        )

    def test_authorization_url_returns_sso_endpoint(self):
        from infra.authlib_sso import SsoSession

        session = SsoSession(self.config)
        url = session.authorization_url(
            redirect_uri="https://app.example.com/callback",
            state="some-state",
        )
        self.assertEqual(url, "https://idp.example.com/sso")

    def test_authorization_url_no_sso_url_raises(self):
        from infra.authlib_sso import SsoSession, SsoProviderConfig, SsoConfigError

        bad_config = SsoProviderConfig(
            name="bad-saml", kind="saml",
        )
        session = SsoSession(bad_config)
        with self.assertRaises(SsoConfigError):
            session.authorization_url(
                redirect_uri="https://app.example.com/callback",
                state="s",
            )

    def test_parse_callback_saml_success(self):
        from infra.authlib_sso import SsoSession

        session = SsoSession(self.config)
        identity = session.parse_callback(saml_response=_B64_SAML)
        self.assertEqual(identity.provider, "test-saml")
        self.assertEqual(identity.external_sub, "user@example.com")
        self.assertEqual(identity.email, "user@example.com")
        self.assertEqual(identity.display_name, "Jane Doe")

    def test_parse_callback_saml_no_response_raises(self):
        from infra.authlib_sso import SsoSession, SsoAuthError

        session = SsoSession(self.config)
        with self.assertRaises(SsoAuthError):
            session.parse_callback()

    def test_unknown_kind_raises(self):
        from infra.authlib_sso import SsoSession, SsoProviderConfig, SsoConfigError

        bad = SsoProviderConfig(name="bad", kind="unknown")
        session = SsoSession(bad)
        with self.assertRaises(SsoConfigError):
            session.authorization_url("http://x.com/cb", "s")


# =====================================================================
# SAML response parsing tests
# =====================================================================

_B64_SAML = _b64(_SAML_ASSERTION_XML)


class TestParseSamlResponse(unittest.TestCase):
    """parse_saml_response structural and edge-case tests."""

    def test_parse_valid_response(self):
        from infra.authlib_sso import parse_saml_response

        identity = parse_saml_response(_B64_SAML)
        self.assertEqual(identity.external_sub, "user@example.com")
        self.assertEqual(identity.email, "user@example.com")
        self.assertEqual(identity.display_name, "Jane Doe")
        self.assertIn("email", identity.attributes)
        self.assertIn("displayName", identity.attributes)

    def test_parse_expired_assertion_raises(self):
        from infra.authlib_sso import parse_saml_response, SsoAuthError

        expired_xml = _SAML_ASSERTION_XML.replace(
            'NotOnOrAfter="2099-12-31T23:59:59Z"',
            'NotOnOrAfter="2020-01-01T00:00:00Z"',
        )
        with self.assertRaises(SsoAuthError):
            parse_saml_response(_b64(expired_xml))

    def test_parse_no_assertion_raises(self):
        from infra.authlib_sso import parse_saml_response, SsoAuthError

        no_assertion = """<?xml version="1.0"?>
<saml2p:Response xmlns:saml2p="urn:oasis:names:tc:SAML:2.0:protocol"
                 ID="_empty" Version="2.0">
  <saml2p:Status>
    <saml2p:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Responder"/>
  </saml2p:Status>
</saml2p:Response>"""
        with self.assertRaises(SsoAuthError):
            parse_saml_response(_b64(no_assertion))

    def test_parse_no_name_id_raises(self):
        from infra.authlib_sso import parse_saml_response, SsoAuthError

        no_subject_xml = _SAML_ASSERTION_XML.replace(
            "<saml2:NameID", "<saml2:NameIDRemoved",
        ).replace("</saml2:NameID>", "")
        # This should produce no NameID
        with self.assertRaises(SsoAuthError):
            parse_saml_response(_b64(no_subject_xml))

    def test_parse_malformed_base64_raises(self):
        from infra.authlib_sso import parse_saml_response, SsoAuthError

        with self.assertRaises(SsoAuthError):
            parse_saml_response("not-valid-base64!!!")  # noqa

    def test_parse_xxe_doctype_raises(self):
        """XXE attempt via DOCTYPE is rejected."""
        from infra.authlib_sso import parse_saml_response, SsoAuthError

        xxe_xml = """<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<saml2p:Response xmlns:saml2p="urn:oasis:names:tc:SAML:2.0:protocol"
                 ID="_xxe" Version="2.0">
  <saml2:Assertion xmlns:saml2="urn:oasis:names:tc:SAML:2.0:assertion"
                   ID="_a1" IssueInstant="2026-01-01T00:00:00Z">
    <saml2:Issuer>https://idp.example.com</saml2:Issuer>
    <saml2:Subject>
      <saml2:NameID>user@example.com</saml2:NameID>
    </saml2:Subject>
  </saml2:Assertion>
</saml2p:Response>"""
        with self.assertRaises(SsoAuthError):
            parse_saml_response(_b64(xxe_xml))

    def test_parse_attribute_no_values(self):
        """Attribute element with no AttributeValue is handled."""
        from infra.authlib_sso import parse_saml_response

        no_val_xml = _SAML_ASSERTION_XML.replace(
            "<saml2:AttributeValue>Jane Doe</saml2:AttributeValue>",
            "",
        )
        identity = parse_saml_response(_b64(no_val_xml))
        self.assertEqual(identity.external_sub, "user@example.com")
        self.assertEqual(identity.display_name, "user@example.com")


# =====================================================================
# SAML signature verification (fail-closed when dep missing)
# =====================================================================

class TestVerifySamlSignature(unittest.TestCase):
    """verify_saml_signature fails closed without pyxmlsec."""

    def test_fails_closed_without_xmlsec(self):
        from infra.authlib_sso import verify_saml_signature, SsoSignatureUnverified

        with self.assertRaises(SsoSignatureUnverified):
            verify_saml_signature(_B64_SAML, cert_pem="-----BEGIN CERTIFICATE-----\nINVALID\n-----END CERTIFICATE-----")

    def test_fails_closed_without_cert(self):
        from infra.authlib_sso import verify_saml_signature, SsoSignatureUnverified

        with self.assertRaises(SsoSignatureUnverified):
            verify_saml_signature(_B64_SAML, cert_pem=None)


class TestSsoErrors(unittest.TestCase):
    """SSO error hierarchy."""

    def test_error_hierarchy(self):
        from infra.authlib_sso import SsoError, SsoConfigError, SsoAuthError, SsoSignatureUnverified

        self.assertTrue(issubclass(SsoConfigError, SsoError))
        self.assertTrue(issubclass(SsoAuthError, SsoError))
        self.assertTrue(issubclass(SsoSignatureUnverified, SsoError))

    def test_sso_identity_dataclass(self):
        from infra.authlib_sso import SsoIdentity

        ident = SsoIdentity(
            provider="okta",
            external_sub="sub-1",
            email="a@b.com",
            display_name="Alice",
            attributes={"role": "admin"},
        )
        self.assertEqual(ident.provider, "okta")
        self.assertEqual(ident.external_sub, "sub-1")
        self.assertEqual(ident.email, "a@b.com")
        self.assertEqual(ident.display_name, "Alice")
        self.assertEqual(ident.attributes["role"], "admin")


if __name__ == "__main__":
    unittest.main()
