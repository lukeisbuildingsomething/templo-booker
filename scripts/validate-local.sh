#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# smoke_test.py hard-codes this local endpoint. Fail if its port is occupied.
export PYTHON_DOTENV_DISABLED=1
container=$(docker run -d --rm -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=templo postgres:16)
trap 'docker stop "$container" >/dev/null' EXIT
ready=0
for attempt in {1..60}; do
  if docker exec "$container" pg_isready -U postgres >/dev/null; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo 'Postgres did not become ready' >&2; exit 1; }
uv sync --locked
uv run python smoke_test.py
