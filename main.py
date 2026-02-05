from fastapi import FastAPI, Request
from fastapi import HTTPException
import os
import asyncio
from typing import List
from fastapi import Query
from db import *

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok"}

def create_tables():
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""CREATE TABLE IF NOT EXISTIS venda(
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
        
        cur.execute("""CREATE TABLE IF NOT EXISTIS perfil(
            id TEXT PRIMARY KEY NOT NULL,
            url TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTIS pdfvenda(
            id TEXT PRIMARY KEY NOT NULL,
            venda_id INTEGER,
            caminho_pdf DECIMAL(10,2),
            data  TEXT,
            data_geracao TEXT,
            hora_geracao TEXT
            )""")

        cur.execute("""CREATE TABLE IF NOT EXISTIS pagamentos(
            id TEXT PRIMARY KEY NOT NULL,
            data  TEXT,
            valor DECIMAL(10,2),
            motivo TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTIS itenvendas(
            id TEXT PRIMARY KEY NOT NULL,
            venda_id INTEGER,
            tipo TEXT,
            nome TEXT,
            valor DECIMAL(10,2),
            quantidade INTEGER
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTIS debitosclienteEty(
            id TEXT PRIMARY KEY NOT NULL,
            codigo_cliente TEXT,
            periodo TEXT,
            valor TEXT,
            situacao TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTIS clientes(
            id TEXT PRIMARY KEY NOT NULL,
            nome TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            total_debitos TEXT
            )""")
        
        conn.commit()
        print("Tabelas criadas com sucesso")
    except Exception as e:
        conn.rollback()
        print(f"Erro ao criar tabelas: {e}")
    finally:
        if conn:
            put_conn(conn) # LIBERTA A CONEXÃO MESMO COM ERRO 
        