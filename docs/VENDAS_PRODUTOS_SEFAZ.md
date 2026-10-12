# Venda de produtos e NFC-e

Fluxo adaptado de `EcoCAL/first_api/servicos/fiscal.py` e `utilits/nfe.py`.
O motor de assinatura e transporte foi copiado para `fiscal/sefaz_engine.py`.

Instale `pip install -r requirements-sefaz.txt` e execute `python migrate.py`.
Configure `SEFAZ_EMPRESAS_JSON` no ambiente do servidor: um objeto cujas chaves
sao os UUIDs das empresas e cujos valores contem `cnpj`, `uf`, `ambiente`
(`homologacao` ou `producao`), `pfx_base64`, `pfx_password`, `csc_id`,
`csc_token`, `razao_social` e `ie`. Nunca envie essas credenciais pelo aplicativo.
O emitente deve estar credenciado e seu certificado/CSC configurados.
Mesmo com essas credenciais presentes, `ambiente=producao` retorna HTTP 409
`PRODUCTION_DISABLED`. Este backend ainda nao possui manifesto aprovado de
homologacao oficial para liberar transmissao em producao.

Todas as rotas usam o header legado `validation-uuid`, validado pelo middleware.

- `POST /vendas/produtos/sefaz`: corpo igual a `/venda-completa`, acrescentando
  `xml` com a NFC-e modelo 65 preparada pelo aplicativo. `valor` de cada item
  representa preco unitario; quantidade vezes preco deve conferir com a venda
  e com `vNF`. Empresa e ambiente do XML devem corresponder a configuracao.
- `GET /vendas/produtos/sefaz/notificacoes?limit=50`: resultados ainda nao
  confirmados, com `vendaId`, `status` e `nfce` (resposta fiscal e XML).
- `POST /vendas/produtos/sefaz/{venda_id}/notificado`: confirma recebimento
  de um resultado final da tentativa. Nao altera a autorizacao da nota.

Venda e itens sao gravados na mesma transacao antes do envio. Repetir a mesma
venda retorna seu resultado sem enviar novamente; mudar o conteudo gera 409.
Autorizacao exige cStat 100/150, protocolo, chave e XML autorizado.
Falhas ficam PENDENTE, preservando a venda e permitindo sua notificacao.
PROCESSANDO persistido apos interrupcao do processo exige verificacao manual
na SEFAZ. Nao ha reenvio automatico nem reconciliacao nesta rota.
As notificacoes usam consulta e confirmacao pelo aplicativo, sem push/email.
Esta integracao e independente das rotas `/api/v1/fiscal` existentes.
