import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from jose import JWTError, jwt
from passlib.context import CryptContext
from psycopg2.extras import RealDictCursor

from db import get_conn, put_conn
from .schemas import LoginIn, SetupIn, SupplierIn


router = APIRouter(prefix="/api/fornecedores", tags=["Fornecedores"])
passwords = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


@contextmanager
def connection():
    conn = get_conn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def initialize_tables():
    sql = Path(__file__).with_name("migration.sql").read_text(encoding="utf-8")
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        _ensure_primary_user(cur)
        conn.commit()


def _primary_credentials():
    return (
        os.getenv("PRIMARY_ADMIN_NAME", "Cleber Dev"),
        os.getenv("PRIMARY_ADMIN_LOGIN", "Cleber Dev"),
        os.getenv("PRIMARY_ADMIN_PASSWORD", "02032002"),
    )


def _ensure_primary_user(cur):
    """Garante o acesso administrativo sem persistir senha em texto aberto."""
    name, login, password = _primary_credentials()
    cur.execute("SELECT id,nome,login,senha_hash FROM frn_usuario WHERE lower(login)=lower(%s)", (login,))
    user = cur.fetchone()
    if user:
        user_id, current_name, current_login, password_hash = user
        if current_name != name or current_login != login or not passwords.verify(password, password_hash):
            cur.execute("UPDATE frn_usuario SET nome=%s,login=%s,senha_hash=%s,ativo=TRUE WHERE id=%s",
                        (name, login, passwords.hash(password), user_id))
        return
    cur.execute("INSERT INTO frn_usuario (nome,login,senha_hash) VALUES (%s,%s,%s)",
                (name, login, passwords.hash(password)))


def secret():
    return os.getenv("FORNECEDORES_SECRET", os.getenv("SECRET_KEY", "fornecedores-local-change-me"))


def make_token(user_id: int):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=12)}, secret(), algorithm="HS256")


def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Autenticação necessária")
    try:
        user_id = int(jwt.decode(authorization[7:], secret(), algorithms=["HS256"])["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Sessão inválida ou expirada")
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,nome,login FROM frn_usuario WHERE id=%s AND ativo=TRUE", (user_id,))
        user = cur.fetchone()
    if not user:
        raise HTTPException(401, "Usuário não encontrado")
    return user


def audit(conn, request: Request, action: str, user_id=None, supplier_id=None, details=None):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO frn_auditoria (usuario_id,acao,fornecedor_id,detalhes,ip)
            VALUES (%s,%s,%s,%s,%s)""", (
            user_id, action, supplier_id, json.dumps(details or {}),
            request.client.host if request.client else None,
        ))


@router.get("/auth/status")
def auth_status():
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM frn_usuario)")
        return {"configurado": cur.fetchone()[0]}


@router.post("/auth/setup", status_code=201)
def setup(data: SetupIn, request: Request):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("LOCK TABLE frn_usuario IN EXCLUSIVE MODE")
        cur.execute("SELECT EXISTS(SELECT 1 FROM frn_usuario)")
        if cur.fetchone()["exists"]:
            raise HTTPException(409, "O acesso inicial já foi configurado")
        cur.execute("""INSERT INTO frn_usuario (nome,login,senha_hash) VALUES (%s,%s,%s)
            RETURNING id,nome,login""", (data.nome, data.login.lower(), passwords.hash(data.senha)))
        user = cur.fetchone()
        audit(conn, request, "CONFIGURACAO_INICIAL", user["id"])
        conn.commit()
        return {"token": make_token(user["id"]), "usuario": user}


@router.post("/auth/login")
def login(data: LoginIn, request: Request):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,nome,login,senha_hash FROM frn_usuario WHERE lower(login)=lower(%s) AND ativo=TRUE", (data.login.strip(),))
        user = cur.fetchone()
        if not user or not passwords.verify(data.senha, user["senha_hash"]):
            raise HTTPException(401, "Login ou senha inválidos")
        audit(conn, request, "LOGIN", user["id"])
        conn.commit()
        user.pop("senha_hash")
        return {"token": make_token(user["id"]), "usuario": user}


@router.get("")
def list_suppliers(busca: str = Query(default="", max_length=120), user=Depends(current_user)):
    term = f"%{busca.strip()}%"
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT * FROM frn_fornecedor WHERE usuario_id=%s AND ativo=TRUE
            AND (%s='' OR nome ILIKE %s OR documento ILIKE %s OR contato ILIKE %s)
            ORDER BY nome""", (user["id"], busca.strip(), term, term, term))
        return cur.fetchall()


@router.get("/cadastros/{supplier_id}")
def get_supplier(supplier_id: int, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM frn_fornecedor WHERE id=%s AND usuario_id=%s AND ativo=TRUE", (supplier_id, user["id"]))
        result = cur.fetchone()
        if not result:
            raise HTTPException(404, "Fornecedor não encontrado")
        return result


@router.post("", status_code=201)
def create_supplier(data: SupplierIn, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""INSERT INTO frn_fornecedor
            (usuario_id,nome,documento,contato,telefone,email,chave_pix,endereco,observacoes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (
            user["id"], data.nome, data.documento, data.contato, data.telefone,
            data.email, data.chave_pix, data.endereco, data.observacoes,
        ))
        result = cur.fetchone()
        audit(conn, request, "FORNECEDOR_CRIADO", user["id"], result["id"])
        conn.commit()
        return result


@router.put("/cadastros/{supplier_id}")
def update_supplier(supplier_id: int, data: SupplierIn, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""UPDATE frn_fornecedor SET nome=%s,documento=%s,contato=%s,telefone=%s,
            email=%s,chave_pix=%s,endereco=%s,observacoes=%s,atualizado_em=NOW()
            WHERE id=%s AND usuario_id=%s AND ativo=TRUE RETURNING *""", (
            data.nome, data.documento, data.contato, data.telefone, data.email,
            data.chave_pix, data.endereco, data.observacoes, supplier_id, user["id"],
        ))
        result = cur.fetchone()
        if not result:
            raise HTTPException(404, "Fornecedor não encontrado")
        audit(conn, request, "FORNECEDOR_ATUALIZADO", user["id"], supplier_id)
        conn.commit()
        return result


@router.delete("/cadastros/{supplier_id}", status_code=204)
def delete_supplier(supplier_id: int, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE frn_fornecedor SET ativo=FALSE,atualizado_em=NOW()
            WHERE id=%s AND usuario_id=%s AND ativo=TRUE""", (supplier_id, user["id"]))
        if not cur.rowcount:
            raise HTTPException(404, "Fornecedor não encontrado")
        audit(conn, request, "FORNECEDOR_REMOVIDO", user["id"], supplier_id)
        conn.commit()


@router.get("/auditoria/registros")
def audit_records(user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT id,acao,fornecedor_id,detalhes,ip,criado_em FROM frn_auditoria
            WHERE usuario_id=%s ORDER BY criado_em DESC LIMIT 100""", (user["id"],))
        return cur.fetchall()
