-- F2: auth Bearer definitivo. Aditiva e idempotente; transação aberta pelo migrate.py.

-- deviceId opcional do login (contrato 3.1), guardado só como SHA-256.
ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS device_id_hash CHAR(64);

-- Todo refresh já usado. Apresentar qualquer um deles de novo revoga a família
-- inteira (REFRESH_TOKEN_REUSED), não só o imediatamente anterior.
CREATE TABLE IF NOT EXISTS auth_refresh_token_uses (
    token_hash CHAR(64) PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES auth_sessions(id),
    refresh_family_id UUID NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS auth_refresh_token_uses_family_idx
    ON auth_refresh_token_uses (refresh_family_id);

-- Marca de "permissões padrão já concedidas ao dono" por empresa. Concede uma
-- vez só: se um operador revogar um grant depois, ele não volta sozinho.
CREATE TABLE IF NOT EXISTS auth_permission_defaults (
    tenant_id TEXT PRIMARY KEY REFERENCES empresa(uuid),
    owner_user_id TEXT NOT NULL REFERENCES usuariomei(uuid),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dono = primeiro usuário da empresa (ativo antes de inativo, depois o
-- datacadastro mais antigo, depois o uuid). Mesma regra de
-- fiscal/auth.py:OWNER_DEFAULTS_SQL, aplicada também no login de empresas novas.
-- As 6 permissões da seção 3.4 do contrato; FISCAL_PRODUCTION_ENABLE nunca.
WITH owners AS (
    SELECT DISTINCT ON (u.empresauuid) u.uuid AS user_id, u.empresauuid AS tenant_id
    FROM usuariomei u
    JOIN empresa e ON e.uuid = u.empresauuid
    ORDER BY u.empresauuid, (u.ativo IS TRUE) DESC, u.datacadastro ASC NULLS LAST, u.uuid
), applied AS (
    INSERT INTO auth_permission_defaults (tenant_id, owner_user_id)
    SELECT tenant_id, user_id FROM owners
    ON CONFLICT (tenant_id) DO NOTHING
    RETURNING tenant_id, owner_user_id
)
INSERT INTO auth_permissions (user_id, tenant_id, permission)
SELECT a.owner_user_id, a.tenant_id, p.permission
FROM applied a
CROSS JOIN (VALUES
    ('FISCAL_CONFIG_READ'),
    ('FISCAL_CONFIG_WRITE'),
    ('FISCAL_CERTIFICATE_MANAGE'),
    ('FISCAL_DOCUMENT_CREATE'),
    ('FISCAL_DOCUMENT_READ'),
    ('FISCAL_DOCUMENT_CANCEL')
) AS p(permission)
ON CONFLICT (user_id, tenant_id, permission) DO NOTHING;
