"""The portable build's bundled CLIs: layout, release lookups, verified
installs, activation and cleanup.

The Windows zip used to carry a bare codex.exe. Codex 0.160 runs every tool
call through codex-code-mode-host.exe beside the binary, so each one failed
with "failed to spawn code-mode host" until the sidecar was copied in by
hand (2026-10-07). The package layout the official installer uses carries
it, and the same module installs that layout at build time and updates it
at run time, so the two can never drift apart again.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

import portable_tools as pt


# ─── versions ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("rust-v0.160.1", (0, 160, 1)),
    ("codex-cli 0.160.1", (0, 160, 1)),
    ("2.1.292 (Claude Code)", (2, 1, 292)),
    ("v2.56.0.windows.2", (2, 56, 0, 2)),
    ("git version 2.56.0.windows.2", (2, 56, 0, 2)),
    ("2.56.0.2", (2, 56, 0, 2)),
    ("nothing here", None),
    ("", None),
])
def test_versions_are_read_from_tags_and_version_lines(text, expected):
    assert pt.parse_version(text) == expected


def test_git_for_windows_release_and_binary_numbers_agree():
    assert pt.version_string("v2.56.0.windows.2") == pt.version_string("git version 2.56.0.windows.2")


def test_newer_compares_numerically_and_treats_unknown_as_older():
    assert pt.is_newer("0.161.0", "0.160.1")
    assert pt.is_newer("0.160.10", "0.160.9")
    assert not pt.is_newer("0.160.1", "0.160.1")
    assert not pt.is_newer("0.159.0", "0.160.1")
    assert pt.is_newer("2.56.0.2", None)
    assert not pt.is_newer("garbage", "0.1.0")


# ─── layout ────────────────────────────────────────────────────────────────

def _managed(base: Path, name: str, version: str, activate: bool = True) -> Path:
    directory = base / name / version
    exe = directory / pt.EXE_REL[name]
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"exe")
    if activate:
        pt.activate(name, version, base)
    return directory


def test_the_current_file_names_the_active_version(tmp_path):
    assert pt.current_version("codex", tmp_path) is None
    directory = _managed(tmp_path, "codex", "0.160.1")
    assert pt.current_version("codex", tmp_path) == "0.160.1"
    assert pt.managed_exe("codex", tmp_path) == directory / "bin" / "codex.exe"
    assert pt.resolve_exe("codex", tmp_path) == (directory / "bin" / "codex.exe", "managed")
    # A marker naming a directory that is gone counts as nothing installed.
    (tmp_path / "codex" / pt.CURRENT_FILE).write_text("9.9.9\n", encoding="utf-8")
    assert pt.current_version("codex", tmp_path) is None
    assert pt.managed_exe("codex", tmp_path) is None


def test_activation_refuses_a_version_that_is_not_installed(tmp_path):
    with pytest.raises(RuntimeError):
        pt.activate("codex", "0.1.0", tmp_path)


def test_the_flat_layouts_still_resolve_until_a_managed_copy_exists(tmp_path):
    legacy = tmp_path / "codex.exe"
    legacy.write_bytes(b"exe")
    git = tmp_path / "git" / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"exe")
    assert pt.resolve_exe("codex", tmp_path) == (legacy, "legacy")
    assert pt.resolve_exe("git", tmp_path) == (git, "legacy")
    assert pt.resolve_exe("claude", tmp_path) == (None, None)
    managed = _managed(tmp_path, "codex", "0.160.1")
    assert pt.resolve_exe("codex", tmp_path) == (managed / "bin" / "codex.exe", "managed")


def test_version_of_path_maps_an_executable_to_its_version_directory(tmp_path):
    directory = _managed(tmp_path, "codex", "0.160.1")
    assert pt.version_of_path("codex", directory / "bin" / "codex.exe", tmp_path) == "0.160.1"
    assert pt.version_of_path("codex", tmp_path / "codex.exe", tmp_path) is None
    assert pt.version_of_path("codex", None, tmp_path) is None
    assert pt.version_of_path("codex", "/elsewhere/codex.exe", tmp_path) is None


def test_cleanup_keeps_the_active_version_its_predecessor_and_anything_in_use(tmp_path):
    for version in ("0.158.0", "0.159.0", "0.160.1", "0.161.0"):
        _managed(tmp_path, "codex", version, activate=False)
    pt.activate("codex", "0.160.1", tmp_path)
    stale = tmp_path / "codex" / f"{pt.STAGING_PREFIX}0.157.0-123"
    stale.mkdir()
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = tmp_path / "codex" / f"{pt.STAGING_PREFIX}0.162.0-456"
    fresh.mkdir()
    removed = pt.cleanup("codex", {"0.158.0"}, tmp_path)
    assert sorted(removed) == [f"{pt.STAGING_PREFIX}0.157.0-123", "0.161.0"]
    assert sorted(pt.installed_versions("codex", tmp_path)) == ["0.158.0", "0.159.0", "0.160.1"]
    assert fresh.is_dir()  # an install in progress keeps its staging directory
    assert pt.previous_version("codex", tmp_path) == "0.159.0"


def test_discard_never_removes_the_active_version(tmp_path):
    _managed(tmp_path, "codex", "0.160.1")
    _managed(tmp_path, "codex", "0.161.0", activate=False)
    with pytest.raises(RuntimeError):
        pt.discard("codex", "0.160.1", tmp_path)
    pt.discard("codex", "0.161.0", tmp_path)
    assert pt.installed_versions("codex", tmp_path) == ["0.160.1"]


def test_legacy_files_are_retired_only_once_a_managed_copy_is_active(tmp_path):
    (tmp_path / "codex.exe").write_bytes(b"exe")
    (tmp_path / "codex-code-mode-host.exe").write_bytes(b"exe")
    (tmp_path / "git" / "cmd").mkdir(parents=True)
    (tmp_path / "git" / "cmd" / "git.exe").write_bytes(b"exe")
    assert pt.retire_legacy("codex", tmp_path) == []
    assert (tmp_path / "codex.exe").is_file()
    _managed(tmp_path, "codex", "0.160.1")
    assert sorted(pt.retire_legacy("codex", tmp_path)) == ["codex-code-mode-host.exe", "codex.exe"]
    assert not (tmp_path / "codex.exe").exists()
    (tmp_path / "git" / "git-bash.exe").write_bytes(b"exe")
    assert pt.retire_legacy("git", tmp_path) == []
    _managed(tmp_path, "git", "2.56.0.2")
    # The flat Git shared tools/git with the version directories: only its
    # own entries go.
    assert sorted(pt.retire_legacy("git", tmp_path)) == ["cmd", "git-bash.exe"]
    assert not (tmp_path / "git" / "cmd").exists()
    assert (tmp_path / "git" / "2.56.0.2" / "cmd" / "git.exe").is_file()
    assert pt.current_version("git", tmp_path) == "2.56.0.2"


def test_status_reports_what_is_on_disk(tmp_path):
    _managed(tmp_path, "codex", "0.160.1")
    _managed(tmp_path, "codex", "0.159.0", activate=False)
    out = pt.status(tmp_path)
    assert out["codex"]["version"] == "0.160.1"
    assert out["codex"]["source"] == "managed"
    assert out["codex"]["installed_versions"] == ["0.159.0", "0.160.1"]
    assert out["claude"] == {"source": None, "exe": None, "version": None, "installed_versions": []}


# ─── process environment ───────────────────────────────────────────────────

def test_codex_env_follows_the_active_copy_but_not_an_explicit_setting(tmp_path):
    env: dict = {}
    assert pt.apply_env("codex", tmp_path, env) is False
    legacy = tmp_path / "codex.exe"
    legacy.write_bytes(b"exe")
    assert pt.apply_env("codex", tmp_path, env) is True
    assert env[pt.CODEX_BIN_ENV] == str(legacy)
    managed = _managed(tmp_path, "codex", "0.160.1")
    assert pt.apply_env("codex", tmp_path, env) is True
    assert env[pt.CODEX_BIN_ENV] == str(managed / "bin" / "codex.exe")
    assert pt.apply_env("codex", tmp_path, env) is False
    custom = {pt.CODEX_BIN_ENV: str(tmp_path.parent / "my-own-codex.exe")}
    assert pt.apply_env("codex", tmp_path, custom) is False
    assert custom[pt.CODEX_BIN_ENV].endswith("my-own-codex.exe")


def _git_layout(base: Path, version: str | None) -> Path:
    directory = base / "git" / version if version else base / "git"
    for sub in ("cmd", "bin", "usr/bin"):
        (directory / sub).mkdir(parents=True, exist_ok=True)
    (directory / "cmd" / "git.exe").write_bytes(b"exe")
    (directory / "bin" / "bash.exe").write_bytes(b"exe")
    if version:
        pt.activate("git", version, base)
    return directory


def test_git_env_moves_bash_and_path_to_the_active_copy(tmp_path):
    env = {"PATH": "existing-tools"}
    legacy = _git_layout(tmp_path, None)
    assert pt.apply_env("git", tmp_path, env) is True
    assert env[pt.GIT_BASH_ENV] == str(legacy / "bin" / "bash.exe")
    assert env["PATH"].split(os.pathsep) == [
        str(legacy / "cmd"), str(legacy / "usr" / "bin"), "existing-tools"]
    assert pt.apply_env("git", tmp_path, env) is False  # idempotent
    managed = _git_layout(tmp_path, "2.56.0.2")
    assert pt.apply_env("git", tmp_path, env) is True
    assert env[pt.GIT_BASH_ENV] == str(managed / "bin" / "bash.exe")
    # The old entries are gone, not shadowed.
    assert env["PATH"].split(os.pathsep) == [
        str(managed / "cmd"), str(managed / "usr" / "bin"), "existing-tools"]
    env[pt.GIT_BASH_ENV] = "custom-bash"
    pt.apply_env("git", tmp_path, env)
    assert env[pt.GIT_BASH_ENV] == "custom-bash"


# ─── release lookups ───────────────────────────────────────────────────────

def _codex_release(tag="rust-v0.160.1"):
    return {
        "tag_name": tag, "html_url": "https://github.com/openai/codex/releases/tag/" + tag,
        "published_at": "2026-10-05T18:29:37Z",
        "assets": [
            {"name": "codex-x86_64-pc-windows-msvc.exe", "size": 1, "browser_download_url": "u1"},
            {"name": "codex-package-x86_64-pc-windows-msvc.tar.gz", "size": 157434529,
             "browser_download_url": "https://x/codex-package-x86_64-pc-windows-msvc.tar.gz",
             "digest": "sha256:" + "ab" * 32},
            {"name": "codex-package-aarch64-pc-windows-msvc.tar.gz", "size": 2,
             "browser_download_url": "u3", "digest": "sha256:" + "cd" * 32},
            {"name": "codex-package_SHA256SUMS", "size": 3, "browser_download_url": "https://x/sums"},
        ],
    }


def test_codex_latest_picks_the_package_for_this_machine(monkeypatch):
    monkeypatch.setattr(pt, "_arch", lambda: "x64")
    seen = []

    def fake_get_json(url):
        seen.append(url)
        return _codex_release()

    monkeypatch.setattr(pt, "_get_json", fake_get_json)
    candidate = pt.latest_codex()
    assert seen == ["https://api.github.com/repos/openai/codex/releases/latest"]
    assert candidate.version == "0.160.1" and candidate.label == "rust-v0.160.1"
    assert candidate.url.endswith("codex-package-x86_64-pc-windows-msvc.tar.gz")
    assert candidate.size == 157434529 and candidate.sha256 == "ab" * 32
    assert candidate.sums_url == "https://x/sums"
    assert "url" not in candidate.public() and candidate.public()["version"] == "0.160.1"


def test_codex_latest_refuses_a_release_without_the_package(monkeypatch):
    monkeypatch.setattr(pt, "_arch", lambda: "arm64")
    release = _codex_release()
    release["assets"] = [a for a in release["assets"] if "aarch64" not in a["name"]]
    monkeypatch.setattr(pt, "_get_json", lambda url: release)
    with pytest.raises(RuntimeError, match="aarch64"):
        pt.latest_codex()


def test_git_latest_picks_the_portable_archive(monkeypatch):
    monkeypatch.setattr(pt, "_arch", lambda: "x64")
    release = {
        "tag_name": "v2.56.0.windows.2", "html_url": "h", "published_at": "p",
        "assets": [
            {"name": "MinGit-2.56.0.2-64-bit.zip", "size": 1, "browser_download_url": "m"},
            {"name": "PortableGit-2.56.0.2-arm64.7z.exe", "size": 2, "browser_download_url": "a"},
            {"name": "PortableGit-2.56.0.2-64-bit.7z.exe", "size": 60027568,
             "browser_download_url": "https://x/PortableGit-2.56.0.2-64-bit.7z.exe",
             "digest": "sha256:" + "ef" * 32},
        ],
    }
    monkeypatch.setattr(pt, "_get_json", lambda url: release)
    candidate = pt.latest_git()
    assert candidate.version == "2.56.0.2" and candidate.label == "v2.56.0.windows.2"
    assert candidate.url.endswith("PortableGit-2.56.0.2-64-bit.7z.exe")
    assert candidate.sha256 == "ef" * 32 and candidate.sums_url is None


def test_claude_latest_reads_the_version_and_its_manifest(monkeypatch):
    monkeypatch.setattr(pt, "_arch", lambda: "x64")
    monkeypatch.setattr(pt, "_get_text", lambda url: "2.1.292\n")
    manifests = []

    def fake_get_json(url):
        manifests.append(url)
        return {"version": "2.1.292", "buildDate": "2026-10-06T05:43:35Z", "platforms": {
            "win32-x64": {"binary": "claude.exe", "checksum": "AB" * 32, "size": 254858400},
            "win32-arm64": {"binary": "claude.exe", "checksum": "cd" * 32, "size": 5},
        }}

    monkeypatch.setattr(pt, "_get_json", fake_get_json)
    candidate = pt.latest_claude()
    assert manifests == [f"{pt.CLAUDE_RELEASES_URL}/2.1.292/manifest.json"]
    assert candidate.version == "2.1.292"
    assert candidate.url == f"{pt.CLAUDE_RELEASES_URL}/2.1.292/win32-x64/claude.exe"
    assert candidate.size == 254858400 and candidate.sha256 == "ab" * 32


def test_claude_latest_rejects_an_html_error_page_as_a_version(monkeypatch):
    monkeypatch.setattr(pt, "_get_text", lambda url: "<html>503</html>")
    monkeypatch.setattr(pt, "_get_json", lambda url: pytest.fail("must not fetch a manifest"))
    with pytest.raises(RuntimeError):
        pt.latest_claude()


def test_a_github_token_only_goes_to_the_api(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert pt._headers("application/vnd.github+json")["Authorization"] == "Bearer secret"
    assert "Authorization" not in pt._headers()
    assert "Authorization" not in pt._headers("text/plain")


# ─── download / verify ─────────────────────────────────────────────────────

def _candidate(tool="codex", sha256="ab" * 32, sums_url=None, size=3, url="https://x/codex-package-x86_64-pc-windows-msvc.tar.gz"):
    return pt.Candidate(tool=tool, version="0.160.1", label="rust-v0.160.1", url=url,
                        size=size, sha256=sha256, sums_url=sums_url)


def test_the_sums_file_must_agree_with_the_api_digest(monkeypatch):
    monkeypatch.setattr(pt, "_get_text", lambda url: f"{'ab' * 32}  codex-package-x86_64-pc-windows-msvc.tar.gz\n")
    assert pt.expected_sha256(_candidate(sums_url="s")) == "ab" * 32
    monkeypatch.setattr(pt, "_get_text", lambda url: f"{'ff' * 32}  codex-package-x86_64-pc-windows-msvc.tar.gz\n")
    with pytest.raises(RuntimeError, match="disagrees"):
        pt.expected_sha256(_candidate(sums_url="s"))
    # The sums file fills in a digest the API did not publish.
    assert pt.expected_sha256(_candidate(sha256=None, sums_url="s")) == "ff" * 32
    assert pt.expected_sha256(_candidate(sha256=None)) is None


def test_a_download_whose_digest_mismatches_is_discarded(tmp_path, monkeypatch):
    import hashlib
    payload = b"abc"

    def fake_download(url, dest, expected_size=0, progress_cb=None):
        Path(dest).write_bytes(payload)

    monkeypatch.setattr(pt.self_update, "_download", fake_download)
    good = _candidate(sha256=hashlib.sha256(payload).hexdigest())
    assert pt.download(good, tmp_path / "ok").read_bytes() == payload
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        pt.download(_candidate(sha256="00" * 32), tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


# ─── unpack / install ──────────────────────────────────────────────────────

def _tar_gz(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _codex_members(**overrides) -> dict[str, bytes]:
    members = {name: b"x" for name in pt.CODEX_PACKAGE_FILES}
    members["codex-package.json"] = json.dumps({"layoutVersion": 1}).encode()
    members.update(overrides)
    return members


def test_tar_members_that_escape_the_target_are_refused(tmp_path):
    archive = _tar_gz(tmp_path / "bad.tar.gz", _codex_members(**{"../escape.exe": b"x"}))
    with pytest.raises(RuntimeError, match="suspicious"):
        pt._safe_extract_tar(archive, tmp_path / "out")
    assert not (tmp_path / "escape.exe").exists()


def test_an_incomplete_codex_package_is_refused(tmp_path):
    members = _codex_members()
    del members["bin/codex-code-mode-host.exe"]
    archive = _tar_gz(tmp_path / "short.tar.gz", members)
    with pytest.raises(RuntimeError, match="codex-code-mode-host.exe"):
        pt.unpack_codex(archive, tmp_path / "out")


@pytest.fixture
def fake_fetch(monkeypatch, tmp_path):
    """Serve a release file from disk instead of the network and skip hashing."""
    served: dict[str, Path] = {}

    def fake_download(candidate, dest, progress_cb=None):
        source = served[candidate.tool]
        Path(dest).write_bytes(source.read_bytes())
        return dest

    monkeypatch.setattr(pt, "download", fake_download)
    monkeypatch.setattr(pt, "version_of_executable", lambda name, exe: "0.160.1")
    return served


def test_install_unpacks_verifies_and_leaves_the_version_unactivated(tmp_path, fake_fetch):
    fake_fetch["codex"] = _tar_gz(tmp_path / "pkg.tar.gz", _codex_members())
    base = tmp_path / "tools"
    directory = pt.install("codex", _candidate(), base)
    assert directory == base / "codex" / "0.160.1"
    assert (directory / "bin" / "codex-code-mode-host.exe").is_file()
    manifest = pt.read_manifest("codex", "0.160.1", base)
    assert manifest["version"] == "0.160.1" and manifest["label"] == "rust-v0.160.1"
    assert manifest["sha256"] and manifest["installed_at"]
    assert pt.current_version("codex", base) is None  # the caller activates
    assert not list((base / "codex").glob(pt.STAGING_PREFIX + "*"))
    assert pt.activate("codex", "0.160.1", base) == directory
    assert pt.installed_version("codex", base) == "0.160.1"


def test_install_refuses_a_program_reporting_another_version(tmp_path, fake_fetch, monkeypatch):
    fake_fetch["codex"] = _tar_gz(tmp_path / "pkg.tar.gz", _codex_members())
    monkeypatch.setattr(pt, "version_of_executable", lambda name, exe: "0.159.0")
    base = tmp_path / "tools"
    with pytest.raises(RuntimeError, match="reports version 0.159.0"):
        pt.install("codex", _candidate(), base)
    assert not (base / "codex" / "0.160.1").exists()
    assert not list((base / "codex").glob(pt.STAGING_PREFIX + "*"))


def test_install_places_the_claude_binary_in_its_version_directory(tmp_path, fake_fetch):
    source = tmp_path / "claude.exe"
    source.write_bytes(b"claude")
    fake_fetch["claude"] = source
    base = tmp_path / "tools"
    candidate = pt.Candidate(tool="claude", version="0.160.1", label="0.160.1",
                             url=f"{pt.CLAUDE_RELEASES_URL}/0.160.1/win32-x64/claude.exe",
                             size=6, sha256=None)
    directory = pt.install("claude", candidate, base)
    assert (directory / "claude.exe").read_bytes() == b"claude"


def test_git_unpacking_runs_the_archive_and_its_post_install(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, cwd, timeout):
        calls.append(args)
        if args[0].endswith(".7z.exe"):
            for name in pt.GIT_PACKAGE_FILES:
                (cwd / name).parent.mkdir(parents=True, exist_ok=True)
                (cwd / name).write_bytes(b"exe")
            (cwd / "post-install.bat").write_text("@echo done\n", encoding="utf-8")

        class Result:
            returncode = 0
            stdout = stderr = ""
        return Result()

    monkeypatch.setattr(pt, "_run", fake_run)
    sfx = tmp_path / "PortableGit-2.56.0.2-64-bit.7z.exe"
    sfx.write_bytes(b"sfx")
    staging = tmp_path / "staging"
    pt.unpack_git(sfx, staging)
    assert calls[0] == [str(sfx), "-y", f"-o{staging}"]
    assert calls[1][:3] == ["cmd.exe", "/d", "/c"] and calls[1][3].endswith("post-install.bat")


# ─── check ─────────────────────────────────────────────────────────────────

def test_check_knows_when_an_install_is_due(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "latest", lambda name: _candidate(tool=name))
    monkeypatch.setattr(pt, "version_of_executable", lambda name, exe: "0.160.1")
    # Nothing installed: due.
    assert pt.check("codex", tmp_path)["due"] is True
    # A bare codex.exe at the published version: still due, for the sidecar.
    (tmp_path / "codex.exe").write_bytes(b"exe")
    info = pt.check("codex", tmp_path)
    assert (info["installed"], info["source"], info["due"]) == ("0.160.1", "legacy", True)
    # A hand-extracted Git at the published version is left alone.
    (tmp_path / "git" / "cmd").mkdir(parents=True)
    (tmp_path / "git" / "cmd" / "git.exe").write_bytes(b"exe")
    assert pt.check("git", tmp_path)["due"] is False
    # A managed copy behind the published version is due; a current one is not.
    _managed(tmp_path, "codex", "0.159.0")
    assert pt.check("codex", tmp_path)["due"] is True
    _managed(tmp_path, "codex", "0.160.1")
    assert pt.check("codex", tmp_path)["due"] is False


def test_enabled_only_on_windows_with_a_tools_directory(monkeypatch):
    monkeypatch.setattr(pt.sys, "platform", "linux")
    monkeypatch.setenv(pt.TOOLS_DIR_ENV, "/x")
    assert pt.enabled() is False
    monkeypatch.setattr(pt.sys, "platform", "win32")
    assert pt.enabled() is True
    monkeypatch.delenv(pt.TOOLS_DIR_ENV)
    monkeypatch.setattr(pt.self_update, "is_frozen", lambda: False)
    assert pt.enabled() is False
    monkeypatch.setattr(pt.self_update, "is_frozen", lambda: True)
    assert pt.enabled() is True


def test_the_command_line_status_prints_the_layout(tmp_path, capsys):
    _managed(tmp_path, "git", "2.56.0.2")
    assert pt.main(["status", "--dest", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["git"]["version"] == "2.56.0.2"


def test_an_explicit_setting_outside_tools_marks_the_tool_unmanaged(tmp_path):
    assert pt.is_overridden("codex", tmp_path, {}) is False
    inside = {pt.CODEX_BIN_ENV: str(tmp_path / "codex" / "0.160.1" / "bin" / "codex.exe")}
    assert pt.is_overridden("codex", tmp_path, inside) is False
    outside = {pt.CODEX_BIN_ENV: str(tmp_path.parent / "codex.exe")}
    assert pt.is_overridden("codex", tmp_path, outside) is True
    assert pt.is_overridden("git", tmp_path, {pt.GIT_BASH_ENV: "C:/Program Files/Git/bin/bash.exe"}) is True
    assert pt.is_overridden("claude", tmp_path, {}) is False
