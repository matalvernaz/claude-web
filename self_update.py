"""GitHub-release self-update for the portable Windows build.

Same mechanism as ffn-dl and genericMud: a small ``ZipExtractor.exe`` helper
(ravibpatel/AutoUpdater.NET, MIT, built from source in release.yml) ships
beside ``claude-web.exe``. To update we download the release zip, verify it,
unwrap its ``claude-web/`` folder into a flat zip, copy the helper to %TEMP%
so it is not locked inside the install dir, record a rollback snapshot
(``upgrade_manager``), spawn the helper, and exit. The helper waits for every
``claude-web`` process to end, overlays the zip onto the install directory
(``portable-data/``, ``tools/`` and ``.env`` are not in the zip, so they are
untouched) and relaunches the exe with the original arguments.

Two halves, so the download can happen while chats are live and the swap only
when they are not:

* :func:`stage_update` does everything that needs the network and leaves a
  ``staged.json`` marker in the install's ``.claude-web-upgrade`` folder.
* :func:`apply_staged` writes the rollback snapshot and spawns the helper.
  ``app.py`` calls it from the drain-restart watcher once no run is busy;
  ``launcher.py`` calls :func:`apply_pending_staged` at startup for a staged
  update the previous process never applied (console closed first), and
  ``upgrade_manager.recover_pending_upgrade`` rolls back a half-applied one.

Only a frozen Windows build with the helper beside it can self-replace
(:func:`can_self_replace`); everywhere else the check still runs so the UI
can point at the release page. HTTP is stdlib ``urllib``: ``api.github.com``
needs no browser impersonation, and a self-replace must reject a truncated
or oversized body outright, which is why this keeps its own downloader.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

import build_info
import upgrade_manager

logger = logging.getLogger("claude-web")

REPO = os.getenv("CLAUDE_WEB_UPDATE_REPO", "matalvernaz/claude-web").strip() or "matalvernaz/claude-web"
# A modest page covers far more than the handful of releases that ever sit
# above the installed version; the list form also sees prereleases, which
# ``/releases/latest`` hides.
_RELEASES_PER_PAGE = 30
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page={_RELEASES_PER_PAGE}"

APP_EXE = "claude-web.exe"
# Bundled beside claude-web.exe by release.yml. Without it we refuse to
# self-replace and report the release page instead.
ZIP_EXTRACTOR_EXE = "ZipExtractor.exe"
STAGED_FILE_NAME = "staged.json"

_USER_AGENT = f"claude-web-updater (+https://github.com/{REPO})"
_API_TIMEOUT_S = 15
_DOWNLOAD_TIMEOUT_S = 30  # per socket read; a healthy slow link still completes
_DOWNLOAD_CHUNK = 1 << 20  # 1 MiB
# The Windows bundle is ~180 MB zipped (the SDK's bundled CLI is most of it);
# cap the download and the uncompressed extraction well above that so a
# bad or hostile asset cannot fill the disk.
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB uncompressed
_WORKDIR_PREFIX = "claude-web-update-"

ProgressCallback = Optional[Callable[[int, int], None]]


# ─── version / release selection ───────────────────────────────────────────


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse ``v1.2.3`` into ``(1, 2, 3)``; ``None`` for anything else.

    Anchored so a prerelease-shaped tag like ``v1.2.3-beta`` does not parse
    as stable ``(1, 2, 3)``; those only come through with the prerelease
    opt-in, and then only by publish date.
    """
    if not tag:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_version() -> str:
    """The build's tag label (``vX.Y.Z``, a workflow_dispatch label, or ``dev``)."""
    return str(getattr(build_info, "VERSION", "") or "dev")


def current_built_at() -> str:
    return str(getattr(build_info, "BUILT_AT", "") or "")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get_json(url: str):
    request = Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urlopen(request, timeout=_API_TIMEOUT_S) as response:  # noqa: S310 - https api.github.com
        return json.loads(response.read().decode("utf-8"))


def select_asset(assets: list[dict]) -> dict | None:
    """The Windows portable zip (``claude-web-<tag>-windows-x64.zip``), or ``None``."""
    for asset in assets or []:
        name = str(asset.get("name", "")).lower()
        if name.endswith(".zip") and "windows" in name:
            return asset
    return None


def check_for_update(
    *, include_prerelease: bool = False, releases: list[dict] | None = None,
) -> dict | None:
    """Return info for the newest release above the running build, else ``None``.

    A versioned build (``vX.Y.Z``) is compared by version. A build whose label
    is not a version (``dev``, or a ``workflow_dispatch`` label such as
    ``2026-10-07-shared-projects``) is compared by date: any versioned
    release published after ``BUILT_AT`` counts. With no ``BUILT_AT`` either
    (a source checkout) nothing is ever offered.

    Network/JSON errors propagate; callers run this off the event loop and
    treat any exception as "couldn't check". The returned dict carries
    ``tag``, ``download_url``, ``size``, ``digest`` (``"sha256:<hex>"`` when
    GitHub populates it), ``release_url``, ``published_at`` and ``notes``.
    """
    current = parse_version(current_version())
    built_at = _parse_iso(current_built_at())
    if current is None and built_at is None:
        logger.info("self-update: running build has no version or build date; not checking")
        return None

    if releases is None:
        releases = _get_json(RELEASES_URL)
    best: tuple[tuple[int, int, int], dict] | None = None
    for release in releases or []:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not include_prerelease:
            continue
        parsed = parse_version(str(release.get("tag_name", "")))
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, release)
    if best is None:
        return None
    parsed, release = best

    if current is not None:
        if parsed <= current:
            return None
    else:
        published = _parse_iso(str(release.get("published_at") or ""))
        if published is None or built_at is None or published <= built_at:
            return None

    asset = select_asset(release.get("assets") or [])
    if asset is None:
        logger.warning(
            "self-update: release %s has no Windows zip asset; not offering it",
            release.get("tag_name"),
        )
        return None
    return {
        "tag": str(release["tag_name"]),
        "download_url": str(asset["browser_download_url"]),
        "size": int(asset.get("size") or 0),
        "digest": asset.get("digest"),
        "release_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "notes": release.get("body") or "",
    }


# ─── environment ───────────────────────────────────────────────────────────


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """Directory holding the exe (frozen) or this source file (dev)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def can_self_replace(install: Path | None = None) -> bool:
    """True only for a frozen Windows build with the helper bundled.

    ``.is_file()`` rather than ``.exists()`` so a directory accidentally named
    ``ZipExtractor.exe`` cannot make us offer an in-place update that would
    fail at the copy step.
    """
    if not (is_frozen() and sys.platform.startswith("win")):
        return False
    return ((install or install_dir()) / ZIP_EXTRACTOR_EXE).is_file()


# ─── download / verify / unpack ────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify_digest(path: Path, digest: str | None) -> None:
    """Check the download against the release asset's SHA-256 when GitHub supplied one.

    A missing digest is logged but not fatal: GitHub does not always populate
    it, and the bytes still arrived over HTTPS from github.com, so the channel
    is authenticated and the size checks in :func:`_download` still apply.
    """
    if not digest or ":" not in str(digest):
        logger.warning("self-update: asset has no SHA-256 digest; skipping content verification")
        return
    algorithm, expected = str(digest).split(":", 1)
    if algorithm.lower() != "sha256":
        logger.warning("self-update: asset uses unsupported digest %r; skipping verification", algorithm)
        return
    if _sha256_file(path).lower() != expected.lower():
        raise RuntimeError(
            "Downloaded update failed SHA-256 verification. It was not installed; "
            "the running version is unchanged."
        )


def _download(url: str, dest: Path, expected_size: int = 0, progress_cb: ProgressCallback = None) -> None:
    """Stream ``url`` to ``dest``; raise on HTTP errors, truncation, or overshoot.

    Both the ``Content-Length`` header and the release API's declared size are
    checked independently: a buggy or hostile server can serve a short body
    that matches its own header but disagrees with the asset size. Catching
    that here keeps a partial zip from ever reaching the extractor.
    """
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310 - https github
        header_size = int(response.headers.get("Content-Length") or 0)
        api_size = int(expected_size or 0)
        if header_size > 0 and api_size > 0 and header_size != api_size:
            raise RuntimeError(
                f"Update size mismatch: server reports {header_size} bytes, release API "
                f"reports {api_size}. Refusing to install."
            )
        max_expected = max((size for size in (header_size, api_size) if size > 0), default=0)
        cap = max_expected or _MAX_DOWNLOAD_BYTES
        done = 0
        with open(dest, "wb") as handle:
            while chunk := response.read(_DOWNLOAD_CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if done > cap:
                    raise RuntimeError(
                        f"Update download exceeded {cap} bytes; refusing (possible bad asset)."
                    )
                if progress_cb is not None:
                    progress_cb(done, max_expected)
    if header_size > 0 and done != header_size:
        raise RuntimeError(
            f"Update download truncated: got {done} bytes, expected {header_size}. "
            "The current version is unchanged; please retry."
        )
    if api_size > 0 and done != api_size:
        raise RuntimeError(
            f"Update download size mismatch: got {done} bytes, release API declared "
            f"{api_size}. The current version is unchanged."
        )


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract ``zip_path`` into ``dest``, refusing any member that escapes ``dest``.

    Stdlib ``extractall`` does not block ``../`` or absolute members. The
    digest already authenticates the bytes, but if a compromised release ever
    delivered a traversal payload we refuse it rather than write outside the
    extract dir. A total uncompressed-size cap likewise refuses a
    decompression bomb before writing anything.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > _MAX_EXTRACTED_BYTES:
            raise RuntimeError(
                f"Refusing to extract update: uncompressed size {total} exceeds the cap."
            )
        for info in archive.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise RuntimeError(f"Refusing to extract update: suspicious zip path {name!r}")
            target = (dest / name).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError as exc:
                raise RuntimeError(
                    f"Refusing to extract update: zip member escapes the extract dir: {name!r}"
                ) from exc
            archive.extract(info, dest)


def _find_app_root(extracted: Path, exe_name: str) -> Path:
    """The directory whose contents should overlay the install: the one holding the exe.

    The release zip wraps everything under ``claude-web/``, so the usual case
    is a single top-level dir. A malformed release (stray README,
    ``__MACOSX``) can add siblings, so look for the child dir that actually
    contains the exe, fall back to the bare root if the exe sits there, and
    refuse rather than half-install if neither matches.
    """
    for child in sorted(extracted.iterdir()):
        if child.is_dir() and (child / exe_name).is_file():
            return child
    if (extracted / exe_name).is_file():
        return extracted
    raise RuntimeError(
        f"Downloaded zip does not contain {exe_name} at the expected location. "
        "Update aborted; install unchanged."
    )


def _repack_flat(src_dir: Path, dest_zip: Path) -> None:
    """Re-zip ``src_dir``'s *contents* at the archive root.

    The release zip nests everything under ``claude-web/`` so a person who
    double-clicks it gets a tidy folder. ZipExtractor unpacks as-is into the
    install dir, so the wrapped zip would give ``install/claude-web/...``.
    Re-packing flat keeps one release working for both paths, and the flat
    zip's entries line up with the install layout the rollback manager
    verifies against. Stored (no recompression): the members are mostly
    already-compressed binaries and the file is deleted after the swap.
    """
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(src_dir).as_posix())


# ─── staging ───────────────────────────────────────────────────────────────


@dataclass
class Staged:
    """A verified update waiting for an idle moment to be applied."""

    tag: str
    flat_zip: str
    extractor: str
    workdir: str
    created_at: str

    @property
    def flat_zip_path(self) -> Path:
        return Path(self.flat_zip)

    @property
    def extractor_path(self) -> Path:
        return Path(self.extractor)

    @property
    def workdir_path(self) -> Path:
        return Path(self.workdir)


def _staged_path(install: Path) -> Path:
    return upgrade_manager.state_dir(install) / STAGED_FILE_NAME


def load_staged(install: Path | None = None) -> Staged | None:
    """The staged update recorded for ``install``, or ``None``.

    A marker whose files have vanished (temp cleaned) is discarded.
    """
    install = install or install_dir()
    path = _staged_path(install)
    if not path.is_file():
        return None
    try:
        staged = Staged(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("self-update: unreadable staged marker %s: %s", path, exc)
        discard_staged(install)
        return None
    if not (staged.flat_zip_path.is_file() and staged.extractor_path.is_file()):
        logger.warning("self-update: staged files for %s are gone; discarding marker", staged.tag)
        discard_staged(install)
        return None
    return staged


def discard_staged(install: Path | None = None) -> None:
    """Forget a staged update and delete its working directory."""
    install = install or install_dir()
    path = _staged_path(install)
    staged: Staged | None = None
    try:
        staged = Staged(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    try:
        path.unlink()
    except OSError:
        pass
    if staged is not None and staged.workdir_path.name.startswith(_WORKDIR_PREFIX):
        shutil.rmtree(staged.workdir_path, ignore_errors=True)


def stage_update(
    update_info: dict, install: Path | None = None, progress_cb: ProgressCallback = None,
) -> Staged:
    """Download, verify and unpack ``update_info``; leave it ready to apply.

    Nothing in the install directory is touched. The result (and a
    ``staged.json`` marker under ``.claude-web-upgrade``) points at a flat zip
    and a private copy of the helper in a fresh ``%TEMP%`` working directory.
    """
    install = install or install_dir()
    if not can_self_replace(install):
        raise RuntimeError(
            "In-place update needs the Windows portable build with ZipExtractor.exe "
            "bundled. Download the new version from the release page instead."
        )
    discard_staged(install)
    extractor_src = install / ZIP_EXTRACTOR_EXE
    workdir = Path(tempfile.mkdtemp(prefix=_WORKDIR_PREFIX))
    zip_path = workdir / "claude-web-windows.zip"
    extracted = workdir / "extracted"
    flat_zip = workdir / "claude-web-flat.zip"
    try:
        _download(
            update_info["download_url"], zip_path,
            expected_size=int(update_info.get("size") or 0), progress_cb=progress_cb,
        )
        _verify_digest(zip_path, update_info.get("digest"))
        _safe_extract(zip_path, extracted)
        zip_path.unlink(missing_ok=True)
        app_root = _find_app_root(extracted, APP_EXE)
        _repack_flat(app_root, flat_zip)
        shutil.rmtree(extracted, ignore_errors=True)
        # Copy the helper out of the install dir so it isn't locked when it
        # overwrites its own binary there. apply_staged re-hashes the copy
        # against the source right before spawning.
        extractor_tmp = workdir / ZIP_EXTRACTOR_EXE
        shutil.copy2(extractor_src, extractor_tmp)
        staged = Staged(
            tag=str(update_info.get("tag", "unknown")),
            flat_zip=str(flat_zip), extractor=str(extractor_tmp), workdir=str(workdir),
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        marker = _staged_path(install)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(asdict(staged), indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    logger.info("self-update: %s staged in %s", staged.tag, workdir)
    return staged


# ─── applying ──────────────────────────────────────────────────────────────


def _is_writable(path: Path) -> bool:
    """Whether this process can create/remove a file in ``path`` (else we must elevate)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".claude-web-update-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _shell_execute(verb: str, file: Path, params: str, cwd: Path) -> None:
    """``ShellExecuteW`` wrapper that raises on failure.

    ``subprocess`` cannot request the ``runas`` verb; ``ShellExecuteW`` is the
    only stdlib-reachable way to trigger a UAC prompt from Python. Argtypes
    and restype are declared so the 64-bit ``HINSTANCE`` return is not
    truncated by ctypes' default ``c_int``.
    """
    if not sys.platform.startswith("win"):
        raise RuntimeError("ShellExecuteW is only available on Windows")
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    if not getattr(shell32.ShellExecuteW, "_claude_web_signature_set", False):
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = wintypes.HINSTANCE
        shell32.ShellExecuteW._claude_web_signature_set = True
    sw_shownormal = 1
    handle = shell32.ShellExecuteW(None, verb, str(file), params, str(cwd), sw_shownormal)
    rc = int(ctypes.cast(handle, ctypes.c_void_p).value or 0)
    # ShellExecuteW returns > 32 on success; <= 32 are Win32 error codes.
    if rc <= 32:
        raise RuntimeError(f"ShellExecuteW failed (code {rc}) launching {file}")


def extractor_params(zip_path: Path, install: Path, exe: Path, argv: list[str]) -> str:
    """The helper's command line: overlay ``zip_path`` on ``install``, relaunch ``exe``.

    ``--args`` carries the launcher's original arguments so a ``--headless
    --port 3002`` install comes back the same way. Quoting goes through
    ``subprocess.list2cmdline`` because a hand-rolled ``f'"{path}"'`` breaks
    at a drive root (``"D:\\"`` parses as an escaped quote under
    ``CommandLineToArgvW``).
    """
    params = ["--input", str(zip_path), "--output", str(install), "--current-exe", str(exe)]
    if argv:
        params += ["--args", subprocess.list2cmdline(list(argv))]
    return subprocess.list2cmdline(params)


def _spawn_extractor(extractor: Path, zip_path: Path, install: Path, exe: Path, argv: list[str]) -> None:
    """Launch the helper to overlay ``zip_path`` and relaunch ``exe``.

    ``runas`` only when the install dir is not writable: the common case (a
    folder under the user's profile) needs no prompt, so the update is
    unattended.
    """
    verb = "open" if _is_writable(install) else "runas"
    _shell_execute(verb, extractor, extractor_params(zip_path, install, exe, argv), extractor.parent)


def apply_staged(staged: Staged, install: Path | None = None, argv: list[str] | None = None) -> None:
    """Arm rollback and spawn the helper for ``staged``. The caller must exit promptly.

    The helper blocks until every ``claude-web`` process has exited before it
    touches the install, then relaunches the exe. The staged marker is
    removed first so the relaunched binary does not try to apply it again;
    ``upgrade_manager``'s pending marker (written here) is what the next
    start verifies.
    """
    install = install or install_dir()
    if not can_self_replace(install):
        raise RuntimeError("in-place update is only possible for the frozen Windows build")
    extractor_src = install / ZIP_EXTRACTOR_EXE
    extractor_tmp = staged.extractor_path
    flat_zip = staged.flat_zip_path
    if not (flat_zip.is_file() and extractor_tmp.is_file()):
        discard_staged(install)
        raise RuntimeError("staged update files are missing; download it again")
    # Re-hash the staged helper against the bundled one immediately before an
    # (possibly elevated) spawn: low-privilege malware could otherwise swap
    # the temp copy and ride the user's UAC "Yes". A mismatch aborts.
    if _sha256_file(extractor_src) != _sha256_file(extractor_tmp):
        discard_staged(install)
        raise RuntimeError(
            "Update aborted: the ZipExtractor.exe staging copy did not match the "
            "bundled helper. Refusing to launch a possibly tampered helper."
        )
    upgrade_manager.prepare_for_upgrade(install, flat_zip, staged.tag, exe_name=APP_EXE)
    try:
        _staged_path(install).unlink()
    except OSError:
        pass
    _spawn_extractor(extractor_tmp, flat_zip, install, install / APP_EXE, list(argv or []))
    logger.info("self-update: helper launched for %s; exiting so it can swap files", staged.tag)


def apply_pending_staged(install: Path | None = None, argv: list[str] | None = None) -> bool:
    """Launcher hook: apply a staged update left behind by a previous process.

    Returns True when the helper was launched (the caller must exit so the
    swap can proceed), False when there was nothing to do or applying failed,
    in which case the marker is discarded and the app boots normally.
    """
    install = install or install_dir()
    if not can_self_replace(install):
        return False
    staged = load_staged(install)
    if staged is None:
        return False
    try:
        apply_staged(staged, install, argv)
        return True
    except Exception as exc:  # noqa: BLE001 - never block startup on an update
        logger.warning("self-update: could not apply staged %s at startup: %s", staged.tag, exc)
        discard_staged(install)
        return False


def cleanup_stale_workdirs(max_age_s: float = 24 * 3600) -> None:
    """Sweep ``%TEMP%/claude-web-update-*`` working dirs older than a day (best effort)."""
    cutoff = time.time() - max_age_s
    try:
        for path in Path(tempfile.gettempdir()).glob(_WORKDIR_PREFIX + "*"):
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
    except OSError as exc:
        logger.debug("self-update: could not sweep stale workdirs: %s", exc)
