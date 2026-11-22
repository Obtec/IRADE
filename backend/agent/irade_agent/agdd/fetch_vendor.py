"""Download pinned browser libraries (xterm, Monaco) for offline AGDD console use.

    python -m irade_agent.agdd.fetch_vendor [DEST]

Files land in ``DEST/<package>@<version>/...`` mirroring the jsDelivr layout, so the
console can swap ``https://cdn.jsdelivr.net/npm/<pkg>@<ver>/`` for ``static/vendor/<pkg>@<ver>/``
(see ``api.localize_vendor_urls``). Tarballs are verified against pinned SHA-512 integrity values.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent / "static" / "vendor"

# (package, version, tarball url, npm integrity, path prefixes inside "package/" to keep)
PACKAGES = (
    ("@xterm/xterm", "5.5.0", "https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz",
     "sha512-hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==",
     ("lib/xterm.js", "css/xterm.css")),
    ("@xterm/addon-fit", "0.10.0", "https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz",
     "sha512-UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==",
     ("lib/addon-fit.js",)),
    ("monaco-editor", "0.52.2", "https://registry.npmjs.org/monaco-editor/-/monaco-editor-0.52.2.tgz",
     "sha512-GEQWEZmfkOGLdd3XK8ryrfWz3AIP8YymVXiPHEdewrUq7mh0qrKrfHLNCXcbB6sTnMLnOZ3ztSiKcciFUkIJwQ==",
     ("min/vs/",)),
)


def vendor_dir() -> Path:
    return Path(os.environ.get("AGDD_VENDOR_DIR") or DEFAULT_DIR)


def _verify(data: bytes, integrity: str) -> None:
    algo, _, b64 = integrity.partition("-")
    digest = base64.b64encode(hashlib.new(algo, data).digest()).decode()
    if digest != b64:
        raise RuntimeError(f"integrity mismatch ({algo}): expected {b64}, got {digest}")


def fetch(dest: Path) -> list[str]:
    done = []
    for name, ver, url, integrity, keep in PACKAGES:
        root = dest / f"{name}@{ver}"
        marker = root / ".complete"
        if marker.is_file():
            done.append(f"{name}@{ver} (cached)")
            continue
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        _verify(data, integrity)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            members = []
            for m in tf.getmembers():
                if not m.isfile() or not m.name.startswith("package/"):
                    continue
                rel = m.name[len("package/"):]
                if any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in keep):
                    m.name = rel
                    members.append(m)
            if not members:
                raise RuntimeError(f"{name}@{ver}: no files matched {keep}")
            root.mkdir(parents=True, exist_ok=True)
            tf.extractall(root, members=members, filter="data")
        marker.write_text(integrity + "\n")
        done.append(f"{name}@{ver} ({len(members)} files)")
    return done


def main(argv: list[str]) -> int:
    dest = Path(argv[1]) if len(argv) > 1 else vendor_dir()
    for line in fetch(dest):
        print(f"vendor: {line} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
