"""Tests for the roundtable standing-context layer.

Covers the context pack added to roundtable.core: storage + migration onto a
pre-existing DB, injection into the system prompt (and absence from the
transcript), cache-stability of that prompt across turns, the transcript-budget
floor when a pack is large, oversize truncation, the bind_context allowlist,
and create-time round-trip. The hermetic state dir is set in conftest.py, so
these never touch the host's real ~/.claude-roundtable/state.db.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

import roundtable.core as core


def _gemini_info() -> dict:
    return {"provider": "gemini", "model": "gemini-pro-latest", "label": "Gemini Pro"}


# ─── migration onto a DB that predates thread_context ────────────────────

def test_thread_context_migrates_onto_preexisting_db(tmp_path, monkeypatch):
    # A DB created before this feature has no thread_context table. Point core
    # at one and confirm CREATE TABLE IF NOT EXISTS adds it on connect — no
    # ALTER, no crash — and that a full round-trip works on the migrated DB.
    old = tmp_path / "old-state.db"
    raw = sqlite3.connect(str(old))
    raw.execute(
        "CREATE TABLE threads (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "topic TEXT NOT NULL, participants_json TEXT NOT NULL DEFAULT '[]', "
        "created_at REAL NOT NULL, closed_at REAL, house_rules TEXT)"
    )
    raw.commit()
    raw.close()
    monkeypatch.setattr(core, "DB_PATH", old)
    monkeypatch.setattr(core, "_db", None)  # force a fresh connect to old.db

    conn = core._conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_context)").fetchall()}
    assert {"thread_id", "content", "source", "created_at"} <= cols

    tid = core.roundtable_create("migrate")["thread_id"]
    core.roundtable_set_context(tid, "MIGRATED-OK")
    assert core.roundtable_context(tid)["content"] == "MIGRATED-OK"
    # monkeypatch restores core.DB_PATH / core._db after the test.


# ─── injection into the system prompt ────────────────────────────────────

def test_set_context_injected_after_house_rules_in_system_prompt():
    sp = core._build_system_prompt(
        {
            "id": 1, "topic": "t", "participants": ["gemini-pro"],
            "house_rules": "HR-MARKER", "context_pack": "CTX-MARKER",
        },
        "Gemini Pro", ["gemini-pro"],
    )
    assert "HR-MARKER" in sp
    assert "CTX-MARKER" in sp
    assert "Standing context for this thread" in sp
    # Context block sits after house rules (deterministic order → stable prefix).
    assert sp.index("CTX-MARKER") > sp.index("HR-MARKER")


def test_context_pack_absent_from_transcript_but_present_in_prompt():
    tid = core.roundtable_create("t")["thread_id"]
    core.roundtable_set_context(tid, "SECRET-CTX-XYZ")
    core._append_message(tid, "orchestrator", "hello")

    tx = core._format_transcript(
        core._thread_messages(tid), for_participant_label="Gemini Pro"
    )
    assert "SECRET-CTX-XYZ" not in tx  # pack stays out of the volatile transcript

    sp = core._build_system_prompt(
        core._thread_row(tid), "Gemini Pro", ["gemini-pro"]
    )
    assert "SECRET-CTX-XYZ" in sp  # …and reaches the cached system prefix


def test_system_prompt_is_byte_stable_for_caching():
    # OpenAI keys its prompt cache off sha256(system_prompt); Anthropic matches
    # the longest cached prefix. Both require the prompt to be byte-identical
    # turn-to-turn. The pack is stored once, so two builds must match exactly.
    tid = core.roundtable_create("t")["thread_id"]
    core.roundtable_set_context(tid, "STABLE-CTX")

    sp1 = core._build_system_prompt(
        core._thread_row(tid), "Gemini Pro", ["gemini-pro"]
    )
    sp2 = core._build_system_prompt(
        core._thread_row(tid), "Gemini Pro", ["gemini-pro"]
    )
    assert sp1 == sp2
    assert (
        hashlib.sha256(sp1.encode()).hexdigest()
        == hashlib.sha256(sp2.encode()).hexdigest()
    )


# ─── transcript-budget floor ─────────────────────────────────────────────

def test_run_turn_reserves_transcript_floor_for_large_pack(monkeypatch):
    captured: dict = {}
    real = core._trim_messages_to_cap

    def spy(messages, cap, for_participant_label):
        captured["cap"] = cap
        return real(messages, cap, for_participant_label=for_participant_label)

    monkeypatch.setattr(core, "_trim_messages_to_cap", spy)
    monkeypatch.setattr(core, "PROMPT_CHAR_CAP", 1000)
    monkeypatch.setattr(core, "MIN_TRANSCRIPT_CAP", 200)
    monkeypatch.setattr(core, "_call_gemini", lambda *a, **k: core.ProviderResult(text="ok"))

    thread = {
        "id": 1, "topic": "t", "participants": ["gemini-pro"],
        "house_rules": "", "context_pack": "X" * 5000,
    }
    # Pack (5000) exceeds the whole cap (1000) → budget floors at MIN_TRANSCRIPT_CAP.
    core._run_turn(thread, _gemini_info(), [], "", None, False)
    assert captured["cap"] == 200

    # Small pack → budget is cap minus pack length, above the floor.
    thread["context_pack"] = "Y" * 100
    core._run_turn(thread, _gemini_info(), [], "", None, False)
    assert captured["cap"] == 1000 - 100


# ─── oversize truncation ─────────────────────────────────────────────────

def test_oversize_context_pack_is_truncated(monkeypatch):
    monkeypatch.setattr(core, "CONTEXT_PACK_CHAR_CAP", 1000)
    tid = core.roundtable_create("t")["thread_id"]

    res = core.roundtable_set_context(tid, "Z" * 5000)
    assert res["truncated"] is True
    assert res["bytes"] <= 1000

    stored = core.roundtable_context(tid)["content"]
    assert len(stored) <= 1000
    assert "omitted within this message" in stored


# ─── bind_context allowlist ──────────────────────────────────────────────

def test_bind_context_enforces_allowlist(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(core, "_REPO_ROOT_ALLOWLIST", [str(allowed.resolve())])

    inside = allowed / "conv.md"
    inside.write_text("CONV route via Traefik")
    tid = core.roundtable_create("t")["thread_id"]
    res = core.roundtable_bind_context(tid, [str(inside)])
    assert res["files"] == [str(inside.resolve())]
    assert "CONV route via Traefik" in core.roundtable_context(tid)["content"]

    outside = tmp_path / "outside.md"
    outside.write_text("nope")
    with pytest.raises(ValueError):
        core.roundtable_bind_context(tid, [str(outside)])


# ─── create-time + replace round-trips ───────────────────────────────────

def test_create_with_context_roundtrips():
    created = core.roundtable_create("t", context="HELLO-CREATE-CTX")
    assert created["context_bytes"] == len("HELLO-CREATE-CTX")
    assert created["context_truncated"] is False
    ctx = core.roundtable_context(created["thread_id"])
    assert ctx["content"] == "HELLO-CREATE-CTX"
    assert ctx["source"] == "inline"


def test_set_context_replaces_previous():
    tid = core.roundtable_create("t")["thread_id"]
    core.roundtable_set_context(tid, "FIRST")
    core.roundtable_set_context(tid, "SECOND")
    assert core.roundtable_context(tid)["content"] == "SECOND"


# ─── grounded converge ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("VERDICT: confirmed\nEVIDENCE: line 2", "confirmed"),
    ("verdict - refuted", "refuted"),
    ("this is unresolved, frankly", "unresolved"),
    ("could be confirmed or refuted, unsure", "unresolved"),  # ambiguous, no VERDICT
    ("total nonsense", "unresolved"),
])
def test_parse_verdict(text, expected):
    assert core._parse_verdict(text) == expected


def test_converge_requires_repo_binding(monkeypatch):
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)
    tid = core.roundtable_create("t")["thread_id"]
    with pytest.raises(RuntimeError):
        core.roundtable_converge(tid, [{"claim": "x", "file": "a.py", "line": 1}])


def test_converge_unknown_verifier_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    tid = core.roundtable_create("t")["thread_id"]
    core.roundtable_bind_repo(tid, str(repo))
    with pytest.raises(ValueError):
        core.roundtable_converge(tid, [], verifier="not-a-model")


def test_converge_confirms_finding_against_real_code(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)
    calls = {"n": 0}

    def fake_cli(model, system_prompt, transcript, instruction, *a, **k):
        calls["n"] += 1
        # the actual code excerpt must have reached the verifier
        assert "needle" in transcript
        return core.ProviderResult(text="VERDICT: confirmed\nEVIDENCE: defined at line 2")

    monkeypatch.setattr(core, "_call_anthropic_cli", fake_cli)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("import os\ndef needle():\n    return 1\n")
    tid = core.roundtable_create("t")["thread_id"]
    core.roundtable_bind_repo(tid, str(repo))

    out = core.roundtable_converge(
        tid,
        [{"claim": "needle is defined", "file": "mod.py", "line": 2,
          "proof": "def needle", "severity": "low"}],
    )
    assert calls["n"] == 1
    assert out["summary"] == {"confirmed": 1, "refuted": 0, "unresolved": 0}
    assert out["ledger"][0]["verdict"] == "confirmed"
    assert out["ledger"][0]["verifier"] == core.PARTICIPANTS["claude-opus"]["label"]


def test_converge_unresolved_on_missing_file_skips_model(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)
    called = {"n": 0}
    monkeypatch.setattr(
        core, "_call_anthropic_cli",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1)
        or core.ProviderResult(text="x"),
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    tid = core.roundtable_create("t")["thread_id"]
    core.roundtable_bind_repo(tid, str(repo))

    out = core.roundtable_converge(
        tid,
        [{"claim": "ghost", "file": "nope.py", "line": 9, "proof": "", "severity": "high"}],
    )
    assert called["n"] == 0  # never paid for a model call on an unreadable cite
    assert out["ledger"][0]["verdict"] == "unresolved"
    assert out["summary"]["unresolved"] == 1
