# Backend fiscal — arquitetura e operação segura

Estado: 2026-08-08. A integração governamental está deliberadamente desabilitada e não houve transmissão.

## Autenticação Android

`POST /api/v1/auth/login` recebe login e senha e devolve `accessToken`, `tokenType=Bearer`, `expiresIn=900`, refresh token opaco rotativo, validade do refresh, `sessionId`, usuário e empresa. O Android envia `Authorization: Bearer <accessToken>`. Ao expirar, chama `POST /api/v1/auth/refresh` uma única vez e substitui atomicamente ambos os tokens. Reutilizar um refresh antigo revoga a família da sessão. `POST /api/v1/auth/logout` revoga a sessão atual.

JWTs validam assinatura HS256, `kid`, issuer, audience, tipo, expiração, usuário, empresa e sessão persistida. `JWT_SIGNING_KEYS` aceita várias chaves `kid:segredo`; `JWT_ACTIVE_KID` escolhe a chave emissora, permitindo rotação. Segredos têm no mínimo 32 bytes. A empresa nunca é aceita de header/body nas APIs fiscais. `validation-uuid` continua temporariamente apenas no legado e é ignorado sob `/api/v1/fiscal`.

Permissões são grants em `auth_permissions`: `FISCAL_CONFIG_READ`, `FISCAL_CONFIG_WRITE`, `FISCAL_CERTIFICATE_MANAGE`, `FISCAL_DOCUMENT_CREATE`, `FISCAL_DOCUMENT_READ`, `FISCAL_DOCUMENT_CANCEL` e `FISCAL_PRODUCTION_ENABLE`. Nenhuma é concedida implicitamente pela migration.

## Contrato fiscal v1

O contrato normativo gerado está em `openapi.json`. Todos os endpoints ficam em `/api/v1/fiscal`. DTOs mantêm camelCase e enums do Android. Erros são `application/problem+json` com código estável e `correlationId`. Criação exige `Idempotency-Key`, retorna 202 somente quando persistida e enfileirada e usa JSON canônico + SHA-256. Produção é recusada independentemente do boolean do cliente.

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

## Execução

Instale com `python -m pip install -r requirements-fiscal.txt`, aplique `python migrate.py`, gere o contrato com `python generate_openapi.py`, valide com `python -m ruff check fiscal tests migrate.py generate_openapi.py` e teste com `python -m pytest -q`. Não execute migration sem backup e janela operacional. Não habilite produção sem homologação e aprovação registradas.
