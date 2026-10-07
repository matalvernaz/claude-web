"""Shared transcript store across credential homes + CLI-compatible project keys.

Regression coverage for a Windows portable install where (a) the project
path contained spaces, so the app's project key and the CLI's transcript
directory name disagreed, and (b) the first chat ran under a credential
home before ``CLAUDE_HOME/projects`` existed, so every credential ended up
with a private ``projects`` directory: the sidebar listed nothing and a
mid-conversation account switch failed with "No conversation found with
session ID".
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
from claude_agent_sdk import project_key_for_directory

import app as app_module


# ─── project key parity with the CLI / SDK ─────────────────────────────────


def test_project_key_text_matches_cli_rule_for_spaces() -> None:
    """Every non-alphanumeric character becomes ``-``, spaces included. The
    old rule kept spaces, so this path produced a directory name the CLI
    never writes to."""
    text = r"C:\claudeProjects\Claude Web for Windows\portable-data\workspace"
    assert app_module._project_key_from_text(text) == (
        "C--claudeProjects-Claude-Web-for-Windows-portable-data-workspace"
    )
    assert app_module._project_key_from_text("/home/matt/my.project_v2") == (
        "-home-matt-my-project-v2"
    )


def test_sanitize_project_key_matches_sdk(tmp_path) -> None:
    """The app key must equal what ``claude_agent_sdk`` derives for the same
    directory — that is the rule ``sdk_list_sessions`` (the sidebar) and the
    bundled CLI use, so any drift makes session lookups miss."""
    project = tmp_path / "my project.v2"
    project.mkdir()
    assert app_module._sanitize_project_key(project) == project_key_for_directory(str(project))
    assert app_module._sanitize_project_key(project) == app_module._project_key_from_text(
        str(project.resolve())
    )


def test_legacy_project_key_kept_spaces(tmp_path) -> None:
    """The old rule left spaces (and dots, underscores) in place; the new one
    never does. Both agree on the separators, which is why ``/workspace``
    deployments never saw the bug."""
    spaced = tmp_path / "with space"
    legacy = app_module._legacy_project_key(spaced)
    new = app_module._sanitize_project_key(spaced)
    assert legacy != new
    assert legacy.endswith("-with space")
    assert new.endswith("-with-space")
    assert "/" not in legacy and "\\" not in legacy and ":" not in legacy
    assert app_module._LEGACY_PROJECT_KEY_RE.sub("-", "/workspace") == "-workspace"
    assert app_module._project_key_from_text("/workspace") == "-workspace"


# ─── credential homes always link the shared projects dir ──────────────────


def _link_target(path) -> str:
    return os.path.realpath(str(path))


def test_ensure_credential_home_creates_and_links_shared_projects(tmp_path, monkeypatch) -> None:
    """A fresh CLAUDE_HOME with no ``projects`` yet (nothing has chatted under
    the shared slot) must still give the credential home a ``projects`` link,
    otherwise the CLI creates a private one and transcripts split per
    credential."""
    shared_home = tmp_path / "claude"
    shared_home.mkdir()
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    monkeypatch.setattr(app_module, "PERSONAL_HOMES_DIR", tmp_path / "homes")

    home = app_module._ensure_credential_home("fresh-user", 1)

    shared_projects = shared_home / "projects"
    assert shared_projects.is_dir() and not shared_projects.is_symlink()
    assert app_module._is_link(home / "projects")
    assert _link_target(home / "projects") == _link_target(shared_projects)


def test_ensure_shared_projects_dir_refuses_symlink(tmp_path, monkeypatch) -> None:
    """Same planted-link guard as the mirror: a symlinked ``projects`` in
    CLAUDE_HOME must never become the store every credential shares."""
    shared_home = tmp_path / "claude"
    shared_home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (shared_home / "projects").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this runner")
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    assert app_module._ensure_shared_projects_dir() is None


# ─── startup repair: private projects dirs merge into the shared store ─────


def _private_projects(home, key: str, *sids: str) -> None:
    d = home / "projects" / key
    d.mkdir(parents=True)
    for sid in sids:
        (d / f"{sid}.jsonl").write_text(f'{{"sessionId":"{sid}"}}\n', encoding="utf-8")


def test_startup_share_credential_projects_merges_and_links(tmp_path, monkeypatch) -> None:
    shared_home = tmp_path / "claude"
    shared_home.mkdir()
    homes = tmp_path / "homes"
    sub = homes / app_module._safe_sub("alice")
    home1, home2 = sub / "1", sub / "2"
    key = "C--work-proj"
    _private_projects(home1, key, "sid-a")
    _private_projects(home2, key, "sid-b")
    (home2 / "projects" / key / "tool-results").mkdir()
    (home2 / "projects" / key / "tool-results" / "x.txt").write_text("r", encoding="utf-8")
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    monkeypatch.setattr(app_module, "PERSONAL_HOMES_DIR", homes)

    app_module._startup_share_credential_projects()

    shared_key_dir = shared_home / "projects" / key
    assert (shared_key_dir / "sid-a.jsonl").is_file()
    assert (shared_key_dir / "sid-b.jsonl").is_file()
    assert (shared_key_dir / "tool-results" / "x.txt").is_file()
    for home in (home1, home2):
        assert app_module._is_link(home / "projects")
        assert _link_target(home / "projects") == _link_target(shared_home / "projects")
        # The CLI path under the credential home now resolves to the shared file.
        assert (home / "projects" / key / "sid-a.jsonl").is_file()

    # Idempotent: a second start finds only links and changes nothing.
    before = sorted(p.name for p in shared_key_dir.iterdir())
    app_module._startup_share_credential_projects()
    assert sorted(p.name for p in shared_key_dir.iterdir()) == before
    assert app_module._is_link(home1 / "projects")


def test_startup_share_credential_projects_never_overwrites(tmp_path, monkeypatch) -> None:
    """A transcript that already exists in the shared store wins; the private
    copy stays put and the home is left unlinked for operator review rather
    than silently losing either file."""
    shared_home = tmp_path / "claude"
    key = "C--work-proj"
    (shared_home / "projects" / key).mkdir(parents=True)
    (shared_home / "projects" / key / "sid-a.jsonl").write_text("shared\n", encoding="utf-8")
    homes = tmp_path / "homes"
    home = homes / app_module._safe_sub("bob") / "1"
    _private_projects(home, key, "sid-a", "sid-c")
    (home / "projects" / key / "sid-a.jsonl").write_text("private\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    monkeypatch.setattr(app_module, "PERSONAL_HOMES_DIR", homes)

    app_module._startup_share_credential_projects()

    assert (shared_home / "projects" / key / "sid-a.jsonl").read_text(encoding="utf-8") == "shared\n"
    assert (shared_home / "projects" / key / "sid-c.jsonl").is_file()
    assert (home / "projects" / key / "sid-a.jsonl").read_text(encoding="utf-8") == "private\n"
    assert not app_module._is_link(home / "projects")


def test_startup_share_credential_projects_without_homes_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLAUDE_HOME", tmp_path / "claude")
    monkeypatch.setattr(app_module, "PERSONAL_HOMES_DIR", tmp_path / "missing")
    app_module._startup_share_credential_projects()  # must not raise
    assert not (tmp_path / "claude" / "projects").exists()


# ─── startup migration: legacy project keys → CLI keys ─────────────────────


def _count(conn, table: str, key: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE project_key=?", (key,)
    ).fetchone()[0]


def test_startup_migrate_project_keys_rewrites_rows_and_dirs(tmp_path, monkeypatch) -> None:
    project = tmp_path / "my project"
    project.mkdir()
    legacy = app_module._legacy_project_key(project)
    new = app_module._sanitize_project_key(project)
    assert legacy != new
    shared_home = tmp_path / "claude"
    legacy_dir = shared_home / "projects" / legacy
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "forged.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    monkeypatch.setattr(app_module, "PROJECTS", [project])

    conn = app_module._state_db()
    now = time.time()
    tag = uuid.uuid4().hex
    conv_id, bind_id, run_id, sid, thread = (
        f"conv_{tag}", f"bind_{tag}", f"run-{tag}", f"sess-{tag}", f"thr-{tag}",
    )
    rt_thread = int(now * 1000) % 2_000_000_000
    conn.execute(
        "INSERT INTO conversation(conversation_id, owner_sub, project_key, title,"
        " last_seq, capture_state, created_at, updated_at) VALUES(?,?,?,?,0,'live_complete',?,?)",
        (conv_id, "u", legacy, None, now, now),
    )
    conn.execute(
        "INSERT INTO conversation_binding(binding_id, conversation_id, provider,"
        " native_session_id, project_key, status, created_at, updated_at)"
        " VALUES(?,?,'claude',?,?,'active',?,?)",
        (bind_id, conv_id, sid, legacy, now, now),
    )
    conn.execute(
        "INSERT INTO runs(run_id, owner_sub, session_id, project_key, created_at,"
        " finished_at, last_activity) VALUES(?,?,?,?,?,?,?)",
        (run_id, "u", sid, legacy, now, now, now),
    )
    conn.execute(
        "INSERT INTO session_owners(session_id, owner_sub, project_key, created_at)"
        " VALUES(?,?,?,?)", (sid, "u", legacy, now),
    )
    conn.execute(
        "INSERT INTO codex_session(thread_id, owner_sub, project_key, title, model,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (thread, "u", legacy, "t", None, now, now),
    )
    conn.execute(
        "INSERT INTO roundtable_thread_project(thread_id, project_key, created_by,"
        " created_at) VALUES(?,?,?,?)", (rt_thread, legacy, "u", now),
    )
    try:
        app_module._startup_migrate_project_keys()

        for table in app_module._PROJECT_KEY_TABLES:
            assert _count(conn, table, legacy) == 0, table
        assert conn.execute(
            "SELECT project_key FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0] == new
        assert conn.execute(
            "SELECT project_key FROM conversation_binding WHERE binding_id=?", (bind_id,)
        ).fetchone()[0] == new
        assert conn.execute(
            "SELECT project_key FROM roundtable_thread_project WHERE thread_id=?", (rt_thread,)
        ).fetchone()[0] == new
        assert not legacy_dir.exists()
        assert (shared_home / "projects" / new / "forged.jsonl").is_file()
        # And the app now looks exactly where the CLI writes.
        assert app_module._sessions_dir(project) == shared_home / "projects" / new

        # Idempotent.
        app_module._startup_migrate_project_keys()
        assert (shared_home / "projects" / new / "forged.jsonl").is_file()
    finally:
        conn.execute("DELETE FROM conversation_binding WHERE binding_id=?", (bind_id,))
        conn.execute("DELETE FROM conversation WHERE conversation_id=?", (conv_id,))
        conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM session_owners WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM codex_session WHERE thread_id=?", (thread,))
        conn.execute("DELETE FROM roundtable_thread_project WHERE thread_id=?", (rt_thread,))


def test_startup_migrate_project_keys_removes_alias_link(tmp_path, monkeypatch) -> None:
    """An operator who bridged the mismatch with a link named after the old
    key gets it cleaned up instead of a dangling duplicate directory."""
    project = tmp_path / "my project"
    project.mkdir()
    legacy = app_module._legacy_project_key(project)
    new = app_module._sanitize_project_key(project)
    shared_home = tmp_path / "claude"
    new_dir = shared_home / "projects" / new
    new_dir.mkdir(parents=True)
    (new_dir / "real.jsonl").write_text("{}\n", encoding="utf-8")
    alias = shared_home / "projects" / legacy
    try:
        alias.symlink_to(new_dir, target_is_directory=True)
    except OSError:
        if not app_module.IS_WINDOWS:
            pytest.skip("symlinks unavailable on this runner")
        # No Developer Mode: use the junction _link_or_copy would fall back to.
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(new_dir), str(alias))
    assert app_module._is_link(alias)
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    monkeypatch.setattr(app_module, "PROJECTS", [project])

    app_module._startup_migrate_project_keys()

    assert not alias.exists()
    assert not app_module._is_link(alias)
    assert (new_dir / "real.jsonl").is_file()


def test_startup_migrate_project_keys_noop_when_keys_agree(tmp_path, monkeypatch) -> None:
    """``/workspace`` → ``-workspace`` under both rules: nothing to do, and
    nothing is created. (pytest's tmp_path carries underscores, which the
    old rule kept, so force agreement rather than depend on the path.)"""
    project = tmp_path / "plain"
    project.mkdir()
    monkeypatch.setattr(app_module, "_legacy_project_key", app_module._sanitize_project_key)
    shared_home = tmp_path / "claude"
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared_home)
    monkeypatch.setattr(app_module, "PROJECTS", [project])
    app_module._startup_migrate_project_keys()
    assert not shared_home.exists()
