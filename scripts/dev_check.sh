#!/usr/bin/env bash
# Gate local do backend: ruff + pytest em Python 3.13 com Postgres de TESTE local.
# Tudo roda em Docker (gratuito); nunca usa o banco do Render.
#   scripts/dev_check.sh            -> ruff check . && pytest -q
#   scripts/dev_check.sh -k migrate -> argumentos extras vão para o pytest
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY_IMAGE="python:3.13-slim"
PG_IMAGE="postgres:17"
PG_CONTAINER="server-mei-pg-test"
PG_PORT="55432"
VENV_VOLUME="server-mei-venv313"
DSN="postgresql://postgres@127.0.0.1:${PG_PORT}/server_mei_test"

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    docker start "$PG_CONTAINER" >/dev/null
  else
    # trust só vale porque a porta fica presa a 127.0.0.1 e o banco é descartável.
    docker run -d --name "$PG_CONTAINER" -p "127.0.0.1:${PG_PORT}:5432" \
      -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=server_mei_test \
      "$PG_IMAGE" >/dev/null
  fi
fi
for _ in $(seq 1 30); do
  docker exec "$PG_CONTAINER" pg_isready -U postgres -d server_mei_test >/dev/null 2>&1 && break
  sleep 1
done

REQ_HASH="$(cat "$ROOT"/requirements.txt "$ROOT"/requirements-dev.txt | sha256sum | cut -c1-16)"
docker volume create "$VENV_VOLUME" >/dev/null

docker run --rm --network host \
  -v "$ROOT":/src -v "$VENV_VOLUME":/venv -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e RUFF_NO_CACHE=true \
  -e SERVER_MEI_TEST_DSN="$DSN" -e REQ_HASH="$REQ_HASH" \
  "$PY_IMAGE" bash -c '
    set -euo pipefail
    if [ "$(cat /venv/.req-hash 2>/dev/null)" != "$REQ_HASH" ]; then
      python -m venv /venv
      /venv/bin/pip install -q --upgrade pip
      /venv/bin/pip install -q -r requirements-dev.txt
      echo "$REQ_HASH" > /venv/.req-hash
    fi
    /venv/bin/ruff check .
    /venv/bin/python -m pytest -q -p no:cacheprovider "$@"
  ' _ "$@"
