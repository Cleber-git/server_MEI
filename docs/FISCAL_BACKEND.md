# Backend fiscal — arquitetura e operação segura

Estado: 2026-08-08. A integração governamental está deliberadamente desabilitada e não houve transmissão.

## Autenticação Android (F2, 2026-11-11)

Contrato: seção 3 de `Caltech-Mei/docs/fiscal-api-contract-v1.md`.

- **`POST /api/v1/auth/login`** recebe `{login, password, deviceId?}` (campo desconhecido dá 422). `login` é e-mail (sem diferenciar maiúsculas) ou CNPJ. Devolve `accessToken`, `tokenType=Bearer`, `expiresIn=900`, `expiresAt`, `refreshToken` opaco, `refreshExpiresIn` (o que **resta** da sessão), `sessionId`, `user{id,email,name,companyId,active}` e `company{id,cnpj,legalName,tradeName,active,blocked}`. O `deviceId` é guardado só como SHA-256.
- **Erros do login:** senha errada e usuário inexistente dão o mesmo `401 INVALID_CREDENTIALS` (com bcrypt fictício para não vazar pelo tempo). Senha certa com usuário ou empresa inativos/bloqueados dá `403 ACCOUNT_BLOCKED`. Cinco falhas em 15 min por login+IP dão `429 RATE_LIMITED` com `Retry-After`; o sucesso zera o contador.
- **Senha (`fiscal/passwords.py`):** o `POST /login` legado e o Bearer aceitam bcrypt e texto puro. No sucesso, o texto puro vira bcrypt (`UPDATE … WHERE senhahash=<lida>`, para não atropelar uma redefinição concorrente). Senha com mais de 72 bytes continua em texto puro, porque o bcrypt truncaria. O `/login` legado mantém os códigos 401/402 que o app espera e passa a devolver `senhaHash=""` (antes devolvia a senha guardada); o app não usa esse campo.
- **`POST /api/v1/auth/refresh`:** rotaciona o refresh e devolve o mesmo corpo do login. A sessão tem validade absoluta de `JWT_REFRESH_TTL_SECONDS` desde o login: o refresh não a estende. Todo refresh usado fica em `auth_refresh_token_uses`. Reapresentar **qualquer** um deles dá `401 REFRESH_TOKEN_REUSED` e revoga a família. Refresh desconhecido, revogado ou vencido dá `401 INVALID_TOKEN`; vínculo inativo dá `403 ACCOUNT_BLOCKED`. Dois refreshes simultâneos com o mesmo token contam como reuso (o app faz single-flight).
- **Bearer nas rotas:** JWT HS256 com `kid`, issuer, audience, `typ=access` e sessão viva no banco. Expirado dá `401 TOKEN_EXPIRED`; qualquer outra falha (assinatura, `kid`, claims, sessão revogada, tenant trocado) dá `401 INVALID_TOKEN`. Todo 401 leva `WWW-Authenticate: Bearer`. Sem Bearer: `401 FISCAL_AUTH_UPGRADE_REQUIRED` se for rota `/api/v1/fiscal` com `validation-uuid`, senão `401 AUTHENTICATION_REQUIRED`. O `validation-uuid` nunca muda o tenant.
- **`GET /api/v1/auth/me`** devolve `{user, company, permissions}`. **`POST /api/v1/auth/logout`** (corpo opcional `{refreshToken}`) revoga a sessão atual e, se informado, a sessão daquele refresh, desde que seja do mesmo usuário e empresa. Devolve 204.
- **Chaves:** `JWT_SIGNING_KEYS` aceita várias chaves `kid:segredo` (segredo ≥ 32 bytes; mais curto é descartado) e `JWT_ACTIVE_KID` escolhe a que assina, o que permite rotação. Com `FISCAL_FEATURE_ENABLED=true`, o startup (`fiscal.config.assert_startup_ready`) **não sobe** sem uma chave válida e um `JWT_ACTIVE_KID` presente nela. Com o fiscal desligado, o servidor sobe e as rotas de auth respondem `503` sem detalhes.
- **Permissões:** grants em `auth_permissions`: `FISCAL_CONFIG_READ`, `FISCAL_CONFIG_WRITE`, `FISCAL_CERTIFICATE_MANAGE`, `FISCAL_DOCUMENT_CREATE`, `FISCAL_DOCUMENT_READ`, `FISCAL_DOCUMENT_CANCEL` e `FISCAL_PRODUCTION_ENABLE`. A migration `002` dá as seis primeiras ao **dono** de cada empresa: o primeiro usuário (ativo antes de inativo, depois o `datacadastro` mais antigo, depois o `uuid`). Empresas criadas depois recebem o mesmo no primeiro login Bearer do dono. A concessão é registrada em `auth_permission_defaults` e acontece uma única vez, então um grant revogado por operador não volta. `FISCAL_PRODUCTION_ENABLE` nunca é concedida automaticamente.
- **Testes:** `tests/test_auth_f2.py` (login, CNPJ, credenciais, bloqueio, rate limit, bcrypt/texto puro/CAS, `/login` legado, refresh e reuso, expiração, tokens adulterados, logout, `/me`, upgrade-required, permissões padrão, isolamento A/B em todas as rotas com id e startup). Apoio comum em `tests/fiscal_support.py`.

## Contrato fiscal v1

O contrato normativo gerado está em `openapi.json`. Todos os endpoints ficam em `/api/v1/fiscal`. DTOs mantêm camelCase e enums do Android. Erros seguem o envelope `{"error":{…}}` do contrato (seção 2), em `application/json` — ver “Erros, correlação e logs” abaixo. Criação exige `Idempotency-Key`, retorna 202 somente quando persistida e enfileirada e usa JSON canônico + SHA-256. Produção é recusada independentemente do boolean do cliente.

Queries incluem `tenant_id` da sessão. Recursos de outro tenant respondem como inexistentes. Jobs carregam tenant, documento e correlação. A constraint impede `AUTHORIZED` sem série, número, chave, protocolo, timestamp e artefato XML; o domínio também exige SHA-256 válido.

## Cofre de segredos (F4, 2026-11-11)

Contrato: seção 4.3 e o `PUT /csc` da seção 4 de `Caltech-Mei/docs/fiscal-api-contract-v1.md`. Código em `fiscal/vault.py`; testes em `tests/test_vault_f4.py`.

- **Envelope AES-256-GCM no Postgres (`fiscal_vault_blobs`, migration `005`):** cada segredo tem uma DEK aleatória de 256 bits, embrulhada pela master key ativa. AAD = versão + tenant + id do blob + finalidade (`CERTIFICATE`/`CSC`), então blob movido de linha ou de tenant não decifra. Falha de integridade dá `VaultIntegrityError` (500 genérico, sem detalhe).
- **Master keys:** `FISCAL_VAULT_MASTER_KEY=kid:base64(32 bytes)[,kid:base64…]` e `FISCAL_VAULT_ACTIVE_KEY_ID` (opcional com uma chave só). Entrada inválida é descartada. Sem chave válida, as rotas do cofre dão `503 FISCAL_DISABLED`; com `FISCAL_FEATURE_ENABLED=true`, o servidor nem sobe. Gerar: `python -c "import os,base64;print('v1:'+base64.b64encode(os.urandom(32)).decode())"`.
- **Rotação:** acrescente a chave nova em `FISCAL_VAULT_MASTER_KEY`, aponte `FISCAL_VAULT_ACTIVE_KEY_ID` para ela e faça o deploy (blobs novos já usam a nova; os antigos continuam legíveis). Depois rode `vault.rewrap_all(conn, vault.keyring())` (reembrulha só a DEK, em lotes com `SKIP LOCKED`, idempotente) e, quando não sobrar blob com o kid antigo (`SELECT key_id,count(*) FROM fiscal_vault_blobs WHERE destroyed_at IS NULL GROUP BY 1`), tire a chave antiga da env.
- **`POST /certificates`** (multipart `certificate` + `password`, até 64 KB):
  - Estrutura PFX conferida antes de abrir, para separar `CERTIFICATE_INVALID` de `CERTIFICATE_INVALID_PASSWORD`.
  - Exige RSA ≥ 2048 que corresponda ao certificado, folha que não seja AC nem autoassinada, política A1 da ICP-Brasil (`2.16.76.1.2.1.n`), `digitalSignature` quando houver KeyUsage e validade em curso (`CERTIFICATE_EXPIRED`).
  - Cadeia do PFX conferida por nome, assinatura, `ca=true` e validade de cada emissora. **Não** há âncora nas raízes da ICP-Brasil (não versionadas): quem confere a confiança é a SEFIN/SEFAZ na transmissão.
  - Titular: SAN otherName `2.16.76.1.3.3` (CNPJ do e-CNPJ) ou `2.16.76.1.3.1` (CPF do e-CPF); na falta, o sufixo `:<id>` do CN. Precisa ser o CNPJ do estabelecimento, ou o CPF do titular MEI em `fiscal_establishments.responsible_cpf` (preenchido pela F5). Sem titular legível, recusa (`CERTIFICATE_HOLDER_MISMATCH`).
  - Recifra a chave PKCS#8 + certificado + cadeia num blob `CERTIFICATE`. O PFX e a senha não são guardados; a senha é convertida em `bytearray` e zerada após a leitura (o `str` do formulário fica com o Python até o GC).
  - Mesmo fingerprint reenviado reativa a linha existente. Um certificado novo deixa o anterior `REPLACED`. Uploads do mesmo tenant são serializados por `FOR UPDATE` no estabelecimento.
  - Resposta e `GET /certificates/status` trazem `holderTaxId` (colunas novas `holder_tax_id`/`holder_kind`).
- **`DELETE /certificates/{id}`:** sem `X-Confirm-Revocation: REVOKE` dá `400 REVOCATION_CONFIRMATION_REQUIRED`. Revogar ou substituir **destrói** o blob (zera DEK embrulhada e cifra, `destroyed_at`). As versões antigas da linha somem do disco no vacuum do Postgres; a rotação da master key as torna ilegíveis de vez.
- **`PUT /csc`** (`FISCAL_CONFIG_WRITE`): `{cscId: 1–6 dígitos, não zero, cscToken: 16–36 alfanuméricos}` → 204, sem corpo. Vai para um blob `CSC` referenciado por `fiscal_credentials` (`NFCE_CSC`); o anterior é destruído. Erro de validação não ecoa o valor. `vault.get_csc()` devolve o par em memória (F11).
- **Assinatura e mTLS:** `vault.get_signing_credential(cur, tenant, ring)` devolve chave, certificado e cadeia em memória (só certificado `ACTIVE` e dentro da validade; o fingerprint do blob precisa bater com o cadastro). `vault.mtls_context(credential)` monta o `SSLContext` cliente (TLS ≥ 1.2, verificação do servidor ligada) a partir de um arquivo temporário 0600 (em `/dev/shm` quando existe), apagado logo após o `load_cert_chain`; cache por fingerprint por até 1 h ou até o vencimento, limpo na revogação/substituição.

Artefatos (XML/PDF) e o payload de `/documents` ainda usam o storage local Fernet (`FISCAL_LOCAL_VAULT_KEY`) até a F7, que os passa para o Postgres.

## Fila

`PostgresFiscalJobQueue` persiste jobs e dá base para lease, tentativas e DLQ. Não há worker transmissor ativo. Nenhum provider consegue gerar autorização falsa. Antes de ativar processamento, implemente claim com `FOR UPDATE SKIP LOCKED`, backoff+jitter, métricas, alertas e reconciliação conforme cada autorizador.

## Referências oficiais verificadas

- NFS-e, produção atualizada em 17/04/2026: XSD `NFSe-ESQUEMAS_XSD-v1.01-20260209`, leiaute v1.01 e domínios NBS/IBS/CBS v1.01 de 22/01/2026: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual/documentacao-atual
- APIs de Produção Restrita e Produção, atualizada em 29/12/2025: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/apis-prod-restrita-e-producao
- Implantações atualizadas em 01/07/2026: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/atualizacoes-e-implantacoes
- RTC atualizada em 15/07/2026: NT 009 e anexos 1.04.00/1.02.00 publicados, explicitamente ainda não disponíveis em Produção/Produção Restrita em agosto/2026: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/rtc
- NF-e/NFC-e: https://www.nfe.fazenda.gov.br/ — o portal entrou em loop de redirecionamento durante esta execução; MOC/schemas/NTs não foram presumidos.
- NFF/SVRS: o portal descreve emissão pelo App NFF e sistema centralizado; nenhuma API privada foi inferida: https://dfe-portal.svrs.rs.gov.br/Nff

## Contrato canônico e plano

A partir da F0 (2026-11-11) quem manda é o contrato do app: `Caltech-Mei/docs/fiscal-api-contract-v1.md`. Em divergência, este servidor se adapta ao app (exceto requisito de segurança). O plano vivo e o histórico ficam em `Caltech-Mei/app/src/main/java/com/caltechmei/Dialog/promptFiscalE2E.md` e `promptFiscalE2EExecutado.md`. As seções acima descrevem a base herdada da branch `cli` e serão revistas etapa a etapa.

## Execução

- Python 3.13 (`.python-version`). Dependências de runtime em `requirements.txt` (todas fixadas); desenvolvimento em `requirements-dev.txt`. `requirements-fiscal.txt`/`requirements-sefaz.txt` só repassam para o dev.
- Gate local: `scripts/dev_check.sh` — sobe `server-mei-pg-test` (Postgres 17 em `127.0.0.1:55432`, descartável) e roda `ruff check .` + `pytest -q` num container Python 3.13. Testes de banco só aceitam DSN local (`SERVER_MEI_TEST_DSN`).
- Migrations: `python migrate.py --dry-run` lista as pendentes sem escrever; `python migrate.py` aplica. Regras: arquivos `NNN_nome.sql`, nunca `*.down.sql`, sem `BEGIN/COMMIT` (cada arquivo roda numa transação do script), sem `DROP TABLE/COLUMN`, `TRUNCATE` ou `DELETE`, com `pg_advisory_lock` contra réplicas concorrentes.
- Antes de qualquer migration em produção: `pg_dump --format=custom --no-owner --file=backup-$(date +%Y%m%d-%H%M).dump "$DATABASE_URL"` (URL externa do Render, guardada fora do repo) e conferência do arquivo com `pg_restore --list`.
- `000_legacy_uuid_unique.sql` (F1): cria `empresa_uuid_key`/`usuariomei_uuid_key` (índice único em `uuid`), exigidos pelo `REFERENCES` da `001`. Antes de criar, confere o banco. Coluna ausente ou que não seja texto, ou `uuid` duplicado, faz a migration parar com erro claro, e a transação desfaz tudo. Se já existir índice único só em `uuid`, ela não faz nada. O usuário confirmou no Render, em 2026-11-11, que não há `uuid` duplicado.
- `002_auth_sessions_and_owner_permissions.sql` (F2): `auth_sessions.device_id_hash`, `auth_refresh_token_uses`, `auth_permission_defaults` e as permissões padrão do dono. Só `ADD COLUMN IF NOT EXISTS`, `CREATE … IF NOT EXISTS` e `INSERT … ON CONFLICT DO NOTHING`. Lê `usuariomei.datacadastro` e `ativo`.
- `005_fiscal_vault.sql` (F4): `fiscal_vault_blobs`, `fiscal_certificates.holder_tax_id`/`holder_kind` e `fiscal_establishments.responsible_cpf`. Só `CREATE … IF NOT EXISTS` e `ADD COLUMN IF NOT EXISTS`; não lê nem altera dados existentes.
- Não habilite produção sem homologação e aprovação registradas.

## Erros, correlação e logs (F1, 2026-11-11)

- **Instalação:** `fiscal.http.install(app)` (chamado no `main.py`) registra os handlers e o middleware de `/api/v1/**`. As rotas legadas continuam com o formato do FastAPI (`{"detail": …}`) e com o 500 em texto puro.
- **Envelope:** `{"error":{"code","message","retryable","correlationId","officialCode","officialMessage","fieldErrors"?,"reasons"?}}`, em `application/json`, para:
  - `ApiProblem(status, code, message, *, field_errors, reasons, retryable, official_code, official_message, headers)`: códigos da seção 2.1;
  - `HTTPException`: 404 `NOT_FOUND`, 405 `METHOD_NOT_ALLOWED` etc.;
  - `RequestValidationError`: 422 `VALIDATION_FAILED`, com `fieldErrors` só de caminho e tipo (nunca o valor recebido);
  - exceção genérica: 500 `INTERNAL_ERROR` com mensagem genérica. O traceback só vai para o log redigido.
- **Correlação:** `X-Correlation-Id` só é aceito se for UUID. Senão, o servidor gera outro. Ele volta sempre no header e no envelope e entra em todo log (`contextvars`). Headers fixos em `/api/v1`: `nosniff`, `DENY`, `no-referrer` e `no-store`.
- **JSONB:** sempre via `psycopg2.extras.Json` (o `_audit` gravava `dict` cru e dava 500). Em `fiscal/__init__.py` fica registrado o adaptador de `uuid.UUID` para parâmetro: sem ele, `POST /api/v1/auth/login` dava 500 (`can't adapt type 'UUID'`). A leitura continua devolvendo `str`.
- **Logs (`fiscal/logging.py`):**
  - JSON por linha no logger `fiscal` (`get_logger(__name__)`).
  - `RedactingFilter` no handler e nos loggers `fiscal.*`, `uvicorn.error` e `uvicorn.access`.
  - Redige mensagem, argumentos, campos extras e traceback: CPF, CNPJ (inclusive o alfanumérico), e-mail, XML, PEM, base64 longo, JWT, Bearer, tokens opacos e `senha=`/`password:`/`csc…`/`token…`.
  - UUID e SHA-256 hex continuam legíveis.
  - Não há mais `print` em `fiscal/`. No `sefaz_engine.py`, o corpo da resposta da SEFAZ e o C14N deixaram de ser impressos. Só vão status e tamanho.
  - Saíram do `main.py` os `print` de `EMAIL_USER`/`EMAIL_PASSWORD` e do payload da NFS-e legada.
- **Testes:**
  - `tests/test_fiscal_logging.py`: redação, pipeline configurado sem CNPJ completo e filtro do uvicorn.
  - `tests/test_fiscal_http.py`: envelope em 401/403/404/405/422/429/500, correlação, legado intacto e, no Postgres local, tenants A/B (404 sem revelar existência, auditoria em JSONB e upgrade de senha em texto puro para bcrypt).
  - O `TestClient` do Starlette 0.36 não roda com o `httpx` 0.28 fixado, por isso os testes usam `httpx.ASGITransport`.
