from __future__ import annotations

from uuid import uuid4

from db import get_conn, put_conn
from .config import settings


class PostgresFiscalJobQueue:
    def _enqueue(self, kind, tenant_id, document_id, correlation_id):
        job_id = uuid4()
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO fiscal_jobs(id,tenant_id,document_id,job_type,correlation_id,max_attempts)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,document_id,job_type,status) DO UPDATE SET updated_at=NOW()
              RETURNING id""",
                (
                    job_id,
                    tenant_id,
                    document_id,
                    kind,
                    correlation_id,
                    settings.job_max_attempts,
                ),
            )
            result = cur.fetchone()[0]
            conn.commit()
            return str(result)
        finally:
            put_conn(conn)

    def enqueue_validation(self, *args):
        return self._enqueue("VALIDATE", *args)

    def enqueue_submission(self, *args):
        return self._enqueue("SUBMIT", *args)

    def enqueue_reconciliation(self, *args):
        return self._enqueue("RECONCILE", *args)

    def enqueue_cancellation(self, *args):
        return self._enqueue("CANCEL", *args)
