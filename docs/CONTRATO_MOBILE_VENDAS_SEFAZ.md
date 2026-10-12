# Contrato mobile — venda de produtos para SEFAZ

## Autenticacao

Todas as chamadas usam `validation-uuid: <UUID da empresa autenticada>`.
POST com JSON usa tambem `Content-Type: application/json`.
Certificado, senha e CSC sao configurados somente no servidor.

## Criar venda e emitir NFC-e

`POST /vendas/produtos/sefaz`

```json
{
  "venda": {
    "id": "b5a0a698-5984-4705-a98d-b241b86d265b",
    "empresaUuid": "UUID-DA-EMPRESA",
    "formaPagamento": "dinheiro",
    "valor": "20.00",
    "data": "2026-10-06",
    "sincronizado": true,
    "dataCadastro": 1791288000000,
    "atualizadoEm": 1791288000000,
    "deletado": false
  },
  "itens": [
    {
      "id": "c3c46945-0850-47f0-9104-00588c8799db",
      "vendaId": "b5a0a698-5984-4705-a98d-b241b86d265b",
      "tipo": "produto",
      "nome": "Produto exemplo",
      "valor": 10.00,
      "quantidade": 2
    }
  ],
  "xml": "<NFe xmlns=\"http://www.portalfiscal.inf.br/nfe\">...XML completo da NFC-e...</NFe>"
}
```

O XML acima e um placeholder, nao um XML valido para emissao.
O app precisa montar o XML completo modelo 65, como no fluxo EcoCAL;
esta rota nao gera XML a partir dos itens. A assinatura e o envio ficam no servidor.

Todos os campos do exemplo sao obrigatorios. `venda.valor` e string decimal;
`itens[].valor` e numero e representa preco unitario, nao subtotal.
`quantidade` e inteiro positivo; a rota atual nao aceita quantidade fracionaria.
Use UUIDs gerados pelo app para venda e itens. Todos os `vendaId` dos itens
devem ser iguais a `venda.id`. `empresaUuid` deve corresponder ao header.
Use timestamps em milissegundos nos campos de cadastro/atualizacao para manter
o padrao do mobile; a rota armazena esses inteiros sem converter a unidade.

A soma de preco unitario vezes quantidade deve conferir com `venda.valor` e
com `total/ICMSTot/vNF` do XML. Emitente/CNPJ e `tpAmb` devem corresponder a
configuracao do servidor. Esta rota nao implementa desconto/frete separado
no contrato da venda. Limites: 1 a 990 itens e XML de ate 2.000.000 caracteres.
`formaPagamento` e texto no cadastro; os pagamentos fiscais sao os grupos
`pag/detPag` do XML. O app deve manter esses dados consistentes.

### Retorno HTTP 200

```json
{
  "vendaId": "b5a0a698-5984-4705-a98d-b241b86d265b",
  "status": "AUTORIZADO",
  "nfce": {
    "sucesso": true,
    "cStat": "100",
    "xMotivo": "Autorizado o uso da NF-e",
    "nProt": "PROTOCOLO",
    "chNFe": "CHAVE-DE-44-DIGITOS",
    "xml_assinado": "<NFe>...</NFe>",
    "xml_autorizado": "<nfeProc>...</nfeProc>",
    "lote": "...",
    "envelope": "...",
    "resposta_sefaz": "..."
  }
}
```

Os valores de protocolo/chave/XML sao ilustrativos. `nfce` e um objeto de
resposta do motor fiscal: seus campos sao opcionais e podem variar.
Nao exigir esses campos no DTO quando o status for PENDENTE/PROCESSANDO.

```json
{
  "vendaId": "b5a0a698-5984-4705-a98d-b241b86d265b",
  "status": "PENDENTE",
  "nfce": {
    "sucesso": false,
    "erro": "Falha de comunicacao/configuracao; consulte a situacao fiscal antes de reenviar"
  }
}
```

| Status | Comportamento no mobile |
| --- | --- |
| AUTORIZADO | Venda salva e nota autorizada. Guardar chave, protocolo e XML autorizado. |
| PENDENTE | Venda salva, mas a tentativa nao confirmou autorizacao. Exibir `xMotivo` ou `erro`; nao emitir outra nota automaticamente. |
| PROCESSANDO | Tentativa registrada sem resultado final; `nfce` pode ser null. Consultar notificacoes e manter pendencia. |

HTTP 200 nao significa autorizacao fiscal. Use `status` para apresentar a situacao.
PENDENTE tambem pode representar rejeicao SEFAZ ou resultado incerto.

### Idempotencia e falha de rede

O identificador idempotente e `venda.id`, no escopo da empresa.
Salve o corpo original na fila local antes do POST. Apos timeout ou perda de
conexao, repita exatamente o mesmo corpo e ID, inclusive timestamps, itens e XML.
Nao gere outro ID/XML para tentar recuperar a resposta. Repeticao retorna
o estado gravado sem novo envio a SEFAZ; conteudo diferente retorna 409.
Uma venda ja cadastrada pela rota antiga nao pode ser emitida por esta rota.
Nao envie a mesma venda primeiro para `/venda-completa`.

## Consultar notificacoes

`GET /vendas/produtos/sefaz/notificacoes?limit=50`

`limit` e opcional: padrao 50, minimo 1, maximo 200.

Retorno HTTP 200: array, sem envelope/paginacao.

```json
[
  {
    "vendaId": "b5a0a698-5984-4705-a98d-b241b86d265b",
    "status": "PENDENTE",
    "nfce": { "sucesso": false, "xMotivo": "Motivo retornado pela SEFAZ" }
  }
]
```

Lista apenas registros da empresa autenticada ainda nao confirmados,
em ordem de criacao. Pode incluir PROCESSANDO. Array vazio significa
que nao ha notificacoes disponiveis. Nao ha push, webhook ou parametro de cursor.

## Confirmar recebimento

Depois de salvar o resultado no banco local:

`POST /vendas/produtos/sefaz/{venda_id}/notificado`

Sem corpo. Retorno HTTP 200:

```json
{ "sucesso": true, "vendaId": "b5a0a698-5984-4705-a98d-b241b86d265b" }
```

A confirmacao e repetivel e retira o resultado da lista. Pode confirmar
AUTORIZADO ou PENDENTE. PROCESSANDO nao pode ser confirmado (retorna 404).
Confirmar PENDENTE significa apenas que o app recebeu o resultado, nao que
a nota foi autorizada. Nao modifica o status fiscal nem reenvia a nota.

## Erros

| HTTP | Significado |
| --- | --- |
| 401 | Header ausente ou empresa nao autorizada pelo middleware. |
| 403 | Empresa do corpo diferente da empresa autenticada. |
| 404 | Confirmacao de resultado inexistente, de outra empresa ou PROCESSANDO. |
| 409 | Configuracao fiscal incompleta, venda/item existente, repeticao com outro conteudo ou `PRODUCTION_DISABLED`. |
| 422 | Corpo invalido, venda deletada, itens inconsistentes ou XML/total/emitente/ambiente invalido. |
| 500 | Falha interna; recuperar com o mesmo corpo/ID. |

Erros de negocio usam `{"detail":"mensagem"}`; validacao Pydantic pode
retornar `detail` como array de erros. Trate ambas as formas.
Falhas do motor apos persistencia, inclusive dependencia de emissao ausente,
retornam HTTP 200 com status PENDENTE.

## Sequencia sugerida

1. Persistir venda, itens, XML e corpo original na fila local.
2. Fazer POST diretamente para `/vendas/produtos/sefaz`.
3. Salvar resultado localmente; em perda de resposta repetir o corpo original.
4. Consultar notificacoes ao sincronizar e aplicar os resultados por `vendaId`.
5. Confirmar cada resultado final somente depois da gravacao local.

Nao existe endpoint de reenvio/reconciliacao automatica nesta entrega.
PROCESSANDO persistente e PENDENTE exigem verificacao fiscal antes de qualquer
nova emissao. O modelo de NFC-e, CSC, certificado e credenciamento precisam
ser preparados/configurados para a UF e ambiente utilizados.
