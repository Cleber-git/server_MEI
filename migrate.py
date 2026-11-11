"""Aplica migrations PostgreSQL ordenadas, aditivas e idempotentes.

Regras (docs/FISCAL_BACKEND.md e plano fiscal do app):
- Só arquivos ``NNN_nome.sql``; ``*.down.sql`` nunca é executado.
- Cada arquivo roda numa transação própria, aberta e fechada aqui. Por isso o SQL
  não pode ter ``BEGIN``/``COMMIT``/``ROLLBACK`` de topo.
- Comandos destrutivos (``DROP TABLE``, ``DROP COLUMN``, ``TRUNCATE``, ``DELETE``)
  são recusados antes de qualquer contato com o banco.
- ``pg_advisory_lock`` impede duas réplicas migrando ao mesmo tempo.
- ``--dry-run`` só lista o que seria aplicado, sem escrever nada.

Antes de rodar em produção: ``pg_dump`` (ver docs/FISCAL_BACKEND.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Chave fixa do advisory lock; qualquer bigint serve, desde que seja só deste script.
ADVISORY_LOCK_KEY = 7_311_002_001

_FILE_NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")
_DOLLAR_QUOTED = re.compile(r"\$([A-Za-z_]*)\$.*?\$\1\$", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STRING = re.compile(r"'(?:''|[^'])*'")
_TX_CONTROL = re.compile(
    r"(?:^|;)\s*(BEGIN|COMMIT|ROLLBACK|END|START\s+TRANSACTION|SAVEPOINT)\b",
    re.I,
)
# DROP só é aceito para objetos sem dado: constraint, índice, default, NOT NULL,
# trigger e policy (o padrão "DROP CONSTRAINT IF EXISTS + ADD" é idempotente).
_DESTRUCTIVE = re.compile(
    r"\bDROP\s+(?!CONSTRAINT\b|INDEX\b|DEFAULT\b|NOT\s+NULL\b|TRIGGER\b|POLICY\b)\w+"
    r"|\bTRUNCATE\b|\bDELETE\s+FROM\b",
    re.I,
)


class MigrationRejected(RuntimeError):
    """Arquivo de migration fora das regras; nada foi aplicado."""


def _code_only(sql: str) -> str:
    """Remove comentários, corpos ``$$`` e strings para checar só os comandos de topo."""
    for pattern in (_BLOCK_COMMENT, _LINE_COMMENT, _DOLLAR_QUOTED, _STRING):
        sql = pattern.sub(" ", sql)
    return sql


def validate_sql(name: str, sql: str) -> None:
    code = _code_only(sql)
    tx = _TX_CONTROL.search(code)
    if tx:
        raise MigrationRejected(
            f"{name}: controle de transação '{tx.group(1).upper()}' não é permitido; "
            "o migrate.py abre e fecha a transação de cada arquivo"
        )
    destructive = _DESTRUCTIVE.search(code)
    if destructive:
        raise MigrationRejected(
            f"{name}: comando destrutivo '{destructive.group(0).strip()}' não é "
            "permitido; migrations são apenas aditivas"
        )


def discover(root: Path = MIGRATIONS_DIR) -> list[Path]:
    """Lista as migrations aplicáveis em ordem, validando todas antes de devolver."""
    files = []
    for path in sorted(root.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        if not _FILE_NAME.match(path.name):
            raise MigrationRejected(f"{path.name}: nome fora do padrão NNN_nome.sql")
        validate_sql(path.name, path.read_text(encoding="utf-8"))
        files.append(path)
    versions = [path.name[:3] for path in files]
    duplicated = sorted({v for v in versions if versions.count(v) > 1})
    if duplicated:
        raise MigrationRejected(f"número de migration repetido: {', '.join(duplicated)}")
    return files


def _applied_versions(cur) -> set[str]:
    cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
    if not cur.fetchone()[0]:
        return set()
    cur.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def migrate(conn, *, dry_run: bool = False, root: Path = MIGRATIONS_DIR) -> list[str]:
    """Aplica (ou, em dry-run, só lista) as migrations pendentes. Devolve os nomes."""
    files = discover(root)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    conn.commit()
    try:
        applied = _applied_versions(cur)
        pending = [path for path in files if path.name not in applied]
        if dry_run:
            return [path.name for path in pending]
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        conn.commit()
        done = []
        for path in pending:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (path.name,))
            conn.commit()
            done.append(path.name)
        return done
    finally:
        # Desfaz a transação em curso (dry-run ou falha) antes de liberar o lock.
        conn.rollback()
        cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        conn.commit()
        cur.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="só lista as pendentes, sem aplicar"
    )
    args = parser.parse_args(argv)

    from db import get_conn, put_conn

    conn = get_conn()
    try:
        names = migrate(conn, dry_run=args.dry_run)
    finally:
        put_conn(conn)
    label = "pendentes (dry-run, nada aplicado)" if args.dry_run else "aplicadas"
    print(f"migrations {label}: {len(names)}")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
