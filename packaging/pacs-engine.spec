# PyInstaller spec for the Carino PACS engine (onedir).
# Build from the repo root:
#     pyinstaller packaging/pacs-engine.spec --distpath desktop/engine --workpath build/pyi
# Produces:  desktop/engine/pacs-engine/pacs-engine[.exe]  (+ _internal/)
# The Electron app bundles that folder as an extraResource and launches the binary.

import os

from PyInstaller.utils.hooks import collect_all

# Paths in a .spec resolve relative to the spec's own dir (SPECPATH), so anchor
# everything to the repo root (the spec lives in <root>/packaging/).
ROOT = os.path.dirname(SPECPATH)

datas, binaries, hiddenimports = [], [], []
for pkg in ("pynetdicom", "pydicom", "PIL", "psutil"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Our Flask dashboard assets (served from disk by pacs/web.py).
datas += [(os.path.join(ROOT, "pacs", "web"), "pacs/web")]

a = Analysis(
    [os.path.join(ROOT, "packaging", "engine_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["flask", "werkzeug", "jinja2"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pacs-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # Electron spawns it with windowsHide:true, so no window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pacs-engine",
)
