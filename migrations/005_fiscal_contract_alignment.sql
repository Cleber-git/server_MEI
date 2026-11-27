BEGIN;

-- Needed by the Android certificate status DTO. Keep this value alongside the
-- other certificate metadata; never write it to application logs.
ALTER TABLE fiscal_certificates
  ADD COLUMN IF NOT EXISTS holder_tax_id VARCHAR(32);

-- Persist the effective ruleset identity for tenant/UF capability responses.
-- No capability is enabled by this table until an official, reviewed row is
-- deliberately inserted by an operator.
CREATE TABLE IF NOT EXISTS fiscal_capabilities (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    uf CHAR(2) NOT NULL,
    version TEXT NOT NULL,
    rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_reference TEXT,
    source_published_at DATE,
    consulted_at TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, uf, version)
);
CREATE INDEX IF NOT EXISTS fiscal_capabilities_active_idx
  ON fiscal_capabilities (tenant_id, uf, active);

COMMIT;
