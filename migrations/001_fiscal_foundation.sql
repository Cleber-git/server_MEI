BEGIN;

CREATE TABLE IF NOT EXISTS auth_sessions (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    refresh_token_hash CHAR(64) NOT NULL UNIQUE,
    refresh_family_id UUID NOT NULL,
    previous_refresh_token_hash CHAR(64),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT,
    user_agent_hash CHAR(64),
    ip_prefix_hash CHAR(64),
    CONSTRAINT auth_sessions_user_fk FOREIGN KEY (user_id) REFERENCES usuariomei(uuid),
    CONSTRAINT auth_sessions_tenant_fk FOREIGN KEY (tenant_id) REFERENCES empresa(uuid)
);
CREATE INDEX IF NOT EXISTS auth_sessions_active_idx ON auth_sessions (user_id, tenant_id, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_permissions (
    user_id TEXT NOT NULL REFERENCES usuariomei(uuid),
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    permission TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, tenant_id, permission)
);

CREATE TABLE IF NOT EXISTS auth_login_attempts (
    subject_hash CHAR(64) PRIMARY KEY,
    window_started_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    blocked_until TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fiscal_establishments (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    legal_name TEXT NOT NULL,
    trade_name TEXT,
    cnpj VARCHAR(32) NOT NULL,
    state_registration TEXT,
    municipal_registration TEXT,
    tax_regime TEXT NOT NULL,
    is_mei BOOLEAN NOT NULL DEFAULT FALSE,
    simei BOOLEAN NOT NULL DEFAULT FALSE,
    crt SMALLINT,
    address_street TEXT,
    address_number TEXT,
    address_complement TEXT,
    address_district TEXT,
    address_zip_code TEXT,
    ibge_city_code CHAR(7),
    uf CHAR(2) NOT NULL,
    cnaes JSONB NOT NULL DEFAULT '[]'::jsonb,
    fiscal_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    production_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, cnpj)
);

CREATE TABLE IF NOT EXISTS fiscal_configurations (
    establishment_id UUID PRIMARY KEY REFERENCES fiscal_establishments(id),
    nfse_environment TEXT NOT NULL DEFAULT 'RESTRICTED_PRODUCTION',
    nfe_environment TEXT NOT NULL DEFAULT 'RESTRICTED_PRODUCTION',
    nfse_provider TEXT,
    nfe_provider TEXT,
    nfce_provider TEXT,
    production_requested BOOLEAN NOT NULL DEFAULT FALSE,
    production_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    homologation_approved_at TIMESTAMPTZ,
    approved_by TEXT REFERENCES usuariomei(uuid),
    last_validated_at TIMESTAMPTZ,
    monitoring_active BOOLEAN NOT NULL DEFAULT FALSE,
    operational_checklist_approved_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    CHECK (nfse_environment IN ('RESTRICTED_PRODUCTION','PRODUCTION')),
    CHECK (nfe_environment IN ('RESTRICTED_PRODUCTION','PRODUCTION'))
);

INSERT INTO fiscal_establishments
  (id,tenant_id,legal_name,trade_name,cnpj,tax_regime,is_mei,simei,uf,cnaes,fiscal_enabled)
SELECT md5(e.uuid || ':fiscal-establishment')::uuid,e.uuid,
       COALESCE(NULLIF(e.razaosocial,''),NULLIF(e.nomefantasia,''),'Cadastro incompleto'),
       e.nomefantasia,COALESCE(e.cnpj,''),COALESCE(e.regime_tributario,'MEI'),
       COALESCE(e.regime_tributario,'MEI')='MEI',COALESCE(e.optante_simples,FALSE),e.uf,
       CASE WHEN e.cnae IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(e.cnae) END,FALSE
FROM empresa e
WHERE e.uf ~ '^[A-Z]{2}$'
ON CONFLICT (tenant_id,cnpj) DO NOTHING;

INSERT INTO fiscal_configurations(establishment_id)
SELECT id FROM fiscal_establishments ON CONFLICT(establishment_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS fiscal_series (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    establishment_id UUID NOT NULL REFERENCES fiscal_establishments(id),
    document_type TEXT NOT NULL,
    environment TEXT NOT NULL,
    series TEXT NOT NULL,
    next_number BIGINT NOT NULL DEFAULT 1 CHECK (next_number > 0),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, establishment_id, document_type, environment, series)
);

CREATE TABLE IF NOT EXISTS fiscal_certificates (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    establishment_id UUID NOT NULL REFERENCES fiscal_establishments(id),
    vault_reference TEXT NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    serial_number TEXT NOT NULL,
    holder_masked TEXT NOT NULL,
    issuer TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    type TEXT NOT NULL CHECK (type = 'A1'),
    status TEXT NOT NULL,
    last_validated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (tenant_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS fiscal_credentials (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    establishment_id UUID NOT NULL REFERENCES fiscal_establishments(id),
    credential_type TEXT NOT NULL,
    vault_reference TEXT NOT NULL,
    masked_identifier TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (tenant_id, establishment_id, credential_type) DEFERRABLE INITIALLY IMMEDIATE
);

CREATE TABLE IF NOT EXISTS fiscal_documents (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    establishment_id UUID NOT NULL REFERENCES fiscal_establishments(id),
    idempotency_key VARCHAR(200) NOT NULL,
    canonical_request_hash CHAR(64) NOT NULL,
    schema_version VARCHAR(20) NOT NULL,
    sale_id TEXT,
    document_type TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    series TEXT,
    number BIGINT,
    official_key TEXT,
    protocol TEXT,
    authorized_at TIMESTAMPTZ,
    fiscal_error_code TEXT,
    fiscal_error_message TEXT,
    correlation_id UUID NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, establishment_id, idempotency_key),
    UNIQUE (tenant_id, id),
    CHECK (document_type IN ('NFSE','NFCE_65','NFE_55')),
    CHECK (environment IN ('RESTRICTED_PRODUCTION','PRODUCTION')),
    CHECK (status <> 'AUTHORIZED' OR
      (series IS NOT NULL AND number IS NOT NULL AND official_key IS NOT NULL AND protocol IS NOT NULL AND authorized_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS fiscal_documents_history_idx ON fiscal_documents (tenant_id, establishment_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS fiscal_documents_sale_idx ON fiscal_documents (tenant_id, sale_id) WHERE sale_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fiscal_document_payloads (
    document_id UUID PRIMARY KEY REFERENCES fiscal_documents(id),
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    canonical_request_ciphertext BYTEA NOT NULL,
    technical_response_ciphertext BYTEA,
    encryption_key_reference TEXT NOT NULL,
    retention_until DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fiscal_artifacts (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    document_id UUID NOT NULL REFERENCES fiscal_documents(id),
    artifact_type TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    sha256 CHAR(64) NOT NULL,
    content_type TEXT NOT NULL,
    size BIGINT NOT NULL CHECK (size >= 0),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, document_id, artifact_type)
);

ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS xml_artifact_id UUID REFERENCES fiscal_artifacts(id);
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS auxiliary_artifact_id UUID REFERENCES fiscal_artifacts(id);
ALTER TABLE fiscal_documents DROP CONSTRAINT IF EXISTS fiscal_documents_authorized_complete;
ALTER TABLE fiscal_documents ADD CONSTRAINT fiscal_documents_authorized_complete CHECK (status <> 'AUTHORIZED' OR
  (series IS NOT NULL AND number IS NOT NULL AND official_key IS NOT NULL AND protocol IS NOT NULL AND authorized_at IS NOT NULL
   AND xml_artifact_id IS NOT NULL));

CREATE TABLE IF NOT EXISTS fiscal_events (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    document_id UUID NOT NULL REFERENCES fiscal_documents(id),
    event_type TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    protocol TEXT,
    status TEXT NOT NULL,
    event_artifact_id UUID REFERENCES fiscal_artifacts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, document_id, event_type, sequence)
);

CREATE TABLE IF NOT EXISTS fiscal_idempotency (
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    establishment_id UUID NOT NULL REFERENCES fiscal_establishments(id),
    operation TEXT NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    canonical_payload_hash CHAR(64) NOT NULL,
    resource_id UUID,
    status TEXT NOT NULL,
    response_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, establishment_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS fiscal_audit_log (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    actor_id TEXT NOT NULL REFERENCES usuariomei(uuid),
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    correlation_id UUID NOT NULL,
    result TEXT NOT NULL,
    sanitized_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS fiscal_audit_tenant_time_idx ON fiscal_audit_log (tenant_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS fiscal_homologation_checks (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    establishment_id UUID NOT NULL REFERENCES fiscal_establishments(id),
    uf CHAR(2) NOT NULL,
    model TEXT NOT NULL,
    scenario TEXT NOT NULL,
    result TEXT NOT NULL,
    evidence_reference TEXT,
    schema_manual_version TEXT NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL,
    responsible_user_id TEXT NOT NULL REFERENCES usuariomei(uuid)
);

CREATE TABLE IF NOT EXISTS fiscal_jobs (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    document_id UUID REFERENCES fiscal_documents(id),
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    correlation_id UUID NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, document_id, job_type, status)
);
CREATE INDEX IF NOT EXISTS fiscal_jobs_claim_idx ON fiscal_jobs (status, available_at) WHERE status IN ('QUEUED','RETRY');

COMMIT;
