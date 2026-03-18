from fastapi import FastAPI, Request, HTTPException, Query, Header, Depends
import os
import asyncio
from typing import List
from db import *
from models import *
from jose import JWTError, jwt
from datetime import datetime, timedelta
# from email_ import email_routes
from random import randrange
import re 
from email_ import email_service
import requests

from email.mime.text import MIMEText
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from fastapi.responses import JSONResponse


# SECRET_KEY = "minha_chave_super_secreta_123"
# ALGORITHM = "HS256"
# ACCESS_TOKEN_EXPIRE_MINUTES = 60

app = FastAPI()

def get_empresa(validation_uuid: str = Header(alias="validation-uuid")):
    return validation_uuid

@app.middleware("http")
async def validar_empresa(request: Request, call_next):

    path = request.url.path.rstrip("/")
    if path == "/venda-completa":
        body = await request.body()
        print(body)
        
    rotas_publicas = [
        "/empresa",
        "/email",
        "/validaEmail",
        "/redefinirSenha",
        "/codigoSenha",
        "/login"
    ]

    chave = request.headers.get("validation-uuid")
    chave_env = os.getenv("key_first_acess")
    print(chave, chave_env, "endPoint: ", path)
    if request.method == "POST" and path in rotas_publicas:

        if chave != chave_env:
            return JSONResponse(
                status_code=401,
                content={"detail": "Não autorizado para criar empresa"}
            )

        return await call_next(request)

    empresa_uuid = request.headers.get("validation-uuid")

    if not empresa_uuid:
        return JSONResponse(
            status_code=401,
            content={"detail": "Header validation-uuid não informado"}
        )

    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT 1 FROM empresa WHERE uuid = %s LIMIT 1
        """, (empresa_uuid,))

        if not cur.fetchone():
            return JSONResponse(
                status_code=401,
                content={"detail": "Empresa não autorizada"}
            )

        request.state.empresa_uuid = empresa_uuid

    finally:
        put_conn(conn)

    return await call_next(request)
# app.include_router(email_routes)

# def create_access_token(data: dict):
#     to_encode = data.copy()
#     expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
#     to_encode.update({"exp": expire})
#     return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

@app.get("/")
def read_root():
    return {"status": "ok"}

def create_tables():
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""CREATE TABLE IF NOT EXISTS venda
(
    id text COLLATE pg_catalog."default" NOT NULL,
    empresauuid text,
    forma_pagamento text COLLATE pg_catalog."default",
    valor numeric(10,2),
    data text COLLATE pg_catalog."default",
    atualizadoem bigint,
    sincronizado boolean DEFAULT true,
    datacadastro text COLLATE pg_catalog."default",
    deletado boolean
)
            """)
        
        cur.execute("""CREATE TABLE IF NOT EXISTS servico
(
    id text COLLATE pg_catalog."default" NOT NULL,
    empresauuid text COLLATE pg_catalog."default" NOT NULL,
    nome text COLLATE pg_catalog."default" NOT NULL,
    preco numeric(10,2),
    preco_anterior numeric(10,2),
    data_criacao text COLLATE pg_catalog."default",
    tipo text COLLATE pg_catalog."default",
    pendentesync boolean,
    atualizadoem bigint,
    deletado boolean
)""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS perfil(
            id TEXT PRIMARY KEY NOT NULL,
            url TEXT
            )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS pdfvenda
(
    id text COLLATE pg_catalog."default" NOT NULL,
    empresaUuid text COLLATE pg_catalog."default" NOT NULL,
    venda_id text COLLATE pg_catalog."default",
    caminho_pdf text COLLATE pg_catalog."default",
    data_geracao BIGINT COLLATE pg_catalog."default",
    hora_geracao text COLLATE pg_catalog."default"
)""")

        cur.execute("""CREATE TABLE IF NOT EXISTS pagamentos
(
    id text COLLATE pg_catalog."default" NOT NULL,
    empresauuid text COLLATE pg_catalog."default" NOT NULL,
    data text COLLATE pg_catalog."default",
    valor numeric(10,2),
    motivo text COLLATE pg_catalog."default",
    atualizadoem bigint,
    pendentesync boolean,
    deletado boolean
    )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS itenvendas
(
    id text COLLATE pg_catalog."default" NOT NULL,
    venda_id text COLLATE pg_catalog."default",
    tipo text COLLATE pg_catalog."default",
    nome text COLLATE pg_catalog."default",
    valor numeric(10,2),
    quantidade integer
)""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS debitosclienteety
(
    id text COLLATE pg_catalog."default" NOT NULL,
    empresauuid text COLLATE pg_catalog."default" NOT NULL,
    codigo_cliente text COLLATE pg_catalog."default",
    periodo text COLLATE pg_catalog."default",
    valor text COLLATE pg_catalog."default",
    situacao text COLLATE pg_catalog."default",
    atualizadoem bigint,
    pendentesync boolean DEFAULT true,
    deletado boolean DEFAULT false
)""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS clientes
(
    id text COLLATE pg_catalog."default" NOT NULL,
    nome text COLLATE pg_catalog."default",
    telefone text COLLATE pg_catalog."default",
    email text COLLATE pg_catalog."default",
    endereco text COLLATE pg_catalog."default",
    totaldebitos text COLLATE pg_catalog."default",
    atualizadoem bigint,
    pendentesync boolean DEFAULT true,
    deletado boolean DEFAULT false,
    empresauuid text COLLATE pg_catalog."default"
    
)""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS empresa
(
    uuid text COLLATE pg_catalog."default" NOT NULL,
    cnpj text COLLATE pg_catalog."default",
    razaosocial text COLLATE pg_catalog."default",
    nomefantasia text COLLATE pg_catalog."default",
    municipio text COLLATE pg_catalog."default",
    uf text COLLATE pg_catalog."default",
    cnae text COLLATE pg_catalog."default",
    ativo boolean,
    bloqueado boolean,
    motivobloqueio text COLLATE pg_catalog."default",
    plano text COLLATE pg_catalog."default",
    statusassinatura text COLLATE pg_catalog."default",
    datainicioassinatura text COLLATE pg_catalog."default",
    datafimassinatura text COLLATE pg_catalog."default",
    origemassinatura text COLLATE pg_catalog."default",
    datacadastro text COLLATE pg_catalog."default",
    dataatualizacao text COLLATE pg_catalog."default",
    sincronizado boolean DEFAULT false,
    id bigint NOT NULL GENERATED BY DEFAULT AS IDENTITY ( INCREMENT 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1 )
    )""")
        
        cur.execute("""CREATE TABLE IF NOT EXISTS validationemail
(
    email text COLLATE pg_catalog."default" NOT NULL,
    codigo text COLLATE pg_catalog."default" NOT NULL,
    valida boolean DEFAULT true
)"""
        )
        
        cur.execute("""CREATE TABLE IF NOT EXISTS usuariomei
(
    uuid text COLLATE pg_catalog."default" NOT NULL,
    email text COLLATE pg_catalog."default" NOT NULL,
    senhahash text COLLATE pg_catalog."default" NOT NULL,
    nome text COLLATE pg_catalog."default",
    empresauuid text COLLATE pg_catalog."default",
    ativo boolean,
    datacadastro bigint,
    ultimologin bigint
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
    
    
# -------------------------------------------------------------------------------------
# CLIENTES
# =====================================================================================
@app.post("/clientes")
def create_cliente(data: ClienteIn):
    if exists("clientes", "id", data.id):
        raise HTTPException(409, "Cliente já existe")
    
    #     uuid: str
    # empresaUuid: str
    # nome: str
    # telefone: Optional[str] = None
    # email: Optional[str] = None
    # endereco: Optional[str] = None
    # totalDebitos: str
    # atualizadoEm : int
    # pendenteSync : bool
    # deletado: bool

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clientes (id, empresauuid, nome, telefone, email, endereco, totaldebitos, atualizadoem, pendentesync, deletado)
            VALUES (%s,%s,%s,%s,%s,%s, %s, %s, %s, %s)
        """, (
            data.id, data.empresaUuid, data.nome, data.telefone,
            data.email, data.endereco, data.totalDebitos, data.atualizadoEm, data.pendenteSync, data.deletado
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/clientes")
def list_clientes(empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
        SELECT
        id,
        empresauuid,
        nome,
        telefone,
        email,
        endereco,
        totaldebitos,
        atualizadoem,
        pendentesync,
        deletado
        FROM clientes where empresauuid = %s
        """, (empresa_atual,))

        rows = cur.fetchall()

        clientes = []

        for r in rows:
            clientes.append(ClienteIn(
                id=r[0],
                empresaUuid=r[1],
                nome=r[2],
                telefone=r[3],
                email=r[4],
                endereco=r[5],
                totalDebitos=r[6],
                atualizadoEm=r[7],
                pendenteSync=r[8],
                deletado=r[9]
            ))
            
        print("Clientes: ", clientes)
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
        "SELECT 1 FROM debitosclienteEty WHERE codigo_cliente = %s AND situacao = 'PENDENTE' LIMIT 1",
        (id,)
        )
        if cur.fetchone():
            raise HTTPException(409, "Cliente possui débitos vinculados")

        cur.execute("DELETE FROM clientes WHERE id = %s", (id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)
# =====================================================================================

# -------------------------------------------------------------------------------------
# VENDAS
# =====================================================================================
@app.post("/vendas")
def create_venda(data: VendaIn):
    if exists("venda", "id", data.id):
        raise HTTPException(409, "Venda já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO venda (id, empresauuid, forma_pagamento, valor, data, sincronizado, datacadastro, atualizadoem, deletado)
            VALUES (%s,%s,%s,%s, %s, %s, %s, %s)
        """, (
            data.id,data.empresaUuid, data.formaPagamento, data.valor, data.data, data.sincronizado, data.dataCadastro, data.atualizadoEm, data.deletado
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/vendas")
def list_vendas(empresa_atual: str = Depends(get_empresa)):    
    conn = get_conn()
    try:
        cur = conn.cursor()
        print("empresa atual: ", empresa_atual)
        cur.execute("SELECT id, empresauuid, forma_pagamento, valor, data, sincronizado, datacadastro, atualizadoem, deletado FROM venda where empresauuid = %s", (empresa_atual,))
        
        result = cur.fetchall()
        vendas = []
        for row in result:
            vendas.append(VendaIn(id= row[0], 
                                  empresaUuid= row[1],
                                  formaPagamento=row[2],
                                  valor=row[3],
                                  data=row[4],
                                  sincronizado=row[5],
                                  dataCadastro=row[6],
                                  atualizadoEm=row[7],
                                  deletado=row[8]
                                  ))
        
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
            "DELETE FROM itenvendas WHERE venda_id = %s",
            (id,)
        )

        cur.execute("DELETE FROM venda WHERE id = %s", (id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)


@app.post("/venda-completa")
def create_venda_completa(data: VendaCompletaIn):

    if exists("venda", "id", data.venda.id):
        raise HTTPException(409, "Venda já existe")

    conn = get_conn()

    try:
        cur = conn.cursor()

        # -------- VENDA --------
        cur.execute("""
            INSERT INTO venda
            (id, empresauuid, forma_pagamento, valor, data, sincronizado, datacadastro, atualizadoem, deletado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data.venda.id,
            data.venda.empresaUuid,
            data.venda.formaPagamento,
            data.venda.valor,
            data.venda.data,
            data.venda.sincronizado,
            data.venda.dataCadastro,
            data.venda.atualizadoEm,
            data.venda.deletado
        ))

        # -------- ITENS --------
        for item in data.itens:

            if exists("itenvendas", "id", item.id):
                raise HTTPException(409, f"Item {item.id} já existe")

            cur.execute("""
                INSERT INTO itenvendas
                (id, venda_id, tipo, nome, valor, quantidade)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (
                item.id,
                item.vendaId,
                item.tipo,
                item.nome,
                item.valor,
                item.quantidade
            ))

        # # -------- PDF --------
        # if not exists("pdfvenda", "id", data.pdf.id):

        #     cur.execute("""
        #         INSERT INTO pdfvenda
        #         (id, empresauuid, venda_id, caminho_pdf, data_geracao, hora_geracao)
        #         VALUES (%s,%s,%s,%s,%s,%s)
        #     """, (
        #         data.pdf.id,
        #         data.pdf.empresaUuid,
        #         data.pdf.vendaId,
        #         data.pdf.caminhoPdf,
        #         data.pdf.dataGeracao,
        #         data.pdf.horaGeracao
        #     ))

        conn.commit()

        return {"status": "venda completa criada"}

    except Exception as e:
        conn.rollback()
        raise e

    finally:
        put_conn(conn)

# =====================================================================================

# -------------------------------------------------------------------------------------
# ITENS VENDA
# =====================================================================================
@app.post("/vendas/itens")
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

@app.get("/vendas/itens")
def list_itens_venda(empresa_atual: str = Depends(get_empresa)):
    # if not exists():
    #     raise HTTPException(404, "Venda não encontrada")

    conn = get_conn()
    try:
        cur = conn.cursor()
                 
        cur.execute("""
        SELECT iv.id,
           iv.venda_id,
           iv.tipo,
           iv.nome,
           iv.valor,
           iv.quantidade
        FROM itenvendas iv
        JOIN venda v ON iv.venda_id = v.id
        WHERE v.empresauuid = %s
        """, (empresa_atual,))
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
        
@app.delete("/vendas/itens/{id}")
def delete_item_venda(id: str):
    return delete_by_id("itenvendas", id)
# =====================================================================================


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
# -------------------------------------------------------------------------------------
# SERVIÇOS
# =====================================================================================
@app.post("/servicos")
def create_servico(data: ServicoIn):
    if exists("servico", "id", data.id):
        raise HTTPException(409, "Serviço já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO servico
            (id, nome, preco, preco_anterior, data_criacao, tipo, empresaUuid, pendenteSync, atualizadoEm, deletado)
            VALUES (%s,%s,%s,%s,%s,%s, %s, %s, %s, %s)
        """, (
            data.id, data.nome, data.preco,
            data.precoAnterior, data.dataCriacao, data.tipo, data.empresaUuid, data.pendenteSync, data.atualizadoEm, data.deletado
        ))
        
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/servicos")
def list_servicos(empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, preco, preco_anterior, data_criacao, tipo, empresauuid, pendenteSync, atualizadoEm, deletado FROM servico where empresauuid = %s", (empresa_atual,))
        rows = cur.fetchall()

        return [
            ServicoIn(
                id=r[0],
                nome=r[1],
                preco=r[2],
                precoAnterior=r[3],
                dataCriacao=r[4],
                tipo=r[5],
                empresaUuid= r[6],
                pendenteSync=r[7],
                atualizadoEm=r[8],
                deletado=r[9]
            ) for r in rows
        ]
    finally:
        put_conn(conn)

@app.delete("/servicos/{id}")
def delete_servico(id: str):
    return delete_by_id("servico", id)

@app.put("/servicos")
def update_servico(data: ServicoIn):

    if not exists("servico", "id", data.id):
        raise HTTPException(404, "Serviço não encontrado")

    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
            UPDATE servico
            SET
                nome = %s,
                preco = %s,
                preco_anterior = %s,
                data_criacao = %s,
                tipo = %s,
                empresaUuid = %s,
                pendenteSync = %s,
                atualizadoEm = %s,
                deletado = %s
            WHERE id = %s
        """, (
            data.nome,
            data.preco,
            data.precoAnterior,
            data.dataCriacao,
            data.tipo,
            data.empresaUuid,
            data.pendenteSync,
            data.atualizadoEm,
            data.deletado,
            data.id
        ))

        conn.commit()

        return {"status": "atualizado"}

    finally:
        put_conn(conn)

@app.get("/servicos/{id}", response_model=ServicoIn)
def get_servico(id: str, empresa_atual: str = Depends(get_empresa)):
    
    conn = get_conn()
    
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT id,
                   empresauuid,
                   nome,
                   preco,
                   preco_anterior,
                   data_criacao,
                   tipo,
                   pendentesync,
                   atualizadoem,
                   deletado
            FROM public.servico
            WHERE id = %s and empresauuid = %s
        """, (id, empresa_atual))
        

        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Serviço não encontrado")

        servico = {
            "id": row[0],
            "empresaUuid": row[1],
            "nome": row[2],
            "preco": float(row[3]),
            "precoAnterior": float(row[4]) if row[4] else 0,
            "dataCriacao": row[5],
            "tipo": row[6],
            "pendenteSync": row[7],
            "atualizadoEm": row[8],
            "deletado": row[9]
        }

        return servico

    finally:
        put_conn(conn)
# =====================================================================================

# -------------------------------------------------------------------------------------
# PERFIL
# =====================================================================================
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
def list_perfil(empresa_atual: str = Depends(get_empresa)):
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
# =====================================================================================

# -------------------------------------------------------------------------------------
# PDF VENDA
# =====================================================================================
@app.post("/pdfVenda")
def create_pdf_venda(data: PdfVendaIn):
    if exists("pdfvenda", "id", data.id):
        raise HTTPException(409, "PDF da venda já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pdfvenda
            (id, empresauuid, venda_id, caminho_pdf, data_geracao, hora_geracao)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            data.id, data.empresaUuid, data.vendaId, data.caminhoPdf,
            data.dataGeracao, data.horaGeracao
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/pdfVenda")
def list_pdf_venda(empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, empresauuid, venda_id, caminho_pdf, data_geracao, hora_geracao FROM pdfvenda")
        rows = cur.fetchall()

        return [
            PdfVendaIn(
                id=r[0],
                empresaUuid=r[1],
                vendaId=r[2],
                caminhoPdf=r[3],
                dataGeracao=r[4],
                horaGeracao=r[5]
            ) for r in rows
        ]
    finally:
        put_conn(conn)

@app.delete("/pdfVenda/{id}")
def delete_pdf_venda(id: str):
    return delete_by_id("pdfvenda", id)
# =====================================================================================

# -------------------------------------------------------------------------------------
# PAGAMENTOS
# =====================================================================================
@app.post("/pagamentos")
def create_pagamento(data: PagamentoIn):
    if exists("pagamentos", "id", data.id):
        raise HTTPException(409, "Pagamento já foi efetuado")

            # id TEXT PRIMARY KEY NOT NULL,
            # empresauuid TEXT NOT NULL,
            # data  TEXT,
            # valor DECIMAL(10,2),
            # motivo TEXT,
            # atualizadoem INTEGER,
            # pendentesync bool,
            # deletado bool
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pagamentos
            (id, empresauuid, data, valor, motivo, atualizadoem, pendentesync, deletado)
            VALUES (%s,%s,%s,%s, %s, %s, %s,%s)
        """, (
            (
    data.id,
    data.empresaUuid,
    data.data,
    data.valor,
    data.motivo,
    data.atualizadoEm,
    data.pendenteSync,
    data.deletado
)
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)
        
@app.put("/pagamentos")
def update_pagamento(data: PagamentoIn):

    if not exists("pagamentos", "id", data.id):
        raise HTTPException(404, "Pagamento não encontrado")

    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
            UPDATE pagamentos
            SET
                empresauuid = %s,
                data = %s,
                valor = %s,
                motivo = %s,
                atualizadoem = %s,
                pendentesync = %s,
                deletado = %s
            WHERE id = %s
        """, (
            data.empresaUuid,
            data.data,
            data.valor,
            data.motivo,
            data.atualizadoEm,
            data.pendenteSync,
            data.deletado,
            data.id
        ))

        conn.commit()

        return {"status": "atualizado"}

    finally:
        put_conn(conn)

@app.get("/pagamentos")
def list_pagamentos(empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pagamentos where empresauuid = %s", (empresa_atual,))
        rows = cur.fetchall()

        return [
            PagamentoIn(
                id=r[0],
                empresaUuid = r[1],
                data=r[2],
                valor=r[3],
                motivo=r[4],
                atualizadoEm= r[5],
                pendenteSync= r[6],
                deletado=r[7]
            ) for r in rows
        ]
    finally:
        put_conn(conn)

@app.delete("/pagamentos/{id}")
def delete_pagamento(id: str):
    return delete_by_id("pagamentos", id)
# =====================================================================================

# -------------------------------------------------------------------------------------
# DEBITOS CLIENTE
# =====================================================================================
@app.post("/debitos-cliente")
def create_debito_cliente(data: DebitoClienteIn):
    if exists("debitosclienteEty", "id", data.id):
        raise HTTPException(409, "Débito já existe")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO debitosclienteEty
            (id,empresauuid, codigo_cliente, periodo, valor, situacao, atualizadoem, pendentesync, deletado)
            VALUES (%s,%s,%s,%s,%s, %s, %s, %s, %s)
        """, (
            data.id, data.empresaUuid, data.codigoCliente,
            data.periodo, data.valor, data.situacao, data.atualizadoEm, data.pendenteSync, data.deletado
        ))
        conn.commit()
        return {"status": "ok"}
    finally:
        put_conn(conn)

@app.get("/debitos-cliente")
def list_debitos_cliente(empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM debitosclienteEty where empresauuid = %s", (empresa_atual,))
        rows = cur.fetchall()

        return [
            DebitoClienteIn(
                id=r[0],
                empresaUuid = r[1],
                codigoCliente=r[2],
                periodo=r[3],
                valor=r[4],
                situacao=r[5],
                atualizadoEm=r[6],
                pendenteSync=r[7],
                deletado=r[8]
            ) for r in rows
        ]
    finally:
        put_conn(conn)

@app.delete("/debitos-cliente/{id}")
def delete_debito_cliente(id: str):
    return delete_by_id("debitosclienteEty", id)

@app.put("/debitos-cliente")
def update_debito_cliente(data: DebitoClienteIn):

    if not exists("debitosclienteEty", "id", data.id):
        raise HTTPException(404, "Débito não encontrado")

    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            UPDATE debitosclienteEty
            SET empresauuid = %s,
                codigo_cliente = %s,
                periodo = %s,
                valor = %s,
                situacao = %s,
                atualizadoem = %s,
                pendentesync = %s,
                deletado = %s
            WHERE id = %s
        """, (
            data.empresaUuid,
            data.codigoCliente,
            data.periodo,
            data.valor,
            data.situacao,
            data.atualizadoEm,
            data.pendenteSync,
            data.deletado,
            data.id
        ))

        conn.commit()

        return {"status": "atualizado"}

    finally:
        put_conn(conn) 
# =====================================================================================


# -------------------------------------------------------------------------------------
# EMPRESA
# =====================================================================================
# @app.post("/empresa")
# def criar_empresa(empresa:Empresa):
#     if exists("empresa", "uuid", empresa.uuid):
#         raise HTTPException(409, "Empresa já cadastrada")
    

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#         INSERT INTO empresa (
#             uuid, cnpj, razaoSocial, nomefantasia, municipio,
#             uf, cnae, ativo, bloqueado, motivobloqueio, plano,
#             statusassinatura, datainicioassinatura, datafimassinatura,
#             origemassinatura, datacadastro, dataatualizacao
#         ) VALUES (
#             %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s
#         )
#     """,     (empresa.uuid,
#     empresa.cnpj,
#     empresa.razaoSocial,
#     empresa.nomeFantasia,
#     empresa.municipio,
#     empresa.uf,
#     empresa.cnae,
#     empresa.ativo,
#     empresa.bloqueado,
#     empresa.motivoBloqueio,
#     empresa.plano,
#     empresa.statusAssinatura,
#     empresa.dataInicioAssinatura,
#     empresa.dataFimAssinatura,
#     empresa.origemAssinatura,
#     empresa.dataCadastro,
#     empresa.dataAtualizacao))
#         conn.commit()
#         return {"msg": "Empresa cadastrada com sucesso"}
#     finally:
#         put_conn(conn)
        

        
@app.post("/empresa")
def criar_empresa(data: Empresa):
    """cadastrar empresa na base de dados"""

    if exists("empresa", "uuid", data.uuid) or exists("empresa", "cnpj", data.cnpj):
        print("Empresa já existe: ", data.cnpj)
        return {"sucesso": False, "mensagem": "Empresa já existe no sistema"}



    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO empresa (
                uuid, cnpj, razaoSocial, nomeFantasia,
                municipio, uf, cnae, ativo, bloqueado,
                motivoBloqueio, plano, statusAssinatura,
                dataInicioAssinatura, dataFimAssinatura,
                origemAssinatura, dataCadastro, dataAtualizacao,
                sincronizado
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data.uuid,
            data.cnpj,
            data.razaoSocial,
            data.nomeFantasia,
            data.municipio,
            data.uf,
            data.cnae,
            data.ativo,
            data.bloqueado,
            data.motivoBloqueio,
            data.plano,
            data.statusAssinatura,
            data.dataInicioAssinatura,
            data.dataFimAssinatura,
            data.origemAssinatura,
            data.dataCadastro,
            data.dataAtualizacao,
            data.sincronizado
        ))

        conn.commit()

        return {"sucesso": True, "mensagem": "Empresa criada"}

    finally:
        put_conn(conn)

@app.get("/empresa")
def get_empresa(empresa_atual: str = Depends(get_empresa)):
    TOKEN_API = os.getenv('key_first_acess')
     
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT nomefantasia, cnpj FROM empresa where ativo = True where uuid = %s", (empresa_atual,))
        row = cur.fetchall()
        
        empresas = []
        
        for r in row:
            empresas.append(getEmpresa(
                nomeFantasia=r[0],
                cnpj=r[1]
                ))
        return empresas
    finally:
        put_conn(conn)

@app.put("/empresa")
def atualizar_empresa(data: Empresa):
    """Atualizar empresa e refletir no usuariomei"""

    if not exists("empresa", "uuid", data.uuid):
        return {"sucesso": False, "mensagem": "Empresa não encontrada"}

    conn = get_conn()

    try:
        cur = conn.cursor()

        # 🔥 Atualiza empresa
        cur.execute("""
            UPDATE empresa SET
                cnpj = %s,
                razaoSocial = %s,
                nomeFantasia = %s,
                municipio = %s,
                uf = %s,
                cnae = %s,
                ativo = %s,
                bloqueado = %s,
                motivoBloqueio = %s,
                plano = %s,
                statusAssinatura = %s,
                dataInicioAssinatura = %s,
                dataFimAssinatura = %s,
                origemAssinatura = %s,
                dataAtualizacao = %s,
                sincronizado = %s
            WHERE uuid = %s
        """, (
            data.cnpj,
            data.razaoSocial,
            data.nomeFantasia,
            data.municipio,
            data.uf,
            data.cnae,
            data.ativo,
            data.bloqueado,
            data.motivoBloqueio,
            data.plano,
            data.statusAssinatura,
            data.dataInicioAssinatura,
            data.dataFimAssinatura,
            data.origemAssinatura,
            data.dataAtualizacao,
            data.sincronizado,
            data.uuid
        ))

        #  Atualizando usuariomei
        cur.execute("""
            UPDATE usuariomei
            SET nome = %s
            WHERE empresauuid = %s
        """, (
            data.nomeFantasia,  # ou razaoSocial
            data.uuid
        ))

        conn.commit()

        return {"sucesso": True, "mensagem": "Empresa atualizada com sucesso"}

    except Exception as e:
        conn.rollback()
        return {"sucesso": False, "mensagem": str(e)}

    finally:
        put_conn(conn)

@app.delete("/empresa/{id}")
def deletar_empresa(id: str):

    if not exists("empresa", "uuid", id):
        raise HTTPException(404, "Empresa não encontrada")

    conn = get_conn()

    try:
        cur = conn.cursor()

        # Deleta usuários da empresa
        cur.execute("""
            DELETE FROM usuariomei
            WHERE empresauuid = %s
        """, (id,))

        # Deleta a empresa
        cur.execute("""
            delete from  empresa
            WHERE uuid = %s
        """, (id,))

        cur.execute("""
                    delete from servico where empresauuid = %s
                    """, (id,))
        
        cur.execute("""
            delete from debitosclienteety where empresauuid = %s
            """, (id,))
        conn.commit()

        return {"status": "ok", "mensagem": "Empresa e usuários deletados"}

    except Exception as e:
        conn.rollback()
        raise e

    finally:
        put_conn(conn)

# =====================================================================================

# -------------------------------------------------------------------------------------
# EMAIL
# =====================================================================================   
def validar_email(email:str):
    regex = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(regex, email)

@app.post("/email")
def receber_email(email: receiverEmail):
    """Receber o email para validar se a formatação é válida e enviar código"""

    if exists("empresa", "cnpj", email.cnpj) :
        return {"sucesso": False, "mensagem": "Cnpj já foi cadastrado anteriormente"}
    elif exists("usuariomei", "email", email.email):
        return {"sucesso": False, "mensagem": "Email já foi cadastrado anteriormente"}
# Adicionar timeOut para tirar código da fila de validade
    if not validar_email(email.email):

        raise HTTPException(400, "Modelo não reconhecido como email")

    codigo = randrange(100000, 999999)

    while exists("validationEmail", "codigo", str(codigo)):
        codigo = randrange(100000, 999999)

    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO validationEmail (email, codigo, valida)
        VALUES (%s, %s, %s)
            """,    
            (email.email, str(codigo), True)
        )

        conn.commit()
        # enviado = True
        mensagem = f"""
    <div style="background:#f4f6f8;padding:40px 20px;font-family:Arial,sans-serif;">
        
        <div style="
            max-width:500px;
            margin:auto;
            background:white;
            border-radius:10px;
            padding:30px;
            text-align:center;
            box-shadow:0 4px 12px rgba(0,0,0,0.1);
        ">

            <h2 style="color:#0b4a47;margin-bottom:10px;">
                Verificação de Email
            </h2>

            <p style="color:#555;font-size:15px;">
                Para concluir sua verificação de email, utilize o código abaixo:
            </p>

            <div style="
                font-size:32px;
                letter-spacing:6px;
                font-weight:bold;
                color:white;
                background:#1cc7b5;
                padding:15px;
                border-radius:8px;
                margin:25px 0;
            ">
                {codigo}
            </div>

            <p style="color:#666;font-size:14px;">
                Este código é válido por tempo limitado.
         </p>

            <hr style="margin:25px 0;border:none;border-top:1px solid #eee;">

            <p style="font-size:13px;color:#888;">
                Caso você não tenha solicitado esta verificação,<br>
                ignore esta mensagem.
            </p>

            <p style="margin-top:25px;font-weight:bold;color:#0b4a47;">
                Equipe Caltech
            </p>

        </div>

    </div>
"""
        print(os.getenv("EMAIL_USER"))
        print(os.getenv("EMAIL_PASSWORD"))
        enviado = email_service.enviar_email(
            email.email,
            "Código de validação de email",
            mensagem
            )

        if enviado:
            return {"sucesso": True, "mensagem": "Email enviado com sucesso"}
        else:
            return {"sucesso": False, "mensagem": "Falha ao enviar email"}

    finally:
        put_conn(conn)   
          
@app.post("/validaEmail")
def validaEmail(data: ValidarEmailIn):
    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT valida
            FROM validationEmail
            WHERE email = %s AND codigo = %s
        """, (data.email, data.codigo))

        row = cur.fetchone()

        if not row:
            return {
                "sucesso": False,
                "mensagem": "Código ou email inválido"
            }

        if not row[0]:
            return {
                "sucesso": False,
                "mensagem": "Código já utilizado"
            }

        # marcar como usado
        cur.execute("""
            UPDATE validationEmail
            SET valida = FALSE
            WHERE email = %s AND codigo = %s
        """, (data.email, data.codigo))

        conn.commit()

        return {
            "sucesso": True,
            "mensagem": "Email validado com sucesso"
        }

    finally:
        put_conn(conn)
# =====================================================================================
    

# -------------------------------------------------------------------------------------
# USUÁRIOS
# =====================================================================================
@app.post("/usuarios")
def create_usuario(data: Usuario):

    if exists("usuarioMei", "uuid", data.uuid) :
        raise HTTPException(409, "Usuário já existe")

    if exists("usuarioMei", "email", data.email) :
        raise HTTPException(409, "Email já associado à um usuário")
    
    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO usuarioMei
            (uuid, email, senhahash, nome, empresauuid, ativo, datacadastro, ultimologin)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data.uuid,
            data.email,
            data.senhaHash,
            data.nome,
            data.empresaUuid,
            data.ativo,
            data.dataCadastro,
            data.ultimoLogin
        ))

        conn.commit()

        return {"status": "ok"}

    finally:
        put_conn(conn)
        
@app.put("/usuarios")
def update_usuario(data: Usuario):

    if not exists("usuario", "uuid", data.uuid):
        raise HTTPException(404, "Usuário não encontrado")

    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
            UPDATE usuarioMei
            SET
                email = %s,
                senhahash = %s,
                nome = %s,
                empresauuid = %s,
                ativo = %s,
                datacadastro = %s,
                ultimologin = %s
            WHERE uuid = %s
        """, (
            data.email,
            data.senhaHash,
            data.nome,
            data.empresaUuid,
            data.ativo,
            data.dataCadastro,
            data.ultimoLogin,
            data.uuid
        ))

        conn.commit()

        return {"status": "atualizado"}

    finally:
        put_conn(conn)
        
@app.get("/usuarios")
def get_usuarios(empresa_atual: str = Depends(get_empresa)):

    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT uuid, email, senhahash, nome, empresauuid, ativo, datacadastro, ultimologin
            FROM usuario where empresauuid = %s
        """, (empresa_atual))

        rows = cur.fetchall()

        usuarios = []

        for row in rows:
            usuarios.append({
                "uuid": row[0],
                "email": row[1],
                "senhaHash": row[2],
                "nome": row[3],
                "empresaUuid": row[4],
                "ativo": row[5],
                "dataCadastro": row[6],
                "ultimoLogin": row[7]
            })

        return usuarios

    finally:
        put_conn(conn)  
              
@app.delete("/usuarios/{uuid}")
def delete_usuario(uuid: str):

    if not exists("usuario", "uuid", uuid):
        raise HTTPException(404, "Usuário não encontrado")

    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
            DELETE FROM usuarioMei
            WHERE uuid = %s
        """, (uuid,))

        conn.commit()

        return {"status": "deletado"}

    finally:
        put_conn(conn)
 # =====================================================================================


# -------------------------------------------------------------------------------------
# LOGIN
# =====================================================================================
@app.post("/login", response_model=loginResponse)
def login(data: loginIn):

    conn = get_conn()
    try:
        cur = conn.cursor()

        # buscar usuário pelo email
        cur.execute("""
            SELECT uuid, email, senhahash, nome, empresauuid, ativo, datacadastro, ultimologin
            FROM usuarioMei
            WHERE email = %s AND ativo = True
        """, (data.login,))

        user = cur.fetchone()

        # se não achou pelo email, tenta pelo CNPJ da empresa
        if not user:
            cur.execute("""
                SELECT u.uuid, u.email, u.senhahash, u.nome, u.empresauuid, u.ativo, u.datacadastro, u.ultimologin
                FROM usuarioMei u
                JOIN empresa e ON e.uuid = u.empresauuid
                WHERE e.cnpj = %s
            """, (data.login,))

            user = cur.fetchone()

        if not user:
            raise HTTPException(401, "Usuário não encontrado")

        # verificar senha
        # senha_hash = hashlib.sha256(data.senha.encode()).hexdigest()

        if data.senha != user[2]:
            raise HTTPException(402, "Senha incorreta")

        # buscar empresa
        cur.execute("""
            SELECT id, uuid, cnpj, razaosocial, nomefantasia, municipio, uf,
                   cnae, ativo, bloqueado, motivobloqueio, plano,
                   statusassinatura, datainicioassinatura, datafimassinatura,
                   origemassinatura, datacadastro, dataatualizacao, sincronizado
            FROM empresa
            WHERE uuid = %s
        """, (user[4],))

        emp = cur.fetchone()

        if not emp:
            raise HTTPException(404, "Empresa não encontrada")

        usuario_obj = Usuario(
            uuid=user[0],
            email=user[1],
            senhaHash=user[2],
            nome=user[3],
            empresaUuid=user[4],
            ativo=user[5],
            dataCadastro=user[6],
            ultimoLogin=user[7],
            
        )

        empresa_obj = Empresa(
            id=emp[0],
            uuid=emp[1],
            cnpj=emp[2],
            razaoSocial=emp[3],
            nomeFantasia=emp[4],
            municipio=emp[5],
            uf=emp[6],
            cnae=emp[7],
            ativo=emp[8],
            bloqueado=emp[9],
            motivoBloqueio=emp[10],
            plano=emp[11],
            statusAssinatura=emp[12],
            dataInicioAssinatura=emp[13],
            dataFimAssinatura=emp[14],
            origemAssinatura=emp[15],
            dataCadastro=emp[16],
            dataAtualizacao=emp[17],
            sincronizado=emp[18]
        )

        return loginResponse(
            sucesso= "True",
            mensagem="Login realizado com sucesso",
            usuario=usuario_obj,
            empresa=empresa_obj
        )

    finally:
        put_conn(conn)
        
@app.post("/codigoSenha")
def get_senha(email: receiverEmail):
    """EndPoint para recuperar senha"""
    if exists("usuarioMei", "email", email.email) == False:
        raise HTTPException(404, "Email inexistente")
    
    codigo = randrange(100000, 999999)
    
    conn = get_conn()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO validationEmail (email, codigo, valida)
        VALUES (%s, %s, %s)
            """,    
            (email.email, str(codigo), True)
        )

        conn.commit()
   
        mensagem = f"""
        <div style="background:#f4f6f8;padding:40px 20px;font-family:Arial,sans-serif;">
            
            <div style="
                max-width:500px;
                margin:auto;
                background:white;
                border-radius:10px;
                padding:30px;
                text-align:center;
                box-shadow:0 4px 12px rgba(0,0,0,0.1);
            ">

                <h2 style="color:red;margin-bottom:10px;">
                    Verificação de Email
                </h2>

                <p style="color:#fff;font-size:15px;">
                    Para redefinir sua senha de acesso, utilize o código abaixo:
                </p>

                <div style="
                    font-size:32px;
                    letter-spacing:6px;
                    font-weight:bold;
                    color:white;
                    background:#1cc7b5;
                    padding:15px;
                    border-radius:8px;
                    margin:25px 0;
                ">
                    {codigo}
                </div>

                <p style="color:#666;font-size:14px;">
                    Este código é válido por tempo limitado.
            </p>

                <hr style="margin:25px 0;border:none;border-top:1px solid #eee;">

                <p style="font-size:13px;color:#888;">
                    Caso você não tenha solicitado esta verificação,<br>
                    ignore esta mensagem.
                </p>

                <p style="margin-top:25px;font-weight:bold;color:#0b4a47;">
                    Equipe Caltech
                </p>

            </div>

        </div>
    """
        print(os.getenv("EMAIL_USER"))
        print(os.getenv("EMAIL_PASSWORD"))
        enviado = email_service.enviar_email(
            email.email,
            "Código para redefinir senha",
            mensagem
            )
        
        if enviado:
            return {"sucesso": True, "mensagem": "Email para redefinir senha enviado com sucesso"}
        else:
            return {"sucesso": False, "mensagem": "Falha ao enviar email para redefinir senha"}

    finally:
        put_conn(conn)    
        
@app.post("/redefinirSenha")
def redefinir_senha(valida: ValidarSenha):
    conn = get_conn()

    try:
        cur = conn.cursor()

        # marcar como usado
        cur.execute("""
            UPDATE usuariomei
            SET senhahash = %s
            WHERE email = %s
        """, (valida.novaSenha, valida.email))

        conn.commit()

        return {
            "sucesso": True,
            "mensagem": "Senha redefinida com sucesso"
        }

    finally:
        put_conn(conn)
    
    
