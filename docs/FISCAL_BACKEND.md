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

Desde a F7, o payload de `/documents` (`DOCUMENT_PAYLOAD`) e os artefatos XML/PDF (`ARTIFACT`) também ficam neste cofre. O storage local Fernet (`FISCAL_LOCAL_VAULT_KEY`) foi removido.

## Estabelecimento, config e capabilities (F5, 2026-11-11)

Contrato: seções 4.1, 4.2, 5 e 6 de `Caltech-Mei/docs/fiscal-api-contract-v1.md`. Código em `fiscal/establishment.py`, `fiscal/capabilities.py` e `fiscal/api.py`; testes em `tests/test_capabilities_f5.py`.

- **`GET/PUT /establishment`** (`FISCAL_CONFIG_READ`/`WRITE`):
  - O CNPJ vem sempre de `empresa.cnpj` do tenant do token. Se o corpo trouxer `cnpj`, precisa ser o mesmo (com ou sem máscara); outro valor dá `422 VALIDATION_FAILED` (`cnpj`/`READ_ONLY`), sem eco.
  - Valida UF, `cityCode` com o prefixo IBGE da UF, CEP de 8 dígitos, IE só com dígitos (ISENTO não vale para quem emite), CPF do titular com DV e `taxRegime = MEI`. Campo desconhecido dá 422.
  - `missingFields` lista o que falta para `NFSE`, `NFCE_65` e `NFE_55` (base: CNPJ válido, razão social, UF, município IBGE e endereço; produto também exige IE). `fiscalEnabled` liga sozinho quando a NFS-e está completa e desliga se o cadastro voltar a ficar incompleto.
  - Sem estabelecimento, o GET mostra o que se sabe da empresa e o PUT cria a linha (mesmo id determinístico da `001`) e a `fiscal_configurations`.
  - Auditado (`ESTABLISHMENT_UPDATE`) só com `fiscalEnabled` e a contagem do que falta, sem PII.
- **`GET/PUT /config`:** `productionEnabled` é calculado (env `FISCAL_PRODUCTION_ENABLED` + flags do estabelecimento e da configuração + homologação, monitoramento e checklist aprovados) e é somente leitura: enviar outro valor dá `422` (`READ_ONLY`). Pedir ambiente `PRODUCTION` sem isso dá `403 PRODUCTION_NOT_ENABLED`. `provider` só aceita `SEFIN_NACIONAL | SEFAZ_NFCE | SEFAZ_NFE | null`. Sem estabelecimento: `422 ESTABLISHMENT_INCOMPLETE` (antes `FISCAL_CONFIGURATION_INCOMPLETE`, fora do contrato).
- **`fiscal_capabilities` (migration `006`):** uma linha imutável por UF × operação (`NFSE`, `NFCE_65`, `NFE_55`, `OFFICIAL_NFF`, `STATE_NFAE`) × ambiente × `effective_from`. Vale a mais nova já em vigor; mudar um dado = inserir linha nova. Linha `available`/`accredited` sem `source_url` e `verified_at` é recusada por CHECK, e `source_url`/`official_channel_url` só aceitam `https` em `*.gov.br`, sem credenciais nem porta (CHECK no banco + `is_safe_gov_url`, a mesma régua do app).
  - **Seed `2026-11-11.1`:** tudo indisponível nas 27 UFs. Exceção: NFF (canal oficial; o servidor não emite) em RS, SC e PI, conferido no portal oficial em 2026-11-11 (`https://dfe-portal.svrs.rs.gov.br/Nff/Noticias/2916`: módulo MEI em RS/SC desde 23/10/2023 e no PI desde 21/03/2024). Nenhuma URL de emissor estadual foi semeada (não verificada).
  - **Emissão automática** (`nfseAutomatic`/`nfce`/`nfe`) só com linha disponível, credenciada, com fonte e dentro da validade **e** homologação `APPROVED` do tenant na UF em `fiscal_homologation_checks` (`model` = operação) **e** `FISCAL_FEATURE_ENABLED=true`.
- **`GET /capabilities?uf=`:** formato exato do app, sempre com `nfseUnidentifiedRecipientAllowed` e `stateOfficialChannelUrl` (podem ser null). UF diferente da do cadastro, ou cadastro incompleto, dá tudo falso com o motivo. Sem canal automático ligado, `requiresCertificate` é sempre `true`.
- **`POST /capabilities/evaluate`:** fatos de serviço (`{documentType: "NFSE", service}`) ou de produto (`{operation, recipient, logistics}`), espelhando o `ProductDocumentRoutingPolicy` do app e falhando para o lado bloqueado. A UF que vale é a do cadastro (a do corpo diferente ⇒ revisão fiscal). Ordem no produto: finalidade ≠ venda, estrangeiro ou ICMS desconhecido ⇒ revisão; NFF disponível ⇒ `OFFICIAL_NFF`; NFC-e só em venda interna, presencial, consumidor final PF não contribuinte e sem entrega; NF-e exige endereço completo do destinatário; depois cadastro, certificado e CSC. Decisão positiva vale 1 h (`validUntil`). `productionEligible` só em `PRODUCTION` com o gate do servidor.
- **`GET /catalogs/{type}/metadata`** (`fiscal_catalog_metadata`, fonte só `*.gov.br`, SHA-256): nenhum catálogo foi carregado, então todos (e tipo desconhecido) respondem `404 NOT_FOUND`.

## Motor de documentos (F7, 2026-11-11)

Contrato: seção 4.4 de `Caltech-Mei/docs/fiscal-api-contract-v1.md`. Código em `fiscal/documents.py` (persistência, estados, eventos, artefatos), `fiscal/worker.py` (worker), `fiscal/providers.py` (providers) e `fiscal/api.py`; testes em `tests/test_documents_f7.py`.

- **`POST /documents`** (`FISCAL_DOCUMENT_CREATE`, `Idempotency-Key` = UUID ou 16–128 `[A-Za-z0-9_-]`):
  - Corpo igual ao `CreateFiscalDocumentRequest` do app: `1.2` (NFS-e: `service`, `items=[]`, `payments=[]`), `1.1` (produto legado: parseia e responde `422 FISCAL_OPERATION_INCOMPLETE` com `PRODUCT_INTENT_V2_REQUIRED`) e `2.3` (produto roteado: `product` com a mesma `totals` do topo). Outra versão, campo desconhecido ou corpo incoerente com a versão dá `422 VALIDATION_FAILED`. Campos nulos podem vir ausentes (o Gson do app omite).
  - Ordem: estabelecimento → repetição idempotente (mesma chave + mesmo hash devolve o mesmo documento; hash diferente dá `409 IDEMPOTENCY_KEY_REUSED`) → `OFFICIAL_NFF` (`422 CAPABILITY_UNAVAILABLE`) → fiscal ligado (`503 FISCAL_DISABLED`) → ambiente (`403 PRODUCTION_NOT_ENABLED`, ou `422` se diferente do configurado) → totais recalculados (`422 VALIDATION_FAILED`, `TOTAL_MISMATCH`) → a mesma decisão do `/capabilities/evaluate` (revisão ⇒ `FISCAL_OPERATION_INCOMPLETE`; cadastro ⇒ `ESTABLISHMENT_INCOMPLETE`; resto ⇒ `CAPABILITY_UNAVAILABLE` com os motivos). No produto, o `route.capabilityId` precisa ser o da linha que o servidor usaria.
  - **Certificado só conta se for do titular do cadastro** (CNPJ do estabelecimento ou CPF do titular MEI). Isso vale também para o `/evaluate`: depois de trocar o CNPJ da empresa, um certificado antigo não libera a emissão.
  - `saleId` + tipo + hash já com documento ativo (fora `REJECTED`/`ERROR_FINAL`) dá `409 DUPLICATE_FISCAL_INTENT` com o `id` existente em `reasons` (índice único parcial, então a corrida também é barrada).
  - Uma transação: idempotência por `INSERT … ON CONFLICT DO NOTHING` (a requisição concorrente espera o commit da outra e devolve o mesmo documento; antes dava 500), reserva do número (`UPDATE fiscal_series … RETURNING`, só depois de ganhar a chave), payload cifrado no cofre, documento, eventos `CREATED`/`QUEUED`, job `SUBMIT` (outbox) e auditoria. Responde `202` com o documento em `QUEUED`.
- **`FiscalDocumentDto`:** `number` vai como **texto** (é `String` no app). Evidência (`xmlSha256`, `xmlArtifactId`, `auxiliaryArtifactId`) só aparece em `AUTHORIZED`/`CANCEL_PENDING`/`CANCELLED`. Aditivos: `retryAfter` (segundos até a próxima tentativa agendada) e `etag` (versão).
- **Estados:** `TRANSITIONS` é cópia exata de `FiscalRules.transitions` do `shared` (antes o servidor tinha 3 transições para `ERROR_FINAL` que o app não aceita). Toda mudança passa por `documents.transition`: CAS em `status` + `version`, evento gravado na mesma transação.
- **Worker in-process** (`fiscal/worker.py`, sobe no startup com `FISCAL_FEATURE_ENABLED=true` e `FISCAL_WORKER_ENABLED`≠`false`; uma thread por processo, seguro com várias réplicas):
  - Claim com `FOR UPDATE SKIP LOCKED` e lease de 120 s; lease vencido (processo morto) volta a ser reivindicável. Heartbeat antes de cada chamada ao autorizador; se outro worker assumiu, este não mexe em nada.
  - `SUBMITTED` + `outcome_uncertain=TRUE` é gravado **antes** de chamar o autorizador. Com o envio incerto (timeout, exceção, processo reiniciado, `PROCESSING`), a próxima execução **consulta**; só reenvia se a consulta disser que nada chegou. Retry nunca gera número novo.
  - Desfechos: autorizado com toda a evidência (chave, protocolo, data, XML, série e número) ⇒ `AUTHORIZED`, XML/PDF cifrados no cofre com SHA-256; evidência incompleta ⇒ `PROCESSING` e nova consulta (nunca autoriza pela metade); rejeição ⇒ `REJECTED` com o código oficial em `fiscalErrorCode` e a mensagem sanitizada; erro temporário (comprovadamente não chegou) ⇒ `ERROR_RETRYABLE` e reenvio automático; nada enviado (provider indisponível) ⇒ `ERROR_RETRYABLE` sem retry automático.
  - Backoff exponencial com jitter (entre metade e o teto de `5·2^(n-1)` s, máximo 600 s, ou o `Retry-After` do autorizador) só em timeout, erro temporário e processando. Esgotadas as `FISCAL_JOB_MAX_ATTEMPTS`, o job fica `DEAD` e o documento `ERROR_RETRYABLE` (`AUTHORIZATION_PENDING` ou `UPSTREAM_UNAVAILABLE`).
  - Exceção inesperada do provider conta como timeout (pode ter chegado).
- **Comandos** (todos com `Idempotency-Key`; outro tenant = `404`; com o fiscal desligado, `503` depois do 404):
  - `cancel` (`FISCAL_DOCUMENT_CANCEL`): só `AUTHORIZED` e sem substituição em andamento ⇒ `CANCEL_PENDING`. O motivo vai cifrado no cofre. Job `CANCEL`: cancelado ⇒ `CANCELLED` (XML do evento guardado); recusado ⇒ volta a `AUTHORIZED` com o código; incerto ⇒ consulta antes de reenviar.
  - `retry` (`FISCAL_DOCUMENT_CREATE`): só `ERROR_RETRYABLE` ⇒ `QUEUED`, job zerado; se o último envio ficou incerto, o worker consulta primeiro.
  - `substitute` (`FISCAL_DOCUMENT_CANCEL`): `{reason, replacement}` só para NFS-e `AUTHORIZED` e provider com `supports_substitution` (senão `422 CAPABILITY_UNAVAILABLE`, `SUBSTITUTION_UNAVAILABLE`). O substituto passa pelo mesmo `POST` (novo número) e devolve `202` com ele; quando ele é autorizado, o original vai a `CANCEL_PENDING` → `CANCELLED` (evento `SUBSTITUTED`). Uma substituição ativa por documento (índice único).
- **Leitura** (`FISCAL_DOCUMENT_READ`): `GET /documents?limit&cursor&documentType&status&from&to` com cursor opaco (`created_at`+`id`, base64url), `limit` ≤ 100 e `nextCursor` (substitui `page/size`); `GET /documents/{id}/events` (`{items:[{id,type,fromStatus,toStatus,occurredAt,correlationId,officialCode,message}]}`, textos fixos, sem PII); `/xml` e `/auxiliary-document` só com evidência, decifrados do cofre com o SHA-256 conferido (diferente ⇒ 500 genérico), `Cache-Control: no-store`, `nosniff`, `Content-Disposition: attachment` e auditoria `FISCAL_ARTIFACT_DOWNLOAD`.
- **Providers (`fiscal/providers.py`):** `FISCAL_PROVIDER=unavailable` (padrão) nunca transmite: o documento fica `ERROR_RETRYABLE` (`PROVIDER_UNAVAILABLE`). `FISCAL_PROVIDER=fake` é o autorizador em memória para backend local (F9) e testes: recusa `PRODUCTION`, e o servidor não sobe com ele e `FISCAL_PRODUCTION_ENABLED=true`. Os providers reais entram na F8 (NFS-e), F11 (NFC-e) e F13 (NF-e) com o mesmo protocolo (`submit`/`query`/`cancel` → `Outcome`).
- **Banco (`007_fiscal_document_engine.sql`):** finalidades `DOCUMENT_PAYLOAD`/`ARTIFACT` no cofre; colunas novas em `fiscal_documents` (payload, motivo do cancelamento, `xml_sha256`, `outcome_uncertain`, `official_code`, datas, `substitutes_document_id`); CHECK `NOT VALID` de evidência completa em `AUTHORIZED`/`CANCEL_PENDING`/`CANCELLED` (inclui `xml_sha256` com 64 hex) e de status conhecido; índices únicos parciais da intenção por venda e da substituição; `fiscal_artifacts` imutável por trigger; colunas de evento e índices de job.
- **`db.py`:** o pool passou a `ThreadedConnectionPool` (o `SimpleConnectionPool` não é seguro entre threads, e agora há a do worker além das do FastAPI).

## NFS-e pela SEFIN Nacional (F8, 2026-11-12)

Código em `fiscal/nfse.py` (DPS, XSD, assinatura, GZip+Base64 e leitura da NFS-e), `fiscal/sefin.py` (`SefinNacionalProvider`) e `fiscal/danfse.py` (DANFSe). Os testes ficam em `tests/test_nfse_f8.py`, com a SEFIN falsa em `tests/sefin_fake.py` e as fixtures em `tests/fixtures/nfse/`. Nenhum teste usa rede.

- **Ligar:** `FISCAL_PROVIDER=sefin` usa a SEFIN para `NFSE`. Os outros tipos continuam `unavailable`.
  - Ambiente **travado em produção restrita**: o módulo só conhece `https://sefin.producaorestrita.nfse.gov.br/API/SefinNacional`, e documento em `PRODUCTION` vira `NOT_SENT` (`PRODUCTION_LOCKED`).
  - Envs opcionais: `FISCAL_NFSE_TIMEOUT_SECONDS` (padrão 30, limitado a 5–60, abaixo do lease de 120 s) e `FISCAL_NFSE_CA_FILE` (CA para validar o servidor da SEFIN; padrão é o repositório do sistema). Se o arquivo não existir, o servidor não sobe.
- **Leiaute:** XSD oficial `1.01` (pacote de 09/02/2026) versionado byte a byte em `fiscal/xsd/nfse/`, com fonte, data e SHA-256 em `SOURCE.md` e `SHA256SUMS` (`.gitattributes` tira esses arquivos do `diff --check`).
  - O XSD usa `^…$` em `TSSerieDPS`. Em regex de XML Schema esses sinais são literais e o libxml2 recusaria toda série. A correção é feita só em memória, por um resolver local que também impede `include`/`import` fora da pasta (nada vem da rede).
  - A NT 009 v1.01 (CNPJ alfanumérico, IBS/CBS no Simples) ainda não está em produção. CNPJ alfanumérico do prestador dá `ISSUER_CNPJ_UNSUPPORTED`, e o do tomador dá `RECIPIENT_UNSUPPORTED`.
- **DPS do MEI** (regras do Anexo I v1.01 conferidas):
  - `tpEmit=1`, `opSimpNac=2`, `regEspTrib=0` e `tpRetISSQN=1`. MEI não tem retenção de ISSQN, então `withholdingResponsibility` dá `MEI_WITHHOLDING_NOT_ALLOWED`.
  - `tribISSQN=1` e `totTrib/indTotTrib=0`.
  - Prestador só com `CNPJ` (+ `IM` se cadastrada) e `regTrib`, **sem** `xNome`/`end` (E0121/E0128).
  - Tomador só com CPF/CNPJ válido + nome (e e-mail, se houver). Sem `taxId`, não há grupo `toma`.
  - `cLocPrestacao` = `incidenceMunicipalityCode` ou o município do cadastro.
  - Id = `DPS` + município + `2` + CNPJ + série (5) + nº (15). A série `1` do motor (`00001`) está na faixa "aplicativo próprio" (00001–49999), o que fecha a pendência da série provisória da F7.
  - `dhEmi` em `-03:00`. `dCompet` = data da criação (nunca depois da emissão).
  - Texto limpo para o `TSString` (Latin-1 imprimível; emoji e símbolos saem).
- **Assinatura:** XMLDSig envelopada (RSA-SHA256, digest SHA-256, C14N 1.0) com o `xmlsec` sobre o XML reparseado. A `Signature` fica **sem prefixo** de namespace, porque o Anexo I recusa prefixo na área de dados.
  - O `signxml` não gera assinatura válida sem prefixo.
  - O C14N do lxml numa árvore montada em memória gera `xmlns=""` espúrio. Por isso a assinatura é feita sobre o XML reparseado.
- **Antes de assinar, o worker carrega o cadastro e a credencial do cofre** e confere o titular: e-CNPJ igual ao CNPJ do cadastro, ou e-CPF igual ao `responsibleCpf` do MEI.
  - Certificado de outro titular, vencido ou ausente vira `NOT_SENT` com `CERTIFICATE_HOLDER_MISMATCH`/`CERTIFICATE_EXPIRED`/`CERTIFICATE_INVALID`, e nada sai para a rede.
  - Isso resolve a pendência da F7 (troca de CNPJ ou certificado depois da criação).
- **Os 9 passos** (detalhe na docstring de `fiscal/sefin.py`):
  - `GET /parametros_municipais/{cMun}/convenio`, em cache de 10 min. **Nunca bloqueia**: para o MEI, a emissão vale mesmo sem convênio.
  - DPS → XSD → assinatura → XSD → GZip+Base64 em `{"dpsXmlGZipB64": …}` → `POST /nfse` por mTLS (`vault.mtls_context`).
- **Desfechos:**
  - `200/201` com `nfseXmlGZipB64` ⇒ a NFS-e é conferida (XSD da versão dela, Id da DPS, CNPJ do prestador e ambiente) e o desfecho é `AUTHORIZED`, com `officialKey` = chave de 50 dígitos, `protocol` = `nNFSe`, `authorizedAt` = `dhProc`, XML e DANFSe. NFS-e que não confere ⇒ `PROCESSING` (nova consulta, nunca autoriza).
  - `400/422`:
    - Com `E0014` (DPS já usada) ⇒ consulta `GET /dps/{id}` → `GET /nfse/{chave}`.
    - Outro erro ⇒ `REJECTED` com `Codigo`/`Descricao`. O `Complemento` nunca é guardado (pode ter dado do contribuinte).
  - Falha de conexão ⇒ `TEMPORARY`.
  - Timeout de leitura, `429` e `5xx` ⇒ `TIMEOUT`: o worker consulta antes de reenviar.
  - `401/403` ⇒ `NOT_SENT` (`UPSTREAM_AUTH_REJECTED`).
- **Consulta:**
  - Sem chave: `GET /dps/{id}` (`404` ⇒ `NOT_FOUND`, o único caso em que o worker reenvia).
  - Com chave (cancelamento incerto): `GET /nfse/{chave}/eventos/101101`.
  - Sem credencial, a consulta nunca diz "não chegou" (`TIMEOUT`).
- **Cancelamento:** `pedRegEvento` 1.01, Id `PRE` + chave + `101101`, `cMotivo=9` + `xMotivo` = motivo do app, assinado. Vai em `POST /nfse/{chave}/eventos` (`pedidoRegistroEventoXmlGZipB64`). Recusa (`400`) confere antes se o evento já existe (evento repetido ⇒ `CANCELLED`). O XML do evento fica guardado.
- **Substituição:** o substituto leva o grupo `subst` (`chSubstda`, `cMotivo=99`, motivo). A SEFIN cancela a original sozinha (evento 105102), e o worker fecha a original como na F7.
- **DANFSe:** a API do ADN foi **suspensa em 03/08/2026** (NT 008 v1.02). O PDF é gerado no servidor a partir do XML autorizado, pela NT 008:
  - Uma página A4, margens de 0,2 cm, bordas de 1/0,5 pt e cinza a 5% nos títulos.
  - QR para `https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave=`.
  - "NFS-e SEM VALIDADE JURÍDICA" em vermelho em produção restrita.
  - Blocos de tomador, destinatário e intermediário com as frases de supressão.
  - Desvios: fonte Helvetica (métrica da Arial; a Microsoft Sans Serif não é livre) e grade sem as coordenadas exatas de cada campo do Anexo I da NT.
  - Falha no PDF não impede a autorização: o documento fica sem `auxiliaryArtifactId`.
- **Códigos:** `sanitize_official` passou a guardar até 40 caracteres (antes 20), para caberem os códigos do próprio servidor (`CERTIFICATE_HOLDER_MISMATCH`…). Os códigos oficiais da SEFIN têm 5.
- **Dependências novas:** `reportlab==4.2.5`, `pillow==11.0.0` e `chardet==5.2.0`, todas gratuitas. O `requirements.txt` perdeu o BOM UTF-8 que ainda tinha.
- **Não conferido:**
  - Os nomes dos campos JSON da SEFIN (`dpsXmlGZipB64`, `nfseXmlGZipB64`, `chaveAcesso`, `erros[].Codigo/Descricao`, `eventoXmlGZipB64`, `pedidoRegistroEventoXmlGZipB64`). O Swagger oficial só abre com certificado (403/TLS). A leitura ignora maiúsculas.
  - O algoritmo RSA-SHA256, o CA do servidor da SEFIN e a `IM` (E0116/E0120 dependem do CNC do município).
  - Tudo isso fica para a F10.

## Backend local para o app debug (F9, 2026-11-12)

- **`scripts/dev_local_server.sh`:** recria o banco `server_mei_local` no mesmo container do gate (`server-mei-pg-test`, `127.0.0.1:55432`), semeia com `scripts/dev_seed_local.py` e serve o FastAPI em `127.0.0.1:8000` com `FISCAL_PROVIDER=fake` e o worker ligado. As chaves JWT e do cofre são geradas a cada execução e não são gravadas. Precisa do venv criado por `scripts/dev_check.sh`.
- **Seed:** legado mínimo dos testes (`create_legacy`; o `main.create_tables()` engole os próprios erros e não serve aqui), migrations, tenant sintético `tenant-a` (login `tenant-a@example.com` / `senha-tenant-a`, só local), `PUT /establishment` completo, capability NFS-e de SP credenciada, homologação `APPROVED` e um certificado sintético (o `FakeProvider` não assina). O script recusa rodar fora de `DB_HOST=127.0.0.1` + `DB_NAME=server_mei_local`.
- **No app:** `fiscal.debugApiBaseUrl=http://10.0.2.2:8000/` e `fiscal.debugRestrictedProduction=true` no `local.properties` (só debug). O smoke instrumentado `FiscalLocalBackendSmokeTest` roda com `-Pandroid.testInstrumentationRunnerArguments.fiscalLocalBackend=http://10.0.2.2:8000/`.

## Referências oficiais verificadas

- NFS-e (conferido em 2026-11-12, F8): "Documentação Atual (Produção)" atualizada em 04/11/2026 com o XSD `nfse-esquemas_xsd-v1-01-20260209.zip` (SHA-256 em `fiscal/xsd/nfse/SOURCE.md`), Anexo I v1.01 (09/02/2026), Anexo II v1.01 (22/01/2026) e manual de APIs do contribuinte v1.2 (out/2025). NT 008 v1.02 (14/07/2026): DANFSe gerado pelo emissor, API do ADN suspensa em 03/08/2026. NT 009 v1.01 (11/09/2026): ainda sem cronograma de produção. RTC: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/rtc
- NFS-e, produção atualizada em 17/04/2026: XSD `NFSe-ESQUEMAS_XSD-v1.01-20260209`, leiaute v1.01 e domínios NBS/IBS/CBS v1.01 de 22/01/2026: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual/documentacao-atual
- APIs de Produção Restrita e Produção, atualizada em 29/12/2025: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/apis-prod-restrita-e-producao
- Implantações atualizadas em 01/07/2026: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/atualizacoes-e-implantacoes
- RTC atualizada em 15/07/2026: NT 009 e anexos 1.04.00/1.02.00 publicados, explicitamente ainda não disponíveis em Produção/Produção Restrita em agosto/2026: https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/rtc
- NF-e/NFC-e: https://www.nfe.fazenda.gov.br/ — o portal entrou em loop de redirecionamento durante esta execução; MOC/schemas/NTs não foram presumidos.
- NFF/SVRS: o portal descreve emissão pelo App NFF e sistema centralizado; nenhuma API privada foi inferida: https://dfe-portal.svrs.rs.gov.br/Nff
- NFF módulo MEI (conferido em 2026-11-11): RS e SC desde 23/10/2023, PI desde 21/03/2024; a página não traz lista consolidada de UFs: https://dfe-portal.svrs.rs.gov.br/Nff/Noticias/2916

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
- `006_fiscal_capabilities.sql` (F5): `fiscal_establishments.address_city`, `fiscal_capabilities` com o seed conservador e `fiscal_catalog_metadata` (vazia). Só `ADD COLUMN IF NOT EXISTS`, `CREATE … IF NOT EXISTS` e `INSERT … ON CONFLICT DO NOTHING`; não lê nem altera dados existentes.
- `007_fiscal_document_engine.sql` (F7): motor de documentos (ver a seção acima). Só `ADD COLUMN IF NOT EXISTS`, `CREATE … IF NOT EXISTS`, CHECKs `NOT VALID` e o padrão `DROP CONSTRAINT/TRIGGER IF EXISTS` + `ADD`. Não lê nem altera linhas existentes; os índices únicos novos conferem duplicata antes e, se houver, a migration para com erro claro.
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
