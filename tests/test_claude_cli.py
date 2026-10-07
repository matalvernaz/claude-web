"""Which `claude` CLI gets run.

The desktop binary carries the Agent SDK's bundled CLI, but sign-in used to
look on PATH alone, so a fresh Windows machine reported "claude CLI not found
on PATH" and could never sign in. On Windows an npm install adds a claude.cmd
shim that the SDK refuses to spawn, so it must never win over a native exe.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import claude_cli


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Control PATH lookups, the platform, the home dir and the SDK bundle."""
    found: dict[str, str] = {}
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(claude_cli, "_is_windows", lambda: False)
    monkeypatch.setattr(claude_cli.portable_tools, "enabled", lambda: False)
    bundle = tmp_path / "sdk" / "_bundled"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(claude_cli, "_bundled_dir", lambda: bundle)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(claude_cli, "_home", lambda: home)

    class Fake:
        which = found

        @staticmethod
        def windows():
            monkeypatch.setattr(claude_cli, "_is_windows", lambda: True)

        @staticmethod
        def bundled(name="claude"):
            path = bundle / name
            path.write_text("")
            return str(path)

        @staticmethod
        def native_home_exe():
            path = home / ".local" / "bin" / "claude.exe"
            path.parent.mkdir(parents=True)
            path.write_text("")
            return str(path)

    return Fake


def test_installed_cli_wins_over_the_bundled_copy(fake):
    fake.which["claude"] = "/usr/local/bin/claude"
    fake.bundled()
    assert claude_cli.find() == ("/usr/local/bin/claude", claude_cli.SYSTEM)


def test_bundled_copy_is_used_when_nothing_is_installed(fake):
    bundled = fake.bundled()
    assert claude_cli.find() == (bundled, claude_cli.BUNDLED)


def test_nothing_at_all(fake):
    assert claude_cli.find() == (None, None)
    assert claude_cli.resolve() is None


def test_windows_bundled_exe_beats_the_npm_shim(fake):
    fake.windows()
    fake.which["claude"] = r"C:\Users\m\AppData\Roaming\npm\claude.cmd"
    bundled = fake.bundled("claude.exe")
    assert claude_cli.find() == (bundled, claude_cli.BUNDLED)


def test_windows_native_exe_later_on_path_beats_the_shim(fake):
    fake.windows()
    fake.which["claude"] = r"C:\npm\claude.cmd"
    fake.which["claude.exe"] = r"C:\Users\m\.local\bin\claude.exe"
    fake.bundled("claude.exe")
    assert claude_cli.find() == (r"C:\Users\m\.local\bin\claude.exe", claude_cli.SYSTEM)


def test_windows_native_installer_location_off_path(fake):
    fake.windows()
    fake.which["claude"] = r"C:\npm\claude.cmd"
    native = fake.native_home_exe()
    assert claude_cli.find() == (native, claude_cli.SYSTEM)


def test_windows_pathext_cannot_pass_a_batch_file_off_as_an_exe(fake):
    fake.windows()
    fake.which["claude"] = r"C:\npm\claude.cmd"
    fake.which["claude.exe"] = r"C:\npm\claude.exe.cmd"
    bundled = fake.bundled("claude.exe")
    assert claude_cli.find() == (bundled, claude_cli.BUNDLED)


def test_windows_shim_alone_is_a_last_resort(fake):
    fake.windows()
    fake.which["claude"] = r"C:\npm\claude.cmd"
    assert claude_cli.find() == (r"C:\npm\claude.cmd", claude_cli.SHIM)


def test_windows_looks_for_claude_exe_in_the_bundle(fake):
    fake.windows()
    fake.bundled("claude")  # the POSIX name must not count on Windows
    assert claude_cli.bundled() is None
    exe = fake.bundled("claude.exe")
    assert claude_cli.bundled() == exe


def test_install_advice_is_the_native_installer(fake):
    assert "install.sh" in claude_cli.install_instructions()
    fake.windows()
    assert "install.ps1" in claude_cli.install_instructions()
    assert "npm" not in claude_cli.install_instructions()


def test_bundle_dir_is_where_the_sdk_itself_looks():
    """If the SDK moves its bundled CLI, this breaks before a release does."""
    from claude_agent_sdk._internal.transport import subprocess_cli

    source = inspect.getsource(subprocess_cli.SubprocessCLITransport._find_bundled_cli)
    assert 'Path(__file__).parent.parent.parent / "_bundled"' in source
    sdk_dir = Path(subprocess_cli.__file__).parent.parent.parent / "_bundled"
    assert claude_cli._bundled_dir() == sdk_dir


def test_sign_in_uses_the_bundled_copy(fake):
    import setup_flow

    bundled = fake.bundled()
    assert setup_flow._resolve_claude_cli() == bundled


def test_sign_in_keeps_the_bare_name_when_there_is_no_cli(fake):
    import setup_flow

    # The FileNotFoundError handlers around the subprocess depend on this.
    assert setup_flow._resolve_claude_cli() == "claude"


def test_cli_status_counts_the_bundled_copy(fake, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "_NODE_HINT_DIRS", ())
    bundled = fake.bundled()
    status = app_module._claude_cli_status()
    assert status["cli_present"] is True
    assert (status["cli_path"], status["cli_source"]) == (bundled, claude_cli.BUNDLED)


@pytest.mark.parametrize("source,expect", [
    (claude_cli.SYSTEM, None),
    (claude_cli.BUNDLED, "bundled with claude-web"),
    (claude_cli.SHIM, "claude.cmd"),
    (None, "no usable `claude` CLI"),
])
def test_launcher_startup_message(fake, monkeypatch, capsys, source, expect):
    import launcher

    monkeypatch.setattr(launcher.claude_cli, "find", lambda: ("x" if source else None, source))
    launcher._check_claude_cli()
    out = capsys.readouterr().out
    if expect is None:
        assert out == ""
    else:
        assert expect in out
        assert "npm install" not in out


def test_every_claude_spawn_goes_through_the_resolver():
    """Only the auto-updater may read PATH directly: the bundled copy is part
    of the install and must never be updated in place."""
    root = Path(__file__).resolve().parents[1]
    counts = {
        name: (root / name).read_text(encoding="utf-8").count('shutil.which("claude")')
        for name in ("app.py", "setup_flow.py", "launcher.py")
    }
    assert counts == {"app.py": 1, "setup_flow.py": 0, "launcher.py": 0}


def test_the_portable_builds_managed_copy_wins_over_everything(fake, monkeypatch, tmp_path):
    system = fake.bundled("claude-system")
    fake.which["claude"] = system
    bundled = fake.bundled()
    assert claude_cli.find() == (system, claude_cli.SYSTEM)
    managed = tmp_path / "tools" / "claude" / "2.1.292" / "claude.exe"
    managed.parent.mkdir(parents=True)
    managed.write_text("")
    monkeypatch.setattr(claude_cli.portable_tools, "enabled", lambda: True)
    monkeypatch.setattr(claude_cli.portable_tools, "managed_exe", lambda name: managed if name == "claude" else None)
    assert claude_cli.find() == (str(managed), claude_cli.MANAGED)
    assert claude_cli.resolve() == str(managed)
    # Outside the portable build the module is never consulted.
    monkeypatch.setattr(claude_cli.portable_tools, "enabled", lambda: False)
    assert claude_cli.find() == (system, claude_cli.SYSTEM)
    del fake.which["claude"]
    assert claude_cli.find() == (bundled, claude_cli.BUNDLED)
