# IRADE Public Let's Encrypt TLS Guide (`proof-notebook.p-e.kr`)

IRADE enforces **HTTPS / WSS only** across all components:

- **Public Gateway / Host Edge**: `https://proof-notebook.p-e.kr` (Let's Encrypt ECDSA certificate managed by Certbot)
- **Wall Gateway (`irade-nginx`)**: Terminates public TLS (`NGINX_PUBLIC_TLS_CERT_DIR`) and validates internal service upstreams (`irade-spring`, `irade-cpp`, `irade-web`, `irade-agent`, `irade-broker`) using the private IRADE CA (`NGINX_TLS_CERT_DIR/root-ca.pem`)
- **Web Studio (`web/scripts/serve-https.mjs`)**: HTTPS-only (`node:https`, TLSv1.3); `serve.mjs` delegates strictly to `serve-https.mjs`
- **Proof Kernel Studio (`notebook-kernel`)**: HTTPS-only (`lean4-kernel serve`, TLSv1.2+ with `--tls-dir`, `--cert`/`--key`, or `--self-signed`)

Public Let's Encrypt certificates are kept strictly separated from the internal IRADE CA and storage certificates. Host templates in `wall/deploy/` use separate files so existing host services remain untouched.

---

## 1. Prerequisites & DNS Setup

1. Ensure the DNS `A` record for `proof-notebook.p-e.kr` resolves to this host's public IP (`118.34.144.201`):
   ```bash
   dig +short A proof-notebook.p-e.kr
   ```
2. Verify existing Certbot & OpenSSL installation:
   ```bash
   /home/steam/Github/IRADE/wall/scripts/public-tls.sh status proof-notebook.p-e.kr
   ```

---

## 2. Issuing the Let's Encrypt Certificate

Use `wall/scripts/public-tls.sh issue` depending on whether host TCP/80 is reachable:

### Option A: DNS-01 Challenge (Works When Host TCP/80 Is Closed)

```bash
sudo /home/steam/Github/IRADE/wall/scripts/public-tls.sh issue proof-notebook.p-e.kr --challenge dns
```

Follow Certbot's prompt to add the `_acme-challenge.proof-notebook.p-e.kr` TXT record in your DNS provider, verify propagation (`dig +short TXT _acme-challenge.proof-notebook.p-e.kr`), and press Enter to complete issuance.

### Option B: HTTP-01 Webroot Challenge (When Host TCP/80 Is Open)

Uncomment the port 80 ACME + 301 redirect `server {}` block in [wall/deploy/nginx/proof-notebook.p-e.kr.conf](../../wall/deploy/nginx/proof-notebook.p-e.kr.conf) (or start Docker with `wall/docker-compose.nginx.letsencrypt.yml`), create `/var/www/letsencrypt`, reload Nginx, and run:

```bash
sudo mkdir -p /var/www/letsencrypt
sudo /home/steam/Github/IRADE/wall/scripts/public-tls.sh issue proof-notebook.p-e.kr \
  --challenge webroot --webroot /var/www/letsencrypt
```

---

## 3. Installing Host Nginx Virtual Host & Automated Renewal

Install the isolated host templates cleanly (no existing site files are modified):

```bash
# 1. Host Nginx virtual host for proof-notebook.p-e.kr
sudo cp /home/steam/Github/IRADE/wall/deploy/nginx/proof-notebook.p-e.kr.conf \
  /etc/nginx/sites-available/proof-notebook.p-e.kr.conf
sudo ln -sf /etc/nginx/sites-available/proof-notebook.p-e.kr.conf \
  /etc/nginx/sites-enabled/proof-notebook.p-e.kr.conf
sudo nginx -t && sudo systemctl reload nginx

# 2. Certbot deploy hook (verifies SAN/pubkey, syncs to ~/.local/share/irade/letsencrypt, reloads irade-nginx)
sudo install -m 0755 /home/steam/Github/IRADE/wall/deploy/certbot/irade-letsencrypt-deploy-hook.sh \
  /etc/letsencrypt/renewal-hooks/deploy/91-irade-sync.sh

# 3. Twice-daily Certbot renewal systemd timer
sudo cp /home/steam/Github/IRADE/wall/deploy/certbot/irade-certbot.service \
  /etc/systemd/system/irade-certbot.service
sudo cp /home/steam/Github/IRADE/wall/deploy/certbot/irade-certbot.timer \
  /etc/systemd/system/irade-certbot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now irade-certbot.timer
```

Run an immediate sync and dry-run renewal test:

```bash
sudo RENEWED_LINEAGE=/etc/letsencrypt/live/proof-notebook.p-e.kr \
  /etc/letsencrypt/renewal-hooks/deploy/91-irade-sync.sh
sudo /home/steam/Github/IRADE/wall/scripts/public-tls.sh verify-renewal proof-notebook.p-e.kr
```

---

## 4. Configuring IRADE Docker & Gateway with Public + Internal TLS

Point both `backend` and `wall` at the synced Let's Encrypt certificate directory while preserving internal `tls-dev` mTLS/CA validation:

```bash
PUBLIC_TLS_DIR="$HOME/.local/share/irade/letsencrypt/proof-notebook.p-e.kr"
INTERNAL_TLS_DIR="$HOME/.local/share/irade/tls-dev"

python3 /home/steam/Github/IRADE/backend/scripts/setup-docker.py \
  --db-dir /home/steam/Github/IRADE/db \
  --tls-dir "$INTERNAL_TLS_DIR" \
  --public-tls-dir "$PUBLIC_TLS_DIR" \
  --public-origin https://proof-notebook.p-e.kr

python3 /home/steam/Github/IRADE/wall/scripts/setup.py \
  --tls-dir "$INTERNAL_TLS_DIR" \
  --public-tls-dir "$PUBLIC_TLS_DIR" \
  --public-origin https://proof-notebook.p-e.kr \
  --backend-dir /home/steam/Github/IRADE/backend

cd /home/steam/Github/IRADE/wall
./scripts/nginx-docker.sh production
```

This sets:

- `NGINX_PUBLIC_TLS_CERT_DIR=$HOME/.local/share/irade/letsencrypt/proof-notebook.p-e.kr`
- `NGINX_PUBLIC_HOSTNAME=proof-notebook.p-e.kr`
- `NGINX_EDGE_TRUST=system` (verified against `/etc/ssl/certs/ca-certificates.crt` in both host Nginx `proxy_ssl_verify` and `irade-nginx` Docker healthcheck)
- `PASSKEY_RP_ID=proof-notebook.p-e.kr`, `PASSKEY_ORIGIN=https://proof-notebook.p-e.kr`, OAuth redirect URIs, and `BROKER_ALLOWED_ORIGINS`

For local development before a public certificate is issued, omit `--public-tls-dir` and keep `--public-origin https://localhost:8453` (`NGINX_EDGE_TRUST=internal`).
