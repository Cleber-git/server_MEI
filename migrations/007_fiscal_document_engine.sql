-- F7: motor de documentos (outbox, worker, eventos, artefatos no cofre). Aditiva e idempotente.
-- Transação aberta e fechada pelo migrate.py (sem BEGIN/COMMIT aqui).
--
-- Não lê nem altera linhas existentes. As CHECKs novas entram NOT VALID (valem para toda
-- escrita nova, sem revalidar o que já existe) e os índices únicos novos conferem antes
-- se há duplicata; se houver, a migration para com erro claro e nada é aplicado.

-- Cofre: payload da intenção e artefatos (XML/PDF) também vão para fiscal_vault_blobs.
ALTER TABLE fiscal_vault_blobs DROP CONSTRAINT IF EXISTS fiscal_vault_blobs_purpose_check;
ALTER TABLE fiscal_vault_blobs ADD CONSTRAINT fiscal_vault_blobs_purpose_check
    CHECK (purpose IN ('CERTIFICATE', 'CSC', 'DOCUMENT_PAYLOAD', 'ARTIFACT'));

-- Documento: evidência, incerteza do último envio, payload e substituição.
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS payload_blob_id UUID REFERENCES fiscal_vault_blobs(id);
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS cancel_reason_blob_id UUID REFERENCES fiscal_vault_blobs(id);
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS xml_sha256 CHAR(64);
-- TRUE enquanto um envio (emissão ou cancelamento) pode ter chegado ao autorizador sem
-- resposta: o worker sempre consulta antes de reenviar.
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS outcome_uncertain BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS official_code TEXT;
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE fiscal_documents ADD COLUMN IF NOT EXISTS substitutes_document_id UUID REFERENCES fiscal_documents(id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'fiscal_documents_authorized_evidence'
                     AND conrelid = 'fiscal_documents'::regclass) THEN
        ALTER TABLE fiscal_documents ADD CONSTRAINT fiscal_documents_authorized_evidence
            CHECK (status NOT IN ('AUTHORIZED', 'CANCEL_PENDING', 'CANCELLED') OR (
                series IS NOT NULL AND number IS NOT NULL AND official_key IS NOT NULL
                AND protocol IS NOT NULL AND authorized_at IS NOT NULL
                AND xml_artifact_id IS NOT NULL AND xml_sha256 ~ '^[0-9a-f]{64}$'))
            NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'fiscal_documents_status_known'
                     AND conrelid = 'fiscal_documents'::regclass) THEN
        ALTER TABLE fiscal_documents ADD CONSTRAINT fiscal_documents_status_known
            CHECK (status IN ('DRAFT', 'VALIDATING', 'QUEUED', 'SUBMITTED', 'PROCESSING',
                'AUTHORIZED', 'REJECTED', 'CANCEL_PENDING', 'CANCELLED', 'CONTINGENCY_PENDING',
                'ERROR_RETRYABLE', 'ERROR_FINAL'))
            NOT VALID;
    END IF;

    -- Mesma venda + tipo + intenção com outra Idempotency-Key ⇒ 409 DUPLICATE_FISCAL_INTENT.
    -- Rejeitada ou com erro final não conta: a venda pode ser emitida de novo.
    IF to_regclass('fiscal_documents_intent_uidx') IS NULL THEN
        IF EXISTS (SELECT 1 FROM fiscal_documents
                   WHERE sale_id IS NOT NULL AND status NOT IN ('REJECTED', 'ERROR_FINAL')
                   GROUP BY tenant_id, sale_id, document_type, canonical_request_hash
                   HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'fiscal_documents tem intenção duplicada por venda; revise antes de aplicar a 007';
        END IF;
        CREATE UNIQUE INDEX fiscal_documents_intent_uidx
            ON fiscal_documents (tenant_id, sale_id, document_type, canonical_request_hash)
            WHERE sale_id IS NOT NULL AND status NOT IN ('REJECTED', 'ERROR_FINAL');
    END IF;
END
$$;

-- Uma substituição ativa por documento original.
CREATE UNIQUE INDEX IF NOT EXISTS fiscal_documents_substitution_uidx
    ON fiscal_documents (tenant_id, substitutes_document_id)
    WHERE substitutes_document_id IS NOT NULL AND status NOT IN ('REJECTED', 'ERROR_FINAL');

-- Artefatos: conteúdo cifrado no cofre; a linha é imutável.
ALTER TABLE fiscal_artifacts ADD COLUMN IF NOT EXISTS vault_blob_id UUID REFERENCES fiscal_vault_blobs(id);

CREATE OR REPLACE FUNCTION fiscal_artifacts_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'fiscal_artifacts é imutável';
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS fiscal_artifacts_immutable_trg ON fiscal_artifacts;
CREATE TRIGGER fiscal_artifacts_immutable_trg BEFORE UPDATE OR DELETE ON fiscal_artifacts
    FOR EACH ROW EXECUTE FUNCTION fiscal_artifacts_immutable();

-- Eventos: transições sem payload sensível (GET /documents/{id}/events).
ALTER TABLE fiscal_events ADD COLUMN IF NOT EXISTS from_status TEXT;
ALTER TABLE fiscal_events ADD COLUMN IF NOT EXISTS to_status TEXT;
ALTER TABLE fiscal_events ADD COLUMN IF NOT EXISTS correlation_id UUID;
ALTER TABLE fiscal_events ADD COLUMN IF NOT EXISTS official_code TEXT;
ALTER TABLE fiscal_events ADD COLUMN IF NOT EXISTS message TEXT;
CREATE INDEX IF NOT EXISTS fiscal_events_document_idx ON fiscal_events (tenant_id, document_id, sequence);

-- Jobs: lease expirado também é reivindicável (processo reiniciado no meio).
CREATE INDEX IF NOT EXISTS fiscal_jobs_lease_idx ON fiscal_jobs (lease_expires_at) WHERE status = 'RUNNING';
CREATE INDEX IF NOT EXISTS fiscal_jobs_document_idx ON fiscal_jobs (tenant_id, document_id, job_type);
