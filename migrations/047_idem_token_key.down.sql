-- 047 down: drop the SSO signing-key and IdP-metadata-cache tables.
--
-- Pure additive migration (047 only ADDS tables), so the down migration
-- simply drops them. No data is migrated out — these tables hold only
-- derived credentials/metadata, never primary memory data, so there is
-- no primary-data loss (Hard Rule 19).

DROP TABLE IF EXISTS sso_idp_cache;
DROP TABLE IF EXISTS idem_token_key;
