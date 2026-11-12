"""Semeia o banco LOCAL do backend de desenvolvimento (F9). Nunca o Render.

Chamado por ``scripts/dev_local_server.sh`` dentro do container. Cria o legado mínimo dos
testes (``create_legacy``; o ``main.create_tables()`` engole os próprios erros), aplica as migrations e deixa o tenant sintético ``tenant-a`` pronto
para emitir NFS-e em produção restrita com o ``FakeProvider``: cadastro fiscal completo,
capability credenciada, homologação aprovada e certificado (linha sintética, sem PFX; o
``FakeProvider`` não assina).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

if os.getenv("DB_HOST") not in {"127.0.0.1", "localhost"} or os.getenv("DB_NAME") != "server_mei_local":
    sys.exit("dev_seed_local: só roda no Postgres local (DB_HOST=127.0.0.1, DB_NAME=server_mei_local)")

import db  # noqa: E402
import migrate  # noqa: E402
from fiscal_support import ApiClient, base_app, bearer, create_legacy, insert_tenant, login  # noqa: E402
from test_capabilities_f5 import (  # noqa: E402
    _add_capability,
    _add_certificate,
    _homologate,
    _put_est,
)

CNPJ = "11222333000181"


def seed() -> None:
    conn = db.get_conn()
    try:
        create_legacy(conn)
        insert_tenant(conn, "tenant-a", CNPJ)
        migrate.migrate(conn)
        client = ApiClient(base_app())
        _put_est(client, bearer(login(client, "tenant-a")))
        _add_capability(conn, "SP", "NFSE")
        _homologate(conn, "SP", "NFSE", "tenant-a")
        _add_certificate(conn, "tenant-a")
    finally:
        db.put_conn(conn)
    host = urlparse("http://" + os.getenv("DB_HOST", "")).hostname
    print(f"dev_seed_local: tenant-a pronto em {host}/{os.getenv('DB_NAME')}")


if __name__ == "__main__":
    seed()
