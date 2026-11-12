"""Derive browser/app icon sizes from the generated master without changing its design."""
from pathlib import Path
import shutil
from PIL import Image

root = Path(__file__).resolve().parents[1]
assets = root / 'assets'
source = Image.open(assets / 'irade-icon-source.png').convert('RGBA')
for size in (16, 32, 48, 128, 180, 192, 512):
    source.resize((size, size), Image.Resampling.LANCZOS).save(assets / f'irade-icon-{size}.png', optimize=True)
source.save(assets / 'irade-favicon.ico', format='ICO', sizes=[(16, 16), (32, 32), (48, 48)])

server = root.parent / 'IDE/resources/server'
for name in ('irade-icon-180.png', 'irade-icon-192.png', 'irade-icon-512.png', 'irade-favicon.ico'):
    shutil.copyfile(assets / name, server / name)
