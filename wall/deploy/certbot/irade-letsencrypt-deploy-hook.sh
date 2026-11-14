#!/bin/sh
# Certbot deploy hook for proof-notebook.p-e.kr.
# Install to /etc/letsencrypt/renewal-hooks/deploy/91-irade-sync.sh
set -eu

DOMAIN="proof-notebook.p-e.kr"
case "${RENEWED_LINEAGE:-}" in
    */"$DOMAIN") ;;
    *) exit 0 ;;
esac

IRADE_USER="${IRADE_HOST_USER:-steam}"
DEST_DIR="${IRADE_PUBLIC_TLS_DIR:-/home/$IRADE_USER/.local/share/irade/letsencrypt/$DOMAIN}"
PUBLIC_TLS_SH="${IRADE_PUBLIC_TLS_SCRIPT:-/home/$IRADE_USER/Github/IRADE/wall/scripts/public-tls.sh}"

"$PUBLIC_TLS_SH" sync "$DOMAIN" --source-dir "$RENEWED_LINEAGE" --dest-dir "$DEST_DIR" --no-reload
chown -R "$IRADE_USER:$IRADE_USER" "$DEST_DIR"
chmod 750 "$DEST_DIR"
chmod 600 "$DEST_DIR/privkey.pem"
chmod 644 "$DEST_DIR/fullchain.pem"
if [ -f "$DEST_DIR/chain.pem" ]; then
    chmod 644 "$DEST_DIR/chain.pem"
fi

if command -v docker >/dev/null 2>&1 && [ "$(docker inspect -f '{{.State.Running}}' irade-nginx 2>/dev/null || true)" = "true" ]; then
    docker exec irade-nginx nginx -t
    docker exec irade-nginx nginx -s reload
fi

if command -v nginx >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
    /usr/sbin/nginx -t
    /bin/systemctl reload nginx
fi
