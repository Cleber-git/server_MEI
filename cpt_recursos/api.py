import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from jose import JWTError, jwt
from passlib.context import CryptContext
from psycopg2.extras import RealDictCursor

from db import get_conn, put_conn
from .schemas import (
    CampaignIn, LoginIn, OrderIn, PaymentIn, ProductIn, ResponsibleIn,
    SetupIn, TeamMemberIn,
)


router = APIRouter(prefix="/api/cpt_recursos", tags=["Captação de recursos"])
# PBKDF2 evita a limitação de 72 bytes do bcrypt e funciona com a versão
# atual do Python/bcrypt usada pelo servidor.
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
JWT_ALGORITHM = "HS256"


def _secret():
    return os.getenv("CPT_RECURSOS_SECRET", os.getenv("SECRET_KEY", "cpt-recursos-local-change-me"))


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
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def _token(user_id: int):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=12)}, _secret(), algorithm=JWT_ALGORITHM)


def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Autenticação necessária")
    try:
        payload = jwt.decode(authorization[7:], _secret(), algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Sessão inválida ou expirada")
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,nome,chave_pix,login FROM cpt_responsavel WHERE id=%s AND ativo=TRUE", (user_id,))
        user = cur.fetchone()
    if not user:
        raise HTTPException(401, "Responsável não encontrado")
    return user


def audit(conn, request: Request, action: str, entity: str, entity_id=None, user_id=None, details=None):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO cpt_auditoria
            (responsavel_id,acao,entidade,entidade_id,detalhes,ip,user_agent)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (
            user_id, action, entity, entity_id, json.dumps(details or {}),
            request.client.host if request.client else None,
            request.headers.get("user-agent", "")[:1000],
        ))


def make_slug(value: str):
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "campanha"


def owned_campaign(cur, campaign_id: int, user_id: int):
    cur.execute("SELECT id FROM cpt_campanha WHERE id=%s AND responsavel_id=%s", (campaign_id, user_id))
    if not cur.fetchone():
        raise HTTPException(404, "Campanha não encontrada")


@router.get("/auth/status")
def setup_status():
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM cpt_responsavel)")
        return {"configurado": cur.fetchone()[0]}


@router.post("/auth/setup", status_code=201)
def setup(data: SetupIn, request: Request):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("LOCK TABLE cpt_responsavel IN EXCLUSIVE MODE")
        cur.execute("SELECT EXISTS(SELECT 1 FROM cpt_responsavel)")
        if cur.fetchone()["exists"]:
            raise HTTPException(409, "O responsável inicial já foi cadastrado")
        cur.execute("""INSERT INTO cpt_responsavel (nome,login,senha_hash)
            VALUES (%s,%s,%s) RETURNING id,nome,login""", (data.nome, data.login.lower(), pwd.hash(data.senha)))
        user = cur.fetchone()
        audit(conn, request, "CONFIGURACAO_INICIAL", "responsavel", user["id"], user["id"])
        conn.commit()
        return {"token": _token(user["id"]), "responsavel": user}


@router.post("/auth/login")
def login(data: LoginIn, request: Request):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,nome,login,senha_hash FROM cpt_responsavel WHERE lower(login)=lower(%s) AND ativo=TRUE", (data.login,))
        user = cur.fetchone()
        if not user or not pwd.verify(data.senha, user["senha_hash"]):
            audit(conn, request, "LOGIN_FALHOU", "sessao", details={"login": data.login[:60]})
            conn.commit()
            raise HTTPException(401, "Login ou senha inválidos")
        audit(conn, request, "LOGIN", "sessao", user_id=user["id"])
        conn.commit()
        user.pop("senha_hash")
        return {"token": _token(user["id"]), "responsavel": user}


@router.get("/public/campanhas")
def public_campaigns():
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT c.id,c.nome,c.slug,c.descricao,c.chave_pix,r.nome responsavel_nome,
            COUNT(p.id)::int produtos FROM cpt_campanha c JOIN cpt_responsavel r ON r.id=c.responsavel_id
            LEFT JOIN cpt_produto p ON p.campanha_id=c.id AND p.ativo=TRUE WHERE c.ativa=TRUE
            GROUP BY c.id,r.nome ORDER BY c.criada_em DESC""")
        return cur.fetchall()


@router.get("/public/campanhas/{slug}")
def public_campaign(slug: str):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT c.id,c.nome,c.slug,c.descricao,c.chave_pix,r.nome responsavel_nome
            FROM cpt_campanha c JOIN cpt_responsavel r ON r.id=c.responsavel_id
            WHERE c.slug=%s AND c.ativa=TRUE""", (slug,))
        campaign = cur.fetchone()
        if not campaign:
            raise HTTPException(404, "Campanha não encontrada")
        cur.execute("SELECT id,nome,descricao,preco FROM cpt_produto WHERE campanha_id=%s AND ativo=TRUE ORDER BY nome", (campaign["id"],))
        campaign["produtos"] = cur.fetchall()
        return campaign


@router.post("/public/pedidos", status_code=201)
def create_order(data: OrderIn, request: Request):
    ids = [item.produto_id for item in data.itens]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Cada produto deve aparecer apenas uma vez")
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,responsavel_id FROM cpt_campanha WHERE id=%s AND ativa=TRUE", (data.campanha_id,))
        campaign = cur.fetchone()
        if not campaign:
            raise HTTPException(404, "Campanha não encontrada")
        cur.execute("SELECT id,nome,preco FROM cpt_produto WHERE campanha_id=%s AND ativo=TRUE AND id=ANY(%s)", (data.campanha_id, ids))
        products = {row["id"]: row for row in cur.fetchall()}
        if len(products) != len(ids):
            raise HTTPException(422, "Um ou mais produtos não estão disponíveis")
        total = sum(products[item.produto_id]["preco"] * item.quantidade for item in data.itens)
        cur.execute("""INSERT INTO cpt_pedido (campanha_id,cliente_nome,data_coleta,valor_total)
            VALUES (%s,%s,%s,%s) RETURNING id,status_pagamento,valor_total,data_coleta""",
            (data.campanha_id, data.cliente_nome, data.data_coleta, total))
        order = cur.fetchone()
        for item in data.itens:
            product = products[item.produto_id]
            cur.execute("""INSERT INTO cpt_pedido_item
                (pedido_id,produto_id,produto_nome,quantidade,valor_unitario,subtotal)
                VALUES (%s,%s,%s,%s,%s,%s)""", (
                order["id"], product["id"], product["nome"], item.quantidade,
                product["preco"], product["preco"] * item.quantidade,
            ))
        audit(conn, request, "PEDIDO_CRIADO", "pedido", order["id"], campaign["responsavel_id"], {"campanha_id": data.campanha_id})
        conn.commit()
        return order


@router.get("/me")
def me(user=Depends(current_user)):
    return user


@router.put("/me")
def update_me(data: ResponsibleIn, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("UPDATE cpt_responsavel SET nome=%s,chave_pix=%s,atualizado_em=NOW() WHERE id=%s RETURNING id,nome,chave_pix,login", (data.nome, data.chave_pix, user["id"]))
        result = cur.fetchone(); audit(conn, request, "RESPONSAVEL_ATUALIZADO", "responsavel", user["id"], user["id"]); conn.commit()
        return result


@router.get("/campanhas")
def campaigns(user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cpt_campanha WHERE responsavel_id=%s ORDER BY criada_em DESC", (user["id"],))
        return cur.fetchall()


@router.post("/campanhas", status_code=201)
def add_campaign(data: CampaignIn, request: Request, user=Depends(current_user)):
    base = make_slug(data.nome)
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        slug = base
        cur.execute("SELECT 1 FROM cpt_campanha WHERE slug=%s", (slug,))
        if cur.fetchone(): slug = f"{base}-{int(datetime.now().timestamp())}"
        cur.execute("""INSERT INTO cpt_campanha (responsavel_id,nome,slug,descricao,chave_pix,ativa)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""", (user["id"], data.nome, slug, data.descricao, data.chave_pix or user["chave_pix"], data.ativa))
        result=cur.fetchone(); audit(conn,request,"CAMPANHA_CRIADA","campanha",result["id"],user["id"]); conn.commit(); return result


@router.post("/campanhas/{campaign_id}/produtos", status_code=201)
def add_product(campaign_id: int, data: ProductIn, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        owned_campaign(cur,campaign_id,user["id"])
        cur.execute("""INSERT INTO cpt_produto (campanha_id,nome,descricao,preco,ativo)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""", (campaign_id,data.nome,data.descricao,data.preco,data.ativo))
        result=cur.fetchone(); audit(conn,request,"PRODUTO_CRIADO","produto",result["id"],user["id"]); conn.commit(); return result


@router.get("/campanhas/{campaign_id}/produtos")
def products(campaign_id: int, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        owned_campaign(cur,campaign_id,user["id"]); cur.execute("SELECT * FROM cpt_produto WHERE campanha_id=%s ORDER BY nome",(campaign_id,)); return cur.fetchall()


@router.get("/equipe")
def team(user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT *, (tipo='oficial' OR fim_em >= CURRENT_DATE) vigente FROM cpt_integrante WHERE responsavel_id=%s AND ativo=TRUE ORDER BY tipo,nome",(user["id"],)); return cur.fetchall()


@router.post("/equipe", status_code=201)
def add_member(data: TeamMemberIn, request: Request, user=Depends(current_user)):
    if data.fim_em and data.fim_em < data.inicio_em: raise HTTPException(422,"A data final deve ser posterior à inicial")
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""INSERT INTO cpt_integrante (responsavel_id,nome,tipo,inicio_em,fim_em)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""",(user["id"],data.nome,data.tipo,data.inicio_em,data.fim_em))
        result=cur.fetchone(); audit(conn,request,"INTEGRANTE_CRIADO","integrante",result["id"],user["id"]); conn.commit(); return result


@router.delete("/equipe/{member_id}", status_code=204)
def remove_member(member_id: int, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE cpt_integrante SET ativo=FALSE WHERE id=%s AND responsavel_id=%s",(member_id,user["id"]))
        if not cur.rowcount: raise HTTPException(404,"Integrante não encontrado")
        audit(conn,request,"INTEGRANTE_REMOVIDO","integrante",member_id,user["id"]); conn.commit()


@router.get("/pedidos")
def orders(data_coleta: date | None = None, campaign_id: int | None = None, user=Depends(current_user)):
    target = data_coleta or date.today()
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        params=[user["id"],target]; extra=""
        if campaign_id is not None: extra=" AND c.id=%s"; params.append(campaign_id)
        cur.execute("""SELECT p.id,p.cliente_nome,p.data_coleta,p.status_pagamento,p.valor_total,p.criado_em,
            c.id campanha_id,c.nome campanha_nome,COALESCE(json_agg(json_build_object('nome',i.produto_nome,'quantidade',i.quantidade,'subtotal',i.subtotal)) FILTER (WHERE i.id IS NOT NULL),'[]') itens
            FROM cpt_pedido p JOIN cpt_campanha c ON c.id=p.campanha_id LEFT JOIN cpt_pedido_item i ON i.pedido_id=p.id
            WHERE c.responsavel_id=%s AND p.data_coleta=%s"""+extra+" GROUP BY p.id,c.id ORDER BY p.criado_em DESC",params)
        return cur.fetchall()


@router.patch("/pedidos/{order_id}/pagamento")
def payment(order_id: int, data: PaymentIn, request: Request, user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""UPDATE cpt_pedido p SET status_pagamento=%s,atualizado_em=NOW() FROM cpt_campanha c
            WHERE p.campanha_id=c.id AND p.id=%s AND c.responsavel_id=%s RETURNING p.id,p.status_pagamento,p.atualizado_em""",(data.status,order_id,user["id"]))
        result=cur.fetchone()
        if not result: raise HTTPException(404,"Pedido não encontrado")
        audit(conn,request,"PAGAMENTO_ATUALIZADO","pedido",order_id,user["id"],{"status":data.status}); conn.commit(); return result


@router.get("/dashboard")
def dashboard(user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT COUNT(*)::int pedidos,COALESCE(SUM(valor_total),0) total,
            COALESCE(SUM(valor_total) FILTER (WHERE status_pagamento='pago'),0) pago,
            COALESCE(SUM(valor_total) FILTER (WHERE status_pagamento='pendente'),0) pendente
            FROM cpt_pedido p JOIN cpt_campanha c ON c.id=p.campanha_id
            WHERE c.responsavel_id=%s AND p.data_coleta=CURRENT_DATE""",(user["id"],)); today=cur.fetchone()
        cur.execute("""SELECT COALESCE(SUM(valor_total),0) total,COUNT(*)::int pedidos FROM cpt_pedido p
            JOIN cpt_campanha c ON c.id=p.campanha_id WHERE c.responsavel_id=%s AND p.data_coleta=CURRENT_DATE-1""",(user["id"],)); previous=cur.fetchone()
        base=previous["total"]
        variation=float((today["total"]-base)/base*100) if base else None
        return {"hoje":today,"dia_anterior":previous,"variacao_percentual":variation,"atualizado_em":datetime.now(timezone.utc)}


@router.get("/relatorios/ultimas-24h")
def report(user=Depends(current_user)):
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT p.id,p.cliente_nome,p.data_coleta,p.status_pagamento,p.valor_total,p.criado_em,c.nome campanha
            FROM cpt_pedido p JOIN cpt_campanha c ON c.id=p.campanha_id
            WHERE c.responsavel_id=%s AND p.criado_em >= NOW()-INTERVAL '24 hours' ORDER BY p.criado_em DESC""",(user["id"],)); rows=cur.fetchall()
        return {"gerado_em":datetime.now(timezone.utc),"quantidade":len(rows),"total":sum(r["valor_total"] for r in rows),"pedidos":rows}


@router.get("/auditoria")
def audit_log(limit: int = 100, user=Depends(current_user)):
    limit=max(1,min(limit,500))
    with connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,acao,entidade,entidade_id,detalhes,ip,criado_em FROM cpt_auditoria WHERE responsavel_id=%s ORDER BY criado_em DESC LIMIT %s",(user["id"],limit)); return cur.fetchall()
