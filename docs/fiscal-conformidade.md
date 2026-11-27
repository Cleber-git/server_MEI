# Conformidade com `PRD context.txt`

Consulta do PRD e fontes oficiais: 2026-11-10. Este documento registra a implementação parcial deste ciclo; não declara a API pronta para emissão fiscal.

## Itens do contrato e evidências

| Item do PRD | Evidência no servidor | Situação / desvio |
|---|---|---|
| Bearer obrigatório e tenant derivado da sessão | `fiscal/auth.py`, middleware em `main.py`, testes `tests/test_fiscal_contract.py` | Implementado; `validation-uuid` não autoriza rotas fiscais. |
| Login, refresh e logout | `POST /api/v1/auth/login`, `/refresh`, `/logout`; `docs/FISCAL_BACKEND.md` | Implementado e mantido. Contrato existente: `accessToken`, `tokenType`, `expiresIn`, `refreshToken`, `refreshExpiresIn`, `sessionId`. Confirmar com o responsável pelo app se deve continuar antes de alterar qualquer resposta. |
| Erros fiscais com `code`, `message`, `correlationId` | `fiscal/errors.py` | Implementado para problemas de domínio e validação de request no namespace fiscal. |
| `GET /capabilities` fechado por padrão | `fiscal/api.py`, `fiscal/schemas.py` | Capacidades de emissão permanecem `false`; versão do conjunto de regras é `server-mei-fiscal-capabilities-v1`. Tabela versionada criada em `migrations/005_fiscal_contract_alignment.sql`, sem regras oficiais ativadas. `capabilityId` e verificação de versão na criação aguardam confirmação do responsável pelo app. |
| Upload A1 limitado a 64 KB; senha não persistida | `fiscal/api.py`, `fiscal/vault.py` | Parte do arquivo chama-se `certificate`; limite máximo de 64 KB aplicado. A senha é removida das variáveis após processamento. A implementação atual não comprova cadeia ICP-Brasil nem revogação OCSP/CRL e, portanto, não atende integralmente ao critério de `valid=true`. |
| Comparação do titular com o CNPJ do tenant | `fiscal/api.py`, `fiscal/vault.py`, migration 005 | Lê OID `2.16.76.1.3.3` no SubjectAltName (otherName), valida dígito alfanumérico e recusa CNPJ ausente/divergente; `holderTaxId` entra na resposta e na tabela de metadados. Testes com PFX ICP-Brasil reais ainda pendentes. |
| Idempotência e unicidade | `fiscal/api.py`, `migrations/001_fiscal_foundation.sql` | Chave + payload iguais devolvem o mesmo documento; conflito usa `IDEMPOTENCY_CONFLICT`. Advisory lock serializa chave ainda não existente. Repetição retorna HTTP 200. |
| Histórico cursor/limit, tenant e datas | `GET /documents` em `fiscal/api.py` | Cursor opaco baseado em `createdAt` e `id`; filtros `type`, `status`, `from`, `to`, `limit`; tenant vem do token. |
| `AUTHORIZED` só com evidência | `fiscal/domain.py`, migration 001 | Gating de domínio e banco requer campos oficiais e artefato XML. Não há adaptador que possa produzir autorização nesta versão. |
| Persistência, auditoria e fila | `migrations/001_fiscal_foundation.sql`, `fiscal/queue.py`, `fiscal/api.py` | Base criada; worker transmissor e storage de artefatos oficiais não estão ativos. Downloads retornam indisponível. |
| NFS-e Nacional / NF-e / NFC-e | `fiscal/interfaces.py`, `fiscal/api.py` | **Bloqueado**: provider de produção restrita/homologação não implementado. Todas as capacidades ficam desligadas; nenhuma autorização é simulada. |
| OpenAPI 3 | `generate_openapi.py`, `openapi.json` | Regenerar após a confirmação pendente e completar os schemas do PRD. O schema de payload legado ainda não corresponde ao novo formato 1.2/2.3. |

## Fontes oficiais consultadas

Consulta em 2026-11-10:

- [Documentação Atual da NFS-e (produção)](https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual): XSD v1.01 de 2026-02-09; anexos de leiaute v1.01 de 2026-02-09/2026-01-22.
- [Orientações oficiais sobre leiaute em produção e NT 009](https://www.gov.br/nfse/pt-br/noticias/nfs-e-nacional-confira-oas-rientacoes-sobre-leiaute-em-producao-regras-de-preenchimento-de-ibs-cbs-e-status-da-nota-tecnica-009/): produção segue NT 004 e domínio NT 007; produção restrita está com NT 009 v1.0 release 1. A entrada prevista em produção é 2026-12-01, portanto não foi tratada como vigente em produção nesta data.
- [Comunicado da NT 009 release 1 em ambiente de testes](https://www.gov.br/nfse/pt-br/noticias/cgnfs-e-informa-disponibilizadas-no-ambiente-de-testes-o-release-1-das-evolucoes-da-nota-tecnica-009/): especifica diferenças do ambiente restrito e reforça que a atualização de produção ainda é futura.
- [Portal Nacional da NF-e](https://www.nfe.fazenda.gov.br/): busca oficial indica pacote de esquemas 010c (NT 2022.002 v1.30), publicado em 2026-03-26. A página não abriu nesta consulta devido a loop de redirecionamento. Nenhum XSD, URL estadual ou regra NF-e foi copiado ou inferido.

## Desvios e bloqueios

1. A confirmar: manter o fluxo atual de login da API Android ou especificar outro contrato.
2. A confirmar: adicionar `capabilityId` e validação de `capabilityVersion`.
3. O corpo de criação atual usa schema legado (versão `1`) e difere dos formatos do PRD (`1.2` e `2.3`).
4. Falta validação criptográfica completa do certificado (OID SubjectAltName, cadeia ICP-Brasil e situação de revogação) antes de declarar `valid=true`.
5. Falta obter e validar tabelas fiscais oficiais vigentes (serviços, NBS, NCM/GTIN/CEST/CFOP/CSOSN e parâmetros municipais/UF).
6. Faltam adaptadores separados SEFIN Nacional e SEFAZ, worker, reconciliação após timeout, assinatura XMLDSig/mTLS e download de artefatos oficiais.
7. Não existe autorização do responsável para ativar produção; `productionEnabled` permanece `false`.
8. Pendências por tenant: certificado A1 válido; credenciamento nacional e estadual quando aplicável; CSC/ID CSC para NFC-e; aprovação da bateria de homologação. Nenhuma dessas dependências é simulada.

## Produção

Produção continua desligada por padrão. Os adaptadores governamentais não estão ativos; a implementação presente não pode emitir notas e não pode devolver `AUTHORIZED` por resposta HTTP isolada.
