# Backend fiscal — arquitetura e operação segura

Estado: 2026-08-08. A integração governamental está deliberadamente desabilitada e não houve transmissão.

## Autenticação Android

`POST /api/v1/auth/login` recebe login e senha e devolve `accessToken`, `tokenType=Bearer`, `expiresIn=900`, refresh token opaco rotativo, validade do refresh, `sessionId`, usuário e empresa. O Android envia `Authorization: Bearer <accessToken>`. Ao expirar, chama `POST /api/v1/auth/refresh` uma única vez e substitui atomicamente ambos os tokens. Reutilizar um refresh antigo revoga a família da sessão. `POST /api/v1/auth/logout` revoga a sessão atual.

JWTs validam assinatura HS256, `kid`, issuer, audience, tipo, expiração, usuário, empresa e sessão persistida. `JWT_SIGNING_KEYS` aceita várias chaves `kid:segredo`; `JWT_ACTIVE_KID` escolhe a chave emissora, permitindo rotação. Segredos têm no mínimo 32 bytes. A empresa nunca é aceita de header/body nas APIs fiscais. `validation-uuid` continua temporariamente apenas no legado e é ignorado sob `/api/v1/fiscal`.

Permissões são grants em `auth_permissions`: `FISCAL_CONFIG_READ`, `FISCAL_CONFIG_WRITE`, `FISCAL_CERTIFICATE_MANAGE`, `FISCAL_DOCUMENT_CREATE`, `FISCAL_DOCUMENT_READ`, `FISCAL_DOCUMENT_CANCEL` e `FISCAL_PRODUCTION_ENABLE`. Nenhuma é concedida implicitamente pela migration.

## Contrato fiscal v1

O contrato normativo gerado está em `openapi.json`. Todos os endpoints ficam em `/api/v1/fiscal`. DTOs mantêm camelCase e enums do Android. Erros seguem o envelope `{"error":{…}}` do contrato (seção 2), em `application/json` — ver “Erros, correlação e logs” abaixo. Criação exige `Idempotency-Key`, retorna 202 somente quando persistida e enfileirada e usa JSON canônico + SHA-256. Produção é recusada independentemente do boolean do cliente.

Queries incluem `tenant_id` da sessão. Recursos de outro tenant respondem como inexistentes. Jobs carregam tenant, documento e correlação. A constraint impede `AUTHORIZED` sem série, número, chave, protocolo, timestamp e artefato XML; o domínio também exige SHA-256 válido.

## Certificados, segredos e artefatos

O upload aceita multipart PKCS#12 até o limite configurado, valida conteúdo, senha, chave privada, período, fingerprint e CNPJ do titular quando presente. Somente metadata e referência do cofre vão ao banco. O arquivo é cifrado com Fernet e permissão restrita; senha nunca é persistida. A implementação local exige disco persistente e chave entregue pelo secret manager. Para implantação distribuída, substitua pela interface `CertificateVault` usando KMS/HSM e object storage privado.

CSC e ID CSC possuem estrutura para referências de cofre, mas não têm endpoint ativo nesta entrega; NFC-e permanece indisponível. O storage local cifrado é imutável e particionado pelo hash do tenant/documento. URL assinada e downloads ficam indisponíveis até object storage privado ser configurado.

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
- Bloqueio conhecido: a `001` referencia `empresa(uuid)` e `usuariomei(uuid)`, que no DDL legado do `main.py` não têm `UNIQUE`; a correção aditiva é da F1 e exige inspecionar o schema real (somente leitura) antes.
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
