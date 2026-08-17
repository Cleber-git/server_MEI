CREATE TABLE IF NOT EXISTS cpt_responsavel (
    id BIGSERIAL PRIMARY KEY,
    nome VARCHAR(140) NOT NULL,
    chave_pix VARCHAR(180) NOT NULL DEFAULT '',
    login VARCHAR(60) NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cpt_campanha (
    id BIGSERIAL PRIMARY KEY,
    responsavel_id BIGINT NOT NULL REFERENCES cpt_responsavel(id),
    nome VARCHAR(140) NOT NULL,
    slug VARCHAR(160) NOT NULL UNIQUE,
    descricao TEXT NOT NULL DEFAULT '',
    chave_pix VARCHAR(180) NOT NULL DEFAULT '',
    ativa BOOLEAN NOT NULL DEFAULT TRUE,
    criada_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cpt_produto (
    id BIGSERIAL PRIMARY KEY,
    campanha_id BIGINT NOT NULL REFERENCES cpt_campanha(id) ON DELETE CASCADE,
    nome VARCHAR(140) NOT NULL,
    descricao TEXT NOT NULL DEFAULT '',
    preco NUMERIC(12,2) NOT NULL CHECK (preco > 0),
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cpt_pedido (
    id BIGSERIAL PRIMARY KEY,
    campanha_id BIGINT NOT NULL REFERENCES cpt_campanha(id),
    cliente_nome VARCHAR(140) NOT NULL,
    data_coleta DATE NOT NULL DEFAULT CURRENT_DATE,
    status_pagamento VARCHAR(12) NOT NULL DEFAULT 'pendente'
        CHECK (status_pagamento IN ('pago', 'pendente')),
    valor_total NUMERIC(12,2) NOT NULL CHECK (valor_total >= 0),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cpt_pedido_item (
    id BIGSERIAL PRIMARY KEY,
    pedido_id BIGINT NOT NULL REFERENCES cpt_pedido(id) ON DELETE CASCADE,
    produto_id BIGINT NOT NULL REFERENCES cpt_produto(id),
    produto_nome VARCHAR(140) NOT NULL,
    quantidade INTEGER NOT NULL CHECK (quantidade > 0),
    valor_unitario NUMERIC(12,2) NOT NULL CHECK (valor_unitario > 0),
    subtotal NUMERIC(12,2) NOT NULL CHECK (subtotal > 0)
);

CREATE TABLE IF NOT EXISTS cpt_integrante (
    id BIGSERIAL PRIMARY KEY,
    responsavel_id BIGINT NOT NULL REFERENCES cpt_responsavel(id) ON DELETE CASCADE,
    nome VARCHAR(140) NOT NULL,
    tipo VARCHAR(12) NOT NULL CHECK (tipo IN ('oficial', 'temporario')),
    inicio_em DATE NOT NULL DEFAULT CURRENT_DATE,
    fim_em DATE,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (tipo = 'oficial' OR fim_em IS NOT NULL),
    CHECK (fim_em IS NULL OR fim_em >= inicio_em)
);

CREATE TABLE IF NOT EXISTS cpt_auditoria (
    id BIGSERIAL PRIMARY KEY,
    responsavel_id BIGINT REFERENCES cpt_responsavel(id),
    acao VARCHAR(80) NOT NULL,
    entidade VARCHAR(80) NOT NULL,
    entidade_id BIGINT,
    detalhes JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip VARCHAR(64),
    user_agent TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cpt_pedido_campanha_data ON cpt_pedido(campanha_id, data_coleta);
CREATE INDEX IF NOT EXISTS idx_cpt_pedido_criado_em ON cpt_pedido(criado_em);
CREATE INDEX IF NOT EXISTS idx_cpt_auditoria_criado_em ON cpt_auditoria(criado_em);
