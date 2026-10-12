from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from fiscal import product_sales as sales


def sale():
    return sales.ProductSale.model_validate({
        'venda': {'id': 'sale-1', 'empresaUuid': 'company', 'formaPagamento': 'dinheiro',
                  'valor': '10.00', 'data': '2026-10-06', 'sincronizado': True,
                  'dataCadastro': 1, 'atualizadoEm': 1, 'deletado': False},
        'itens': [{'id': 'item-1', 'vendaId': 'sale-1', 'tipo': 'produto',
                   'nome': 'Produto', 'valor': 5, 'quantidade': 2}],
        'xml': '<NFe xmlns="http://www.portalfiscal.inf.br/nfe"><infNFe>'
               '<ide><mod>65</mod><tpAmb>2</tpAmb></ide><emit><CNPJ>123</CNPJ></emit>'
               '<total><ICMSTot><vNF>10.00</vNF></ICMSTot></total></infNFe></NFe>'})


def test_validates_company_and_totals():
    data = sale()
    config = {'cnpj': '123', 'ambiente': 'homologacao'}
    sales.validate_sale(data, 'company', config)
    with pytest.raises(HTTPException) as error:
        sales.validate_sale(data, 'other', config)
    assert error.value.status_code == 403
    data.itens[0].quantidade = 3
    with pytest.raises(HTTPException):
        sales.validate_sale(data, 'company', config)


def test_duplicate_sale_does_not_submit_again():
    data = sale()
    conn = Mock()
    conn.cursor.return_value.fetchone.return_value = (
        sales.canonical_hash(data.model_dump(mode='json')), 'PENDENTE', {'sucesso': False})
    with patch.object(sales, 'get_conn', return_value=conn), patch.object(sales, 'put_conn'), \
         patch.object(sales, 'load_config', return_value={'cnpj': '123', 'ambiente': 'homologacao'}), \
         patch.object(sales, 'submit') as submit:
        assert sales.create_sale(data, 'company')['status'] == 'PENDENTE'
        submit.assert_not_called()


def test_failed_submission_preserves_sale_and_notifies_pending():
    conn = Mock()
    conn.cursor.return_value.fetchone.return_value = None
    with patch.object(sales, 'get_conn', return_value=conn), patch.object(sales, 'put_conn'), \
         patch.object(sales, 'load_config', return_value={'cnpj': '123', 'ambiente': 'homologacao'}), \
         patch.object(sales, 'submit', side_effect=TimeoutError):
        result = sales.create_sale(sale(), 'company')
    assert result['status'] == 'PENDENTE'
    assert conn.commit.call_count == 2
