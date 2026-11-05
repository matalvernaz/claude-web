"""setup_flow.is_configured / save_api_key / sign_out behaviour."""
from __future__ import annotations

import os
import importlib
import json

import pytest


_IS_WINDOWS = os.name == "nt"


@pytest.fixture
def fresh_setup_flow(tmp_path, monkeypatch):
    """Re-import setup_flow with a fresh STATE_DIR so tests don't share state."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    state.mkdir()
    home.mkdir()
    monkeypatch.setenv("CLAUDE_WEB_STATE_DIR", str(state))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import setup_flow
    importlib.reload(setup_flow)
    return setup_flow


def test_is_configured_false_when_nothing_present(fresh_setup_flow) -> None:
    assert fresh_setup_flow.is_configured() is False
    assert fresh_setup_flow.whoami() == {"mode": "none"}


def test_is_configured_true_with_env_var(fresh_setup_flow, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert fresh_setup_flow.is_configured() is True
    assert fresh_setup_flow.whoami() == {"mode": "api_key"}


@pytest.mark.parametrize("oauth, configured", [
    ({"accessToken": "", "refreshToken": "", "expiresAt": 0,
      "subscriptionType": "team"}, False),
    ({"accessToken": "expired", "refreshToken": "refresh", "expiresAt": 1}, True),
    ({"accessToken": "", "refreshToken": "refresh", "expiresAt": 1}, True),
    ({}, False),
])
def test_configured_requires_tokens_but_does_not_reject_refreshable_expiry(
    fresh_setup_flow, oauth, configured,
):
    fresh_setup_flow.credentials_path().write_text(json.dumps({"claudeAiOauth": oauth}))
    assert fresh_setup_flow.is_configured() is configured


@pytest.mark.parametrize("contents", ["{broken", "[]", "null", '{}'])
def test_invalid_credential_file_is_not_signed_in(fresh_setup_flow, contents):
    fresh_setup_flow.credentials_path().write_text(contents)
    assert fresh_setup_flow.is_configured() is False


# Realistic-looking key shape (sk-ant- + 90 url-safe chars). The format
# validator only checks structure; the value never reaches the network in
# tests, so any well-shaped string works.
_FAKE_KEY = "sk-ant-" + "a" * 90


def test_is_configured_true_with_persisted_api_key(fresh_setup_flow, monkeypatch) -> None:
    """Regression: previously is_configured() returned False between
    save_api_key() and the next load_api_key_into_env(), even though the
    file existed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fresh_setup_flow.save_api_key(_FAKE_KEY)
    # Simulate a fresh process: clear the env var that save_api_key set so
    # is_configured has to fall back to the file.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert fresh_setup_flow.is_configured() is True


def test_save_api_key_rejects_empty(fresh_setup_flow) -> None:
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("   ")


def test_save_api_key_rejects_malformed(fresh_setup_flow) -> None:
    """A pasted bash export line or junk text should be rejected at the
    boundary rather than failing the first API call with an opaque 401."""
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("not-a-real-key")
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("export ANTHROPIC_API_KEY=sk-ant-abc")


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX file modes don't apply on NTFS")
def test_save_api_key_writes_mode_600(fresh_setup_flow) -> None:
    fresh_setup_flow.save_api_key(_FAKE_KEY)
    mode = oct(fresh_setup_flow.API_KEY_FILE.stat().st_mode)[-3:]
    assert mode == "600"
    # The per-home copy must also be 0o600 — that's the one the SDK reads
    # for per-credential slots, where a permissive mode would matter on a
    # shared host.
    home_copy = fresh_setup_flow.api_key_path(fresh_setup_flow.CLAUDE_HOME)
    assert oct(home_copy.stat().st_mode)[-3:] == "600"


def test_save_api_key_persists_on_windows(fresh_setup_flow) -> None:
    """The atomic-write path must still produce a readable file on Windows
    even though the chmod step is skipped. The directory permissions on
    %USERPROFILE% are the real access boundary there."""
    fresh_setup_flow.save_api_key(_FAKE_KEY)
    assert fresh_setup_flow.API_KEY_FILE.read_text(encoding="utf-8") == _FAKE_KEY
    home_copy = fresh_setup_flow.api_key_path(fresh_setup_flow.CLAUDE_HOME)
    assert home_copy.read_text(encoding="utf-8") == _FAKE_KEY


# ── long-lived tokens (claude setup-token) ──────────────────────────────

# Real oat01 tokens are 108 characters; the parser leans on that length.
_TOKEN = "sk-ant-oat01-" + "t" * 93 + "AA"


def test_long_lived_token_counts_as_configured_and_is_described(fresh_setup_flow, tmp_path) -> None:
    sf = fresh_setup_flow
    home = tmp_path / "slot"
    home.mkdir()
    assert sf.is_configured(home) is False
    sf.save_oauth_token(_TOKEN, home=home)
    assert sf.is_configured(home) is True
    who = sf.whoami(home)
    assert who["mode"] == "oauth_token"
    assert who["minted_at"] > 0
    assert sf.read_oauth_token(home) == _TOKEN
    if not _IS_WINDOWS:
        assert oct(sf.oauth_token_path(home).stat().st_mode & 0o777) == "0o600"
    # Per-user tokens never touch the process env.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


async def test_shared_long_lived_token_feeds_the_env_and_sign_out_clears_it(fresh_setup_flow, monkeypatch) -> None:
    sf = fresh_setup_flow
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sf, "_resolve_claude_cli", lambda: "/nonexistent/claude")
    sf.save_oauth_token(_TOKEN)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN
    assert sf.is_configured() is True
    assert sf.whoami()["mode"] == "oauth_token"
    # Not monkeypatch.delenv: that would put the token back at teardown.
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN")
    assert sf.load_oauth_token_into_env() == _TOKEN
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN
    await sf.sign_out()
    assert not sf.oauth_token_path().exists()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert sf.is_configured() is False


@pytest.mark.parametrize("bad", ["sk-ant-api03-" + "x" * 80, "sk-ant-oat01-short", "", "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-" + "y" * 80])
def test_anything_but_a_long_lived_token_is_refused(fresh_setup_flow, tmp_path, bad) -> None:
    with pytest.raises(ValueError):
        fresh_setup_flow.save_oauth_token(bad, home=tmp_path)
    assert not fresh_setup_flow.oauth_token_path(tmp_path).exists()


_FAKE_SETUP_TOKEN = '''#!{python}
import sys
url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&scope=user%3Ainference&code_challenge=abc&state=def"
# The real command draws a TUI: the URL arrives inside an OSC 8 hyperlink
# followed by a wrapped visible copy, and the prompt comes a beat later.
sys.stdout.write("Welcome to Claude Code\\n\\x1b]8;id=1;" + url + "\\x1b\\\\" + url[:60] + "\\x1b]8;;\\x1b\\\\\\n")
sys.stdout.write("Paste code here if prompted > ")
sys.stdout.flush()
code = sys.stdin.readline().strip()
if code == "good#code":
    # The real UI wraps the token over lines, styling each piece.
    t = "{token}"
    sys.stdout.write("\\n\\x1b[32mYour token:\\x1b[0m\\n  \\x1b[1m" + t[:60] + "\\x1b[0m\\r\\n  \\x1b[1m" + t[60:] + "\\x1b[0m\\n\\nStore this token securely.\\n")
elif code == "odd#code":
    sys.stdout.write("\\nSomething unexpected happened. Press Enter.\\n")
else:
    sys.stdout.write("\\n" + "*" * len(code) + " OAuth error: Request failed with status code 400. Press Enter to retry.\\n")
sys.stdout.flush()
sys.stdin.readline()
'''


@pytest.fixture
def fake_setup_token(fresh_setup_flow, tmp_path, monkeypatch):
    import sys
    script = tmp_path / "fake-claude"
    script.write_text(_FAKE_SETUP_TOKEN.format(python=sys.executable, token=_TOKEN))
    script.chmod(0o755)
    monkeypatch.setattr(fresh_setup_flow, "_resolve_claude_cli", lambda: str(script))
    return fresh_setup_flow


@pytest.mark.skipif(_IS_WINDOWS, reason="the token command needs a pseudo-terminal")
async def test_the_token_flow_drives_setup_token_through_a_pty_and_saves_the_token(fake_setup_token, tmp_path) -> None:
    sf = fake_setup_token
    home = tmp_path / "slot"
    state = await sf.start_oauth("token", flow_key="tok-good", home=home)
    assert state.status == "awaiting_code"
    assert state.url.startswith("https://claude.com/cai/oauth/authorize?")
    assert state.url.endswith("state=def")  # the whole URL, not the wrapped copy
    result = await sf.submit_code("good#code", flow_key="tok-good")
    assert result.status == "done"
    assert sf.read_oauth_token(home) == _TOKEN
    assert sf.whoami(home)["mode"] == "oauth_token"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


@pytest.mark.skipif(_IS_WINDOWS, reason="the token command needs a pseudo-terminal")
async def test_a_refused_code_fails_the_token_flow_with_the_cli_message(fake_setup_token, tmp_path) -> None:
    sf = fake_setup_token
    home = tmp_path / "slot"
    state = await sf.start_oauth("token", flow_key="tok-bad", home=home)
    assert state.status == "awaiting_code"
    result = await sf.submit_code("bad#code", flow_key="tok-bad")
    assert result.status == "failed"
    assert result.error.startswith("OAuth error: Request failed")
    assert "bad#code" not in result.error
    assert not sf.oauth_token_path(home).exists()
    await sf.cancel_flow("tok-bad")


def test_a_wrapped_and_styled_token_is_read_back_whole(fresh_setup_flow) -> None:
    sf = fresh_setup_flow

    def read(raw: bytes):
        return sf._extract_token(sf._render_screen(raw))

    raw = (b"\x1b[32mYour token:\x1b[0m\r\n  \x1b[1m" + _TOKEN[:50].encode() + b"\x1b[0m\r\n  "
           + _TOKEN[50:].encode() + b"\r\n\r\nStore this token securely. export CLAUDE_CODE_OAUTH_TOKEN=...\r\n")
    assert read(raw) == _TOKEN
    assert read(b"OAuth error: Request failed with status code 400") is None
    # A short fragment never passes as a token.
    assert read(b"sk-ant-oat01-tooshort\r\nStore this") is None
    # Drawn inside a box, with borders between the pieces.
    boxed = ("\u250c" + "\u2500" * 40 + "\u2510\r\n\u2502 " + _TOKEN[:40] + " \u2502\r\n\u2502 "
             + _TOKEN[40:80] + " \u2502\r\n\u2502 " + _TOKEN[80:] + " \u2502\r\n\u2514" + "\u2500" * 40 + "\u2518\r\n")
    assert read(boxed.encode()) == _TOKEN


# A token with no repeats in its first stretch, so a missing or extra character is easy to spot.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_VARIED_TOKEN = "sk-ant-oat01-" + "".join(_ALPHABET[(index * 7) % 64] for index in range(95))
# Cells setup-token skipped on 2026-11-05: the 't' of "oat", then two in the body.
_PREFIX_SKIPS = (9,)
_BODY_SKIPS = (40, 77)


def _diff_rendered_token(token: str, skips: tuple[int, ...]) -> bytes:
    """Bytes the way setup-token's UI draws a token over an earlier row: each run of characters
    placed with a cursor jump, and every cell that already shows the right character left out."""
    out = b"\r"
    index = 0
    while index < len(token):
        if index in skips:
            index += 1
            continue
        run_start = index
        while index < len(token) and index not in skips:
            index += 1
        out += b"\x1b[%dG" % (2 + run_start) + token[run_start:index].encode()
    return out


def _earlier_frame_holding(token: str, skips: tuple[int, ...]) -> bytes:
    """A row drawn before the token, which happens to hold the token's character in every skipped cell
    (on the real screen it was the sign-in URL's text)."""
    cells = [" "] * 120
    for index in skips:
        cells[1 + index] = token[index]
    return "".join(cells).rstrip().encode()


@pytest.mark.parametrize("skips", [_PREFIX_SKIPS, _BODY_SKIPS], ids=["prefix", "body"])
def test_a_token_drawn_over_cells_that_already_match_is_read_back_whole(fresh_setup_flow, skips) -> None:
    sf = fresh_setup_flow
    raw = (b"Welcome\r\n" + _earlier_frame_holding(_VARIED_TOKEN, skips) + b"\r\n\r\nPaste code here if prompted >"
           + b"\x1b[2A" + _diff_rendered_token(_VARIED_TOKEN, skips) + b"\r\n\x1b[J\x1b[2GStore\x1b[8Gthis\x1b[13Gtoken\r\n")
    assert sf._extract_token(sf._render_screen(raw)) == _VARIED_TOKEN


def test_a_token_that_runs_on_past_its_length_is_refused_not_cut(fresh_setup_flow) -> None:
    sf = fresh_setup_flow
    assert sf._extract_token(sf._render_screen(_VARIED_TOKEN.encode() + b"x\r\n")) is None
    assert sf._extract_token(sf._render_screen(_VARIED_TOKEN.encode() + b" next\r\n")) == _VARIED_TOKEN


def test_the_newest_copy_of_a_token_on_screen_wins(fresh_setup_flow) -> None:
    sf = fresh_setup_flow
    # A half-drawn copy from an earlier frame, followed by other text that would make up the length.
    half = _VARIED_TOKEN[:60].encode() + b"\r\n" + b"z" * 48 + b" done\r\n"
    assert sf._extract_token(sf._render_screen(half + _VARIED_TOKEN.encode() + b"\r\n")) == _VARIED_TOKEN


# Draws like the real setup-token: raw mode, words placed with cursor jumps instead of spaces,
# and after the code the token is drawn over earlier rows, leaving out every cell that already
# shows the right character. Row 3 holds those characters before the code arrives.
_FAKE_DIFF_RENDERING_SETUP_TOKEN = '''#!@PYTHON@
import os
import tty

token = "@TOKEN@"
held = [int(index) for index in "@HELD@".split(",")]
skips_for_code = {"prefix#code": [int(index) for index in "@PREFIX_SKIPS@".split(",")],
                  "body#code": [int(index) for index in "@BODY_SKIPS@".split(",")]}
url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&scope=user%3Ainference&code_challenge=abc&state=def"
tty.setraw(0)
status = [" "] * 120
for index in held:
    status[1 + index] = token[index]
os.write(1, b"Welcome\\x1b[9Gto\\x1b[12GClaude\\x1b[19GCode\\r\\n")
os.write(1, b"\\x1b]8;id=1;" + url.encode() + b"\\x07" + url[:150].encode() + b"\\x1b]8;;\\x07\\r\\n\\r\\n")
os.write(1, "".join(status).rstrip().encode() + b"\\r\\n\\r\\n")
os.write(1, b"\\x1b[2GPaste\\x1b[8Gcode\\x1b[13Ghere\\x1b[18Gif\\x1b[21Gprompted\\x1b[30G>\\r\\n")
typed = b""
while not typed.endswith(b"\\r"):
    typed += os.read(0, 1024)
code = typed.strip().decode()
skips = skips_for_code[code]
out = b"\\r\\x1b[31C\\x1b[1A" + b"*" * len(code) + b"\\r\\n"
out += b"\\x1b[4A\\r\\x1b[2GLong-lived\\x1b[13Gtoken\\x1b[19Gcreated\\r\\n\\r"
index = 0
while index < len(token):
    if index in skips:
        index += 1
        continue
    run_start = index
    while index < len(token) and index not in skips:
        index += 1
    out += b"\\x1b[%dG" % (2 + run_start) + token[run_start:index].encode()
out += b"\\r\\n\\x1b[J\\x1b[2GStore\\x1b[8Gthis\\x1b[13Gtoken\\x1b[19Gsecurely.\\r\\n"
os.write(1, out)
os.read(0, 1)
'''


@pytest.fixture
def diff_rendering_setup_token(fresh_setup_flow, tmp_path, monkeypatch):
    import sys
    script = tmp_path / "fake-claude-diff"
    body = (_FAKE_DIFF_RENDERING_SETUP_TOKEN
            .replace("@PYTHON@", sys.executable)
            .replace("@TOKEN@", _VARIED_TOKEN)
            .replace("@HELD@", ",".join(str(index) for index in _PREFIX_SKIPS + _BODY_SKIPS))
            .replace("@PREFIX_SKIPS@", ",".join(str(index) for index in _PREFIX_SKIPS))
            .replace("@BODY_SKIPS@", ",".join(str(index) for index in _BODY_SKIPS)))
    script.write_text(body)
    script.chmod(0o755)
    monkeypatch.setattr(fresh_setup_flow, "_resolve_claude_cli", lambda: str(script))
    return fresh_setup_flow


@pytest.mark.skipif(_IS_WINDOWS, reason="the token command needs a pseudo-terminal")
@pytest.mark.parametrize("code", ["prefix#code", "body#code"])
async def test_a_token_the_ui_only_partly_redrew_is_saved_whole(diff_rendering_setup_token, tmp_path, code) -> None:
    sf = diff_rendering_setup_token
    home = tmp_path / "slot"
    flow_key = "tok-" + code.split("#")[0]
    state = await sf.start_oauth("token", flow_key=flow_key, home=home)
    assert state.status == "awaiting_code"
    result = await sf.submit_code(code, flow_key=flow_key)
    assert result.status == "done", result.error
    assert sf.read_oauth_token(home) == _VARIED_TOKEN


# What the real setup-token 2.1.318 wrote on a 220x50 pty, played back in the same two halves:
# up to the moment a wrong code was typed, and after it.
_FAKE_REPLAYING_SETUP_TOKEN = '''#!@PYTHON@
import os
import tty

with open("@FIXTURE@", "rb") as handle:
    data = handle.read()
typed_at = @TYPED_AT@
tty.setraw(0)
os.write(1, data[:typed_at])
typed = b""
while not typed.endswith(b"\\r"):
    typed += os.read(0, 1024)
os.write(1, data[typed_at:])
os.read(0, 1)
'''


@pytest.mark.skipif(_IS_WINDOWS, reason="the token command needs a pseudo-terminal")
async def test_the_real_setup_token_output_gives_its_url_and_its_error(fresh_setup_flow, tmp_path, monkeypatch) -> None:
    import sys
    from pathlib import Path
    sf = fresh_setup_flow
    fixture = Path(__file__).parent / "fixtures" / "setup_token_wrong_code_2.1.318.bin"
    script = tmp_path / "fake-claude-replay"
    script.write_text(_FAKE_REPLAYING_SETUP_TOKEN
                      .replace("@PYTHON@", sys.executable)
                      .replace("@FIXTURE@", str(fixture))
                      .replace("@TYPED_AT@", "2286"))
    script.chmod(0o755)
    monkeypatch.setattr(sf, "_resolve_claude_cli", lambda: str(script))
    home = tmp_path / "slot"
    state = await sf.start_oauth("token", flow_key="tok-real", home=home)
    assert state.status == "awaiting_code"
    assert state.url.startswith("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-")
    assert state.url.endswith("&state=LCa3gMLwNDhfEhxP9slqDzaZcPs0YcH2vgl7oI2eg0g")
    result = await sf.submit_code("wrong#code", flow_key="tok-real")
    assert result.status == "failed"
    assert result.error == "OAuth error: That code wasn't accepted."
    assert not sf.oauth_token_path(home).exists()
    await sf.cancel_flow("tok-real")


@pytest.mark.skipif(_IS_WINDOWS, reason="the token command needs a pseudo-terminal")
async def test_an_unreadable_result_is_kept_for_diagnosis(fake_setup_token, tmp_path, monkeypatch) -> None:
    sf = fake_setup_token
    monkeypatch.setenv("CLAUDE_WEB_SIGNIN_DEBUG_DIR", str(tmp_path / "debug"))
    monkeypatch.setattr(sf, "EXCHANGE_TIMEOUT_SECONDS", 2)
    home = tmp_path / "slot"
    await sf.start_oauth("token", flow_key="tok-odd", home=home)
    result = await sf.submit_code("odd#code", flow_key="tok-odd")
    assert result.status == "failed"
    assert result.error == "claude setup-token printed no token"
    dump, = (tmp_path / "debug").glob("setup-token-tok-odd-*.txt")
    assert "Something unexpected" in dump.read_text()
    assert oct(dump.stat().st_mode & 0o777) == "0o600"
    await sf.cancel_flow("tok-odd")


async def test_sign_out_of_a_personal_slot_never_carries_the_shared_credentials(fresh_setup_flow, tmp_path, monkeypatch) -> None:
    sf = fresh_setup_flow
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-shared")
    seen = {}

    class _Done:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs["env"]
        return _Done()

    monkeypatch.setattr(sf.asyncio, "create_subprocess_exec", fake_exec)
    home = tmp_path / "slot"
    home.mkdir()
    sf.save_oauth_token(_TOKEN, home=home)
    await sf.sign_out(home)
    assert seen["args"][1:] == ("auth", "logout")
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(home)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert not sf.oauth_token_path(home).exists()
    # The shared slot's own variables are untouched by a personal sign-out.
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN
