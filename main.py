from fastapi import FastAPI, Request, HTTPException, Query, Header, Depends
import os
import asyncio
from typing import List, Optional
from db import *
from models import *
from jose import JWTError, jwt
from datetime import datetime, timedelta
from random import randrange
import re 
from email_ import email_service
import requests

import json
from email.mime.text import MIMEText
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
import hashlib
from pathlib import Path
from psycopg2.extras import Json
try:
    import nfe
except ImportError:
    nfe = None



# SECRET_KEY = "minha_chave_super_secreta_123"
# ALGORITHM = "HS256"
# ACCESS_TOKEN_EXPIRE_MINUTES = 60

app = FastAPI()

KARAVAGGIO_DEMO_PREFIX = os.getenv("KARAVAGGIO_DEMO_PREFIX", "apresentacao").strip("/") or "apresentacao"
KARAVAGGIO_DEMO_TOKEN = os.getenv("KARAVAGGIO_DEMO_TOKEN", "karavaggio-preview-2026").strip("/") or "karavaggio-preview-2026"
KARAVAGGIO_SITE_DIR = (Path(__file__).resolve().parent / "site-karavaggio").resolve()


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

    karavaggio_demo_base = f"/{KARAVAGGIO_DEMO_PREFIX}"
    if path == karavaggio_demo_base or path.startswith(f"{karavaggio_demo_base}/"):
        return await call_next(request)

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

def validar_acesso_karavaggio(access_key: str):
    if access_key != KARAVAGGIO_DEMO_TOKEN:
        raise HTTPException(status_code=404, detail="Pagina nao encontrada")


def arquivo_karavaggio(caminho: str = "index.html"):
    alvo = (KARAVAGGIO_SITE_DIR / caminho).resolve()
    try:
        alvo.relative_to(KARAVAGGIO_SITE_DIR)
    except ValueError:
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")

    if not alvo.is_file():
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")

    return alvo


@app.get(f"/{KARAVAGGIO_DEMO_PREFIX}/{{access_key}}", include_in_schema=False)
def abrir_site_karavaggio(access_key: str):
    validar_acesso_karavaggio(access_key)
    return RedirectResponse(url=f"/{KARAVAGGIO_DEMO_PREFIX}/{access_key}/", status_code=307)


@app.get(f"/{KARAVAGGIO_DEMO_PREFIX}/{{access_key}}/", include_in_schema=False)
def site_karavaggio(access_key: str):
    validar_acesso_karavaggio(access_key)
    return FileResponse(arquivo_karavaggio())


@app.get(f"/{KARAVAGGIO_DEMO_PREFIX}/{{access_key}}/{{caminho:path}}", include_in_schema=False)
def site_karavaggio_assets(access_key: str, caminho: str):
    validar_acesso_karavaggio(access_key)
    return FileResponse(arquivo_karavaggio(caminho))

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
    deletado boolean,
    gtin TEXT,
    estoque TEXT
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

        cur.execute("""CREATE TABLE IF NOT EXISTS notas_servico
(
    id text COLLATE pg_catalog."default" PRIMARY KEY,
    empresauuid text COLLATE pg_catalog."default" NOT NULL,
    payload_enviado jsonb NOT NULL,
    status text COLLATE pg_catalog."default" NOT NULL,
    numero text COLLATE pg_catalog."default",
    codigo_verificacao text COLLATE pg_catalog."default",
    url_pdf text COLLATE pg_catalog."default",
    url_xml text COLLATE pg_catalog."default",
    url_consulta text COLLATE pg_catalog."default",
    protocolo text COLLATE pg_catalog."default",
    erro_fiscal text COLLATE pg_catalog."default",
    ambiente text COLLATE pg_catalog."default",
    provedor text COLLATE pg_catalog."default",
    data_criacao bigint NOT NULL,
    data_atualizacao bigint NOT NULL
)""")

        cur.execute("""ALTER TABLE empresa
            ADD COLUMN IF NOT EXISTS regime_tributario text COLLATE pg_catalog."default" DEFAULT 'MEI'
        """)

        cur.execute("""ALTER TABLE empresa
            ADD COLUMN IF NOT EXISTS optante_simples boolean DEFAULT true
        """)

        cur.execute("""ALTER TABLE empresa
            ADD COLUMN IF NOT EXISTS emissao_nfse_habilitada boolean DEFAULT true
        """)

        
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


def apenas_digitos(valor: Optional[str]) -> str:
    return re.sub(r"\D", "", valor or "")


def apenas_alfanumericos(valor: Optional[str]) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", valor or "")


def cpf_valido(cpf: str) -> bool:
    cpf = apenas_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    def digito(corte: int) -> int:
        soma = sum(int(cpf[i]) * (corte - i) for i in range(corte - 1))
        resto = (soma * 10) % 11
        return 0 if resto == 10 else resto

    return digito(10) == int(cpf[9]) and digito(11) == int(cpf[10])


def cnpj_valido(cnpj: str) -> bool:
    cnpj = apenas_digitos(cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False

    def digito(pesos: list[int], base: str) -> int:
        soma = sum(int(base[i]) * pesos[i] for i in range(len(pesos)))
        resto = soma % 11
        return 0 if resto < 2 else 11 - resto

    primeiro = digito([5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], cnpj[:12])
    segundo = digito([6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], cnpj[:12] + str(primeiro))
    return primeiro == int(cnpj[12]) and segundo == int(cnpj[13])

def documento_tomador_valido(documento: str) -> bool:
    documento = apenas_alfanumericos(documento)
    if not documento:
        return False
    if not documento.isdigit():
        return True
    if len(documento) == 11:
        return cpf_valido(documento)
    if len(documento) == 14:
        return cnpj_valido(documento)
    return False


def fiscal_config():
    ambiente = os.getenv("FISCAL_AMBIENTE", "homologacao").strip().lower()
    return {
        "ambiente": ambiente,
        "provedor": os.getenv("FISCAL_PROVEDOR", "homologacao-local"),
        "base_url": (os.getenv("FISCAL_API_BASE_URL") or "").rstrip("/"),
        "token": os.getenv("FISCAL_API_TOKEN"),
        "emissao_real_habilitada": os.getenv("FISCAL_EMISSAO_REAL_HABILITADA", "false").strip().lower() == "true",
        "mock_homologacao": os.getenv("FISCAL_MOCK_HOMOLOGACAO", "true").strip().lower() == "true",
    }


def buscar_empresa_fiscal(empresa_uuid: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT uuid, cnpj, razaosocial, nomefantasia, municipio, uf, cnae,
                   ativo, bloqueado, motivobloqueio, plano, statusassinatura,
                   regime_tributario, optante_simples, emissao_nfse_habilitada
            FROM empresa
            WHERE uuid = %s
        """, (empresa_uuid,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "uuid": row[0],
            "cnpj": row[1],
            "razaoSocial": row[2],
            "nomeFantasia": row[3],
            "municipio": row[4],
            "uf": row[5],
            "cnae": row[6],
            "ativo": row[7],
            "bloqueado": row[8],
            "motivoBloqueio": row[9],
            "plano": row[10],
            "statusAssinatura": row[11],
            "regimeTributario": row[12],
            "optanteSimples": row[13],
            "emissaoNfseHabilitada": row[14],
        }
    finally:
        put_conn(conn)


def validar_empresa_para_nfse(empresa: Optional[dict]) -> Optional[str]:
    if not empresa:
        return "Empresa nao encontrada."
    if not empresa["ativo"]:
        return "Empresa inativa. Ative a empresa antes de emitir nota."
    if empresa["bloqueado"]:
        motivo = empresa.get("motivoBloqueio") or "Cadastro bloqueado."
        return f"Empresa bloqueada para emissao de nota: {motivo}"
    if not empresa["emissaoNfseHabilitada"]:
        return "Emissao de NFS-e nao habilitada para esta empresa."
    if not cnpj_valido(empresa.get("cnpj")):
        return "CNPJ da empresa invalido ou incompleto."
    if not empresa.get("municipio") or not empresa.get("uf"):
        return "Informe municipio e UF da empresa antes de emitir nota."
    if not empresa.get("cnae"):
        return "Informe o CNAE da empresa antes de emitir nota."

    regime = (empresa.get("regimeTributario") or "").strip().upper()
    if regime and regime not in {"MEI", "SIMPLES", "SIMPLES_NACIONAL"}:
        return "Regime tributario nao permitido para emissao neste fluxo. Use MEI ou Simples Nacional."
    if empresa.get("optanteSimples") is False:
        return "Empresa nao marcada como optante do Simples Nacional/MEI."
    return None


def validar_payload_nfse(data: NotaServicoIn) -> Optional[str]:
    if data.tipo.lower() != "servico":
        return "Tipo de nota invalido. Envie tipo igual a servico."
    if not data.tomador.nome.strip():
        return "Informe o nome do tomador."
    if not documento_tomador_valido(data.tomador.documento):
        
        return "Documento do tomador invalido. Envie CPF, CNPJ ou documento alfanumerico."
    if data.tomador.email and not validar_email(data.tomador.email):
        return "Email do tomador invalido."
    if not data.servico.descricao.strip():
        return "Informe a descricao do servico."
    if data.servico.valor <= 0:
        return "Valor do servico deve ser maior que zero."
    if not data.servico.codigoMunicipalServico.strip():
        return "Informe o codigo municipal do servico."
    return None


def montar_payload_fiscal(data: NotaServicoIn, empresa: dict) -> dict:
    return {
        "ambiente": fiscal_config()["ambiente"],
        "empresa": {
            "uuid": empresa["uuid"],
            "cnpj": apenas_digitos(empresa["cnpj"]),
            "razaoSocial": empresa.get("razaoSocial"),
            "nomeFantasia": empresa.get("nomeFantasia"),
            "municipio": empresa.get("municipio"),
            "uf": empresa.get("uf"),
            "cnae": empresa.get("cnae"),
            "regimeTributario": empresa.get("regimeTributario") or "MEI",
            "optanteSimples": empresa.get("optanteSimples"),
        },
        "tomador": {
            "nome": data.tomador.nome.strip(),
            "documento": apenas_alfanumericos(data.tomador.documento),
            "email": data.tomador.email,
        },
        "servico": {
            "descricao": data.servico.descricao.strip(),
            "valor": data.servico.valor,
            "codigoMunicipalServico": data.servico.codigoMunicipalServico.strip(),
        },
    }


def emitir_nfse_servico(payload_fiscal: dict) -> dict:
    config = fiscal_config()
    if config["ambiente"] == "producao" and not config["emissao_real_habilitada"]:
        return {
            "sucesso": False,
            "status": "BLOQUEADA",
            "mensagem": "Emissao real bloqueada. Configure FISCAL_EMISSAO_REAL_HABILITADA=true para liberar producao.",
            "erroFiscal": "PRODUCAO_SEM_FLAG",
        }

    if config["base_url"] and config["token"]:
        try:
            response = requests.post(
                f"{config['base_url']}/notas/servico",
                json=payload_fiscal,
                headers={"Authorization": f"Bearer {config['token']}"},
                timeout=30,
            )
            response.raise_for_status()
            fiscal = response.json()
            return {
                "sucesso": bool(fiscal.get("sucesso", True)),
                "status": fiscal.get("status", "PROCESSANDO"),
                "mensagem": fiscal.get("mensagem"),
                "numero": fiscal.get("numero"),
                "codigoVerificacao": fiscal.get("codigoVerificacao"),
                "urlPdf": fiscal.get("urlPdf"),
                "urlXml": fiscal.get("urlXml"),
                "urlConsulta": fiscal.get("urlConsulta"),
                "protocolo": fiscal.get("protocolo") or fiscal.get("id") or fiscal.get("uuid"),
                "erroFiscal": fiscal.get("erroFiscal"),
            }
        except requests.RequestException as exc:
            return {
                "sucesso": False,
                "status": "ERRO",
                "mensagem": "Falha ao comunicar com o provedor fiscal. Tente novamente em alguns minutos.",
                "erroFiscal": str(exc),
            }

    if config["ambiente"] == "homologacao" and config["mock_homologacao"]:
        protocolo = hashlib.sha256(json.dumps(payload_fiscal, sort_keys=True).encode()).hexdigest()[:16].upper()
        return {
            "sucesso": True,
            "status": "AUTORIZADA",
            "mensagem": "NFS-e autorizada em homologacao local. Configure FISCAL_API_BASE_URL e FISCAL_API_TOKEN para usar o provedor fiscal.",
            "numero": f"HOM-{protocolo[:8]}",
            "codigoVerificacao": protocolo,
            "urlPdf": None,
            "urlXml": None,
            "urlConsulta": None,
            "protocolo": protocolo,
            "erroFiscal": None,
        }

    return {
        "sucesso": False,
        "status": "CONFIGURACAO_PENDENTE",
        "mensagem": "Provedor fiscal nao configurado. Defina FISCAL_API_BASE_URL e FISCAL_API_TOKEN em homologacao.",
        "erroFiscal": "FISCAL_PROVIDER_NOT_CONFIGURED",
    }


def salvar_historico_nfse(empresa_uuid: str, payload: dict, resultado: dict) -> str:
    nota_id = hashlib.sha256(f"{empresa_uuid}:{json.dumps(payload, sort_keys=True)}:{datetime.utcnow().isoformat()}".encode()).hexdigest()
    agora = int(datetime.utcnow().timestamp() * 1000)
    config = fiscal_config()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO notas_servico
            (id, empresauuid, payload_enviado, status, numero, codigo_verificacao,
             url_pdf, url_xml, url_consulta, protocolo, erro_fiscal, ambiente,
             provedor, data_criacao, data_atualizacao)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            nota_id,
            empresa_uuid,
            Json(payload),
            resultado.get("status") or "ERRO",
            resultado.get("numero"),
            resultado.get("codigoVerificacao"),
            resultado.get("urlPdf"),
            resultado.get("urlXml"),
            resultado.get("urlConsulta"),
            resultado.get("protocolo"),
            resultado.get("erroFiscal"),
            config["ambiente"],
            config["provedor"],
            agora,
            agora,
        ))
        conn.commit()
        return nota_id
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def tentar_salvar_historico_nfse(empresa_uuid: str, payload: dict, resultado: dict):
    try:
        salvar_historico_nfse(empresa_uuid, payload, resultado)
    except Exception as exc:
        print(f"Falha ao salvar historico de NFS-e: {exc}")


def pydantic_to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()

@app.on_event("startup")
def startup(): 
    create_tables()


# -------------------------------------------------------------------------------------
# NOTAS FISCAIS DE SERVICO
# =====================================================================================
@app.post("/notas/servico", response_model=NotaServicoResponse)
def emitir_nota_servico(data: NotaServicoIn, empresa_atual: str = Depends(get_empresa)):
    print(data)
    if data.empresaUuid != empresa_atual:
        tentar_salvar_historico_nfse(data.empresaUuid, pydantic_to_dict(data), {
            "status": "ERRO_VALIDACAO",
            "erroFiscal": "EMPRESA_DIFERENTE_DO_HEADER",
        })
        return NotaServicoResponse(
            sucesso=False,
            status="ERRO_VALIDACAO",
            mensagem="Empresa da nota diferente da empresa autenticada."
        )

    erro_payload = validar_payload_nfse(data)
    if erro_payload:
        tentar_salvar_historico_nfse(data.empresaUuid, pydantic_to_dict(data), {
            "status": "ERRO_VALIDACAO",
            "erroFiscal": erro_payload,
        })
        return NotaServicoResponse(
            sucesso=False,
            status="ERRO_VALIDACAO",
            mensagem=erro_payload
        )

    empresa = buscar_empresa_fiscal(data.empresaUuid)
    erro_empresa = validar_empresa_para_nfse(empresa)
    if erro_empresa:
        tentar_salvar_historico_nfse(data.empresaUuid, pydantic_to_dict(data), {
            "status": "ERRO_VALIDACAO",
            "erroFiscal": erro_empresa,
        })
        return NotaServicoResponse(
            sucesso=False,
            status="ERRO_VALIDACAO",
            mensagem=erro_empresa
        )

    payload_fiscal = montar_payload_fiscal(data, empresa)
    resultado = emitir_nfse_servico(payload_fiscal)

    try:
        salvar_historico_nfse(data.empresaUuid, payload_fiscal, resultado)
    except Exception as exc:
        return NotaServicoResponse(
            sucesso=False,
            status="ERRO",
            mensagem=f"Nota processada pelo fiscal, mas falhou ao salvar historico: {exc}"
        )

    return NotaServicoResponse(
        sucesso=resultado.get("sucesso", False),
        status=resultado.get("status", "ERRO"),
        mensagem=resultado.get("mensagem"),
        numero=resultado.get("numero"),
        codigoVerificacao=resultado.get("codigoVerificacao"),
        urlPdf=resultado.get("urlPdf"),
        urlXml=resultado.get("urlXml"),
        urlConsulta=resultado.get("urlConsulta"),
    )


@app.get("/notas/servico")
def listar_notas_servico(empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, empresauuid, status, numero, codigo_verificacao,
                   url_pdf, url_xml, url_consulta, protocolo, erro_fiscal,
                   ambiente, provedor, data_criacao, data_atualizacao
            FROM notas_servico
            WHERE empresauuid = %s
            ORDER BY data_criacao DESC
        """, (empresa_atual,))
        rows = cur.fetchall()
        return [{
            "id": row[0],
            "empresaUuid": row[1],
            "status": row[2],
            "numero": row[3],
            "codigoVerificacao": row[4],
            "urlPdf": row[5],
            "urlXml": row[6],
            "urlConsulta": row[7],
            "protocolo": row[8],
            "erroFiscal": row[9],
            "ambiente": row[10],
            "provedor": row[11],
            "dataCriacao": row[12],
            "dataAtualizacao": row[13],
        } for row in rows]
    finally:
        put_conn(conn)


@app.get("/notas/servico/{nota_id}")
def consultar_nota_servico(nota_id: str, empresa_atual: str = Depends(get_empresa)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, empresauuid, status, numero, codigo_verificacao,
                   url_pdf, url_xml, url_consulta, protocolo, erro_fiscal,
                   ambiente, provedor, data_criacao, data_atualizacao
            FROM notas_servico
            WHERE id = %s AND empresauuid = %s
        """, (nota_id, empresa_atual))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Nota nao encontrada")
        return {
            "id": row[0],
            "empresaUuid": row[1],
            "status": row[2],
            "numero": row[3],
            "codigoVerificacao": row[4],
            "urlPdf": row[5],
            "urlXml": row[6],
            "urlConsulta": row[7],
            "protocolo": row[8],
            "erroFiscal": row[9],
            "ambiente": row[10],
            "provedor": row[11],
            "dataCriacao": row[12],
            "dataAtualizacao": row[13],
        }
    finally:
        put_conn(conn)
    
    
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
            (id, nome, preco, preco_anterior, data_criacao, tipo, empresaUuid, pendenteSync, atualizadoEm, deletado, gtin, estoque)
            VALUES (%s,%s,%s,%s,%s,%s, %s, %s, %s, %s, %s, %s)
        """, (
            data.id, data.nome, data.preco,
            data.precoAnterior, data.dataCriacao, data.tipo, data.empresaUuid, data.pendenteSync, data.atualizadoEm, data.deletado, data.gtin, data.estoque
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
        cur.execute("SELECT id, nome, preco, preco_anterior, data_criacao, tipo, empresauuid, pendenteSync, atualizadoEm, deletado, gtin, estoque FROM servico where empresauuid = %s", (empresa_atual,))
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
                deletado=r[9], 
                gtin=str(r[10]),
                estoque=str(r[11])
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
                deletado = %s,
                gtin = %s,
                estoque = %s
                
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
            data.gtin,
            data.estoque,
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

        # Atualizando empresa
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
            data.nomeFantasia,
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

        salt = b"caltechHash"

        senha_seguro = hashlib.pbkdf2_hmac(
            'sha256',
            data.senha.encode(),
            salt,
            100000
        )

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
    
