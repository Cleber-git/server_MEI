# Fixtures da NFS-e (F8)

Respostas **sintéticas** da SEFIN Nacional, montadas com os códigos e textos do Anexo I v1.01
(`E0310`, `E0014`) e com os nomes de campo usados pela SEFIN (`erros[].Codigo/Descricao/
Complemento`). O Swagger oficial exige certificado, então os nomes não foram conferidos contra
uma resposta real. A conferência fica para a F10 (produção restrita com certificado).

- `post_nfse_rejeicao.json`: rejeição de regra de negócio (código de tributação inexistente).
  O `Complemento` traz dado do contribuinte de propósito, e o teste confere que ele nunca sai.
- `post_nfse_duplicidade.json`: DPS já usada (`E0014`). O provider consulta `GET /dps/{id}`.
- `post_evento_rejeicao.json`: pedido de evento recusado (código ilustrativo, sem fonte).
- `nfse_autorizada.xml`: NFS-e 1.01 válida no XSD oficial, gerada por `tests/sefin_fake.py`
  (assinatura de um "município" de teste, CNPJ de teste `11222333000181`).
