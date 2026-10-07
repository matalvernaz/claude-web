"""The CLIs the portable Windows build carries, and how they stay current.

The frozen build cannot rely on anything being installed on the machine it
is unzipped on, so it brings its own copies of the three programs a chat
depends on, in ``tools/`` beside ``claude-web.exe``:

* **codex** — OpenAI's Codex CLI, as the official *package* layout from the
  ``codex-package-<target>.tar.gz`` release asset (``bin/codex.exe``,
  ``bin/codex-code-mode-host.exe``, ``codex-path/rg.exe``, …). The bare
  ``codex.exe`` release asset is not enough: since 0.160 every tool call runs
  through the code-mode host beside it, and without it each one fails with
  "failed to spawn code-mode host".
* **claude** — Claude Code's native Windows binary from Anthropic's release
  bucket, verified against the per-version ``manifest.json`` checksum. The
  Agent SDK's bundled copy stays as the fallback; it is frozen at whatever
  the SDK shipped, while this one is kept current.
* **git** — Portable Git for Windows (the ``PortableGit-*.7z.exe`` release),
  which gives the Claude CLI its Bash and the model its Git.

Layout: ``tools/<name>/<version>/…`` plus a ``tools/<name>/current`` file
naming the active version. A new version is downloaded, verified and
unpacked into a sibling directory and only then pointed at, so nothing a
running process holds open is ever replaced or renamed; old directories are
removed later, once no process uses them. The earlier flat layouts
(``tools/codex.exe``, a ``tools/git`` extracted by hand) still resolve and
are retired after the first managed install.

``release.yml`` runs ``python portable_tools.py install --dest …`` so the
``-full`` zip ships the same layout the updater in ``app.py`` maintains.
HTTP is stdlib ``urllib`` like ``self_update``: no browser impersonation is
needed for api.github.com or downloads.claude.ai, and the size/digest checks
there are exactly what an executable download must have.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

import self_update

logger = logging.getLogger("claude-web")

TOOLS_DIR_ENV = "CLAUDE_WEB_TOOLS_DIR"
CODEX_BIN_ENV = "CLAUDE_WEB_CODEX_BIN"
GIT_BASH_ENV = "CLAUDE_CODE_GIT_BASH_PATH"
CURRENT_FILE = "current"
MANIFEST_FILE = "claude-web-tool.json"
STAGING_PREFIX = ".staging-"
TOOL_NAMES = ("codex", "claude", "git")

CODEX_REPO = "openai/codex"
GIT_REPO = "git-for-windows/git"
CLAUDE_RELEASES_URL = "https://downloads.claude.ai/claude-code-releases"

_USER_AGENT = "claude-web-tools (+https://github.com/matalvernaz/claude-web)"
_API_TIMEOUT_S = 20
_SUBPROCESS_TIMEOUT_S = 60
_UNPACK_TIMEOUT_S = 900
_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
_VERSION_RE = re.compile(r"(\d+(?:\.\d+){1,3})")

# Files every codex package must contain (the official installer's own
# completeness check); a tarball missing one is refused before activation.
CODEX_PACKAGE_FILES = (
    "codex-package.json",
    "bin/codex.exe",
    "bin/codex-code-mode-host.exe",
    "codex-path/rg.exe",
    "codex-resources/codex-command-runner.exe",
    "codex-resources/codex-windows-sandbox-setup.exe",
)
GIT_PACKAGE_FILES = ("cmd/git.exe", "bin/bash.exe", "usr/bin/bash.exe")

# Relative path of each tool's executable inside a version directory.
EXE_REL = {
    "codex": "bin/codex.exe",
    "claude": "claude.exe",
    "git": "cmd/git.exe",
}

ProgressCallback = Optional[Callable[[int, int], None]]


# ─── versions ──────────────────────────────────────────────────────────────


def parse_version(text: str) -> tuple[int, ...] | None:
    """``(2, 56, 0, 2)`` from any string carrying a dotted number; ``None`` if none."""
    if not text:
        return None
    match = _VERSION_RE.search(normalize_version(text))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def normalize_version(text: str) -> str:
    """Git for Windows numbers releases ``2.56.0.windows.2``; the portable asset
    and ``git --version`` agree once ``windows.`` is dropped (``2.56.0.2``).
    Other tools pass through unchanged."""
    return (text or "").strip().replace(".windows.", ".")


def version_string(text: str) -> str | None:
    parsed = parse_version(text)
    return ".".join(str(part) for part in parsed) if parsed else None


def is_newer(candidate: str, installed: str | None) -> bool:
    new = parse_version(candidate)
    if new is None:
        return False
    old = parse_version(installed or "")
    return old is None or new > old


# ─── layout ────────────────────────────────────────────────────────────────


def enabled() -> bool:
    """Whether this process manages a ``tools/`` directory: the frozen Windows
    build, or any Windows run told where the directory is."""
    if not sys.platform.startswith("win"):
        return False
    return self_update.is_frozen() or bool(os.environ.get(TOOLS_DIR_ENV))


def tools_dir() -> Path:
    override = os.environ.get(TOOLS_DIR_ENV)
    if override:
        return Path(override)
    return self_update.install_dir() / "tools"


def tool_root(name: str, base: Path | None = None) -> Path:
    return (base or tools_dir()) / name


def _current_file(name: str, base: Path | None = None) -> Path:
    return tool_root(name, base) / CURRENT_FILE


def current_version(name: str, base: Path | None = None) -> str | None:
    """The version ``tools/<name>/current`` names, when that directory exists."""
    try:
        version = _current_file(name, base).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not version or not (tool_root(name, base) / version).is_dir():
        return None
    return version


def version_dir(name: str, version: str, base: Path | None = None) -> Path:
    return tool_root(name, base) / version


def current_dir(name: str, base: Path | None = None) -> Path | None:
    version = current_version(name, base)
    return version_dir(name, version, base) if version else None


def managed_exe(name: str, base: Path | None = None) -> Path | None:
    """The active managed executable, or ``None`` when there is no managed copy."""
    directory = current_dir(name, base)
    if directory is None:
        return None
    exe = directory / EXE_REL[name]
    return exe if exe.is_file() else None


def legacy_exe(name: str, base: Path | None = None) -> Path | None:
    """The pre-versioned layouts: ``tools/codex.exe`` and a hand-extracted ``tools/git``."""
    root = base or tools_dir()
    if name == "codex":
        # A POSIX portable copy carried a bare ``codex``; Windows ``codex.exe``.
        candidates = [root / "codex.exe", root / "codex"]
    elif name == "git":
        candidates = [root / "git" / "cmd" / "git.exe"]
    else:
        return None
    return next((exe for exe in candidates if exe.is_file()), None)


def resolve_exe(name: str, base: Path | None = None) -> tuple[Path | None, str | None]:
    """``(path, "managed" | "legacy" | None)`` for the copy that should run."""
    exe = managed_exe(name, base)
    if exe is not None:
        return exe, "managed"
    exe = legacy_exe(name, base)
    if exe is not None:
        return exe, "legacy"
    return None, None


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def version_of_path(name: str, path: str | os.PathLike | None, base: Path | None = None) -> str | None:
    """Which version directory ``path`` (an executable) lives in, else ``None``."""
    if not path:
        return None
    root = tool_root(name, base)
    try:
        relative = Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return relative.parts[0] if relative.parts else None


def read_manifest(name: str, version: str, base: Path | None = None) -> dict | None:
    path = version_dir(name, version, base) / MANIFEST_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed_version(name: str, base: Path | None = None) -> str | None:
    """The managed version, else the legacy copy's ``--version``, else ``None``."""
    version = current_version(name, base)
    if version:
        return version
    exe = legacy_exe(name, base)
    if exe is None:
        return None
    return version_of_executable(name, exe)


def version_of_executable(name: str, exe: Path | str) -> str | None:
    """Run ``<exe> --version`` and parse the number; ``None`` when it cannot run."""
    try:
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    first = text.splitlines()[0] if text else ""
    return version_string(first)


# ─── release lookups ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """A downloadable release of a tool."""

    tool: str
    version: str
    label: str
    url: str
    size: int
    sha256: Optional[str]
    published_at: Optional[str] = None
    release_url: Optional[str] = None
    sums_url: Optional[str] = None

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in ("url", "sums_url")}


def _headers(accept: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and accept and "github" in accept:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, accept: str | None = None) -> bytes:
    request = Request(url, headers=_headers(accept))
    with urlopen(request, timeout=_API_TIMEOUT_S) as response:  # noqa: S310 - https only
        return response.read()


def _get_json(url: str):
    return json.loads(_get(url, "application/vnd.github+json").decode("utf-8"))


def _get_text(url: str) -> str:
    return _get(url).decode("utf-8", "replace")


def _github_latest(repo: str) -> dict:
    """``/releases/latest``: the newest published release that is neither a
    draft nor a prerelease, which is the only kind we install unattended."""
    return _get_json(f"https://api.github.com/repos/{repo}/releases/latest")


def _digest_hex(asset: dict) -> str | None:
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    return None


def _asset(release: dict, name: str) -> dict | None:
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            return asset
    return None


def _arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x64"


def codex_target() -> str:
    return "aarch64-pc-windows-msvc" if _arch() == "arm64" else "x86_64-pc-windows-msvc"


def claude_platform() -> str:
    return "win32-arm64" if _arch() == "arm64" else "win32-x64"


def git_asset_suffix() -> str:
    return "arm64" if _arch() == "arm64" else "64-bit"


def latest_codex() -> Candidate:
    release = _github_latest(CODEX_REPO)
    tag = str(release.get("tag_name") or "")
    version = version_string(tag)
    if not version:
        raise RuntimeError(f"codex: cannot read a version from release tag {tag!r}")
    name = f"codex-package-{codex_target()}.tar.gz"
    asset = _asset(release, name)
    if asset is None:
        raise RuntimeError(f"codex: release {tag} has no {name} asset")
    sums = _asset(release, "codex-package_SHA256SUMS")
    return Candidate(
        tool="codex", version=version, label=tag,
        url=str(asset["browser_download_url"]), size=int(asset.get("size") or 0),
        sha256=_digest_hex(asset), published_at=release.get("published_at"),
        release_url=release.get("html_url"),
        sums_url=str(sums["browser_download_url"]) if sums else None,
    )


def latest_claude() -> Candidate:
    version_text = _get_text(f"{CLAUDE_RELEASES_URL}/latest").strip()
    version = version_string(version_text)
    if not version or not re.fullmatch(r"\d+\.\d+\.\d+", version_text):
        raise RuntimeError(f"claude: unexpected latest-version response {version_text[:80]!r}")
    manifest = _get_json(f"{CLAUDE_RELEASES_URL}/{version_text}/manifest.json")
    entry = (manifest.get("platforms") or {}).get(claude_platform()) or {}
    checksum = str(entry.get("checksum") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise RuntimeError(f"claude: manifest for {version_text} has no {claude_platform()} checksum")
    return Candidate(
        tool="claude", version=version, label=version_text,
        url=f"{CLAUDE_RELEASES_URL}/{version_text}/{claude_platform()}/claude.exe",
        size=int(entry.get("size") or 0), sha256=checksum,
        published_at=manifest.get("buildDate"),
        release_url="https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md",
    )


def latest_git() -> Candidate:
    release = _github_latest(GIT_REPO)
    tag = str(release.get("tag_name") or "")
    version = version_string(tag)
    if not version:
        raise RuntimeError(f"git: cannot read a version from release tag {tag!r}")
    suffix = git_asset_suffix()
    pattern = re.compile(rf"^PortableGit-.*-{re.escape(suffix)}\.7z\.exe$")
    asset = next((a for a in release.get("assets") or [] if pattern.match(str(a.get("name", "")))), None)
    if asset is None:
        raise RuntimeError(f"git: release {tag} has no PortableGit {suffix} asset")
    return Candidate(
        tool="git", version=version, label=tag,
        url=str(asset["browser_download_url"]), size=int(asset.get("size") or 0),
        sha256=_digest_hex(asset), published_at=release.get("published_at"),
        release_url=release.get("html_url"),
    )


LATEST = {"codex": latest_codex, "claude": latest_claude, "git": latest_git}


def latest(name: str) -> Candidate:
    return LATEST[name]()


def check(name: str, base: Path | None = None) -> dict:
    """What is installed, what is published, and whether an install is due.

    Network errors propagate. A legacy ``codex.exe`` always counts as due:
    that layout lacks the code-mode host and ripgrep the package carries. A
    legacy Git at the published version is left alone until a newer one
    exists; replacing a working copy with the same version buys nothing.
    """
    exe, source = resolve_exe(name, base)
    installed = installed_version(name, base) if exe is not None else None
    candidate = latest(name)
    due = is_newer(candidate.version, installed)
    if not due and source == "legacy" and name == "codex":
        due = True
    return {
        "tool": name, "installed": installed, "source": source,
        "exe": str(exe) if exe else None, "latest": candidate, "due": due,
    }


# ─── download / verify / unpack ────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    return self_update._sha256_file(path)


def _parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[-1].lstrip("*")] = parts[0].lower()
    return sums


def expected_sha256(candidate: Candidate) -> str | None:
    """The digest to verify against: the release API's, cross-checked with the
    release's own SHA256SUMS file when it publishes one. Disagreement is an
    error; neither being available is logged, like ``self_update``: the bytes
    still come over HTTPS from the vendor's host and the size is checked."""
    expected = candidate.sha256
    if candidate.sums_url:
        try:
            sums = _parse_sums(_get_text(candidate.sums_url))
        except OSError as exc:
            logger.warning("%s: could not read SHA256SUMS (%s); using the API digest", candidate.tool, exc)
            sums = {}
        listed = sums.get(candidate.url.rsplit("/", 1)[-1])
        if listed and expected and listed != expected:
            raise RuntimeError(
                f"{candidate.tool}: SHA256SUMS ({listed}) disagrees with the release API "
                f"digest ({expected}); refusing the download")
        expected = expected or listed
    if not expected:
        logger.warning("%s: no SHA-256 published for %s; relying on HTTPS and size checks",
                       candidate.tool, candidate.label)
    return expected


def download(candidate: Candidate, dest: Path, progress_cb: ProgressCallback = None) -> Path:
    """Fetch the release file to ``dest`` and verify its size and digest."""
    expected = expected_sha256(candidate)
    self_update._download(candidate.url, dest, expected_size=candidate.size, progress_cb=progress_cb)
    if expected:
        actual = _sha256_file(dest)
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"{candidate.tool} {candidate.label}: SHA-256 mismatch (got {actual}, "
                f"expected {expected}); the download was discarded")
    return dest


def _safe_extract_tar(archive: Path, dest: Path) -> None:
    """Extract a tarball, refusing members that would land outside ``dest``.

    Python 3.12's ``data`` filter does exactly that (and strips dangerous
    modes); 3.11 builds without it get the same checks by hand.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        total = sum(m.size for m in members)
        if total > _MAX_EXTRACTED_BYTES:
            raise RuntimeError(f"refusing to extract {archive.name}: {total} bytes uncompressed")
        for member in members:
            name = member.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or (len(name) > 1 and name[1] == ":"):
                raise RuntimeError(f"refusing to extract {archive.name}: suspicious member {member.name!r}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"refusing to extract {archive.name}: link member {member.name!r}")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"refusing to extract {archive.name}: special member {member.name!r}")
        try:
            tar.extractall(dest, members=members, filter="data")
        except TypeError:  # Python < 3.11.4: no filter argument
            tar.extractall(dest, members=members)  # noqa: S202 - members were vetted above


def _require_files(directory: Path, names: tuple[str, ...], what: str) -> None:
    missing = [name for name in names if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(f"{what} is incomplete; missing {', '.join(missing)}")


def unpack_codex(archive: Path, staging: Path) -> None:
    _safe_extract_tar(archive, staging)
    _require_files(staging, CODEX_PACKAGE_FILES, "codex package")


def unpack_claude(binary: Path, staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    shutil.move(str(binary), str(staging / "claude.exe"))


def _run(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, check=False)


def unpack_git(sfx: Path, staging: Path) -> None:
    """Extract the PortableGit self-extracting 7-Zip archive silently, then
    run its ``post-install.bat`` the way the archive does when double-clicked
    (it creates the ``/dev`` entries and finishes the MSYS setup)."""
    staging.mkdir(parents=True, exist_ok=True)
    proc = _run([str(sfx), "-y", f"-o{staging}"], staging, _UNPACK_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(f"PortableGit extraction failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout or '').strip()[-300:]}")
    _require_files(staging, GIT_PACKAGE_FILES, "PortableGit")
    post_install = staging / "post-install.bat"
    if post_install.is_file():
        try:
            result = _run(["cmd.exe", "/d", "/c", str(post_install)], staging, _UNPACK_TIMEOUT_S)
            # The script deletes itself as its last step, so its absence
            # means it ran through; its exit status is that of whatever ran
            # last and is not meaningful on its own.
            if post_install.exists():
                logger.warning("git: post-install.bat did not complete (exit %s): %s",
                               result.returncode, (result.stdout or "")[-300:])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git: post-install.bat could not run: %s", exc)


UNPACK = {"codex": unpack_codex, "claude": unpack_claude, "git": unpack_git}


# ─── install / activate / clean up ─────────────────────────────────────────


def _write_manifest(name: str, candidate: Candidate, directory: Path, sha256: str | None) -> None:
    manifest = {
        "tool": name, "version": candidate.version, "label": candidate.label,
        "source_url": candidate.url, "sha256": sha256,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (directory / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def install(name: str, candidate: Candidate, base: Path | None = None,
            progress_cb: ProgressCallback = None) -> Path:
    """Download, verify and unpack ``candidate`` into ``tools/<name>/<version>``.

    Nothing is activated: the caller runs any further checks on the new
    directory (``app.py`` dumps the codex app-server schema) and then calls
    :func:`activate`. The download lands in ``%TEMP%``; unpacking happens in
    a staging sibling of the final directory so the last step is a rename.
    """
    root = tool_root(name, base)
    root.mkdir(parents=True, exist_ok=True)
    final = version_dir(name, candidate.version, base)
    staging = root / f"{STAGING_PREFIX}{candidate.version}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    workdir = Path(tempfile.mkdtemp(prefix="claude-web-tools-"))
    try:
        payload = workdir / candidate.url.rsplit("/", 1)[-1]
        download(candidate, payload, progress_cb)
        digest = _sha256_file(payload) if payload.is_file() else None
        UNPACK[name](payload, staging)
        exe = staging / EXE_REL[name]
        if not exe.is_file():
            raise RuntimeError(f"{name} {candidate.label}: {EXE_REL[name]} is missing after unpacking")
        found = version_of_executable(name, exe)
        if found != candidate.version:
            raise RuntimeError(
                f"{name} {candidate.label}: the unpacked program reports version "
                f"{found or 'unknown'}, not {candidate.version}")
        _write_manifest(name, candidate, staging, digest)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
            if final.exists():
                raise RuntimeError(f"{name}: a previous {candidate.version} directory is in use")
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    logger.info("%s %s installed in %s", name, candidate.version, final)
    return final


def activate(name: str, version: str, base: Path | None = None) -> Path:
    """Point ``current`` at ``version`` (atomic replace of the marker file)."""
    directory = version_dir(name, version, base)
    if not (directory / EXE_REL[name]).is_file():
        raise RuntimeError(f"{name} {version} is not installed")
    marker = _current_file(name, base)
    tmp = marker.with_name(f"{CURRENT_FILE}.{os.getpid()}.tmp")
    tmp.write_text(version + "\n", encoding="utf-8")
    os.replace(tmp, marker)
    return directory


def discard(name: str, version: str, base: Path | None = None) -> None:
    """Delete a version directory that failed validation (never the active one)."""
    if current_version(name, base) == version:
        raise RuntimeError(f"{name} {version} is active; activate another version first")
    shutil.rmtree(version_dir(name, version, base), ignore_errors=True)


def previous_version(name: str, base: Path | None = None) -> str | None:
    """The newest installed version below the active one, kept as a rollback."""
    active = current_version(name, base)
    candidates = [v for v in installed_versions(name, base) if v != active]
    if active:
        active_parsed = parse_version(active)
        candidates = [v for v in candidates if (parse_version(v) or ()) < (active_parsed or ())]
    return max(candidates, key=lambda v: parse_version(v) or ()) if candidates else None


def installed_versions(name: str, base: Path | None = None) -> list[str]:
    root = tool_root(name, base)
    if not root.is_dir():
        return []
    out = []
    for child in root.iterdir():
        if child.is_dir() and not child.name.startswith(STAGING_PREFIX) and parse_version(child.name):
            out.append(child.name)
    return out


def cleanup(name: str, keep: set[str] | None = None, base: Path | None = None) -> list[str]:
    """Remove stale staging directories and version directories not in ``keep``.

    Best effort: a directory a running process still holds files in stays
    (Windows refuses the delete) and is retried on a later pass. The active
    version and the one before it are always kept, so a bad release can be
    rolled back by hand with :func:`activate`.
    """
    root = tool_root(name, base)
    if not root.is_dir():
        return []
    protect = set(keep or ())
    active = current_version(name, base)
    if active:
        protect.add(active)
    rollback = previous_version(name, base)
    if rollback:
        protect.add(rollback)
    removed = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(STAGING_PREFIX):
            # An install in progress; only a leftover from a dead process goes.
            if time.time() - child.stat().st_mtime < 3600:
                continue
        elif not parse_version(child.name) or child.name in protect:
            continue
        shutil.rmtree(child, ignore_errors=True)
        if not child.exists():
            removed.append(child.name)
    return removed


def _is_managed_entry(entry: Path) -> bool:
    """Whether ``entry`` under ``tools/<name>`` belongs to the versioned layout."""
    if entry.name == CURRENT_FILE or entry.name.startswith(STAGING_PREFIX):
        return True
    return entry.is_dir() and parse_version(entry.name) is not None


def retire_legacy(name: str, base: Path | None = None) -> list[str]:
    """Delete the flat layout once a managed copy is active. Best effort.

    A hand-extracted Git sat directly in ``tools/git``, the same directory
    that now holds the version directories, so only its own entries (``cmd``,
    ``usr``, ``git-bash.exe``, …) are removed, never a version directory.
    """
    if managed_exe(name, base) is None:
        return []
    root = base or tools_dir()
    targets: list[Path] = []
    if name == "codex":
        targets = [root / "codex.exe", root / "codex-code-mode-host.exe"]
    elif name == "git" and (root / "git").is_dir():
        targets = [entry for entry in (root / "git").iterdir() if not _is_managed_entry(entry)]
    removed = []
    for target in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink()
        except OSError:
            continue
        if not target.exists():
            removed.append(target.name)
    return removed


# ─── process environment ───────────────────────────────────────────────────


def _owned(value: str | None, base: Path) -> bool:
    """Whether an env value is ours to replace: unset, or pointing into ``tools/``."""
    return not value or _under(Path(value), base)


def is_overridden(name: str, base: Path | None = None, environ: Optional[dict] = None) -> bool:
    """Whether the operator pointed the app at their own copy, outside ``tools/``,
    in which case nothing is downloaded for this tool."""
    env = os.environ if environ is None else environ
    root = base or tools_dir()
    if name == "codex":
        return not _owned(env.get(CODEX_BIN_ENV), root)
    if name == "git":
        return not _owned(env.get(GIT_BASH_ENV), root)
    return False


def apply_env(name: str, base: Path | None = None, environ: Optional[dict] = None) -> bool:
    """Point the process environment at the copy :func:`resolve_exe` picks.

    ``codex``: ``CLAUDE_WEB_CODEX_BIN`` (read per spawn by ``codex_provider``).
    ``git``: ``CLAUDE_CODE_GIT_BASH_PATH`` for the Claude CLI plus ``cmd`` and
    ``usr/bin`` at the front of ``PATH``; entries from older managed or legacy
    Git directories are dropped first so an update does not shadow itself.
    ``claude`` needs nothing: ``claude_cli`` asks this module at each spawn.
    A value the operator set explicitly (outside ``tools/``) is left alone.
    Returns whether anything changed.
    """
    env = os.environ if environ is None else environ
    root = base or tools_dir()
    exe, _source = resolve_exe(name, base)
    if name == "codex":
        if exe is None or not _owned(env.get(CODEX_BIN_ENV), root):
            return False
        if env.get(CODEX_BIN_ENV) == str(exe):
            return False
        env[CODEX_BIN_ENV] = str(exe)
        return True
    if name == "git":
        if exe is None:
            return False
        git_dir = exe.parent.parent
        bash = git_dir / "bin" / "bash.exe"
        changed = False
        if bash.is_file() and _owned(env.get(GIT_BASH_ENV), root) and env.get(GIT_BASH_ENV) != str(bash):
            env[GIT_BASH_ENV] = str(bash)
            changed = True
        git_root = root / "git"
        current_entries = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        kept = [p for p in current_entries if not _under(Path(p), git_root)]
        wanted = [str(d) for d in (git_dir / "cmd", git_dir / "usr" / "bin") if d.is_dir()]
        new_entries = wanted + [p for p in kept if p not in wanted]
        if new_entries != current_entries:
            env["PATH"] = os.pathsep.join(new_entries)
            changed = True
        return changed
    return False


def status(base: Path | None = None) -> dict:
    """What is on disk, for logs and the admin endpoint (no network)."""
    out = {}
    for name in TOOL_NAMES:
        exe, source = resolve_exe(name, base)
        out[name] = {
            "source": source, "exe": str(exe) if exe else None,
            "version": current_version(name, base),
            "installed_versions": sorted(installed_versions(name, base),
                                         key=lambda v: parse_version(v) or ()),
        }
    return out


# ─── command line (release build) ──────────────────────────────────────────


def _cli_install(dest: Path, names: list[str]) -> int:
    failures = 0
    for name in names:
        try:
            candidate = latest(name)
            print(f"{name}: installing {candidate.label} ({candidate.size} bytes)", flush=True)
            directory = install(name, candidate, dest)
            activate(name, candidate.version, dest)
            exe = directory / EXE_REL[name]
            print(f"{name}: {candidate.version} active at {exe}", flush=True)
        except Exception as exc:  # noqa: BLE001 - report every tool, then fail
            failures += 1
            print(f"{name}: FAILED: {exc}", file=sys.stderr, flush=True)
    return 1 if failures else 0


def _cli_check(dest: Path, names: list[str]) -> int:
    for name in names:
        try:
            info = check(name, dest)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: check failed: {exc}", file=sys.stderr, flush=True)
            continue
        candidate = info["latest"]
        print(f"{name}: installed {info['installed'] or '-'} ({info['source'] or 'none'}), "
              f"latest {candidate.version} ({candidate.label}), "
              f"{'update due' if info['due'] else 'current'}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or check the portable build's bundled CLIs.")
    parser.add_argument("command", choices=("install", "check", "status"))
    parser.add_argument("--dest", help="tools directory (default: the running build's tools/)")
    parser.add_argument("--tools", default=",".join(TOOL_NAMES),
                        help="comma-separated subset of codex,claude,git")
    args = parser.parse_args(argv)
    dest = Path(args.dest).resolve() if args.dest else tools_dir()
    names = [n.strip() for n in args.tools.split(",") if n.strip()]
    unknown = [n for n in names if n not in TOOL_NAMES]
    if unknown:
        parser.error(f"unknown tool(s): {', '.join(unknown)}")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "install":
        return _cli_install(dest, names)
    if args.command == "check":
        return _cli_check(dest, names)
    print(json.dumps(status(dest), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
