CREATE TABLE IF NOT EXISTS frn_usuario (
    id BIGSERIAL PRIMARY KEY,
    nome VARCHAR(120) NOT NULL,
    login VARCHAR(60) NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS frn_fornecedor (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES frn_usuario(id) ON DELETE CASCADE,
    nome VARCHAR(160) NOT NULL,
    documento VARCHAR(24) NOT NULL DEFAULT '',
    contato VARCHAR(140) NOT NULL DEFAULT '',
    telefone VARCHAR(30) NOT NULL DEFAULT '',
    email VARCHAR(180) NOT NULL DEFAULT '',
    chave_pix VARCHAR(180) NOT NULL DEFAULT '',
    endereco TEXT NOT NULL DEFAULT '',
    observacoes TEXT NOT NULL DEFAULT '',
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS frn_auditoria (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT REFERENCES frn_usuario(id),
    acao VARCHAR(60) NOT NULL,
    fornecedor_id BIGINT,
    detalhes JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip VARCHAR(64),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_frn_fornecedor_usuario ON frn_fornecedor(usuario_id, ativo);
CREATE INDEX IF NOT EXISTS idx_frn_auditoria_usuario ON frn_auditoria(usuario_id, criado_em DESC);
