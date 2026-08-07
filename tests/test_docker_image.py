"""The Dockerfile must ship every first-party module the app hard-imports.

A user hit `ModuleNotFoundError: No module named 'conversation_replay'` on a plain
`docker run`: the Dockerfile listed the app modules by hand and that one was never
added when it landed. Nothing in the build imports app.py, so the image built clean,
published, and only failed at container startup.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = "app"  # the module uvicorn loads (`CMD ["uvicorn", "app:app", ...]`)


def _copy_sources() -> list[str]:
    """Every COPY source argument in the Dockerfile, destinations dropped."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    sources: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        args = [a for a in line.split()[1:] if not a.startswith("--")]
        sources.extend(args[:-1])  # the final argument is the destination
    return sources


def _shipped_names() -> set[str]:
    """Repo-root entry names the image receives, with COPY globs expanded."""
    return {path.name for source in _copy_sources() for path in ROOT.glob(source)}


def _module_path(name: str) -> Path | None:
    """Repo path backing a first-party module, or None if it isn't one of ours."""
    for candidate in (ROOT / f"{name}.py", ROOT / name / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _hard_imports(module: str) -> set[str]:
    """First-party modules reachable from ``module`` through unguarded imports.

    Only module-level statements count. An import nested in a try/except (the
    roundtable package) or inside a function is optional by construction: the app
    degrades when it's absent, so the image is free not to ship it.
    """
    reached: set[str] = set()
    queue = [module]
    while queue:
        name = queue.pop()
        if name in reached:
            continue
        path = _module_path(name)
        if path is None:
            continue
        reached.add(name)
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                queue.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                queue.append(node.module.split(".")[0])
    return reached


def test_dockerfile_ships_every_hard_imported_module():
    shipped = _shipped_names()
    missing = sorted(
        name
        for name in _hard_imports(ENTRYPOINT)
        if f"{name}.py" not in shipped and name not in shipped
    )
    assert not missing, f"Dockerfile does not COPY: {', '.join(missing)}"


def test_dockerfile_ships_the_web_assets():
    shipped = _shipped_names()
    assert {"static", "templates"} <= shipped
