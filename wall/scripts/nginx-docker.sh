#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
command_name="${1:-development}"
compose() { docker compose --project-directory "$PROJECT_DIR" --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/docker-compose.nginx.yml" "$@"; }
case "$command_name" in
  development|production)
    export FRONTEND_MODE="$command_name"
    python3 "$PROJECT_DIR/scripts/preflight.py"
    docker info >/dev/null 2>&1 || { echo 'Docker engine is unavailable or access is denied' >&2; exit 1; }
    compose config --quiet
    # Recreate so the mounted template is re-rendered (envsubst runs only at container start).
    compose up -d --force-recreate --wait --wait-timeout 60
    compose exec -T nginx nginx -t
    ;;
  reload)
    compose exec -T nginx nginx -t
    compose exec -T nginx nginx -s reload
    ;;
  config) compose config --quiet ;;
  test) compose exec -T nginx nginx -t ;;
  status) compose ps ;;
  stop) compose down ;;
  logs) compose logs --tail=100 --follow ;;
  *) echo 'Usage: nginx-docker.sh development|production|reload|config|test|status|stop|logs' >&2; exit 2 ;;
esac
