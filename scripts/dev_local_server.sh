#!/usr/bin/env bash
# Backend LOCAL para o app debug (F9): FastAPI em 127.0.0.1:8000 com FISCAL_PROVIDER=fake e um
# banco próprio (server_mei_local) no mesmo container Postgres do gate. Recriado a cada execução.
# Nunca usa o Render nem a SEFIN. No emulador, o host é http://10.0.2.2:8000/.
#   scripts/dev_local_server.sh            -> recria, semeia e serve (Ctrl+C para parar)
# Login sintético do tenant semeado: tenant-a@example.com / senha-tenant-a (só local).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY_IMAGE="python:3.13-slim"
PG_CONTAINER="server-mei-pg-test"
VENV_VOLUME="server-mei-venv313"
DB_NAME="server_mei_local"

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  echo "Rode scripts/dev_check.sh uma vez para criar o container $PG_CONTAINER." >&2
  exit 1
fi
docker exec "$PG_CONTAINER" dropdb -U postgres --if-exists "$DB_NAME"
docker exec "$PG_CONTAINER" createdb -U postgres "$DB_NAME"

# Chaves só de desenvolvimento, geradas a cada execução e nunca gravadas.
JWT_SECRET="$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-48)"
VAULT_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"

docker run --rm --network host \
  -v "$ROOT":/src -v "$VENV_VOLUME":/venv -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e DB_HOST=127.0.0.1 -e DB_PORT=55432 -e DB_NAME="$DB_NAME" -e DB_USER=postgres -e DB_PASSWORD= \
  -e FISCAL_FEATURE_ENABLED=true -e FISCAL_PROVIDER=fake -e FISCAL_WORKER_POLL_SECONDS=1 \
  -e JWT_SIGNING_KEYS="dev:$JWT_SECRET" -e JWT_ACTIVE_KID=dev \
  -e FISCAL_VAULT_MASTER_KEY="dev:$VAULT_KEY" -e FISCAL_VAULT_ACTIVE_KEY_ID=dev \
  "$PY_IMAGE" bash -c '
    set -euo pipefail
    test -x /venv/bin/python || { echo "Rode scripts/dev_check.sh antes (cria o venv)." >&2; exit 1; }
    /venv/bin/python scripts/dev_seed_local.py
    exec /venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
  '
