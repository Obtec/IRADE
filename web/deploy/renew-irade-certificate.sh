#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
CERTBOT_CONFIG_DIR="${IRADE_CERTBOT_CONFIG_DIR:-$HOME/.config/irade/certbot}"
CERTBOT_WORK_DIR="${IRADE_CERTBOT_WORK_DIR:-$HOME/.local/state/irade/certbot-work}"
CERTBOT_LOG_DIR="${IRADE_CERTBOT_LOG_DIR:-$HOME/.local/state/irade/certbot-log}"
CERT_DIR="$CERTBOT_CONFIG_DIR/live/irade.p-e.kr"
PUBLIC_CERT_DIR="$HOME/.local/share/irade/letsencrypt/irade.p-e.kr"

if [[ -n "${IRADE_CERTBOT_BIN:-}" ]]; then
    irade_certbot_bin="$IRADE_CERTBOT_BIN"
elif [[ -x /usr/bin/certbot ]]; then
    irade_certbot_bin=/usr/bin/certbot
else
    irade_certbot_bin="$(command -v certbot || true)"
fi
[[ -n "$irade_certbot_bin" && -x "$irade_certbot_bin" ]] || {
    echo 'Certbot is unavailable; set IRADE_CERTBOT_BIN.' >&2
    exit 1
}

if openssl x509 -in "$CERT_DIR/cert.pem" -checkend $((30 * 24 * 60 * 60)) -noout >/dev/null 2>&1; then
    echo 'The certificate is valid for more than 30 days; no renewal is needed.'
    exit 0
fi

# The certificate was issued with manual DNS-01 validation and has no DNS API hook.
# Certbot renew cannot complete that challenge unattended, so request a new
# certificate interactively and let Certbot show the TXT record to publish.
"$irade_certbot_bin" certonly --manual --preferred-challenges dns --key-type ecdsa \
    --cert-name irade.p-e.kr -d irade.p-e.kr --agree-tos --force-renewal \
    --config-dir "$CERTBOT_CONFIG_DIR" \
    --work-dir "$CERTBOT_WORK_DIR" \
    --logs-dir "$CERTBOT_LOG_DIR"

"$REPO_DIR/wall/scripts/public-tls.sh" sync irade.p-e.kr \
    --source-dir "$CERT_DIR" --dest-dir "$PUBLIC_CERT_DIR" --no-reload

sudo nginx -t
sudo systemctl reload nginx
