#!/usr/bin/env bash
# One command to a ready AnalystOS demo with Docker Compose (Linux, macOS, WSL; Windows PowerShell: scripts/demo.ps1).
#
#   scripts/demo.sh            full demo stack: standard (Redis, Temporal, worker, scheduler) + Superset + ServiceNow mock
#   scripts/demo.sh --lite     lite: Postgres, API (local orchestrator), web + ServiceNow mock; publishing goes to the preview
#   scripts/demo.sh --reset    delete the demo's data (docker compose down -v) and start again from nothing
#   scripts/demo.sh --down     stop everything (data is kept)
#
# Brings the stack up, waits for health, runs migrate, seed and demo-seed (all idempotent), prints URLs and logins.
# Walkthrough: docs/30-runbooks/05-demo-walkthrough.md.
set -euo pipefail
cd "$(dirname "$0")/.."

mode=full reset=0 down=0
for arg in "$@"; do
  case "$arg" in
    --lite) mode=lite ;;
    --reset) reset=1 ;;
    --down) down=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option $arg (see --help)" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null || { echo "docker is not installed or not on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "the Docker daemon is not running (start Docker Desktop)" >&2; exit 1; }
[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example (development defaults; never commit it)"; }

files=(--env-file .env --env-file deploy/compose/demo.env)
profiles=(--profile demo)
if [ "$mode" = full ]; then
  files+=(--env-file deploy/compose/standard.env --env-file deploy/compose/bi.env)
  profiles+=(--profile standard --profile bi)
fi
compose() { docker compose "${files[@]}" "${profiles[@]}" "$@"; }

if [ "$down" = 1 ]; then compose down; exit 0; fi
if [ "$reset" = 1 ]; then echo "removing containers and volumes (all demo data)"; compose down -v; fi

echo "starting the $mode stack (first build takes several minutes)"
compose up -d --build

wait_http() {  # url label seconds
  local deadline=$((SECONDS + $3))
  printf "waiting for %s " "$2"
  until curl -fsS -m 5 "$1" >/dev/null 2>&1; do
    if [ $SECONDS -ge $deadline ]; then echo " not ready after $3 s; see: docker compose logs $2" >&2; return 1; fi
    printf "."; sleep 5
  done
  echo " up"
}
wait_http http://localhost:8000/api/health api 600
wait_http http://localhost:5173/healthz web 300
[ "$mode" = full ] && wait_http http://localhost:8088/health superset 900

compose exec -T api analystos migrate
compose exec -T api analystos seed
compose exec -T api analystos demo-seed --api http://localhost:8000 --web http://localhost:5173

echo
curl -fsS -m 10 http://localhost:8000/api/health | python3 -c 'import json,sys; d=json.load(sys.stdin); print("health:", ", ".join(f"{k}={v.get(\"state\")}" for k, v in d["checks"].items())); [print("  !", p["message"]) for p in d.get("problems", [])]' 2>/dev/null || true
cat <<EOF

AnalystOS demo is ready ($mode)
  Web UI      http://localhost:5173
  API docs    http://localhost:8000/docs
EOF
[ "$mode" = full ] && echo "  Superset    http://localhost:8088   (admin / admin)"
cat <<EOF
  Sign in     analyst@analystos.local   ChangeMe123!   (runs investigations)
              approver@analystos.local  ChangeMe123!   (approves publication)
              admin@analystos.local     ChangeMe123!   (administration)
  Walkthrough docs/30-runbooks/05-demo-walkthrough.md
EOF
