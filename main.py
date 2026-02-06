from fastapi import FastAPI, Request
from fastapi import HTTPException
import os
import asyncio
from typing import List
from fastapi import Query
from db import *
from models import *

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok"}

def create_tables():
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""CREATE TABLE IF NOT EXISTS venda(
            id TEXT PRIMARY KEY NOT NULL,
            forma_pagamento TEXT,
            valor DECIMAL(10,2),
            data  TEXT
            )""")
        
        cur.execute(""" CREATE TABLE IF NOT EXISTS servico(
            id TEXT PRIMARY KEY NOT NULL,
            nome TEXT NOT NULL,
            preco DECIMAL(10,2),
            preco_anterior DECIMAL(10,2),
            data_criacao TEXT,
            tipo TEXT
        )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS perfil(
            id TEXT PRIMARY KEY NOT NULL,
            url TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS pdfvenda(
            id TEXT PRIMARY KEY NOT NULL,
            venda_id INTEGER,
            caminho_pdf DECIMAL(10,2),
            data  TEXT,
            data_geracao TEXT,
            hora_geracao TEXT
            )""")

        cur.execute("""CREATE TABLE IF NOT EXISTS pagamentos(
            id TEXT PRIMARY KEY NOT NULL,
            data  TEXT,
            valor DECIMAL(10,2),
            motivo TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS itenvendas(
            id TEXT PRIMARY KEY NOT NULL,
            venda_id TEXT,
            tipo TEXT,
            nome TEXT,
            valor DECIMAL(10,2),
            quantidade INTEGER
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS debitosclienteEty(
            id TEXT PRIMARY KEY NOT NULL,
            codigo_cliente TEXT,
            periodo TEXT,
            valor TEXT,
            situacao TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS clientes(
            id TEXT PRIMARY KEY NOT NULL,
            nome TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            totaldebitos TEXT
            )""")
        
        conn.commit()
        print("Tabelas criadas com sucesso")
    except Exception as e:
        conn.rollback()
        print(f"Erro ao criar tabelas: {e}")
    finally:
        if conn:
            put_conn(conn) # LIBERTA A CONEXÃO MESMO COM ERRO 
        
def exists(table: str, field: str, value: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT 1 FROM {table} WHERE {field} = %s LIMIT 1",
            (value,)
        )
        return cur.fetchone() is not None
    finally:
        put_conn(conn)

@app.on_event("startup")
def startup(): 
    create_tables()
    

@app.post("/clientes")
def create_cliente(data: ClienteIn):
    if exists("clientes", "id", data.id):
        raise HTTPException(409, "Cliente já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clientes (id, nome, telefone, email, endereco, totaldebitos)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            data.id, data.nome, data.telefone,
            data.email, data.endereco, data.totalDebitos
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.get("/clientes")
def list_clientes():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM clientes")
        row = cur.fetchall()
        
        clientes = []
        
        for r in row:
            clientes.append(ClienteIn(
                id= r[0], 
                nome=r[1], 
                telefone=r[2], 
                email=r[3], 
                endereco=r[4], 
                totalDebitos=r[5]))
        return clientes
    finally:
        put_conn(conn)

@app.delete("/clientes/{id}")
def delete_cliente(id: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM clientes WHERE id = %s", (id,))
        if not cur.fetchone():
            raise HTTPException(404, "Cliente não encontrado")

        cur.execute(
            "SELECT 1 FROM debitosclienteEty WHERE codigo_cliente = %s LIMIT 1",
            (id,)
        )
        if cur.fetchone():
            raise HTTPException(409, "Cliente possui débitos vinculados")

        cur.execute("DELETE FROM clientes WHERE id = %s", (id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.post("/vendas")
def create_venda(data: VendaIn):
    if exists("venda", "id", data.id):
        raise HTTPException(409, "Venda já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO venda (id, forma_pagamento, valor, data)
            VALUES (%s,%s,%s,%s)
        """, (
            data.id, data.formaPagamento, data.valor, data.data
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/vendas")
def list_vendas():
    id: str
    formaPagamento: str
    valor: float
    data: str
    
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM venda")
        
        result = cur.fetchall()
        vendas =[]
        for row in result:
            vendas.append(VendaIn(id= row[0], 
                                  formaPagamento=row[1],
                                  valor=row[2],
                                  data=row[3]))
        
        
        return vendas
    finally:
        put_conn(conn)

@app.delete("/vendas/{id}")
def delete_venda(id: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM venda WHERE id = %s", (id,))
        if not cur.fetchone():
            raise HTTPException(404, "Venda não encontrada")

        cur.execute(
            "SELECT 1 FROM itenvendas WHERE venda_id = %s LIMIT 1",
            (id,)
        )
        if cur.fetchone():
            raise HTTPException(409, "Venda possui itens vinculados")

        cur.execute("DELETE FROM venda WHERE id = %s", (id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.post("/itensVenda")
def create_item_venda(data: ItemVendaIn):
    if not exists("venda", "id", data.vendaId):
        raise HTTPException(404, "Venda não encontrada")

    if exists("itenvendas", "id", data.id):
        raise HTTPException(409, "Item já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO itenvendas
            (id, venda_id, tipo, nome, valor, quantidade)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            data.id, data.vendaId, data.tipo,
            data.nome, data.valor, data.quantidade
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/itens-venda")
def list_itens_venda(vendaId: str):
    if not exists("venda", "id", vendaId):
        raise HTTPException(404, "Venda não encontrada")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM itenvendas"
        )
        rows = cur.fetchall()
        
        itemVenda=[]
        
        for r in rows:
            itemVenda.append(ItemVendaIn(id=r[0],
                                         vendaId=r[1],
                                         tipo=r[2],
                                         nome=r[3],
                                         valor=r[4],
                                         quantidade=r[5]))
        return itemVenda    
    finally:
        put_conn(conn)
        
        
@app.delete("/itens-venda/{id}")
def delete_item_venda(id: str):
    return delete_by_id("itenvendas", id)


def delete_by_id(table: str, id_value: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT 1 FROM {table} WHERE id = %s",
            (id_value,)
        )
        if not cur.fetchone():
            raise HTTPException(404, f"Registro não encontrado em {table}")

        cur.execute(
            f"DELETE FROM {table} WHERE id = %s",
            (id_value,)
        )
        conn.commit()
        return {"status": "ok", "table": table, "id": id_value}
    finally:
        put_conn(conn)

@app.post("/servicos")
def create_servico(data: ServicoIn):
    if exists("servico", "id", data.id):
        raise HTTPException(409, "Serviço já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO servico
            (id, nome, preco, preco_anterior, data_criacao, tipo)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            data.id, data.nome, data.preco,
            data.precoAnterior, data.dataCriacao, data.tipo
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.get("/servicos")
def list_servicos():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM servico")
        rows = cur.fetchall()

        return [
            ServicoIn(
                id=r[0],
                nome=r[1],
                preco=r[2],
                precoAnterior=r[3],
                dataCriacao=r[4],
                tipo=r[5]
            ) for r in rows
        ]
    finally:
        put_conn(conn)


@app.delete("/servicos/{id}")
def delete_servico(id: str):
    return delete_by_id("servico", id)

@app.post("/perfil")
def create_perfil(data: PerfilIn):
    if exists("perfil", "id", data.id):
        raise HTTPException(409, "Perfil já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO perfil (id, url)
            VALUES (%s,%s)
        """, (data.id, data.url))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.get("/perfil")
def list_perfil():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM perfil")
        rows = cur.fetchall()

        return [PerfilIn(id=r[0], url=r[1]) for r in rows]
    finally:
        put_conn(conn)


@app.delete("/perfil/{id}")
def delete_perfil(id: str):
    return delete_by_id("perfil", id)

@app.post("/pdf-venda")
def create_pdf_venda(data: PdfVendaIn):
    if exists("pdfvenda", "id", data.id):
        raise HTTPException(409, "PDF da venda já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pdfvenda
            (id, venda_id, caminho_pdf, data, data_geracao, hora_geracao)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            data.id, data.vendaId, data.caminhoPdf,
            data.data, data.dataGeracao, data.horaGeracao
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.get("/pdf-venda")
def list_pdf_venda():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pdfvenda")
        rows = cur.fetchall()

        return [
            PdfVendaIn(
                id=r[0],
                vendaId=r[1],
                caminhoPdf=r[2],
                data=r[3],
                dataGeracao=r[4],
                horaGeracao=r[5]
            ) for r in rows
        ]
    finally:
        put_conn(conn)


@app.delete("/pdf-venda/{id}")
def delete_pdf_venda(id: str):
    return delete_by_id("pdfvenda", id)


@app.post("/pagamentos")
def create_pagamento(data: PagamentoIn):
    if exists("pagamentos", "id", data.id):
        raise HTTPException(409, "Pagamento já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pagamentos
            (id, data, valor, motivo)
            VALUES (%s,%s,%s,%s)
        """, (
            data.id, data.data, data.valor, data.motivo
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.get("/pagamentos")
def list_pagamentos():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pagamentos")
        rows = cur.fetchall()

        return [
            PagamentoIn(
                id=r[0],
                data=r[1],
                valor=r[2],
                motivo=r[3]
            ) for r in rows
        ]
    finally:
        put_conn(conn)


@app.delete("/pagamentos/{id}")
def delete_pagamento(id: str):
    return delete_by_id("pagamentos", id)

@app.post("/debitos-cliente")
def create_debito_cliente(data: DebitoClienteIn):
    if exists("debitosclienteEty", "id", data.id):
        raise HTTPException(409, "Débito já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO debitosclienteEty
            (id, codigo_cliente, periodo, valor, situacao)
            VALUES (%s,%s,%s,%s,%s)
        """, (
            data.id, data.codigoCliente,
            data.periodo, data.valor, data.situacao
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.get("/debitos-cliente")
def list_debitos_cliente():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM debitosclienteEty")
        rows = cur.fetchall()

        return [
            DebitoClienteIn(
                id=r[0],
                codigoCliente=r[1],
                periodo=r[2],
                valor=r[3],
                situacao=r[4]
            ) for r in rows
        ]
    finally:
        put_conn(conn)


@app.delete("/debitos-cliente/{id}")
def delete_debito_cliente(id: str):
    return delete_by_id("debitosclienteEty", id)
