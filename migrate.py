"""Apply ordered, non-destructive PostgreSQL migrations."""

from pathlib import Path

from db import get_conn, put_conn


def migrate() -> None:
    root = Path(__file__).parent / "migrations"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        for path in sorted(root.glob("[0-9][0-9][0-9]_*.sql")):
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s", (path.name,)
            )
            if cur.fetchone():
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations(version) VALUES(%s)", (path.name,)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


if __name__ == "__main__":
    migrate()
