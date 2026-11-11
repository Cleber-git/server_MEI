-- F5: capabilities versionadas por UF × operação × ambiente e metadados de catálogo.
-- Aditiva e idempotente. Transação aberta e fechada pelo migrate.py (sem BEGIN/COMMIT aqui).
--
-- Cada linha é uma versão imutável. A vigente é a de maior effective_from <= NOW() para
-- (uf, operation, environment). Mudar um dado = inserir linha nova, nunca atualizar a antiga.
-- Ausência de linha, ou linha sem fonte, significa bloqueado (fail closed).

-- Nome do município no endereço do estabelecimento (seção 5 do contrato).
ALTER TABLE fiscal_establishments ADD COLUMN IF NOT EXISTS address_city TEXT;

CREATE TABLE IF NOT EXISTS fiscal_capabilities (
    id TEXT PRIMARY KEY,
    uf CHAR(2) NOT NULL CHECK (uf ~ '^[A-Z]{2}$'),
    operation TEXT NOT NULL CHECK (operation IN ('NFSE', 'NFCE_65', 'NFE_55', 'OFFICIAL_NFF', 'STATE_NFAE')),
    environment TEXT NOT NULL CHECK (environment IN ('RESTRICTED_PRODUCTION', 'PRODUCTION')),
    ruleset_version TEXT NOT NULL,
    layout_version TEXT,
    effective_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    available BOOLEAN NOT NULL DEFAULT FALSE,
    accredited BOOLEAN NOT NULL DEFAULT FALSE,
    requires_certificate BOOLEAN NOT NULL DEFAULT TRUE,
    requires_csc BOOLEAN NOT NULL DEFAULT FALSE,
    requires_state_registration BOOLEAN NOT NULL DEFAULT FALSE,
    transport_contract_required BOOLEAN NOT NULL DEFAULT TRUE,
    unidentified_recipient_allowed BOOLEAN,
    -- Canal oficial da UF (emissor da SEFAZ). Só https em *.gov.br, sem credenciais.
    official_channel_url TEXT CHECK (
        official_channel_url ~ '^https://([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*gov\.br(/[A-Za-z0-9._~%/?#=&+-]*)?$'
    ),
    -- Fonte oficial que comprova o dado. Linha "disponível" sem fonte e data é recusada.
    source_url TEXT CHECK (
        source_url ~ '^https://([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*gov\.br(/[A-Za-z0-9._~%/?#=&+-]*)?$'
    ),
    verified_at DATE,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (uf, operation, environment, effective_from),
    CHECK (NOT available OR (source_url IS NOT NULL AND verified_at IS NOT NULL)),
    CHECK (NOT accredited OR (source_url IS NOT NULL AND verified_at IS NOT NULL)),
    CHECK (valid_until IS NULL OR valid_until > effective_from)
);
CREATE INDEX IF NOT EXISTS fiscal_capabilities_current_idx
    ON fiscal_capabilities (uf, environment, operation, effective_from DESC);

-- Seed conservador (ruleset 2026-11-11.1): tudo indisponível. NFS-e, NFC-e e NF-e
-- automáticas só ligam com linha nova + homologação registrada em fiscal_homologation_checks.
INSERT INTO fiscal_capabilities
    (id, uf, operation, environment, ruleset_version, effective_from, available, accredited,
     requires_certificate, requires_csc, requires_state_registration, reason)
SELECT u.uf || ':' || o.op || ':' || e.env || ':2026-11-11.1', u.uf, o.op, e.env, '2026-11-11.1',
       TIMESTAMPTZ '2026-11-11T00:00:00Z', FALSE, FALSE,
       o.op IN ('NFSE', 'NFCE_65', 'NFE_55'), o.op = 'NFCE_65', o.op IN ('NFCE_65', 'NFE_55'),
       'Emissão automática ainda não homologada para esta UF.'
FROM (VALUES ('AC'), ('AL'), ('AM'), ('AP'), ('BA'), ('CE'), ('DF'), ('ES'), ('GO'), ('MA'),
             ('MG'), ('MS'), ('MT'), ('PA'), ('PB'), ('PE'), ('PI'), ('PR'), ('RJ'), ('RN'),
             ('RO'), ('RR'), ('RS'), ('SC'), ('SE'), ('SP'), ('TO')) AS u(uf)
CROSS JOIN (VALUES ('NFSE'), ('NFCE_65'), ('NFE_55'), ('OFFICIAL_NFF')) AS o(op)
CROSS JOIN (VALUES ('RESTRICTED_PRODUCTION'), ('PRODUCTION')) AS e(env)
WHERE NOT (o.op = 'OFFICIAL_NFF' AND u.uf IN ('RS', 'SC', 'PI'))
ON CONFLICT DO NOTHING;

-- NFF (Nota Fiscal Fácil, app oficial; o servidor nunca emite): módulo MEI liberado em
-- RS e SC (notícia de 23/10/2023) e no PI (notícia de 21/03/2024), conferido no portal
-- oficial em 2026-11-11 (dfe-portal.svrs.rs.gov.br/Nff/Noticias/2916). Não exige certificado.
INSERT INTO fiscal_capabilities
    (id, uf, operation, environment, ruleset_version, effective_from, available, accredited,
     requires_certificate, source_url, verified_at, reason)
SELECT u.uf || ':OFFICIAL_NFF:' || e.env || ':2026-11-11.1', u.uf, 'OFFICIAL_NFF', e.env,
       '2026-11-11.1', TIMESTAMPTZ '2026-11-11T00:00:00Z', TRUE, TRUE, FALSE,
       'https://dfe-portal.svrs.rs.gov.br/Nff/Noticias/2916', DATE '2026-11-11',
       'Use o aplicativo oficial Nota Fiscal Fácil (NFF) para notas de produto.'
FROM (VALUES ('RS'), ('SC'), ('PI')) AS u(uf)
CROSS JOIN (VALUES ('RESTRICTED_PRODUCTION'), ('PRODUCTION')) AS e(env)
ON CONFLICT DO NOTHING;

-- Metadados dos catálogos oficiais (NCM, CEST, CFOP, NBS, código de tributação nacional).
-- Sem linha = catálogo sem fonte oficial carregada (a rota responde 404).
CREATE TABLE IF NOT EXISTS fiscal_catalog_metadata (
    type TEXT NOT NULL CHECK (type IN ('ncm', 'cest', 'cfop', 'nbs', 'nationalTaxCode')),
    version TEXT NOT NULL,
    source_url TEXT NOT NULL CHECK (
        source_url ~ '^https://([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*gov\.br(/[A-Za-z0-9._~%/?#=&+-]*)?$'
    ),
    published_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (type, version)
);
