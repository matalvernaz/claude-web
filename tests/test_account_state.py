"""Each Claude account keeps its own Claude Code state.

Until 2026-10 every credential home linked ``.claude.json`` (and the policy
files and ``backups/``) to the shared CLAUDE_HOME, so every account read and
wrote one file holding the signed-in account's details and the feature flags
the server last sent for it. Each account ran on whatever the last one to
start had left there: the model picker flipped between Claude Code's full
list and its five bare aliases, and Default between Opus and Sonnet,
depending on whose chat had run last.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import stat
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

import app as app_module
import claude_models

FIXTURES = Path(__file__).parent / "fixtures"
SERVED = json.loads((FIXTURES / "cli_models_2.1.299_served.json").read_text(encoding="utf-8"))["rows"]
COMPILED = json.loads((FIXTURES / "cli_models_2.1.299_compiled.json").read_text(encoding="utf-8"))["rows"]
ANON = "anonymous"

GEMINI = {"type": "stdio", "command": "/opt/gemini", "args": [], "env": {"GEMINI_API_KEY": "k"}}
LOCAL = {"type": "stdio", "command": "/opt/local-tool", "args": []}
SHARED_CONFIG = {
    "userID": "device-1",
    "hasCompletedOnboarding": True,
    "autoUpdates": False,
    "oauthAccount": {"accountUuid": "acct-alex", "organizationUuid": "org-team",
                     "emailAddress": "alex@example.com"},
    "cachedGrowthBookFeatures": {"tengu_delegated_quail": {"mode": "shadow", "served": "primary"}},
    "cachedGrowthBookFeaturesAt": 1,
    "overageCreditGrantCache": {"org-team": {}},
    "groveConfigCache": {"acct-alex": {}},
    "metricsStatusCacheByPrincipal": {},
    "hasAvailableSubscription": False,
    "claudeAiMcpEverConnected": True,
    "mcpServers": {"gemini": GEMINI},
    "projects": {"/work": {"hasTrustDialogAccepted": True, "mcpServers": {"local": LOCAL}}},
}
ACCOUNT_KEYS = ("oauthAccount", "cachedGrowthBookFeatures", "cachedGrowthBookFeaturesAt",
                "overageCreditGrantCache", "groveConfigCache", "metricsStatusCacheByPrincipal",
                "hasAvailableSubscription")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    shared = tmp_path / "claude"
    shared.mkdir()
    (shared / ".claude.json").write_text(json.dumps(SHARED_CONFIG), encoding="utf-8")
    os.chmod(shared / ".claude.json", 0o644)
    (shared / "policy-limits.json").write_text("{}", encoding="utf-8")
    (shared / "remote-settings.json").write_text("{}", encoding="utf-8")
    (shared / "backups").mkdir()
    (shared / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared)
    monkeypatch.setattr(app_module, "PERSONAL_HOMES_DIR", tmp_path / "homes")
    return shared


def _config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_home(homes: Path, sub: str, cred_id: int) -> Path:
    """A credential home as _ensure_credential_home made them before the split."""
    home = app_module.PERSONAL_HOMES_DIR / app_module._safe_sub(sub) / str(cred_id)
    home.mkdir(parents=True)
    (homes / ".claude.json.backup.123").write_text("{}", encoding="utf-8")
    try:
        for name in (".claude.json", "backups", "policy-limits.json", "remote-settings.json",
                     "settings.json", ".claude.json.backup.123"):
            (home / name).symlink_to(homes / name)
    except OSError:
        pytest.skip("symlinks unavailable on this runner")
    (home / "policy-limits.json.stamp.json").write_text('{"identity": "x"}', encoding="utf-8")
    (home / "remote-settings.json.synced-at").write_text("1", encoding="utf-8")
    (home / ".claude_oauth_token").write_text("sk-ant-oat01-test", encoding="utf-8")
    return home


# ─── a credential home's own files ──────────────────────────────────────────


def test_a_new_account_gets_its_own_settings_file(homes) -> None:
    home = app_module._ensure_credential_home("alice", 1)

    path = home / ".claude.json"
    assert path.is_file() and not app_module._is_link(path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = _config(path)
    for key in ACCOUNT_KEYS:
        assert key not in config
    # The MCP servers come from the shared file at spawn instead.
    assert "mcpServers" not in config
    assert config["projects"] == {"/work": {"hasTrustDialogAccepted": True}}
    assert config["hasCompletedOnboarding"] is True
    assert config["autoUpdates"] is False
    assert config["userID"] == "device-1"
    # The account's policy files and backups are its own, made by its CLI.
    for name in ("policy-limits.json", "remote-settings.json", "backups"):
        assert not (home / name).exists() and not app_module._is_link(home / name)
    # Everything about the person is still shared (a hardlink where Windows
    # can't make symlinks).
    assert app_module._is_link(home / "settings.json") or app_module._same_file(
        home / "settings.json", homes / "settings.json")
    assert app_module._is_link(home / "projects")


def test_a_home_never_relinks_its_own_files(homes) -> None:
    home = app_module._ensure_credential_home("alice", 1)
    config = _config(home / ".claude.json")
    config["oauthAccount"] = {"accountUuid": "acct-alice", "organizationUuid": "org-a"}
    (home / ".claude.json").write_text(json.dumps(config), encoding="utf-8")

    again = app_module._ensure_credential_home("alice", 1)

    assert _config(again / ".claude.json")["oauthAccount"]["accountUuid"] == "acct-alice"
    assert _config(homes / ".claude.json")["oauthAccount"]["accountUuid"] == "acct-alex"


# ─── the startup repair ─────────────────────────────────────────────────────


def test_startup_unshares_a_home_made_before_the_split(homes) -> None:
    home = _legacy_home(homes, "alice", 1)

    app_module._startup_private_account_state()

    for name in (".claude.json", "backups", "policy-limits.json", "remote-settings.json",
                 ".claude.json.backup.123"):
        assert not app_module._is_link(home / name), name
    config = _config(home / ".claude.json")
    assert config["hasCompletedOnboarding"] is True
    for key in ACCOUNT_KEYS + ("mcpServers",):
        assert key not in config
    # The stamps vouched for the shared copies; the CLI fetches its own.
    assert not (home / "policy-limits.json.stamp.json").exists()
    assert not (home / "remote-settings.json.synced-at").exists()
    assert (home / ".claude_oauth_token").read_text(encoding="utf-8") == "sk-ant-oat01-test"
    assert app_module._is_link(home / "settings.json")
    assert "claudeAiMcpEverConnected" not in config

    # Every account had been writing the shared copies, so they lose what
    # describes an account and keep the rest.
    shared = _config(homes / ".claude.json")
    for key in ACCOUNT_KEYS:
        assert key not in shared
    assert shared["mcpServers"] == {"gemini": GEMINI}
    assert shared["projects"]["/work"]["mcpServers"] == {"local": LOCAL}
    if os.name == "posix":
        assert stat.S_IMODE((homes / ".claude.json").stat().st_mode) == 0o644
    assert not (homes / "policy-limits.json").exists()
    assert not (homes / "remote-settings.json").exists()


def test_the_repair_runs_once(homes) -> None:
    _legacy_home(homes, "alice", 1)
    app_module._startup_private_account_state()
    # The shared account's own CLI records who it is again...
    shared = _config(homes / ".claude.json")
    shared["oauthAccount"] = {"accountUuid": "acct-shared", "organizationUuid": "org-team"}
    (homes / ".claude.json").write_text(json.dumps(shared), encoding="utf-8")

    app_module._startup_private_account_state()

    # ...and a later start leaves that alone.
    assert _config(homes / ".claude.json")["oauthAccount"]["accountUuid"] == "acct-shared"


def test_startup_unshares_a_hardlinked_settings_file(homes) -> None:
    # On Windows without symlinks, _link_or_copy hardlinked files instead.
    home = app_module.PERSONAL_HOMES_DIR / app_module._safe_sub("bob") / "2"
    home.mkdir(parents=True)
    os.link(homes / ".claude.json", home / ".claude.json")

    app_module._startup_private_account_state()

    assert not app_module._same_file(home / ".claude.json", homes / ".claude.json")
    assert "oauthAccount" not in _config(home / ".claude.json")
    assert "oauthAccount" not in _config(homes / ".claude.json")


def test_startup_writes_nothing_into_a_home_it_had_nothing_to_unshare(homes) -> None:
    home = app_module.PERSONAL_HOMES_DIR / app_module._safe_sub("carol") / "7"
    home.mkdir(parents=True)

    app_module._startup_private_account_state()

    assert list(home.iterdir()) == []


def test_startup_leaves_a_single_account_install_alone(homes) -> None:
    app_module._ensure_credential_home("alice", 1)  # already private

    app_module._startup_private_account_state()

    assert _config(homes / ".claude.json") == SHARED_CONFIG
    assert (homes / "policy-limits.json").exists()


def test_a_shared_backups_folder_alone_leaves_the_shared_account_alone(homes) -> None:
    # Only the files every account wrote mean the shared copy was overwritten.
    home = app_module._ensure_credential_home("alice", 1)
    try:
        (home / "backups").symlink_to(homes / "backups")
    except OSError:
        pytest.skip("symlinks unavailable on this runner")

    app_module._startup_private_account_state()

    assert not app_module._is_link(home / "backups")
    assert _config(homes / ".claude.json") == SHARED_CONFIG


# ─── signing out ────────────────────────────────────────────────────────────


@pytest.fixture
def api(client, monkeypatch, tmp_path):
    # test_csrf.py reloads the app in strict-CSRF mode and (alphabetically) runs
    # before this file; a matching Origin passes in both modes.
    client.headers["Origin"] = "http://testserver"
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    yield client
    if app_module._STATE_DB is not None:
        app_module._STATE_DB.close()


def test_signing_out_forgets_the_account(api, homes, monkeypatch) -> None:
    cred = app_module._create_credential(ANON, "Alex")
    home = app_module._ensure_credential_home(ANON, cred["id"])
    config = _config(home / ".claude.json")
    config.update(oauthAccount={"accountUuid": "acct-alex", "organizationUuid": "org-team"},
                  cachedGrowthBookFeatures={"x": 1})
    (home / ".claude.json").write_text(json.dumps(config), encoding="utf-8")

    async def sign_out(home=None):
        return None

    monkeypatch.setattr(app_module.setup_flow, "sign_out", sign_out)
    assert api.post(f"/api/account/credentials/{cred['id']}/signout").status_code == 200

    after = _config(home / ".claude.json")
    assert "oauthAccount" not in after and "cachedGrowthBookFeatures" not in after
    assert after["hasCompletedOnboarding"] is True


# ─── MCP servers stay shared ────────────────────────────────────────────────


def test_the_shared_mcp_servers_go_in_a_private_file(homes, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "SHARED_MCP_DIR", tmp_path / "shared-mcp")

    path = app_module._shared_mcp_config_file("/work")

    assert _config(path) == {"mcpServers": {"gemini": GEMINI, "local": LOCAL}}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Outside that project only the user-scope servers apply.
    assert _config(app_module._shared_mcp_config_file(tmp_path)) == {"mcpServers": {"gemini": GEMINI}}

    (homes / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    assert app_module._shared_mcp_config_file("/work") is None


@pytest.fixture
def spawned(monkeypatch):
    captured = {}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        captured["options"] = options
        ready = asyncio.Event()

        class Client:
            async def query(self, prompt, **kwargs):
                ready.set()

            async def receive_messages(self):
                await ready.wait()
                yield SystemMessage(subtype="init", data={"session_id": "account-state-session"})
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="account-state-session",
                )

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *args, **kwargs: True)
    return captured


def test_a_chat_on_another_account_gets_the_shared_mcp_servers(
        api, homes, spawned, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "SHARED_MCP_DIR", tmp_path / "shared-mcp")
    cred = app_module._create_credential(ANON, "Alex")
    home = app_module._ensure_credential_home(ANON, cred["id"])
    (home / ".claude_oauth_token").write_text("sk-ant-oat01-alex", encoding="utf-8")

    response = api.post("/api/chat", data={"message": "hi", "provider": "claude", "model": "opus",
                                           "account_slot": f"cred:{cred['id']}"})
    assert response.status_code == 200
    options = spawned["options"]
    assert options.env["CLAUDE_CONFIG_DIR"] == str(home)
    config_file = Path(options.extra_args["mcp-config"])
    assert _config(config_file)["mcpServers"]["gemini"] == GEMINI


def test_a_shared_account_chat_reads_its_own_servers(api, homes, spawned) -> None:
    response = api.post("/api/chat", data={"message": "hi", "provider": "claude", "model": "opus"})
    assert response.status_code == 200
    assert "mcp-config" not in (spawned["options"].extra_args or {})


# ─── every account's own model list ─────────────────────────────────────────


@pytest.fixture
def restore_models(monkeypatch, tmp_path):
    saved_rows = copy.deepcopy(app_module.CLI_MODEL_ROWS)
    monkeypatch.setattr(app_module, "CLI_MODELS_CACHE", tmp_path / "cli_models.json")
    monkeypatch.setattr(app_module, "MODEL_CONTEXT_CACHE", tmp_path / "ctx.json")
    yield
    app_module._apply_account_model_rows(saved_rows)


def _signed_in(sub: str, label: str, *, token: str = "", api_key: str = "") -> tuple[dict, Path]:
    cred = app_module._create_credential(sub, label)
    home = app_module._ensure_credential_home(sub, cred["id"])
    if token:
        (home / ".claude_oauth_token").write_text(token, encoding="utf-8")
    if api_key:
        (home / ".anthropic_api_key").write_text(api_key, encoding="utf-8")
    return cred, home


def _fake_cli(monkeypatch, answer):
    asked = []

    async def fetch_rows(cli, *, cwd, env=None, timeout=90.0):
        asked.append(dict(env or {}))
        return answer(env or {})

    async def version(cli):
        return "2.1.299 (Claude Code)"

    monkeypatch.setattr(app_module.claude_cli, "resolve", lambda: "/usr/bin/claude")
    monkeypatch.setattr(app_module.claude_models, "fetch_rows", fetch_rows)
    monkeypatch.setattr(app_module, "_cli_version_line", version)
    return asked


def test_each_account_is_asked_under_its_own_identity(api, homes, restore_models, monkeypatch) -> None:
    alex, alex_home = _signed_in(ANON, "Alex", token="sk-ant-oat01-alex")
    office, office_home = _signed_in(ANON, "Office", token="sk-ant-oat01-office")
    _signed_in(ANON, "Emergency", api_key="sk-ant-api03-emergency")
    app_module._create_credential(ANON, "Never signed in")
    asked = _fake_cli(monkeypatch, lambda env: SERVED if env.get(
        "CLAUDE_CONFIG_DIR") == str(alex_home) else COMPILED)

    state = asyncio.run(app_module._refresh_cli_models("test"))

    assert [env["CLAUDE_CONFIG_DIR"] for env in asked] == [
        str(homes), str(alex_home), str(office_home)]
    assert asked[1]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-alex"
    assert asked[1]["ANTHROPIC_API_KEY"] == ""
    assert state["status"] == "ok"
    assert state["accounts"] == {"shared": 5, f"cred:{alex['id']}": 13, f"cred:{office['id']}": 5}
    labels = [m["label"] for m in app_module.KNOWN_MODELS]
    assert "Opus 4.8" in labels and "Sonnet 4.6" in labels
    cached = claude_models.load_cache(app_module.CLI_MODELS_CACHE)["accounts"]
    assert cached[f"cred:{alex['id']}"] == SERVED and cached["shared"] == COMPILED


def test_an_account_that_cannot_answer_keeps_its_last_list(api, homes, restore_models, monkeypatch) -> None:
    alex, alex_home = _signed_in(ANON, "Alex", token="sk-ant-oat01-alex")
    app_module._apply_account_model_rows({"shared": COMPILED, f"cred:{alex['id']}": SERVED})

    def answer(env):
        if env.get("CLAUDE_CONFIG_DIR") == str(alex_home):
            raise TimeoutError("no answer")
        return COMPILED

    _fake_cli(monkeypatch, answer)
    state = asyncio.run(app_module._refresh_cli_models("test"))

    assert state["status"] == "partial"
    assert app_module.CLI_MODEL_ROWS[f"cred:{alex['id']}"] == SERVED
    assert "Opus 4.8" in [m["label"] for m in app_module.KNOWN_MODELS]


def test_the_picker_offers_every_row_of_the_users_accounts(api, homes, restore_models) -> None:
    alex, _ = _signed_in(ANON, "Alex", token="sk-ant-oat01-alex")
    someone_else = [dict(SERVED[0]), {"value": "claude-opus-9", "displayName": "Opus 9",
                                      "resolvedModel": "claude-opus-9"}]
    app_module._apply_account_model_rows({
        "shared": COMPILED, f"cred:{alex['id']}": SERVED, "cred:999": someone_else})
    user = {"sub": ANON}

    payload = {m["key"]: m for m in app_module._models_payload(user)}

    assert payload["claude-opus-4-8"]["label"] == "Opus 4.8"
    assert payload["sonnet"]["label"] == "Sonnet 5.5"
    # Another user's account widens the server's list, not this user's picker.
    assert "claude-opus-9" not in payload
    assert "claude-opus-9" in app_module.MODELS_BY_KEY
    assert payload[""]["labels_by_account"] == {
        "shared": "Default (Sonnet 5.5)", f"cred:{alex['id']}": "Default (Opus 5.5)"}
    assert payload[""]["label"] == "Default (Sonnet 5.5)"

    app_module._set_user_active(ANON, f"cred:{alex['id']}")
    assert app_module._user_model_entries(user)[0]["label"] == "Default (Opus 5.5)"


def test_the_list_can_be_refreshed_on_demand(api, monkeypatch) -> None:
    calls = []

    async def refresh(source):
        calls.append(source)
        return {"status": "ok"}

    monkeypatch.setattr(app_module, "_refresh_cli_models", refresh)
    response = api.post("/api/admin/claude-models")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert calls and calls[0].startswith("api:")
    assert api.get("/api/admin/claude-models").json()["picker"]
