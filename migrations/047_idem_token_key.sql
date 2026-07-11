-- 047: Addressable signing keys for SSO-issued JWTs + IdP metadata cache.
--
-- Phase 2 (SSO/OIDC/SAML). Two additive tables, no destructive change.
--
--   idem_token_key: stores per-kid RSA key pairs used to sign the JWTs
--     this service mints for authenticated SSO sessions. Keeping keys
--     addressable (by kid) enables clean rotation without invalidating
--     tokens signed by still-valid keys.
--   sso_idp_cache: caches fetched IdP metadata (SAML metadata XML or OIDC
--     discovery document) so we don't hammer the IdP on every login and
--     can detect metadata drift. Backed on disk by memory/.auth_cache/.

CREATE TABLE IF NOT EXISTS idem_token_key (
    kid         TEXT PRIMARY KEY,
    public_jwk  TEXT NOT NULL,
    private_jwk TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_idem_token_key_revoked ON idem_token_key(revoked_at);

CREATE TABLE IF NOT EXISTS sso_idp_cache (
    id          TEXT PRIMARY KEY,
    metadata_xml TEXT NOT NULL,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sso_idp_cache_fetched ON sso_idp_cache(fetched_at);
