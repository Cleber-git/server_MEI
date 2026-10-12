"""Legacy product sales and NFC-e delivery, adapted from EcoCAL first_api."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from lxml import etree as ET
from pydantic import Field
from psycopg2.extras import Json

from db import get_conn, put_conn
from models import VendaCompletaIn, ItemVendaIn
from .domain import canonical_hash

router = APIRouter(tags=["vendas-produtos"])
NS = "{http://www.portalfiscal.inf.br/nfe}"


class ProductSaleItem(ItemVendaIn):
    # Keep the legacy JSON number/string contract, but avoid binary float
    # arithmetic for prices inside the fiscal endpoint.
    valor: Decimal


class ProductSale(VendaCompletaIn):
    xml: str = Field(min_length=1, max_length=2000000)
    itens: list[ProductSaleItem] = Field(min_length=1, max_length=990)


def company(validation_uuid: str = Header(alias="validation-uuid")):
    return validation_uuid


def validate_sale(data, empresa, config):
    if data.venda.empresaUuid != empresa:
        raise HTTPException(403, "Empresa da venda diferente da empresa autenticada")
    if data.venda.deletado or any(i.vendaId != data.venda.id for i in data.itens):
        raise HTTPException(422, "Venda deletada ou itens de outra venda")
    try:
        total = sum((Decimal(str(i.valor)) * i.quantidade for i in data.itens), Decimal(0))
        value = Decimal(data.venda.valor)
        if not value.is_finite() or value <= 0 or any(i.quantidade <= 0 or i.valor < 0 for i in data.itens):
            raise ValueError()
        if total.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != value:
            raise ValueError()
        parser = ET.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
            recover=False,
        )
        root = ET.fromstring(data.xml.encode("utf-8"), parser=parser)
        if root.getroottree().docinfo.doctype:
            raise ValueError()
        inf = root.find(f'.//{NS}infNFe')
        if inf is None or inf.findtext(f'{NS}ide/{NS}mod') != '65':
            raise ValueError()
        if inf.findtext(f'{NS}emit/{NS}CNPJ') != config['cnpj']:
            raise ValueError()
        ambiente = '1' if config['ambiente'] == 'producao' else '2'
        if inf.findtext(f'{NS}ide/{NS}tpAmb') != ambiente:
            raise ValueError()
        if Decimal(inf.findtext(f'{NS}total/{NS}ICMSTot/{NS}vNF', '0')) != value:
            raise ValueError()
    except (InvalidOperation, ValueError, ET.XMLSyntaxError, TypeError):
        raise HTTPException(422, "XML NFC-e, itens, emitente, ambiente ou total invalidos")


def load_config(empresa):
    try:
        config = json.loads(os.getenv('SEFAZ_EMPRESAS_JSON', '{}'))[empresa]
        if config['ambiente'] not in {'homologacao', 'producao'}:
            raise ValueError()
        if any(not config.get(k) for k in ('cnpj', 'uf', 'pfx_base64', 'pfx_password', 'csc_id', 'csc_token')):
            raise ValueError()
        return config
    except (KeyError, ValueError, TypeError):
        raise HTTPException(409, "Configuracao SEFAZ da empresa ausente ou incompleta")


def submit(xml, config):
    try:
        from .sefaz_engine import emitir_nfce
    except ImportError:
        raise HTTPException(503, "Instale requirements-sefaz.txt para emitir NFC-e")
    return emitir_nfce(xml, config)


@router.post(
    '/vendas/produtos/sefaz',
    responses={
        401: {'description': 'Empresa não autenticada'},
        403: {'description': 'Empresa do payload diverge da empresa autenticada'},
        409: {'description': 'Conflito idempotente, configuração incompleta ou produção bloqueada'},
    },
)
def create_sale(data: ProductSale, empresa: str = Depends(company)):
    fingerprint = canonical_hash(data.model_dump(mode='json'))
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('sale:' + empresa + ':' + data.venda.id,))
        cur.execute('SELECT request_hash,status,resultado FROM produto_venda_fiscal WHERE empresa_uuid=%s AND venda_id=%s', (empresa, data.venda.id))
        previous = cur.fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise HTTPException(409, 'Venda ja recebida com outro conteudo')
            conn.commit()
            return {'vendaId': data.venda.id, 'status': previous[1], 'nfce': previous[2]}
        config = load_config(empresa)
        if config['ambiente'] == 'producao':
            raise HTTPException(
                409,
                'PRODUCTION_DISABLED: emissão em produção permanece bloqueada até homologação oficial aprovada',
            )
        validate_sale(data, empresa, config)
        # The legacy sales table may have a database-wide unique key for id.
        # Check it before insert so older installations return a mobile-safe
        # conflict instead of an unhandled unique-constraint error.
        cur.execute('SELECT 1 FROM venda WHERE id=%s', (data.venda.id,))
        if cur.fetchone():
            raise HTTPException(409, 'Venda ja cadastrada; utilize um novo identificador')
        v = data.venda
        cur.execute('INSERT INTO venda(id,empresauuid,forma_pagamento,valor,data,sincronizado,datacadastro,atualizadoem,deletado) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)', (v.id, empresa, v.formaPagamento, v.valor, v.data, v.sincronizado, v.dataCadastro, v.atualizadoEm, False))
        for i in data.itens:
            cur.execute('SELECT 1 FROM itenvendas WHERE id=%s', (i.id,))
            if cur.fetchone():
                raise HTTPException(409, 'Item ja cadastrado')
            cur.execute('INSERT INTO itenvendas(id,venda_id,tipo,nome,valor,quantidade) VALUES(%s,%s,%s,%s,%s,%s)', (i.id, v.id, i.tipo, i.nome, i.valor, i.quantidade))
        cur.execute('INSERT INTO produto_venda_fiscal(empresa_uuid,venda_id,request_hash) VALUES(%s,%s,%s)', (empresa, v.id, fingerprint))
        conn.commit()
        # Commit before the remote call: retries must never submit this sale twice.
        try:
            result = submit(data.xml, config)
        except Exception:
            result = {'sucesso': False, 'erro': 'Falha de comunicacao/configuracao; consulte a situacao fiscal antes de reenviar'}
        if not isinstance(result, dict):
            result = {
                'sucesso': False,
                'erro': 'Resultado fiscal em formato inesperado; consulte as notificações antes de reenviar',
            }
        authorized = isinstance(result, dict) and result.get('sucesso') and str(result.get('cStat')) in {'100', '150'} and all(result.get(k) for k in ('nProt', 'chNFe', 'xml_autorizado'))
        status = 'AUTORIZADO' if authorized else 'PENDENTE'
        try:
            cur.execute('UPDATE produto_venda_fiscal SET status=%s,resultado=%s,atualizado_em=NOW() WHERE empresa_uuid=%s AND venda_id=%s', (status, Json(result), empresa, v.id))
            conn.commit()
        except Exception:
            # The submission may already have reached the authority. Preserve
            # the idempotent PROCESSANDO state and let the client reconcile by
            # repeating the same request or polling notifications.
            conn.rollback()
            return {
                'vendaId': v.id,
                'status': 'PROCESSANDO',
                'nfce': {
                    'sucesso': False,
                    'erro': 'Resultado ainda não confirmado; consulte as notificações antes de reenviar',
                },
            }
        return {'vendaId': v.id, 'status': status, 'nfce': result}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


@router.get(
    '/vendas/produtos/sefaz/notificacoes',
    responses={401: {'description': 'Empresa não autenticada'}},
)
def notifications(empresa: str = Depends(company), limit: int = Query(50, ge=1, le=200)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT venda_id,status,resultado FROM produto_venda_fiscal WHERE empresa_uuid=%s AND notificado=FALSE ORDER BY criado_em,venda_id LIMIT %s', (empresa, limit))
        return [{'vendaId': r[0], 'status': r[1], 'nfce': r[2]} for r in cur.fetchall()]
    finally:
        put_conn(conn)


@router.post(
    '/vendas/produtos/sefaz/{venda_id}/notificado',
    responses={
        401: {'description': 'Empresa não autenticada'},
        404: {'description': 'Resultado fiscal inexistente, de outro tenant ou ainda processando'},
    },
)
def acknowledge(venda_id: str, empresa: str = Depends(company)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE produto_venda_fiscal SET notificado=TRUE WHERE empresa_uuid=%s AND venda_id=%s AND status <> 'PROCESSANDO' RETURNING venda_id", (empresa, venda_id))
        row = cur.fetchone()
        if not row:
            # Acknowledgement is idempotent: a lost HTTP response must not
            # make a successfully acknowledged result look missing.
            cur.execute(
                "SELECT 1 FROM produto_venda_fiscal WHERE empresa_uuid=%s AND venda_id=%s AND notificado=TRUE AND status <> 'PROCESSANDO'",
                (empresa, venda_id),
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Resultado fiscal nao encontrado')
        conn.commit()
        return {'sucesso': True, 'vendaId': venda_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
