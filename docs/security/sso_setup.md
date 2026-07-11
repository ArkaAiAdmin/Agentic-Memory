# SSO / OIDC / SAML Setup

Phase 2 — identity provider integration for agentic-memory.

## Overview

The SSO subsystem lets agents authenticate via an external identity provider
(IdP) using either **OIDC** (OpenID Connect) or **SAML 2.0**. On first login a
local `principals` row is created and linked to the IdP identity. Locally-minted
JWTs (signed by an addressable RSA key stored in `idem_token_key`) are issued
for subsequent API requests.

Supported flows:
- **OIDC**: authorization code flow (`login_url` → IdP → `callback` with `code`)
- **SAML**: HTTP-Redirect/POST (`login_url` → IdP → `callback` with `saml_response`)

## Architecture

```
Agent / CLI → memory_maintenance(operation="login_url")
                → builds IdP auth URL (OIDC authorize or SAML SSO endpoint)

Agent / CLI → memory_maintenance(operation="callback")
                → SsoSession.parse_callback()
                  → verify OIDC id_token against IdP JWKS
                  → or parse SAML assertion + verify XML signature
                → resolve_or_create_principal()
                  → INSERT into principals / principal_identities (first login)
                  → or SELECT existing principal
                → sign_token() → returns local JWT + principal_id + audit log
```

The signing key is auto-generated on first use (migration 047) and stored in
`idem_token_key`. Keys are addressable by `kid` so rotation does not invalidate
tokens signed by still-valid keys.

## Configuration

All SSO configuration lives in `memory.toml` under `[memory.auth.sso]`:

```toml
[memory.auth.sso]
default_tenant = "default"

[memory.auth.sso.idps.google]
kind = "oidc"
client_id = "your-client-id"
client_secret = "your-client-secret"
authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
token_url = "https://oauth2.googleapis.com/token"
jwks_url = "https://www.googleapis.com/oauth2/v3/certs"
issuer = "https://accounts.google.com"
scopes = "openid email profile"
tenant_id = "default"

[memory.auth.sso.idps.okta]
kind = "oidc"
client_id = "0oa..."
client_secret = "..."
authorize_url = "https://dev-XXXXXX.okta.com/oauth2/default/v1/authorize"
token_url = "https://dev-XXXXXX.okta.com/oauth2/default/v1/token"
jwks_url = "https://dev-XXXXXX.okta.com/oauth2/default/v1/keys"
issuer = "https://dev-XXXXXX.okta.com/oauth2/default"
scopes = "openid email profile"

[memory.auth.sso.idps.saml-idp]
kind = "saml"
entity_id = "https://memory.example.com/saml/metadata"
metadata_url = "https://idp.example.com/metadata.xml"
sso_url = "https://idp.example.com/sso"
signing_cert_pem = """-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----"""
```

Use the `sso_idp_add` operation to add IdPs programmatically:
```
memory_maintenance(operation="sso_idp_add",
  name="azure", kind="oidc",
  client_id="...", client_secret="...",
  authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
  token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
  jwks_url="https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
  issuer="https://login.microsoftonline.com/{tenant}/v2.0")
```

## Operations (ADMIN — via `memory_maintenance`)

| Operation | Purpose |
|-----------|---------|
| `login_url` | Build a provider auth URL |
| `callback` | Exchange IdP response for local JWT + principal |
| `whoami` | Introspect a local JWT |
| `rotate_key` | Rotate signing key (revoke old, mint new) |
| `sso_sync_metadata` | Fetch + cache IdP metadata |
| `sso_idp_list` | List configured IdPs |
| `sso_idp_add` | Register a new IdP in `memory.toml` |

## OIDC Login Flow

1. **Get login URL:**
   ```
   memory_maintenance(operation="login_url",
     provider="google", redirect_uri="https://app.example.com/callback")
   ```
   Returns: `{"provider": "google", "login_url": "https://accounts.google.com/o/oauth2/v2/auth?..."}`

2. **User authenticates at IdP** → IdP redirects to `redirect_uri` with `?code=...`

3. **Exchange code for JWT:**
   ```
   memory_maintenance(operation="callback",
     provider="google", code="<code-from-idp>")
   ```
   Returns: `{"principal_id": "principal-...", "token": "<local-jwt>", ...}`

4. **Use JWT for API auth:** pass as `Authorization: Bearer <local-jwt>` header.

## SAML Login Flow

1. **Get SSO URL:**
   ```
   memory_maintenance(operation="login_url", provider="saml-idp")
   ```
   Returns the IdP SSO endpoint. The caller constructs an `AuthnRequest` (SP
   signs it with its own certificate) and POSTs or redirects the user.

2. **IdP responds** with a `SAMLResponse` (base64-encoded XML assertion).

3. **Exchange:**
   ```
   memory_maintenance(operation="callback",
     provider="saml-idp", saml_response="<base64>")
   ```
   Parses the assertion, verifies the XML signature, creates/resolves the
   principal, and returns a local JWT.

## Key Rotation

```
memory_maintenance(operation="rotate_key")
```
Revokes the current active key and generates a new one. Tokens signed by the
revoked key remain valid until the key's `revoked_at` timestamp; verification
checks are strict and will reject tokens whose `kid` maps to a revoked key.

## Security Notes

1. **SAML signature verification** is optional at compile time: it requires
   `pyxmlsec` (xmlsec1) + `lxml`. When these are not installed, unsigned
   SAML assertions are rejected (fail-closed).

2. **XXE protection:** All XML parsing uses `defusedxml` when available,
   otherwise strips `<!DOCTYPE>` and `<!ENTITY>` declarations before parsing.

3. **Audit logging:** Every `callback` invocation writes an audit row tagged
   with the resolved `principal_id` for accountability.

4. **JWT validation in API server:** The `_require_auth` handler in
   `infra/api_server.py` tries JWT verification first (SSO-issued tokens),
   then falls back to static bearer token comparison.

5. **JWKS caching:** IdP JWKS / SAML metadata is fetched on demand and
   cached in `sso_idp_cache` (DB) and `memory/.auth_cache/` (disk). Use
   `sso_sync_metadata(force=True)` to refresh.

## Table Reference

| Table | Migration | Purpose |
|-------|-----------|---------|
| `idem_token_key` | 047 | Addressable RSA signing keys (kid-indexed) |
| `sso_idp_cache` | 047 | IdP metadata XML cache |
| `principals` | 043 | Local principal rows |
| `principal_identities` | 043 | (provider, external_sub) → principal mapping |
