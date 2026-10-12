# Guia para implementar NFC-e no mobile

Entregar este arquivo, `NFCE_MOBILE_TEMPLATE.xml` e
`CONTRATO_MOBILE_VENDAS_SEFAZ.md` ao Codex do projeto mobile.

## Origem e escopo

Referencia examinada: `Enterprise/EcoCAL/PdvDefault/nfe/NFeXMLBuilder.cpp`,
metodo `NFeXMLBuilder::build` (linhas 16–397).
O template acompanha sua estrutura: `NFe/infNFe` versao 4.00, modelo 65,
venda presencial interna, consumidor final nao identificado, emissao normal.
Nao e documento autorizado nem exemplo fiscal universal. Os grupos tributarios
do template representam o exemplo `ICMSSN102/PISNT/COFINSNT` do PDV, e devem
ser escolhidos conforme regime, produto e operacao. Nao assumir que CSOSN 102,
PIS/COFINS 07 ou CFOP 5102 se aplicam automaticamente a toda empresa MEI.

Referencias oficiais para validar campos, regras, schemas e atualizacoes:
- [MOC e documentos tecnicos](https://www.nfe.fazenda.gov.br/portal/listaConteudo.aspx?tipoConteudo=ndIjl+iEFdE%3D)
- [Documentacao NFC-e SVRS](https://dfe-portal.svrs.rs.gov.br/Nfce/Documentos)

Validar o resultado com o pacote XSD e notas tecnicas vigentes para a UF,
ambiente, regime e data de emissao, incluindo grupos adicionais quando exigidos.
Este template reproduz o fluxo encontrado; nao declara cobertura de todas as
regras fiscais atuais ou dos novos grupos IBS/CBS. Implementar extensoes com base
no cadastro tributario validado antes de habilitar producao.

## Dados necessarios no mobile

| Origem | Campos |
| --- | --- |
| Cadastro fiscal da empresa | CNPJ, razao social, nome fantasia, IE, CRT, UF, codigo UF, logradouro, numero, bairro, municipio, codigo IBGE, CEP, serie e ambiente |
| Cadastro fiscal do produto | Codigo, descricao, NCM, CFOP, origem, unidade comercial/tributavel, GTIN comercial/tributavel e enquadramento ICMS/PIS/COFINS |
| Venda | UUID, itens ativos, quantidade, preco unitario, total, pagamentos e data de emissao |
| Controle de numeracao | Numero fiscal reservado para empresa/modelo/serie/ambiente, cNF e chave persistidos |

O cadastro legado `ItemVendaIn` nao contem NCM, CFOP, origem e tributacao;
o mobile precisa buscar esses dados no cadastro do produto para gerar XML.
Tambem nao existe nesta entrega endpoint de configuracao fiscal publica nem
reserva central de numero: disponibilizar essas informacoes e definir a reserva
antes de operar varios dispositivos. Credenciais privadas permanecem no servidor.

## Chave de acesso

Montar os 43 digitos nesta ordem:

```text
cUF(2) + AAMM(4) + CNPJ(14) + modelo(2) + serie(3)
+ nNF(9) + tpEmis(1) + cNF(8)
```

Completar serie/nNF/cNF com zeros a esquerda apenas na chave.
`AAMM` vem de `dhEmi`. Modelo = 65 e tpEmis = 1 neste fluxo.
Calcular DV com modulo 11:

```text
peso = 2; soma = 0
para cada digito dos 43, da direita para a esquerda:
    soma += digito * peso
    peso = (peso == 9) ? 2 : peso + 1
resto = soma % 11
dv = (resto == 0 ou resto == 1) ? 0 : 11 - resto
chave44 = chave43 + dv
infNFe.Id = "NFe" + chave44
ide.cDV = dv
```

O UUID da venda e a chave fiscal sao identificadores diferentes.
Persistir ambos; repetir uma requisicao nao pode gerar outra chave.
O PDV sorteia `nNF`; nao copiar esse comportamento. Reservar numero sequencial
sem colisoes entre dispositivos e conforme o controle fiscal da empresa.
`cNF` pode ser gerado uma vez e persistido, com exatamente oito digitos.

## XML e calculos

Usar biblioteca de XML com namespace `http://www.portalfiscal.inf.br/nfe`.
Criar elementos na ordem do template; escapar textos pela biblioteca, inclusive
`&`, `<`, `>` e aspas em atributos. Nao concatenar dados crus em XML.
Usar decimal exato (`BigDecimal` ou equivalente), nunca float/double para dinheiro.
Separador decimal e ponto, sem moeda ou separadores de milhar.

- `qCom/qTrib`: quatro casas, exemplo `2.0000`.
- `vUnCom/vUnTrib`: preservar precisao do preco, dentro do schema; exemplo `10.00`.
- `vProd`: quantidade vezes preco comercial, arredondado a duas casas.
- Para o escopo inicial sem desconto/frete: `vProd` total = soma dos itens;
  `vNF` = total dos produtos = `venda.valor` do JSON.
- Quantidade tributavel/preco tributavel podem diferir dos comerciais quando
  houver conversao de unidade; preencher pelo cadastro, preservando o valor.
- `nItem`: 1, 2, 3… somente para itens ativos; excluir cancelados.
- `dhEmi`: ISO 8601 com um unico offset correto, exemplo
  `2026-10-06T14:30:00-03:00`. Nao acrescentar `-03:00` a uma string que ja
  contem offset, como pode ocorrer no builder Qt.
- `tpAmb`: 2 para homologacao; 1 para producao, correspondente ao servidor.
- Em homologacao, o primeiro `xProd` recebe
  `NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL`.
  Os demais itens mantem suas descricoes; em producao usar descricoes reais.
- `cEAN/cEANTrib`: usar GTIN valido quando existente; `SEM GTIN` quando aplicavel.
  NCM deve vir do produto; nao copiar o `00000000` fixo do PDV.
- Remover tags opcionais sem valor (`xFant`, `infAdic`, por exemplo).
- O consumidor nao identificado nao gera grupo `dest` neste template.
  Consumidor identificado e outros cenarios exigem completar os grupos/regras
  aplicaveis; nao gerar apenas um `dest` vazio.

O contrato HTTP atual suporta quantidade inteira e nao tem campos de desconto,
frete ou acrescimos separados. Implementar primeiro essa combinacao. Nao copiar
o desconto do builder PDV: XML com desconto e JSON com preco bruto nao passa
na validacao atual. Para descontos/quantidades fracionarias, evoluir o contrato
de venda e validacoes do servidor em conjunto.

## Pagamentos

O template usa dinheiro `tPag=01` e `vPag=vNF`, sem troco. Para outros meios,
selecionar o codigo fiscal real e os grupos adicionais exigidos pelo schema e
regra vigente, especialmente dados de cartao/integracao. Nao copiar `01` fixo
quando a venda foi paga por outro meio. Repetir `detPag` para pagamentos mistos.
Em pagamento excedente, preencher `vTroco` apos os `detPag` segundo a regra
aplicavel. Manter pagamentos do XML coerentes com a venda.

## Responsabilidades do servidor

Enviar somente `NFe` nao assinada no campo `xml` do JSON. O mobile nao gera
`Signature`, lote `enviNFe`, envelope SOAP ou protocolo `nfeProc`.
O motor do servidor assina, prepara o envio e gera o suplemento/QR Code.
Consultar o retorno para obter o XML autorizado. Nunca produzir protocolo ou
declarar nota autorizada no mobile apenas porque o XML foi criado.

## Instrucoes prontas para o Codex do mobile

```text
Implemente a montagem e sincronizacao da NFC-e de venda de produtos usando
PADRAO_XML_NFCE_MOBILE.md, NFCE_MOBILE_TEMPLATE.xml e
CONTRATO_MOBILE_VENDAS_SEFAZ.md como contrato da integracao.

Antes de editar, examine arquitetura, persistencia, fila offline, cadastro
fiscal, produtos e pagamentos do projeto. Use as convencoes existentes.

Crie um builder de XML modelo 65 versao 4.00 usando biblioteca XML e decimal
exato. Receba dados fiscais tipados de empresa e produtos; valide dados faltantes
antes do envio e nao invente NCM, regime, CSOSN/CST ou numero fiscal.
Implemente chave de 44 digitos e DV modulo 11. Use numeracao reservada sem
colisoes, persistindo numero, cNF, dhEmi, chave, XML e corpo original na venda.
Se faltar reserva/configuracao fiscal no backend, explicite a dependencia e
prepare a interface para integra-la, sem simular uma reserva segura.

O primeiro escopo e venda interna presencial, consumidor nao identificado,
quantidades inteiras, sem desconto/frete/acrescimos e dinheiro sem troco.
Use o grupo tributario do template somente se corresponder ao cadastro fiscal.
Outros enquadramentos devem selecionar seus grupos validos ou bloquear emissao
com mensagem de dado fiscal ausente. Validar contra XSD vigente quando disponivel.

POST /vendas/produtos/sefaz com validation-uuid e o JSON documentado.
Persistir o corpo exato antes do envio e repetir o mesmo corpo/ID em timeout.
Nao cadastrar antes em /venda-completa; nao regenerar chave/XML em repeticao.
Tratar AUTORIZADO, PENDENTE e PROCESSANDO, com nfce opcional/null e seus campos
opcionais. Consultar notificacoes, salvar localmente e confirmar recebimento
somente depois da persistencia e para resultados diferentes de PROCESSANDO.
Nao reenviar nota fiscal automaticamente nem marcar PENDENTE como autorizada.

Teste: DV e chave; offset unico; escape XML; somas/arredondamento; nItem;
texto de homologacao apenas no primeiro item; descricoes reais em producao;
dados fiscais incompletos; repeticao sem regeneracao e sem novo envio fiscal;
retornos sem campos opcionais; persistencia antes do ACK de notificacao.
Entregue alteracoes e descreva dependencias reais para habilitar producao.
```
