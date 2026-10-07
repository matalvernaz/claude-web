"""Transactional upgrade: diff-based verify set, backup, and best-effort rollback.

The overlay itself is ZipExtractor's job on Windows; here it is faked by
writing files onto the "install" directory, so the diff/backup/verify/rollback
logic is exercised on any platform.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import upgrade_manager as um

_OLD = {
    "claude-web.exe": b"old-exe",
    "_internal/base_library.zip": b"old-base",
    "_internal/python313.dll": b"old-dll",
    "_internal/unchanged.pyd": b"same-bytes",  # identical in NEW -> skipped
}
_NEW = {
    "claude-web.exe": b"new-exe",
    "_internal/base_library.zip": b"new-base",
    "_internal/python313.dll": b"new-dll",
    "_internal/unchanged.pyd": b"same-bytes",
    "_internal/added.pyd": b"brand-new-file",  # a file the upgrade adds
}
_CHANGED = {"claude-web.exe", "_internal/base_library.zip", "_internal/python313.dll"}


def _write_tree(root: Path, contents: dict[str, bytes]) -> None:
    for rel, data in contents.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _make_zip(path: Path, contents: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for rel, data in contents.items():
            archive.writestr(rel, data)
    return path


def _prepared_install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "app"
    install.mkdir()
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "upgrade.zip", _NEW)
    um.prepare_for_upgrade(install, upgrade_zip, "v9.9.9", exe_name="claude-web.exe")
    return install, upgrade_zip


def test_files_to_verify_is_the_changed_set(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "u.zip", _NEW)
    changed = um.files_to_verify(install, upgrade_zip)
    assert set(changed) == _CHANGED | {"_internal/added.pyd"}
    assert "_internal/unchanged.pyd" not in changed


def test_files_to_verify_ignores_own_state_folder(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "u.zip", {**_NEW, f"{um.STATE_DIR_NAME}/pending.json": b"{}"})
    assert f"{um.STATE_DIR_NAME}/pending.json" not in um.files_to_verify(install, upgrade_zip)


def test_prepare_backs_up_changed_files_and_records_hashes(tmp_path):
    install, _zip = _prepared_install(tmp_path)
    state = install / um.STATE_DIR_NAME
    pending = json.loads((state / um.PENDING_FILE_NAME).read_text(encoding="utf-8"))
    assert pending["target_version"] == "v9.9.9"
    assert set(pending["expected_file_hashes_sha256"]) == _CHANGED | {"_internal/added.pyd"}
    assert set(pending["backed_up_files"]) == _CHANGED  # the added file has no prior version
    backup = state / um.BACKUP_DIR_NAME
    for rel in _CHANGED:
        assert (backup / rel).read_bytes() == _OLD[rel]
    assert not (backup / "_internal" / "unchanged.pyd").exists()


def test_prepare_anchors_on_exe_even_when_unchanged(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    _write_tree(install, _OLD)
    same_exe = {**_NEW, "claude-web.exe": b"old-exe"}
    upgrade_zip = _make_zip(tmp_path / "u.zip", same_exe)
    um.prepare_for_upgrade(install, upgrade_zip, "v1", exe_name="claude-web.exe")
    pending = json.loads((install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).read_text(encoding="utf-8"))
    assert "claude-web.exe" in pending["expected_file_hashes_sha256"]


def test_prepare_refuses_a_zip_that_changes_nothing(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "u.zip", _OLD)
    with pytest.raises(um.UpgradeIntegrityError):
        um.prepare_for_upgrade(install, upgrade_zip, "v1")


def test_recover_clean_upgrade_clears_state(tmp_path):
    install, upgrade_zip = _prepared_install(tmp_path)
    with zipfile.ZipFile(upgrade_zip) as archive:
        archive.extractall(install)  # the helper's job
    assert um.recover_pending_upgrade(install) is None
    assert not (install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).exists()
    assert not (install / um.STATE_DIR_NAME / um.BACKUP_DIR_NAME).exists()
    assert (install / "_internal" / "added.pyd").read_bytes() == b"brand-new-file"


def test_recover_partial_upgrade_rolls_back(tmp_path):
    install, _zip = _prepared_install(tmp_path)
    (install / "claude-web.exe").write_bytes(b"new-exe")  # only the exe landed
    result = um.recover_pending_upgrade(install)
    assert result is not None and result.rolled_back
    assert (install / "claude-web.exe").read_bytes() == b"old-exe"
    assert (install / "_internal" / "python313.dll").read_bytes() == b"old-dll"
    assert any("python313.dll" in item for item in result.failed_files)
    assert "portable-data" in result.message
    assert um.recover_pending_upgrade(install) is None


def test_recover_nothing_pending(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    assert um.recover_pending_upgrade(install) is None


def test_recover_unreadable_marker_restores_backup(tmp_path):
    install, _zip = _prepared_install(tmp_path)
    (install / "claude-web.exe").write_bytes(b"garbage")
    (install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).write_text("{not json", encoding="utf-8")
    result = um.recover_pending_upgrade(install)
    assert result is not None and result.rolled_back
    assert (install / "claude-web.exe").read_bytes() == b"old-exe"


def test_verify_install_reports_missing_and_mismatched(tmp_path):
    install = tmp_path / "app"
    install.mkdir()
    _write_tree(install, {"a.txt": b"a"})
    expected = {"a.txt": um._sha256_file(install / "a.txt"), "b.txt": "00"}
    assert um.verify_install(install, expected) == ["b.txt: missing from the install folder"]
    (install / "a.txt").write_bytes(b"changed")
    failures = um.verify_install(install, expected)
    assert any(item.startswith("a.txt") for item in failures)
