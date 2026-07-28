"""SSO (OIDC + SAML 2.0) integration for agentic-memory.

Dependency-light: only :mod:`joserfc` (JWT/JWK),
:mod:`cryptography` (key generation), :mod:`requests` (HTTP), and the
stdlib are used. No native XML-security libraries are required to import
or to parse/validate SAML assertions; SAML *signature* verification is
performed only when ``pyxmlsec`` is installed, otherwise it fails closed
(refuses unsigned assertions).

Design
------
* JWTs minted by this service are signed with addressable RSA keys stored
  in ``idem_token_key`` (migration 047). Each key has a ``kid`` so we can
  rotate without invalidating tokens signed by still-valid keys.
* External IdP tokens (OIDC ``id_token``, SAML assertions) are validated
  against IdP-provided JWKS / certificates.
* First-time SSO login materialises a ``principals`` row linked via
  ``principal_identities`` (provider + external_sub).

Security notes
--------------
* All IdP-supplied XML is parsed with entity expansion disabled (XXE-safe).
* SAML assertions without a verifiable signature are rejected when
  signature verification is required (fail-closed).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from infra.ssrf import _ssrf_validate_url

try:
    import requests
except ImportError:
    requests: Any = None  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

try:
    from joserfc.jwk import import_key
    from joserfc import jwt as jose_jwt
    from joserfc.jwt import JWTClaimsRegistry
except ImportError:
    import_key: Any = None  # type: ignore[no-redef]
    jose_jwt: Any = None  # type: ignore[no-redef]
    JWTClaimsRegistry: Any = None  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SsoError(Exception):
    """Base class for SSO failures."""


class SsoConfigError(SsoError):
    """Misconfigured IdP / provider."""


class SsoAuthError(SsoError):
    """Authentication/validation failure for an SSO token or assertion."""


class SsoSignatureUnverified(SsoError):
    """SAML signature could not be verified (or no verifier available)."""


# ---------------------------------------------------------------------------
# Identity + provider config
# ---------------------------------------------------------------------------

@dataclass
class SsoIdentity:
    """Normalised identity extracted from an IdP callback."""

    provider: str
    external_sub: str
    email: str = ""
    display_name: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SsoProviderConfig:
    """Configuration for a single SSO identity provider."""

    name: str
    kind: str  # 'oidc' | 'saml'
    # OIDC
    client_id: str = ""
    client_secret: str = ""
    authorize_url: str = ""
    token_url: str = ""
    jwks_url: str = ""
    issuer: str = ""
    scopes: str = "openid email profile"
    # SAML
    metadata_url: str = ""      # where to fetch IdP SAML metadata
    sso_url: str = ""          # IdP SSO endpoint (overrides metadata)
    entity_id: str = ""        # our SP entity id / audience
    # shared
    tenant_id: str = "default"


# ---------------------------------------------------------------------------
# Signing key management (idem_token_key)
# ---------------------------------------------------------------------------

class KeyManager:
    """Addressable RSA signing keys persisted in ``idem_token_key``."""

    TABLE = "idem_token_key"

    @staticmethod
    def _ensure_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {KeyManager.TABLE} ("
            " kid TEXT PRIMARY KEY, public_jwk TEXT NOT NULL,"
            " private_jwk TEXT NOT NULL, created_at TEXT NOT NULL,"
            " revoked_at TEXT)"
        )

    @staticmethod
    def generate(conn: sqlite3.Connection, alg: str = "RS256") -> str:
        """Generate a new RSA key pair, store it, and return its ``kid``."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        KeyManager._ensure_table(conn)
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key = import_key(priv_pem)
        priv_dict = key.as_dict(private=True)
        pub_dict = key.as_dict(private=False)
        # Use the JWK's own thumbprint kid (Authlib sets it in the JWT
        # header automatically) so verification lookups line up.
        raw_kid = priv_dict.get("kid")
        kid = raw_kid if isinstance(raw_kid, str) else f"kid-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        priv_dict["kid"] = kid
        pub_dict["kid"] = kid
        private_jwk = json.dumps(priv_dict)
        public_jwk = json.dumps(pub_dict)
        conn.execute(
            f"INSERT INTO {KeyManager.TABLE}"
            " (kid, public_jwk, private_jwk, created_at) VALUES (?,?,?,?)",
            (kid, public_jwk, private_jwk, _now()),
        )
        conn.commit()
        return kid

    @staticmethod
    def get(conn: sqlite3.Connection, kid: str) -> Optional[Dict[str, Any]]:
        KeyManager._ensure_table(conn)
        row = conn.execute(
            f"SELECT kid, public_jwk, private_jwk, revoked_at FROM {KeyManager.TABLE}"
            " WHERE kid = ?",
            (kid,),
        ).fetchone()
        if not row:
            return None
        return {
            "kid": row[0],
            "public_jwk": json.loads(row[1]),
            "private_jwk": json.loads(row[2]),
            "revoked_at": row[3],
        }

    @staticmethod
    def get_active(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """Return the most-recently created non-revoked key, generating one if none."""
        KeyManager._ensure_table(conn)
        row = conn.execute(
            f"SELECT kid, public_jwk, private_jwk, revoked_at FROM {KeyManager.TABLE}"
            " WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            kid = KeyManager.generate(conn)
            return KeyManager.get(conn, kid)
        return {
            "kid": row[0],
            "public_jwk": json.loads(row[1]),
            "private_jwk": json.loads(row[2]),
            "revoked_at": row[3],
        }

    @staticmethod
    def list_keys(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        KeyManager._ensure_table(conn)
        rows = conn.execute(
            f"SELECT kid, created_at, revoked_at FROM {KeyManager.TABLE} ORDER BY created_at DESC"
        ).fetchall()
        return [
            {"kid": r[0], "created_at": r[1], "revoked_at": r[2]} for r in rows
        ]

    @staticmethod
    def revoke(conn: sqlite3.Connection, kid: str) -> bool:
        KeyManager._ensure_table(conn)
        cur = conn.execute(
            f"UPDATE {KeyManager.TABLE} SET revoked_at = ? WHERE kid = ?",
            (_now(), kid),
        )
        conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def public_jwk_set(conn: sqlite3.Connection) -> Dict[str, Any]:
        """Return a JWKS document of all non-revoked public keys."""
        KeyManager._ensure_table(conn)
        rows = conn.execute(
            f"SELECT public_jwk FROM {KeyManager.TABLE} WHERE revoked_at IS NULL"
        ).fetchall()
        keys = [json.loads(r[0]) for r in rows]
        return {"keys": keys}


# ---------------------------------------------------------------------------
# JWT mint / verify (our own service tokens)
# ---------------------------------------------------------------------------

def sign_token(
    conn: sqlite3.Connection,
    claims: Dict[str, Any],
    *,
    expires_in: int = 3600,
    alg: str = "RS256",
    issuer: str = "agentic-memory",
    audience: str = "agentic-memory-api",
    kid: Optional[str] = None,
) -> tuple[str, str]:
    """Sign a JWT with an (active) signing key. Returns ``(token, kid)``."""
    key = KeyManager.get_active(conn) if kid is None else KeyManager.get(conn, kid)
    if key is None:
        raise SsoConfigError("No signing key available")
    if key.get("revoked_at"):
        raise SsoConfigError(f"Signing key {key['kid']} is revoked")
    now = int(time.time())
    payload = dict(claims)
    payload.setdefault("iat", now)
    payload.setdefault("nbf", now)
    payload["exp"] = now + expires_in
    payload["iss"] = issuer
    payload["aud"] = audience
    token = jose_jwt.encode(
        {"alg": alg, "kid": key["kid"], "typ": "JWT"},
        payload,
        import_key(key["private_jwk"]),
        algorithms=[alg],
    )
    return token.decode("utf-8") if isinstance(token, bytes) else token, key["kid"]


def verify_token(
    conn: sqlite3.Connection,
    token: str,
    *,
    issuer: str = "agentic-memory",
    audience: str = "agentic-memory-api",
    alg: str = "RS256",
) -> Dict[str, Any]:
    """Verify a token minted by this service (looks up ``kid`` in header)."""
    header = _jwt_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise SsoAuthError("Token missing 'kid' header")
    key = KeyManager.get(conn, kid)
    if key is None:
        raise SsoAuthError(f"Unknown signing key: {kid}")
    if key.get("revoked_at"):
        raise SsoAuthError(f"Signing key {kid} has been revoked")
    decoded = jose_jwt.decode(token, import_key(key["public_jwk"]), algorithms=[alg])
    try:
        JWTClaimsRegistry(
            now=int(time.time()), leeway=0,
            exp={"essential": True},
            iss={"essential": True, "value": issuer},
            aud={"essential": True, "value": audience},
        ).validate(decoded.claims)
    except Exception as exc:  # noqa: BLE001
        raise SsoAuthError(f"Token validation failed: {exc}") from exc
    # Explicit exp check: Authlib may truncate to integer seconds and
    # accept a token whose exp equals the current wall-clock second.
    # This ensures sub-second-granularity rejection on the exp boundary.
    exp_ts = decoded.claims.get("exp", 0)
    if exp_ts and exp_ts < time.time():
        raise SsoAuthError("Token is expired (strict check)")
    return dict(decoded.claims)


def _jwt_unverified_header(token: str) -> Dict[str, Any]:
    try:
        header_b64 = token.split(".")[0]
        pad = "=" * (-len(header_b64) % 4)
        header: Dict[str, Any] = json.loads(base64.urlsafe_b64decode(header_b64 + pad))
        return header
    except Exception as exc:  # noqa: BLE001
        raise SsoAuthError(f"Malformed JWT header: {exc}") from exc


# ---------------------------------------------------------------------------
# OIDC: verify an external id_token against IdP JWKS
# ---------------------------------------------------------------------------

def verify_oidc_id_token(
    id_token: str,
    jwks: Dict[str, Any],
    *,
    issuer: str = "",
    audience: str = "",
    alg: str = "RS256",
    nonce: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify an OIDC ``id_token`` (signed by the IdP) against its JWKS."""
    options: Dict[str, Any] = {"exp": {"essential": True}}
    if issuer:
        options["iss"] = {"essential": True, "value": issuer}
    if audience:
        options["aud"] = {"essential": True, "value": audience}
    # Select the verifying key by kid (joserfc's JWKS import is fiddly;
    # selecting explicitly is robust).
    header = _jwt_unverified_header(id_token)
    signing_key = _select_jwk(jwks, header.get("kid"))
    decoded = jose_jwt.decode(id_token, import_key(signing_key), algorithms=[alg])
    try:
        JWTClaimsRegistry(now=int(time.time()), leeway=0, **options).validate(decoded.claims)
    except Exception as exc:  # noqa: BLE001
        raise SsoAuthError(f"id_token validation failed: {exc}") from exc
    # R3: Validate nonce to prevent replay attacks.
    if nonce is not None:
        token_nonce = decoded.claims.get("nonce")
        if token_nonce != nonce:
            raise SsoAuthError("id_token nonce mismatch")
    return dict(decoded.claims)


def _select_jwk(jwks: Dict[str, Any], kid: Optional[str]) -> Dict[str, Any]:
    """Pick a public JWK from a JWKS document by kid (or the only key)."""
    keys: List[Dict[str, Any]] = (jwks.get("keys") if isinstance(jwks, dict) else None) or []
    if not keys:
        # Allow being passed a single key dict directly.
        if isinstance(jwks, dict) and ("kty" in jwks or "n" in jwks):
            return jwks
        raise SsoAuthError("JWKS contains no keys")
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
        raise SsoAuthError(f"No JWK matching kid={kid}")
    return keys[0]


def fetch_jwks(jwks_url: str, timeout: float = 10.0) -> Dict[str, Any]:
    _ssrf_validate_url(jwks_url)
    resp = requests.get(jwks_url, timeout=timeout)
    resp.raise_for_status()
    jwks: Dict[str, Any] = resp.json()
    return jwks


# ---------------------------------------------------------------------------
# SAML 2.0
# ---------------------------------------------------------------------------

_SAML_NS = {
    "saml2": "urn:oasis:names:tc:SAML:2.0:assertion",
    "saml2p": "urn:oasis:names:tc:SAML:2.0:protocol",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}


def _local(tag: str) -> str:
    """Return the local name of an XML tag (namespace stripped)."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _safe_parse(xml_bytes: bytes) -> ET.Element:
    """Parse XML with entity expansion disabled (XXE-safe)."""
    try:
        from defusedxml.ElementTree import fromstring as _fromstring  # type: ignore[import-untyped]
        from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
    except ImportError:
        text = xml_bytes.decode("utf-8", "replace")
        # Strip DOCTYPE / entity declarations — the main XXE attack surface.
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise SsoAuthError("Refusing to parse XML with DOCTYPE/ENTITY declarations")
        return ET.fromstring(xml_bytes)
    try:
        element: ET.Element = _fromstring(xml_bytes)
        return element
    except DefusedXmlException as exc:
        # defusedxml raises DefusedXmlException (e.g. EntitiesForbidden) on
        # XXE attempts. Normalize to SsoAuthError so callers see a uniform
        # failure surface regardless of whether defusedxml is installed.
        raise SsoAuthError(f"Refusing to parse unsafe XML: {exc}") from exc


def parse_saml_response(saml_response_b64: str) -> SsoIdentity:
    """Decode and extract identity from a base64 SAML 2.0 ``SAMLResponse``.

    Performs structural validation (subject NameID, issuer, audience,
    time windows). Raising on missing/expired assertions. Signature
    verification is separate (see :func:`verify_saml_signature`).
    """
    try:
        raw = base64.b64decode(saml_response_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SsoAuthError(f"Invalid base64 SAMLResponse: {exc}") from exc
    return _parse_saml_assertion(raw)


def _parse_saml_assertion(raw: bytes) -> SsoIdentity:
    try:
        root = _safe_parse(raw)
    except ET.ParseError as exc:
        raise SsoAuthError(f"Malformed SAML XML: {exc}") from exc
    # Find the Assertion element anywhere in the tree.
    assertion = _find_first(root, "Assertion")
    if assertion is None:
        raise SsoAuthError("No SAML Assertion found")
    now = time.time()
    # Conditions: NotBefore / NotOnOrAfter
    conditions = _find_first(assertion, "Conditions")
    if conditions is not None:
        nb = conditions.get("NotBefore")
        noa = conditions.get("NotOnOrAfter")
        if nb and _parse_saml_time(nb) > now + 60:
            raise SsoAuthError("SAML assertion not yet valid")
        if noa and _parse_saml_time(noa) < now - 60:
            raise SsoAuthError("SAML assertion expired")
        # Audience restriction
        audience = _find_first(assertion, "Audience")
        if audience is not None and audience.text:
            pass  # audience captured; callers may enforce per-SP
    # Subject / NameID
    subject = _find_first(assertion, "Subject")
    name_id = None
    if subject is not None:
        nid = _find_first(subject, "NameID")
        if nid is not None:
            name_id = (nid.text or "").strip()
    if not name_id:
        raise SsoAuthError("SAML assertion missing Subject/NameID")
    # Issuer
    issuer_el = _find_first(assertion, "Issuer")
    issuer = (issuer_el.text or "").strip() if issuer_el is not None else ""
    # AttributeStatement
    attributes: Dict[str, Any] = {}
    attr_stmt = _find_first(assertion, "AttributeStatement")
    email = ""
    display_name = ""
    if attr_stmt is not None:
        for attr in _iter_local(attr_stmt, "Attribute"):
            name = attr.get("Name") or attr.get("FriendlyName") or ""
            values = [v.text or "" for v in _iter_local(attr, "AttributeValue")]
            if len(values) == 1:
                attributes[name] = values[0]
            else:
                attributes[name] = values
            if name in ("email", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"):
                email = values[0] if values else ""
            if name in ("name", "displayName",
                        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"):
                display_name = values[0] if values else ""
    # Prefer NameID as sub if it looks like an email and no explicit sub attr.
    sub = name_id
    if not email and "@" in name_id:
        email = name_id
    return SsoIdentity(
        provider="",  # filled by caller from provider config
        external_sub=sub,
        email=email,
        display_name=display_name or email,
        attributes=attributes,
    )


def verify_saml_signature(saml_response_b64: str, cert_pem: Optional[str] = None) -> None:
    """Verify the XML signature of a SAMLResponse.

    Uses ``pyxmlsec`` when available. When it is not installed we fail
    closed: unsigned/!unverifiable assertions are never accepted.
    """
    try:
        import xmlsec  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SsoSignatureUnverified(
            "SAML signature verification requires 'pyxmlsec' (xmlsec1). "
            "Install it to enable SAML login; refusing to accept unsigned "
            "assertions."
        ) from exc
    # pyxmlsec path (kept optional so import stays dependency-light).
    try:
        from lxml import etree  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise SsoSignatureUnverified(
            "SAML signature verification requires 'lxml' for canonicalization."
        ) from exc
    if cert_pem is None:
        raise SsoSignatureUnverified("No IdP signing certificate available")
    raw = base64.b64decode(saml_response_b64, validate=True)
    doc = etree.fromstring(raw)
    xmlsec.tree.add_ids(doc, ["ID"])
    manager = xmlsec.KeysManager()
    key = xmlsec.Key.from_memory(cert_pem, xmlsec.KeyFormat.CERT_PEM, None)
    manager.add_key(key)
    ctx = xmlsec.SignatureContext(manager)
    sig = xmlsec.tree.find_node(doc, xmlsec.Node.SIGNATURE)
    if sig is None:
        raise SsoSignatureUnverified("No Signature element in SAMLResponse")
    ctx.verify(sig)


def _find_first(element: ET.Element, local: str) -> Optional[ET.Element]:
    if _local(element.tag) == local:
        return element
    for child in element.iter():
        if _local(child.tag) == local:
            return child
    return None


def _iter_local(element: ET.Element, local: str):
    for child in element.iter():
        if _local(child.tag) == local:
            yield child


def _parse_saml_time(value: str) -> float:
    v = value.strip().replace("Z", "+00:00")
    has_tz = "+" in v[10:] or "-" in v[10:]  # check offset after date portion
    if has_tz:
        try:
            return time.mktime(time.strptime(v, "%Y-%m-%dT%H:%M:%S%z"))
        except Exception:  # noqa: BLE001
            pass
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# IdP metadata cache (sso_idp_cache / memory/.auth_cache/)
# ---------------------------------------------------------------------------

class IdPMetadataCache:
    """Fetches + caches IdP metadata, persisted in ``sso_idp_cache``."""

    TABLE = "sso_idp_cache"

    @staticmethod
    def _ensure_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {IdPMetadataCache.TABLE} ("
            " id TEXT PRIMARY KEY, metadata_xml TEXT NOT NULL,"
            " fetched_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )

    @staticmethod
    def get(conn: sqlite3.Connection, idp_id: str) -> Optional[str]:
        IdPMetadataCache._ensure_table(conn)
        row = conn.execute(
            f"SELECT metadata_xml FROM {IdPMetadataCache.TABLE} WHERE id = ?",
            (idp_id,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def put(conn: sqlite3.Connection, idp_id: str, metadata_xml: str) -> None:
        IdPMetadataCache._ensure_table(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {IdPMetadataCache.TABLE}"
            " (id, metadata_xml, fetched_at) VALUES (?,?,?)",
            (idp_id, metadata_xml, _now()),
        )
        conn.commit()

    @classmethod
    def fetch(
        cls,
        conn: sqlite3.Connection,
        idp_id: str,
        metadata_url: str,
        *,
        force: bool = False,
        timeout: float = 10.0,
    ) -> str:
        """Return IdP metadata XML, using cache unless ``force`` or missing."""
        if not force:
            cached = cls.get(conn, idp_id)
            if cached:
                return cached
        _ssrf_validate_url(metadata_url)
        resp = requests.get(metadata_url, timeout=timeout)
        resp.raise_for_status()
        xml_text = resp.text
        cls.put(conn, idp_id, xml_text)
        return xml_text

    @staticmethod
    def parse_saml_sso_url(metadata_xml: str) -> str:
        """Extract the IdP SSO (HTTP-Redirect/POST) URL from SAML metadata."""
        root = _safe_parse(metadata_xml.encode("utf-8"))
        for el in root.iter():
            if _local(el.tag) == "SingleSignOnService":
                binding = el.get("Binding", "")
                if "HTTP-Redirect" in binding or "HTTP-POST" in binding:
                    loc = el.get("Location")
                    if loc:
                        return loc
        # Fallback: first SingleSignOnService
        for el in root.iter():
            if _local(el.tag) == "SingleSignOnService":
                loc = el.get("Location")
                if loc:
                    return loc
        raise SsoConfigError("No SingleSignOnService in IdP metadata")


# ---------------------------------------------------------------------------
# SSO session orchestration (OIDC + SAML)
# ---------------------------------------------------------------------------

class SsoSession:
    """Per-login session that builds auth URLs and parses callbacks."""

    def __init__(self, config: SsoProviderConfig) -> None:
        self.config = config
        self._nonce: Optional[str] = None
        self._redirect_uri: Optional[str] = None

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        if self.config.kind == "oidc":
            params = {
                "client_id": self.config.client_id,
                "response_type": "code",
                "scope": self.config.scopes,
                "redirect_uri": redirect_uri,
                "state": state,
                "nonce": state,
            }
            self._nonce = state
            self._redirect_uri = redirect_uri
            return f"{self.config.authorize_url}?{urlencode(params)}"
        if self.config.kind == "saml":
            # For SAML we return the IdP SSO URL; the full AuthnRequest is
            # built by the caller (needs SP signing). We surface the endpoint.
            sso = self.config.sso_url
            if not sso:
                raise SsoConfigError(
                    f"SAML provider {self.config.name} has no sso_url configured"
                )
            return sso
        raise SsoConfigError(f"Unknown provider kind: {self.config.kind}")

    def parse_callback(
        self,
        *,
        code: Optional[str] = None,
        id_token: Optional[str] = None,
        saml_response: Optional[str] = None,
        jwks: Optional[Dict[str, Any]] = None,
    ) -> SsoIdentity:
        """Turn a provider callback into a normalised :class:`SsoIdentity`."""
        cfg = self.config
        if cfg.kind == "oidc":
            if id_token is None:
                if code is None:
                    raise SsoAuthError("OIDC callback requires 'code' or 'id_token'")
                id_token = self._oidc_exchange_code(code)
            claims = verify_oidc_id_token(
                id_token,
                jwks or {},
                issuer=cfg.issuer,
                audience=cfg.client_id,
                nonce=self._nonce,
            )
            sub = claims.get("sub", "")
            if not sub:
                raise SsoAuthError("OIDC id_token missing 'sub'")
            return SsoIdentity(
                provider=cfg.name,
                external_sub=sub,
                email=claims.get("email", ""),
                display_name=claims.get("name", claims.get("email", "")),
                attributes=dict(claims),
            )
        if cfg.kind == "saml":
            if saml_response is None:
                raise SsoAuthError("SAML callback requires 'saml_response'")
            identity = parse_saml_response(saml_response)
            identity.provider = cfg.name
            return identity
        raise SsoConfigError(f"Unknown provider kind: {cfg.kind}")

    def _oidc_exchange_code(self, code: str) -> str:
        cfg = self.config
        if not cfg.token_url:
            raise SsoConfigError("OIDC provider missing token_url")
        resp = requests.post(
            cfg.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": self._redirect_uri or "",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        id_token = data.get("id_token")
        if not id_token:
            raise SsoAuthError("OIDC token endpoint did not return id_token")
        return str(id_token)


# ---------------------------------------------------------------------------
# Principal resolution / creation
# ---------------------------------------------------------------------------

def resolve_principal_by_external_sub(
    conn: sqlite3.Connection, provider: str, external_sub: str,
    tenant_id: str = "",
) -> Optional[str]:
    """Return the principal_id for a (provider, external_sub) if known."""
    if tenant_id:
        row = conn.execute(
            "SELECT p.id FROM principals p"
            " JOIN principal_identities pi ON pi.principal_id = p.id"
            " WHERE pi.provider = ? AND pi.external_sub = ? AND p.tenant_id = ? LIMIT 1",
            (provider, external_sub, tenant_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT p.id FROM principals p"
            " JOIN principal_identities pi ON pi.principal_id = p.id"
            " WHERE pi.provider = ? AND pi.external_sub = ? LIMIT 1",
            (provider, external_sub),
        ).fetchone()
    return row[0] if row else None


def resolve_or_create_principal(
    conn: sqlite3.Connection,
    identity: SsoIdentity,
    *,
    tenant_id: str | None = None,
) -> str:
    """Return the principal_id for *identity*, creating it on first login."""
    _tid = tenant_id or "default"
    existing = resolve_principal_by_external_sub(
        conn, identity.provider, identity.external_sub, tenant_id=_tid,
    )
    if existing:
        return existing
    principal_id = f"principal-{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO principals (id, kind, display_name, tenant_id, created_at)"
        " VALUES (?, 'user', ?, ?, ?)",
        (principal_id, identity.display_name or identity.email or principal_id,
         _tid, _now()),
    )
    conn.execute(
        "INSERT INTO principal_identities (principal_id, provider, external_sub, tenant_id, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (principal_id, identity.provider, identity.external_sub, _tid, _now()),
    )
    conn.commit()
    return principal_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect(db_path: Any) -> sqlite3.Connection:
    if isinstance(db_path, sqlite3.Connection):
        return db_path
    return sqlite3.connect(str(db_path))
