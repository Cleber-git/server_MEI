# XSDs oficiais da NFS-e nacional

Cópia byte a byte (CRLF e BOM preservados) do pacote oficial. **Não editar**: para atualizar,
baixar o pacote novo, substituir as pastas, regenerar `SHA256SUMS` e registrar aqui.

| Campo | Valor |
|---|---|
| Pacote | `nfse-esquemas_xsd-v1-01-20260209.zip` ("NFSe-ESQUEMAS_XSD-v1.01-20260209") |
| Fonte | https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual/nfse-esquemas_xsd-v1-01-20260209.zip |
| Página | https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual ("Documentação Atual (Produção)", atualizada em 04/11/2026) |
| Baixado em | 2026-11-12 |
| SHA-256 do zip | `e7935cbd9470527c6cc32984c1b2263e614183bf0139ce2733eaaed2de9a8072` |
| Versão usada para montar a DPS | `1.01` (`DPS_v1.01.xsd`, `pedRegEvento_v1.01.xsd`) |
| Versões aceitas na leitura da NFS-e | `1.00` e `1.01` (pelo atributo `versao`) |

Leiaute correspondente: `anexo_i-sefin_adn-dps_nfse-snnfse-v1-01-20260209.xlsx` (regras de negócio da
DPS, SHA-256 `de5bc492959eadc8bfa7540e16939995924f2188f743648eaf84d3b31e9eeb7c`) e
`anexo_ii-sefin_adn-pedregevt_evt-snnfse-v1-01-20260122.xlsx` (eventos), na mesma página.

As NT 009 v1.01 (11/09/2026: CNPJ alfanumérico, ajustes de IBS/CBS e do Simples Nacional) e o
AnexoVI v1.04.01 ainda **não** estão em produção. O cronograma vai ser publicado no portal. Quando
entrarem em produção, chega um pacote XSD novo. Até lá, o CNPJ do prestador na DPS continua
`[0-9]{14}`.
