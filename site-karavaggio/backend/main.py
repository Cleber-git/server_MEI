import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parents[1]
# QUOTE_RECIPIENT = "cotacao@karavaggio.com.br"
QUOTE_RECIPIENT = "cleber192875@gmail.com"

def load_env_file(env_path: Path, *, override: bool = False) -> None:
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


load_env_file(ROOT_DIR.parent / ".env")
load_env_file(ROOT_DIR / ".env", override=True)


class QuoteRequest(BaseModel):
    cnpj_pagador: Annotated[str, Field(max_length=32)] = "Não informado"
    cnpj_origem: Annotated[str, Field(max_length=32)] = "Não informado"
    origem: Annotated[str, Field(max_length=160)] = "Não informado"
    cnpj_destino: Annotated[str, Field(max_length=32)] = "Não informado"
    destino: Annotated[str, Field(max_length=160)] = "Não informado"
    valor_nota: Annotated[str, Field(max_length=40)] = "Não informado"
    volumes: Annotated[str, Field(max_length=20)] = "Não informado"
    peso_bruto: Annotated[str, Field(max_length=40)] = "Não informado"
    cubagem: Annotated[str, Field(max_length=500)] = "Não informado"
    observacoes: Annotated[str, Field(max_length=2000)] = "Não informado"


app = FastAPI(title="Karavaggio Site API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "null",
    ],
    allow_methods=["POST"],
    allow_headers=["*"],
)


def clean(value: str) -> str:
    value = (value or "").strip()
    return value if value else "Não informado"


def build_quote_body(quote: QuoteRequest) -> str:
    return "\n".join(
        [
            "Solicitação de cotação enviada pelo site Karavaggio.",
            "",
            "Dados do pagador",
            f"CNPJ do pagador: {clean(quote.cnpj_pagador)}",
            "",
            "Dados de origem",
            f"CNPJ de origem: {clean(quote.cnpj_origem)}",
            f"Cidade e estado de origem: {clean(quote.origem)}",
            "",
            "Dados de destino",
            f"CNPJ de destino: {clean(quote.cnpj_destino)}",
            f"Cidade e estado de destino: {clean(quote.destino)}",
            "",
            "Dados da nota fiscal",
            f"Valor total da Nota Fiscal: {clean(quote.valor_nota)}",
            f"Quantidade de volumes: {clean(quote.volumes)}",
            f"Peso bruto: {clean(quote.peso_bruto)}",
            f"Cubagem da mercadoria: {clean(quote.cubagem)}",
            "",
            "Observações e comentários",
            clean(quote.observacoes),
        ]
    )


def send_email(subject: str, body: str) -> None:
    smtp_user = os.getenv("EMAIL_USER")
    smtp_password = os.getenv("EMAIL_PASSWORD")

    if not smtp_user or not smtp_password:
        raise HTTPException(
            status_code=503,
            detail="E-mail não configurado. Defina EMAIL_USER e EMAIL_PASSWORD.",
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_user
    message["To"] = QUOTE_RECIPIENT
    message.set_content(body)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        print(f"Erro de autenticação SMTP: {exc}", flush=True)
        raise HTTPException(
            status_code=502,
            detail="Falha na autenticação do Gmail. Verifique EMAIL_USER e EMAIL_PASSWORD.",
        ) from exc
    except smtplib.SMTPException as exc:
        print(f"Erro SMTP ao enviar cotação: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"Falha SMTP ao enviar cotação: {exc}") from exc
    except OSError as exc:
        print(f"Erro de conexão SMTP: {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"Falha de conexão com o servidor SMTP: {exc}") from exc


@app.post("/api/cotacao")
def create_quote(quote: QuoteRequest) -> dict[str, str]:
    send_email("Solicitação de cotação - Site Karavaggio", build_quote_body(quote))
    return {"message": "Cotação enviada com sucesso."}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html")


app.mount("/", StaticFiles(directory=ROOT_DIR), name="site")
