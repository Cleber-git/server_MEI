"""Fixtures compartilhadas. Testes de banco usam SÓ o Postgres local de teste.

``SERVER_MEI_TEST_DSN`` aponta para o container criado por ``scripts/dev_check.sh``.
Sem ela, os testes de banco são pulados; com host não local, a sessão é abortada
para nunca tocar no banco do Render.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

import pytest

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _test_dsn() -> str | None:
    dsn = os.getenv("SERVER_MEI_TEST_DSN")
    if not dsn:
        return None
    host = urlparse(dsn).hostname
    if host not in _LOCAL_HOSTS:
        pytest.exit(
            "SERVER_MEI_TEST_DSN precisa apontar para Postgres local", returncode=2
        )
    return dsn


@pytest.fixture
def pg_conn():
    """Conexão num schema descartável, isolado por teste."""
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip("SERVER_MEI_TEST_DSN não definido (rode scripts/dev_check.sh)")
    import psycopg2

    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE SCHEMA "{schema}"')
    conn = psycopg2.connect(dsn, options=f"-c search_path={schema}")
    try:
        yield conn
    finally:
        conn.close()
        admin.cursor().execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
