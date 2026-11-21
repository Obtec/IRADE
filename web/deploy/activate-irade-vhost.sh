#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VHOST_SOURCE="$SCRIPT_DIR/irade.p-e.kr.conf"
SITES_AVAILABLE="${IRADE_NGINX_SITES_AVAILABLE:-/etc/nginx/sites-available}"
SITES_ENABLED="${IRADE_NGINX_SITES_ENABLED:-/etc/nginx/sites-enabled}"
STREAMS_ENABLED="${IRADE_NGINX_STREAMS_ENABLED:-/etc/nginx/streams-enabled}"

if [[ "$EUID" -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

install -D -m 0644 "$VHOST_SOURCE" "$SITES_AVAILABLE/irade.p-e.kr.conf"
ln -sfn "$SITES_AVAILABLE/irade.p-e.kr.conf" "$SITES_ENABLED/irade.p-e.kr.conf"

python3 - "$STREAMS_ENABLED" <<'PY'
from pathlib import Path
import os
import re
import shutil
import stat
import sys
import tempfile

streams_dir = Path(sys.argv[1])
server_pattern = re.compile(r'\bserver\s*\{(?P<body>[^{}]*)\}', re.S)
map_pattern = re.compile(
    r'map\s+"?\$ssl_preread_server_name:\$ssl_preread_alpn_protocols"?\s+'
    r'\$(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*\{(?P<body>[^{}]*)\}', re.S,
)
matches = []

for path in sorted(streams_dir.glob('*.conf')):
    text = path.read_text()
    maps = list(map_pattern.finditer(text))
    if not maps:
        continue
    for server in server_pattern.finditer(text):
        body = server.group('body')
        if not re.search(r'\blisten\s+443\s*;', body) or not re.search(r'\bssl_preread\s+on\s*;', body):
            continue
        proxy_pass = re.search(r'\bproxy_pass\s+\$([A-Za-z_][A-Za-z0-9_]*)\s*;', body)
        if not proxy_pass:
            continue
        for map_match in maps:
            if map_match.group('variable') == proxy_pass.group(1):
                matches.append((path, text, map_match))

if len(matches) != 1:
    raise SystemExit('Expected one SNI map feeding the port 443 TLS listener; no files were changed.')

path, text, map_match = matches[0]
body = map_match.group('body')
route_pattern = re.compile(r'(?m)^\s*~[^;]*irade[.]p-e[.]kr[^;]*\s+(\S+)\s*;')
existing = route_pattern.search(body)
if existing:
    if existing.group(1) != '127.0.0.1:9446':
        raise SystemExit('An existing IRADE SNI route points elsewhere; no files were changed.')
    print('IRADE SNI route is already configured.')
else:
    default = re.search(r'(?m)^(?P<indent>\s*)default\s+[^;]+;', body)
    if not default:
        raise SystemExit('The SNI map has no default entry; no files were changed.')
    route = f"{default.group('indent')}~^irade[.]p-e[.]kr:.*$ 127.0.0.1:9446;\n"
    updated_body = body[:default.start()] + route + body[default.start():]
    start, end = map_match.start('body'), map_match.end('body')
    updated_text = text[:start] + updated_body + text[end:]

    backup = path.with_name(path.name + '.before-irade')
    if not backup.exists():
        shutil.copy2(path, backup)
    info = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(updated_text)
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'Added the IRADE SNI route in {path}.')
PY

nginx -t
systemctl reload nginx
