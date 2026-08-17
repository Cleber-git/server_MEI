BEGIN;
-- Rollback is intentionally refused when fiscal records exist. Preserve statutory records.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM fiscal_documents LIMIT 1) THEN
    RAISE EXCEPTION 'rollback refused: fiscal_documents contains records';
  END IF;
END $$;
DROP TABLE IF EXISTS fiscal_jobs, fiscal_homologation_checks, fiscal_audit_log,
  fiscal_idempotency, fiscal_events, fiscal_document_payloads, fiscal_credentials,
  fiscal_certificates, fiscal_series, fiscal_configurations, fiscal_establishments,
  auth_login_attempts, auth_permissions, auth_sessions CASCADE;
DROP TABLE IF EXISTS fiscal_artifacts CASCADE;
DROP TABLE IF EXISTS fiscal_documents CASCADE;
COMMIT;
