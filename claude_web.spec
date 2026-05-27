# PyInstaller spec — produces a single-folder bundle for `claude-web`.
#
# Why onedir, not onefile: the frozen binary loads templates/, static/,
# and roundtable/ at runtime. With onefile, PyInstaller extracts those
# into a per-launch temp dir on every start, which slows boot and leaks
# directories on crash. onedir keeps them next to the .exe and starts
# instantly.
#
# Hidden imports: claude_agent_sdk, anthropic, google-genai, and openai
# all import via type-hint strings or lazy submodules that PyInstaller's
# static analyzer misses. The collect_submodules() calls below pull them
# in fully. uvicorn's loop/http/lifespan plug-ins are also lazy and need
# explicit declaration.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files


def _skip_optional_cli(name: str) -> bool:
    """Skip submodules that pull in optional CLI deps we don't ship.

    ``mcp.cli`` imports ``typer`` and ``sys.exit(1)`` when missing — the
    runtime app never touches CLI helpers, so excluding the whole subtree
    keeps the freeze deterministic without needing typer in
    requirements.txt.
    """
    return not name.startswith("mcp.cli")


hidden = []
for pkg, filt in (
    ("claude_agent_sdk", None),
    ("anthropic", None),
    ("google", None),
    ("google.genai", None),
    ("openai", None),
    ("mcp", _skip_optional_cli),
    ("uvicorn.loops", None),
    ("uvicorn.protocols", None),
    ("uvicorn.lifespan", None),
):
    if filt is None:
        hidden += collect_submodules(pkg)
    else:
        hidden += collect_submodules(pkg, filter=filt)

# Some SDKs ship data files (JSON schemas, prompt assets). Pull them in
# alongside the .py modules so the frozen binary can still find them.
datas = []
for pkg in ("claude_agent_sdk", "anthropic", "google.genai", "openai", "mcp"):
    datas += collect_data_files(pkg)

# App-owned data: templates and static assets live next to the binary
# under matching names so Jinja2Templates("templates") and
# StaticFiles(directory="static") resolve identically to the source
# layout.
#
# .env.example is intentionally NOT included here — PyInstaller's onedir
# mode buries datas inside `_internal/`, which is the wrong place for a
# file the user is supposed to find and copy. The release workflow drops
# .env.example as a sibling of the exe in the archive instead.
datas += [
    ("templates", "templates"),
    ("static", "static"),
]

# Roundtable is an OPTIONAL editable dependency that lives outside the
# repo; the directory exists in some development checkouts but is not
# committed. Include it when it's there so a local build is feature-
# complete, omit it otherwise — app.py's `from roundtable import ...`
# is wrapped in try/except and the /roundtable route renders a "not
# installed" panel when missing.
import os
if os.path.isdir("roundtable"):
    datas.append(("roundtable", "roundtable"))


a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="claude-web",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="claude-web",
)
