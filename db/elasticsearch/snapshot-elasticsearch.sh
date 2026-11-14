#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD is required}"
source "$SCRIPT_DIR/curl-auth.sh"
ES_HOST="${ES_HOST:-${ES_EXTERNAL_IP:-127.0.0.1}}"
case "$ES_HOST" in 0.0.0.0|::) ES_HOST=127.0.0.1 ;; esac
ES_SCHEME="${ES_SCHEME:-https}"
ES_PORT="${ES_EXTERNAL_PORT:-${ES_PORT:-19200}}"
ES_URL="${ES_SCHEME}://${ES_HOST}:${ES_PORT}"
REPOSITORY="${ES_SNAPSHOT_REPOSITORY:-irade-filesystem}"
INDEX_NAME="${ES_INDEX:-workspace}"
LOG_INDEX="${ES_LOG_INDEX:-irade-logs}"
SNAPSHOT_NAME="irade-manual-$(date -u +%Y%m%dt%H%M%Sz)"
ES_CURL_TLS_ARGS=()
if [ -n "${ES_CA_CERT:-}" ]; then ES_CURL_TLS_ARGS+=(--cacert "$ES_CA_CERT"); fi
curl_es() { es_curl_authenticated elastic "$ELASTIC_PASSWORD" ${ES_CURL_TLS_ARGS[@]+"${ES_CURL_TLS_ARGS[@]}"} "$@"; }

curl_es -sS -f -X PUT "$ES_URL/_snapshot/$REPOSITORY" \
  -H 'Content-Type: application/json' \
  -d '{"type":"fs","settings":{"location":"/usr/share/elasticsearch/snapshots","compress":true}}' >/dev/null
curl_es -sS -f -X PUT "$ES_URL/_snapshot/$REPOSITORY/$SNAPSHOT_NAME?wait_for_completion=true" \
  -H 'Content-Type: application/json' \
  -d "{\"indices\":\"${INDEX_NAME},${LOG_INDEX}\",\"include_global_state\":false,\"ignore_unavailable\":true}" >/dev/null
echo "Elasticsearch snapshot saved: $SNAPSHOT_NAME"
