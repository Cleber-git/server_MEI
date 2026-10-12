CREATE TABLE IF NOT EXISTS produto_venda_fiscal (
    empresa_uuid TEXT NOT NULL,
    venda_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROCESSANDO',
    resultado JSONB,
    notificado BOOLEAN NOT NULL DEFAULT FALSE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (empresa_uuid, venda_id)
);
