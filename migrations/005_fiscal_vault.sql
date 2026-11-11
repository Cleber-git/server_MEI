-- F4: cofre de segredos no Postgres (envelope AES-256-GCM), aditiva e idempotente.
-- Transação aberta e fechada pelo migrate.py (sem BEGIN/COMMIT aqui).
--
-- Cada linha guarda um segredo cifrado com uma DEK própria; a DEK vai embrulhada
-- pela master key `key_id` (env FISCAL_VAULT_MASTER_KEY). Tenant, id e finalidade
-- entram como AAD, então trocar blobs de linha ou de tenant quebra a decifragem.
-- Destruir = zerar wrapped_dek/ciphertext e marcar destroyed_at (sem DELETE).

CREATE TABLE IF NOT EXISTS fiscal_vault_blobs (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES empresa(uuid),
    purpose TEXT NOT NULL CHECK (purpose IN ('CERTIFICATE', 'CSC')),
    key_id TEXT NOT NULL,
    dek_nonce BYTEA NOT NULL,
    wrapped_dek BYTEA NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rotated_at TIMESTAMPTZ,
    destroyed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS fiscal_vault_blobs_key_idx
    ON fiscal_vault_blobs (key_id) WHERE destroyed_at IS NULL;

-- Titular lido do certificado (CNPJ do e-CNPJ ou CPF do e-CPF do titular MEI).
ALTER TABLE fiscal_certificates ADD COLUMN IF NOT EXISTS holder_tax_id TEXT;
ALTER TABLE fiscal_certificates ADD COLUMN IF NOT EXISTS holder_kind TEXT
    CHECK (holder_kind IN ('CNPJ', 'CPF'));

-- CPF do titular MEI, para conferir um e-CPF (preenchido pelo PUT /establishment, F5).
ALTER TABLE fiscal_establishments ADD COLUMN IF NOT EXISTS responsible_cpf CHAR(11)
    CHECK (responsible_cpf ~ '^[0-9]{11}$');
