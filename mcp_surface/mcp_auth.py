"""SSO (OIDC + SAML) MCP verbs for agentic-memory.

These are ADMIN operations exposed ONLY through the ``memory_maintenance``
router (Hard Rule 6). They are registered as ``@mcp.tool()`` functions so
they inherit the standard MCP surface plumbing, but ``memory_mcp.py``
removes them from the public tool list — they are reachable solely via
``memory_maintenance(operation="<op>")``.

Operations:
  * login_url        — build a provider authorization / SSO URL
  * callback         — exchange a provider callback for a local JWT + principal
  * whoami           — introspect a locally-issued JWT
  * rotate_key       — rotate the signing key (revoke old, mint new)
  * sso_sync_metadata— fetch + cache IdP metadata (SAML metadata / OIDC doc)
  * sso_idp_list     — list configured IdPs
  * sso_idp_add      — register a new IdP in memory.toml
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from mcp_surface.mcp_instance import mcp


def _toml_load(fh) -> Dict[str, Any]:
    """Load TOML from a binary file handle using stdlib tomllib/tomli."""
    try:
        import tomllib as _toml
    except ModuleNotFoundError:
        import tomli as _toml  # type: ignore[no-redef]
    return _toml.load(fh)


def _toml_dump(data: Dict[str, Any], fh) -> None:
    """Minimal TOML serializer for nested dicts of scalars.

    Handles the nested-dict-of-scalars structure written by the SSO IdP
    registration path (``memory.auth.sso.idps``). Avoids the third-party
    ``toml`` dependency so the package runs on stdlib alone.
    """

    def _scalar(v: Any) -> str:
        if v is None:
            return '""'
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _emit(d: Dict[str, Any], indent: str) -> None:
        for key, value in d.items():
            if isinstance(value, dict) and value:
                fh.write(f"{indent}[{key}]\n")
                _emit(value, indent)
            elif isinstance(value, dict):
                fh.write(f"{indent}{key} = {{}}\n")
            else:
                fh.write(f"{indent}{key} = {_scalar(value)}\n")

    _emit(data, "")
from infra.authlib_sso import (
    IdPMetadataCache,
    KeyManager,
    SsoConfigError,
    SsoError,
    SsoIdentity,
    SsoProviderConfig,
    SsoSession,
    resolve_or_create_principal,
    sign_token,
    verify_token,
)
from infra.audit import enqueue_audit
from infra.infrastructure import ErrorCode, _err
from infra.infrastructure import resolve_active_memory_dir
from infra._lazy_imports import open_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + DB helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: str = "") -> str:
    if db_path:
        return str(db_path)
    return str(resolve_active_memory_dir() / "memory.db")


def _sso_config_path() -> Path:
    override = os.environ.get("MEMORY_CONFIG_PATH")
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent.parent / p).resolve()
        return p
    return Path(__file__).resolve().parent.parent / "memory.toml"


def _load_sso_config() -> Dict[str, Any]:
    """Return the ``[memory.auth.sso]`` config block (empty dict if absent)."""
    path = _sso_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            data = _toml_load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse memory.toml: %s", exc)
        return {}
    return data.get("memory", {}).get("auth", {}).get("sso", {}) or {}


def _build_provider_config(name: str) -> SsoProviderConfig:
    """Build a :class:`SsoProviderConfig` from memory.toml by IdP name."""
    sso = _load_sso_config()
    idps = sso.get("idps", {}) or {}
    entry = idps.get(name)
    if not entry:
        raise SsoConfigError(
            f"Unknown SSO provider '{name}'. Known: {', '.join(sorted(idps)) or '<none>'}"
        )
    entry = dict(entry)
    kind = entry.pop("kind", "oidc")
    return SsoProviderConfig(name=name, kind=kind, **entry)


# ---------------------------------------------------------------------------
# Verbs (ADMIN — reachable only via memory_maintenance)
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_login_url(provider: str, redirect_uri: str = "", db_path: str = "") -> str:
    """Build a provider login/authorization URL for an SSO flow.

    Args:
        provider: IdP name as configured in [memory.auth.sso.idps].
        redirect_uri: Where the IdP should redirect after auth.
        db_path: Memory DB path (defaults to the active store).
    """
    try:
        cfg = _build_provider_config(provider)
        session = SsoSession(cfg)
        url = session.authorization_url(redirect_uri or cfg.entity_id or "", state=provider)
        return json.dumps({"provider": provider, "login_url": url})
    except SsoError as exc:
        return _err(ErrorCode.AUTHORIZATION_DENIED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_login_url failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


@mcp.tool()
def memory_callback(
    provider: str,
    code: str = "",
    id_token: str = "",
    saml_response: str = "",
    db_path: str = "",
) -> str:
    """Exchange an SSO provider callback for a local JWT + principal.

    Creates the ``principals`` row on first login (linking
    ``principal_identities``), mints a local JWT signed with the active
    ``idem_token_key``, and writes an audit row tagged with principal_id.
    """
    resolved_db = _resolve_db_path(db_path)
    try:
        cfg = _build_provider_config(provider)
        session = SsoSession(cfg)
        identity = session.parse_callback(
            code=code or None, id_token=id_token or None, saml_response=saml_response or None,
        )
        if cfg.kind == "saml" and saml_response:
            # Cryptographic signature verification is required and fails
            # closed when pyxmlsec is unavailable (no silent acceptance).
            from infra.authlib_sso import verify_saml_signature

            cert = cfg.__dict__.get("signing_cert_pem")
            verify_saml_signature(saml_response, cert_pem=cert)
        with open_db(Path(resolved_db)) as conn:
            principal_id = resolve_or_create_principal(
                conn, identity, tenant_id=cfg.tenant_id or "default"
            )
            token, kid = sign_token(conn, {"sub": identity.external_sub, "provider": provider})
        enqueue_audit(
            db_path=resolved_db,
            tool="sso_callback",
            args={"provider": provider, "external_sub": identity.external_sub},
            principal_id=principal_id,
        )
        return json.dumps({
            "principal_id": principal_id,
            "external_sub": identity.external_sub,
            "provider": provider,
            "token": token,
            "kid": kid,
        })
    except SsoError as exc:
        return _err(ErrorCode.AUTHORIZATION_DENIED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_callback failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


@mcp.tool()
def memory_whoami(token: str, db_path: str = "") -> str:
    """Introspect a locally-issued SSO JWT. Returns the principal claims."""
    resolved_db = _resolve_db_path(db_path)
    try:
        with open_db(Path(resolved_db)) as conn:
            claims = verify_token(conn, token)
        return json.dumps({"valid": True, "claims": claims})
    except SsoError as exc:
        return _err(ErrorCode.AUTHORIZATION_DENIED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_whoami failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


@mcp.tool()
def memory_rotate_key(db_path: str = "") -> str:
    """Rotate the SSO signing key: revoke the current active key, mint a new one."""
    resolved_db = _resolve_db_path(db_path)
    try:
        with open_db(Path(resolved_db)) as conn:
            old = KeyManager.get_active(conn)
            old_kid = old["kid"] if old else None
            if old_kid:
                KeyManager.revoke(conn, old_kid)
            new_kid = KeyManager.generate(conn)
        return json.dumps({
            "previous_kid": old_kid,
            "new_kid": new_kid,
            "rotated_at": _now_iso(),
        })
    except SsoError as exc:
        return _err(ErrorCode.AUTHORIZATION_DENIED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_rotate_key failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


@mcp.tool()
def memory_sso_sync_metadata(
    idp_id: str,
    metadata_url: str = "",
    force: bool = False,
    db_path: str = "",
) -> str:
    """Fetch and cache IdP metadata (SAML metadata XML or OIDC discovery doc).

    Caches into ``sso_idp_cache`` and on disk under ``memory/.auth_cache/``.
    Returns the discovered SSO URL when present.
    """
    resolved_db = _resolve_db_path(db_path)
    try:
        cfg = _load_sso_config().get("idps", {}).get(idp_id, {})
        url = metadata_url or cfg.get("metadata_url", "")
        if not url:
            raise SsoConfigError(f"No metadata_url for IdP '{idp_id}'")
        with open_db(Path(resolved_db)) as conn:
            xml_text = IdPMetadataCache.fetch(
                conn, idp_id, url, force=force, timeout=10.0
            )
            try:
                sso_url = IdPMetadataCache.parse_saml_sso_url(xml_text)
            except SsoError:
                sso_url = ""
        cache_dir = resolve_active_memory_dir() / ".auth_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{idp_id}.xml").write_text(xml_text, encoding="utf-8")
        return json.dumps({"idp_id": idp_id, "sso_url": sso_url, "cached": True})
    except SsoError as exc:
        return _err(ErrorCode.AUTHORIZATION_DENIED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_sso_sync_metadata failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


@mcp.tool()
def memory_sso_idp_list(db_path: str = "") -> str:
    """List configured SSO identity providers."""
    try:
        sso = _load_sso_config()
        idps = sso.get("idps", {}) or {}
        out = [
            {"name": name, "kind": entry.get("kind", "oidc")}
            for name, entry in idps.items()
        ]
        return json.dumps({"idps": out, "count": len(out)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_sso_idp_list failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


@mcp.tool()
def memory_sso_idp_add(
    name: str,
    kind: str = "oidc",
    client_id: str = "",
    client_secret: str = "",
    authorize_url: str = "",
    token_url: str = "",
    jwks_url: str = "",
    issuer: str = "",
    metadata_url: str = "",
    entity_id: str = "",
    tenant_id: str = "default",
    db_path: str = "",
) -> str:
    """Register a new SSO IdP in memory.toml under [memory.auth.sso.idps].

    Secrets (client_secret) are written to disk but never returned.
    """
    try:
        _add_idp_to_toml(
            name, kind,
            client_id=client_id, client_secret=client_secret,
            authorize_url=authorize_url, token_url=token_url, jwks_url=jwks_url,
            issuer=issuer, metadata_url=metadata_url, entity_id=entity_id,
            tenant_id=tenant_id,
        )
        return json.dumps({"added": True, "name": name, "kind": kind})
    except SsoError as exc:
        return _err(ErrorCode.AUTHORIZATION_DENIED, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_sso_idp_add failed: %s", exc)
        return _err(ErrorCode.INVALID_PARAMS, str(exc))


# ---------------------------------------------------------------------------
# TOML IdP registration (append-only, idempotent)
# ---------------------------------------------------------------------------

def _add_idp_to_toml(name: str, kind: str, **fields: Any) -> None:
    path = _sso_config_path()
    data: Dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as fh:
            data = _toml_load(fh)
    memory = data.setdefault("memory", {})
    auth = memory.setdefault("auth", {})
    sso = auth.setdefault("sso", {})
    idps = sso.setdefault("idps", {})
    # Only persist non-empty fields.
    entry = {"kind": kind}
    for k, v in fields.items():
        if v not in ("", None):
            entry[k] = v
    idps[name] = entry
    with open(path, "w", encoding="utf-8") as fh:
        _toml_dump(data, fh)


def _now_iso() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
