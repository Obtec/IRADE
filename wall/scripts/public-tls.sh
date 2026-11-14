#!/usr/bin/env bash
# Manage IRADE public Let's Encrypt certificates via Certbot and sync them to Docker/Nginx.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WALL_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEFAULT_DOMAIN="${IRADE_PUBLIC_DOMAIN:-proof-notebook.p-e.kr}"
CERTBOT_BIN="${IRADE_CERTBOT_BIN:-$(command -v /snap/bin/certbot 2>/dev/null || command -v certbot 2>/dev/null || true)}"

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: public-tls.sh <command> [domain] [options]

Commands:
  status [domain]
      Show live Let's Encrypt certificate details (if readable), local synced cert, and Certbot timers.
  issue <domain> [--challenge dns|webroot|standalone] [--webroot /var/www/letsencrypt] [--email addr]
      Obtain an ECDSA Let's Encrypt certificate via Certbot (defaults to dns when port 80 is closed).
  verify-renewal [domain]
      Run Certbot dry-run renewal with deploy hooks.
  renew [domain]
      Run Certbot renewal for the named certificate.
  sync [domain] [--source-dir /etc/letsencrypt/live/<domain>] [--dest-dir ~/.local/share/irade/letsencrypt/<domain>] [--no-reload]
      Verify fullchain/privkey match and SAN, copy into local IRADE directory with safe permissions, and reload irade-nginx.
EOF
}

validate_domain() {
    local value="$1"
    python3 - "$value" <<'PY'
import ipaddress
import re
import sys

value = sys.argv[1]
if not value or len(value) > 253 or value.startswith('.') or value.startswith('-') or '..' in value:
    sys.exit('Invalid domain or IP certificate name.')
try:
    addr = ipaddress.ip_address(value)
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        sys.exit('Public TLS target cannot be loopback or unspecified.')
    sys.exit(0)
except ValueError:
    pass
if not re.fullmatch(r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}', value):
    sys.exit('Provide a valid public DNS hostname (e.g. proof-notebook.p-e.kr) or public IP.')
PY
}

require_certbot() {
    [[ -n "$CERTBOT_BIN" && -x "$CERTBOT_BIN" ]] || die "Certbot is unavailable. Install Certbot or set IRADE_CERTBOT_BIN."
}

verify_pair_and_san() {
    local cert="$1"
    local key="$2"
    local domain="$3"
    python3 - "$cert" "$key" "$domain" <<'PY'
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

cert, key, domain = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if not cert.is_file() or not key.is_file():
    sys.exit(f"Missing certificate or key: {cert} / {key}")
cert_text = cert.read_text(encoding="utf-8")
if "-----BEGIN CERTIFICATE-----" not in cert_text or "PRIVATE KEY-----" in cert_text:
    sys.exit(f"Invalid public fullchain bundle: {cert}")

pub_pem = subprocess.run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"], check=True, capture_output=True).stdout
cert_der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=pub_pem, check=True, capture_output=True).stdout
key_der = subprocess.run(["openssl", "pkey", "-in", str(key), "-passin", "pass:", "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout
if cert_der != key_der:
    sys.exit("Certificate public key does not match private key.")

expiry = subprocess.run(["openssl", "x509", "-in", str(cert), "-checkend", "86400", "-noout"], capture_output=True)
if expiry.returncode:
    sys.exit("Certificate is expired or expires within 24 hours.")

x509_text = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-text"], check=True, capture_output=True, text=True).stdout
match = re.search(r"X509v3 Subject Alternative Name:[^\n]*\n([^\n]+)", x509_text)
if not match:
    sys.exit("Certificate is missing Subject Alternative Name entries.")
entries = [item.strip() for item in match.group(1).split(",")]

def matches_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        for entry in entries:
            if entry.startswith("IP Address:") and ipaddress.ip_address(entry.split(":", 1)[1].strip()) == ip:
                return True
        return False
    except ValueError:
        host_lower = host.lower()
        for entry in entries:
            if entry.startswith("DNS:"):
                pattern = entry[4:].strip().lower()
                if pattern == host_lower:
                    return True
                if pattern.startswith("*.") and host_lower.count(".") == pattern.count(".") and host_lower.endswith(pattern[1:]):
                    return True
        return False

if not matches_host(domain):
    sys.exit(f"Certificate SAN does not match {domain}.")
print(f"Verified public TLS bundle and SAN for {domain}.")
PY
}

cmd="${1:-status}"
if [[ "$cmd" == "-h" || "$cmd" == "--help" ]]; then
    usage
    exit 0
fi
shift || true

domain="$DEFAULT_DOMAIN"
if [[ $# -gt 0 && "${1:-}" != --* ]]; then
    domain="$1"
    shift
fi
validate_domain "$domain"

case "$cmd" in
    status)
        if [[ -n "$CERTBOT_BIN" && -x "$CERTBOT_BIN" ]]; then
            "$CERTBOT_BIN" --version || true
        fi
        live_cert="/etc/letsencrypt/live/$domain/fullchain.pem"
        synced_cert="${IRADE_PUBLIC_TLS_DIR:-$HOME/.local/share/irade/letsencrypt/$domain}/fullchain.pem"
        if [[ -r "$live_cert" ]]; then
            printf 'Live Let'\''s Encrypt certificate (%s):\n' "$live_cert"
            openssl x509 -in "$live_cert" -noout -issuer -subject -dates -ext subjectAltName
        else
            printf 'Live Let'\''s Encrypt path not directly readable by current user (%s).\n' "$live_cert"
        fi
        if [[ -r "$synced_cert" ]]; then
            printf 'Synced IRADE certificate (%s):\n' "$synced_cert"
            openssl x509 -in "$synced_cert" -noout -issuer -subject -dates -ext subjectAltName
        else
            printf 'No synced IRADE certificate at %s\n' "$synced_cert"
        fi
        systemctl list-timers '*certbot*' --all --no-pager 2>/dev/null || true
        ;;
    issue)
        require_certbot
        challenge="dns"
        webroot="/var/www/letsencrypt"
        email=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --challenge) challenge="${2:?Missing value for --challenge}"; shift 2 ;;
                --webroot) webroot="${2:?Missing value for --webroot}"; shift 2 ;;
                --email) email="${2:?Missing value for --email}"; shift 2 ;;
                *) die "Unknown option: $1" ;;
            esac
        done
        args=(certonly --key-type ecdsa --cert-name "$domain" -d "$domain")
        if [[ -n "$email" ]]; then
            args+=(--email "$email" --agree-tos)
        fi
        case "$challenge" in
            dns) args+=(--manual --preferred-challenges dns) ;;
            webroot) args+=(--webroot -w "$webroot") ;;
            standalone) args+=(--standalone) ;;
            *) die "Unsupported challenge '$challenge'. Use dns, webroot, or standalone." ;;
        esac
        exec "$CERTBOT_BIN" "${args[@]}"
        ;;
    verify-renewal)
        require_certbot
        exec "$CERTBOT_BIN" renew --cert-name "$domain" --dry-run --run-deploy-hooks --no-random-sleep-on-renew
        ;;
    renew)
        require_certbot
        exec "$CERTBOT_BIN" renew --cert-name "$domain" --no-random-sleep-on-renew
        ;;
    sync)
        source_dir="/etc/letsencrypt/live/$domain"
        dest_dir="${IRADE_PUBLIC_TLS_DIR:-$HOME/.local/share/irade/letsencrypt/$domain}"
        reload_nginx=1
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --source-dir) source_dir="${2:?Missing value for --source-dir}"; shift 2 ;;
                --dest-dir) dest_dir="${2:?Missing value for --dest-dir}"; shift 2 ;;
                --no-reload) reload_nginx=0; shift ;;
                *) die "Unknown option: $1" ;;
            esac
        done
        [[ -r "$source_dir/fullchain.pem" && -r "$source_dir/privkey.pem" ]] \
            || die "Cannot read $source_dir/{fullchain.pem,privkey.pem}. Run with sufficient privileges or pass --source-dir."
        verify_pair_and_san "$source_dir/fullchain.pem" "$source_dir/privkey.pem" "$domain"
        umask 077
        install -d -m 0750 "$dest_dir"
        tmp_full="$(mktemp "$dest_dir/.fullchain.XXXXXX")"
        tmp_key="$(mktemp "$dest_dir/.privkey.XXXXXX")"
        trap 'rm -f "$tmp_full" "$tmp_key"' EXIT
        install -m 0644 "$source_dir/fullchain.pem" "$tmp_full"
        install -m 0600 "$source_dir/privkey.pem" "$tmp_key"
        mv -f "$tmp_full" "$dest_dir/fullchain.pem"
        mv -f "$tmp_key" "$dest_dir/privkey.pem"
        if [[ -r "$source_dir/chain.pem" ]]; then
            tmp_chain="$(mktemp "$dest_dir/.chain.XXXXXX")"
            install -m 0644 "$source_dir/chain.pem" "$tmp_chain"
            mv -f "$tmp_chain" "$dest_dir/chain.pem"
        fi
        printf 'Synced verified Let'\''s Encrypt certificate for %s into %s\n' "$domain" "$dest_dir"
        if [[ "$reload_nginx" -eq 1 ]] && command -v docker >/dev/null 2>&1; then
            if [[ "$(docker inspect -f '{{.State.Running}}' irade-nginx 2>/dev/null || true)" == "true" ]]; then
                "$WALL_DIR/scripts/nginx-docker.sh" reload
            fi
        fi
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
