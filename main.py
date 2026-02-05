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
            venda_id INTEGER,
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


@app.post("/itensVenda")
def create_item_venda(data: ItemVendaIn):
    if not exists("venda", "id", data.venda_id):
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
