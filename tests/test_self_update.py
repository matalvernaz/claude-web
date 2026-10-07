"""Self-updater: release selection, download safety, staging/apply, launcher hooks.

The Windows file swap itself (ZipExtractor.exe) cannot run here; these cover
the pure-Python decision logic, the Zip-Slip guard, the staged marker
round-trip, the rollback snapshot it arms, and what the launcher does with a
staged or half-applied update at startup.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

import self_update
import upgrade_manager


# ─── version + release selection ───────────────────────────────────────────


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("v0.4.1", (0, 4, 1)),
        ("v1.2.3-beta", None),
        ("v1.2", None),
        ("2026-10-07-shared-projects", None),
        ("", None),
    ],
)
def test_parse_version(tag, expected):
    assert self_update.parse_version(tag) == expected


def test_select_asset_prefers_windows_zip():
    assets = [
        {"name": "claude-web-v0.5.0-linux-x64.tar.gz", "browser_download_url": "x"},
        {"name": "claude-web-v0.5.0-macos-arm64.tar.gz", "browser_download_url": "y"},
        {"name": "claude-web-v0.5.0-windows-x64.zip", "browser_download_url": "z"},
    ]
    assert self_update.select_asset(assets)["name"].endswith("windows-x64.zip")
    assert self_update.select_asset([{"name": "notes.txt"}]) is None


def _release(tag, *, draft=False, prerelease=False, with_asset=True, published="2026-10-08T00:00:00Z"):
    assets = [{
        "name": f"claude-web-{tag}-windows-x64.zip",
        "browser_download_url": f"http://x/{tag}.zip",
        "size": 42, "digest": None,
    }] if with_asset else []
    return {
        "tag_name": tag, "draft": draft, "prerelease": prerelease,
        "html_url": f"http://x/{tag}", "published_at": published, "body": "notes",
        "assets": assets,
    }


def _running(monkeypatch, version: str, built_at: str = "") -> None:
    monkeypatch.setattr(self_update.build_info, "VERSION", version)
    monkeypatch.setattr(self_update.build_info, "BUILT_AT", built_at)


def test_check_picks_newest_skipping_drafts_and_prereleases(monkeypatch):
    _running(monkeypatch, "v0.4.1")
    releases = [
        _release("v0.4.2"),
        _release("v0.5.0"),
        _release("v0.6.0", draft=True),
        _release("v0.7.0", prerelease=True),
        _release("nightly"),
    ]
    info = self_update.check_for_update(releases=releases)
    assert info is not None
    assert info["tag"] == "v0.5.0"
    assert info["download_url"] == "http://x/v0.5.0.zip"
    assert info["size"] == 42
    with_pre = self_update.check_for_update(releases=releases, include_prerelease=True)
    assert with_pre["tag"] == "v0.7.0"


def test_check_none_when_current_or_newer(monkeypatch):
    _running(monkeypatch, "v0.5.0")
    assert self_update.check_for_update(releases=[_release("v0.5.0")]) is None
    assert self_update.check_for_update(releases=[_release("v0.4.9")]) is None


def test_check_none_when_newer_release_has_no_windows_asset(monkeypatch):
    _running(monkeypatch, "v0.4.1")
    assert self_update.check_for_update(releases=[_release("v0.5.0", with_asset=False)]) is None


def test_check_label_build_compares_by_date(monkeypatch):
    """A workflow_dispatch build has a label, not a version: any versioned
    release published after it was built counts, an older one does not."""
    _running(monkeypatch, "2026-10-07-shared-projects", "2026-10-07T08:05:47Z")
    newer = _release("v0.5.0", published="2026-10-09T00:00:00Z")
    older = _release("v0.4.1", published="2026-09-28T02:13:45Z")
    assert self_update.check_for_update(releases=[older]) is None
    assert self_update.check_for_update(releases=[older, newer])["tag"] == "v0.5.0"


def test_check_dev_build_never_fetches(monkeypatch):
    _running(monkeypatch, "dev", "")

    def boom(_url):
        raise AssertionError("must not hit the network for a dev build")

    monkeypatch.setattr(self_update, "_get_json", boom)
    assert self_update.check_for_update() is None


def test_can_self_replace_requires_frozen_windows_and_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert not self_update.can_self_replace(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert not self_update.can_self_replace(tmp_path)
    (tmp_path / self_update.ZIP_EXTRACTOR_EXE).mkdir()  # a directory must not count
    assert not self_update.can_self_replace(tmp_path)
    (tmp_path / self_update.ZIP_EXTRACTOR_EXE).rmdir()
    (tmp_path / self_update.ZIP_EXTRACTOR_EXE).write_bytes(b"MZ")
    assert self_update.can_self_replace(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    assert not self_update.can_self_replace(tmp_path)


# ─── unpacking safety ──────────────────────────────────────────────────────


def test_safe_extract_normal_zip(tmp_path):
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("claude-web/claude-web.exe", b"exe")
        archive.writestr("claude-web/_internal/base_library.zip", b"lib")
    out = tmp_path / "out"
    self_update._safe_extract(src, out)
    assert (out / "claude-web" / "claude-web.exe").read_bytes() == b"exe"
    assert self_update._find_app_root(out, "claude-web.exe") == out / "claude-web"


@pytest.mark.parametrize("member", ["../escape.txt", "/abs/escape.txt", "a/../../b.txt"])
def test_safe_extract_rejects_traversal(tmp_path, member):
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr(member, b"pwned")
    with pytest.raises(RuntimeError):
        self_update._safe_extract(src, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_find_app_root_bare_and_missing(tmp_path):
    (tmp_path / "claude-web.exe").write_bytes(b"x")
    assert self_update._find_app_root(tmp_path, "claude-web.exe") == tmp_path
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError):
        self_update._find_app_root(empty, "claude-web.exe")


def test_repack_flat_puts_entries_at_root(tmp_path):
    src = tmp_path / "claude-web"
    (src / "_internal").mkdir(parents=True)
    (src / "claude-web.exe").write_bytes(b"exe")
    (src / "_internal" / "x.pyd").write_bytes(b"pyd")
    flat = tmp_path / "flat.zip"
    self_update._repack_flat(src, flat)
    with zipfile.ZipFile(flat) as archive:
        assert sorted(archive.namelist()) == ["_internal/x.pyd", "claude-web.exe"]


def test_extractor_params_quote_paths_and_relaunch_args():
    params = self_update.extractor_params(
        Path(r"C:\Temp\flat.zip"), Path(r"C:\Claude Web"), Path(r"C:\Claude Web\claude-web.exe"),
        ["--headless", "--port", "3002"],
    )
    assert '--input C:\\Temp\\flat.zip' in params
    assert '--output "C:\\Claude Web"' in params
    assert '--current-exe "C:\\Claude Web\\claude-web.exe"' in params
    assert '--args "--headless --port 3002"' in params
    assert "--clear" not in params
    assert "--args" not in self_update.extractor_params(Path("z"), Path("o"), Path("e"), [])


# ─── stage → apply round-trip (no real helper; the spawn is recorded) ──────


_OLD = {"claude-web.exe": b"old-exe", "_internal/base_library.zip": b"old-lib",
        "_internal/same.pyd": b"same", "ZipExtractor.exe": b"helper"}
_NEW = {"claude-web.exe": b"new-exe", "_internal/base_library.zip": b"new-lib",
        "_internal/same.pyd": b"same", "_internal/added.pyd": b"added", "ZipExtractor.exe": b"helper"}


def _write_tree(root: Path, contents: dict[str, bytes]) -> None:
    for rel, data in contents.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _release_zip(path: Path, contents: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for rel, data in contents.items():
            archive.writestr(f"claude-web/{rel}", data)
    return path


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """A fake portable install that self_update believes it can replace."""
    install = tmp_path / "Claude Web"
    _write_tree(install, _OLD)
    (install / "portable-data").mkdir()
    (install / "portable-data" / "keep.txt").write_text("mine", encoding="utf-8")
    monkeypatch.setattr(self_update, "can_self_replace", lambda install=None: True)
    release = _release_zip(tmp_path / "release.zip", _NEW)

    def fake_download(url, dest, expected_size=0, progress_cb=None):
        dest.write_bytes(release.read_bytes())

    monkeypatch.setattr(self_update, "_download", fake_download)
    workdirs = tmp_path / "temp"
    workdirs.mkdir()
    counter = iter(range(1000))

    def fake_mkdtemp(prefix=""):
        d = workdirs / f"{prefix}{next(counter)}"
        d.mkdir()
        return str(d)

    monkeypatch.setattr(self_update.tempfile, "mkdtemp", fake_mkdtemp)
    spawned: list[tuple] = []
    monkeypatch.setattr(
        self_update, "_spawn_extractor",
        lambda extractor, zip_path, install_dir, exe, argv: spawned.append(
            (extractor, zip_path, install_dir, exe, list(argv))),
    )
    return install, spawned


def _info(tag="v0.5.0"):
    return {"tag": tag, "download_url": "http://x/z.zip", "size": 0, "digest": None}


def test_stage_then_apply_arms_rollback_and_spawns_helper(fake_install):
    install, spawned = fake_install
    staged = self_update.stage_update(_info(), install)
    assert staged.tag == "v0.5.0"
    assert staged.flat_zip_path.is_file() and staged.extractor_path.is_file()
    with zipfile.ZipFile(staged.flat_zip_path) as archive:
        assert "claude-web.exe" in archive.namelist()  # unwrapped
    assert self_update.load_staged(install).tag == "v0.5.0"
    # Nothing in the install changed yet.
    assert (install / "claude-web.exe").read_bytes() == b"old-exe"

    self_update.apply_staged(staged, install, ["--headless"])

    assert spawned and spawned[0][2] == install and spawned[0][4] == ["--headless"]
    assert spawned[0][3] == install / "claude-web.exe"
    assert self_update.load_staged(install) is None, "marker must go before the relaunch"
    pending = json.loads(
        (install / upgrade_manager.STATE_DIR_NAME / "pending.json").read_text(encoding="utf-8"))
    assert pending["target_version"] == "v0.5.0"
    assert set(pending["expected_file_hashes_sha256"]) == {
        "claude-web.exe", "_internal/base_library.zip", "_internal/added.pyd",
    }
    backup = install / upgrade_manager.STATE_DIR_NAME / "backup"
    assert (backup / "claude-web.exe").read_bytes() == b"old-exe"

    # Simulate the helper's overlay, then the next launch's verification.
    with zipfile.ZipFile(staged.flat_zip_path) as archive:
        archive.extractall(install)
    assert upgrade_manager.recover_pending_upgrade(install) is None
    assert (install / "claude-web.exe").read_bytes() == b"new-exe"
    assert (install / "portable-data" / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert not (install / upgrade_manager.STATE_DIR_NAME / "pending.json").exists()


def test_half_applied_swap_is_rolled_back_at_next_start(fake_install):
    install, _spawned = fake_install
    staged = self_update.stage_update(_info(), install)
    self_update.apply_staged(staged, install, [])
    # The helper replaced the exe but died before the library.
    (install / "claude-web.exe").write_bytes(b"new-exe")
    result = upgrade_manager.recover_pending_upgrade(install)
    assert result is not None and result.rolled_back
    assert (install / "claude-web.exe").read_bytes() == b"old-exe"
    assert (install / "_internal" / "base_library.zip").read_bytes() == b"old-lib"
    assert any("base_library.zip" in item for item in result.failed_files)
    assert upgrade_manager.recover_pending_upgrade(install) is None  # state cleared


def test_apply_refuses_tampered_helper_copy(fake_install):
    install, spawned = fake_install
    staged = self_update.stage_update(_info(), install)
    staged.extractor_path.write_bytes(b"not the helper")
    with pytest.raises(RuntimeError):
        self_update.apply_staged(staged, install, [])
    assert not spawned
    assert self_update.load_staged(install) is None


def test_stage_failure_cleans_up(fake_install, monkeypatch):
    install, _spawned = fake_install

    def bad_download(url, dest, expected_size=0, progress_cb=None):
        raise RuntimeError("truncated")

    monkeypatch.setattr(self_update, "_download", bad_download)
    with pytest.raises(RuntimeError):
        self_update.stage_update(_info(), install)
    assert self_update.load_staged(install) is None
    assert not list((install.parent / "temp").iterdir())


def test_apply_pending_staged_launches_or_discards(fake_install, monkeypatch):
    install, spawned = fake_install
    assert self_update.apply_pending_staged(install, []) is False
    staged = self_update.stage_update(_info(), install)
    assert self_update.apply_pending_staged(install, ["--port", "3002"]) is True
    assert spawned[-1][4] == ["--port", "3002"]
    # A staged update whose apply fails is discarded so the app can boot.
    staged = self_update.stage_update(_info("v0.5.1"), install)
    monkeypatch.setattr(self_update, "apply_staged", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert self_update.apply_pending_staged(install, []) is False
    assert self_update.load_staged(install) is None
    assert not staged.workdir_path.exists()


def test_load_staged_discards_marker_with_missing_files(fake_install):
    install, _spawned = fake_install
    staged = self_update.stage_update(_info(), install)
    staged.flat_zip_path.unlink()
    assert self_update.load_staged(install) is None
    assert not (install / upgrade_manager.STATE_DIR_NAME / self_update.STAGED_FILE_NAME).exists()


# ─── launcher hooks ────────────────────────────────────────────────────────


@pytest.fixture
def frozen_launcher(monkeypatch, tmp_path):
    fake_exe = tmp_path / "claude-web.exe"
    fake_exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.chdir(tmp_path)
    for var in ("AUTH_MODE", "SESSION_SECRET", "OIDC_ISSUER_URL", "OIDC_CLIENT_ID",
                "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI", "CLAUDE_WEB_UI_MODE"):
        monkeypatch.delenv(var, raising=False)
    import launcher as launcher_mod
    importlib.reload(launcher_mod)
    return launcher_mod


def test_launcher_exits_to_let_a_staged_update_apply(frozen_launcher, monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_apply(install, argv):
        assert Path(install) == tmp_path
        calls.append(list(argv))
        return True

    monkeypatch.setattr(frozen_launcher.self_update, "apply_pending_staged", fake_apply)
    monkeypatch.setattr(frozen_launcher.upgrade_manager, "recover_pending_upgrade", lambda d: None)
    for name in ("_run_headless_mode", "_run_browser_mode", "_run_window_mode"):
        monkeypatch.setattr(frozen_launcher, name, lambda *a, **k: pytest.fail("server must not start"))
    assert frozen_launcher._run(["--headless", "--port", "3002"]) == 0
    assert calls == [["--headless", "--port", "3002"]]


def test_launcher_reports_rollback_and_continues(frozen_launcher, monkeypatch, capsys):
    result = upgrade_manager.RecoveryResult(
        rolled_back=True, title="Update failed. claude-web was restored", message="details",
    )
    monkeypatch.setattr(frozen_launcher.upgrade_manager, "recover_pending_upgrade", lambda d: result)
    monkeypatch.setattr(frozen_launcher.self_update, "apply_pending_staged", lambda d, a: False)
    assert frozen_launcher._settle_pending_update([]) is False
    out = capsys.readouterr().out
    assert "was restored" in out and "details" in out


def test_launcher_recovery_errors_never_block_startup(frozen_launcher, monkeypatch, capsys):
    def boom(_d):
        raise OSError("disk")

    monkeypatch.setattr(frozen_launcher.upgrade_manager, "recover_pending_upgrade", boom)
    monkeypatch.setattr(frozen_launcher.self_update, "apply_pending_staged", lambda d, a: False)
    assert frozen_launcher._settle_pending_update([]) is False
    assert "starting anyway" in capsys.readouterr().out


def test_launcher_version_flag(frozen_launcher, capsys):
    with pytest.raises(SystemExit) as exc:
        frozen_launcher._parse_args(["--version"])
    assert exc.value.code == 0
    assert "claude-web dev" in capsys.readouterr().out


def test_not_frozen_skips_settle(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    import launcher as launcher_mod
    importlib.reload(launcher_mod)
    monkeypatch.setattr(launcher_mod, "_settle_pending_update", lambda argv: pytest.fail("source runs must not settle"))
    monkeypatch.setattr(launcher_mod, "_run_headless_mode", lambda *a, **k: 0)
    monkeypatch.setattr(launcher_mod, "_load_dotenv_files", lambda: [])
    monkeypatch.setenv("AUTH_MODE", "none")
    assert launcher_mod._run(["--headless"]) == 0
    assert os.environ["AUTH_MODE"] == "none"
