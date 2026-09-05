"""Automatic credential-slot failover: entitlement inference and slot ranking.

The fixtures mirror the shape measured against real Anthropic accounts on
2026-09-04: two premium Team seats that carry a per-model ``weekly_scoped``
bucket for Fable, one Pro account and one standard Team seat that don't, and
plan windows in a mix of spent/healthy states.

The load-bearing property under test is that entitlement inference *ranks*
slots and never refuses one. Absence of a scoped bucket is ambiguous — it means
either "not entitled" or "drawn from the general pool" — so a slot without one
must stay attemptable, and only an observed model rejection may exclude it.
"""
from __future__ import annotations

import json
import time

import pytest

import app as app_module


SUB = "failover-test-sub"


def _fable_bucket(percent: float = 0.0) -> dict:
    return {
        "kind": "weekly_scoped",
        "scope": {"model": {"id": None, "display_name": "Fable"}},
        "percent": percent,
        "resets_at": "2099-01-01T00:00:00+00:00",
    }


def _usage(limits: list[dict]) -> dict:
    return {"limits": limits, "extra_usage": {}}


def _profile(tier: str, seat: str | None = None) -> dict:
    return {
        "account": {"email": "x@example.com"},
        "organization": {
            "rate_limit_tier": tier,
            "seat_tier": seat,
            "organization_type": "claude_team" if seat else "claude_pro",
        },
    }


def _write_rate_limit(slot: str, info: dict, age_seconds: float = 0.0) -> None:
    """Seed the per-slot plan-window cache directly, at a chosen age."""
    try:
        data = json.loads(app_module.RATE_LIMIT_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    slots = data.setdefault("slots", {})
    slots[slot] = {"info": info, "captured_at": int(time.time() - age_seconds)}
    app_module.RATE_LIMIT_CACHE.write_text(json.dumps(data), encoding="utf-8")


def _healthy_window() -> dict:
    return {
        "status": "allowed",
        "resetsAt": time.time() + 3600,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
    }


def _spent_window(overage: str = "rejected") -> dict:
    return {
        "status": "rejected",
        "resetsAt": time.time() + 3600,
        "rateLimitType": "five_hour",
        "overageStatus": overage,
    }


@pytest.fixture(autouse=True)
def _clean_caches():
    """Each test starts with empty caches and no failover rows.

    Covers "anonymous" as well as this module's own sub: AUTH_MODE=none means
    the TestClient endpoint tests all run as "anonymous", and settings one of
    them writes would otherwise be the starting state of the next.
    """
    for path in (app_module.RATE_LIMIT_CACHE, app_module.ENTITLEMENT_CACHE):
        if path.exists():
            path.unlink()
    db = app_module._state_db()
    for sub in (SUB, "anonymous"):
        db.execute("DELETE FROM account_failover WHERE user_sub = ?", (sub,))
        db.execute("DELETE FROM account_failover_setting WHERE user_sub = ?", (sub,))
    yield


# ─── family extraction ────────────────────────────────────────────────────


def test_normalize_family_matches_ids_and_display_names() -> None:
    assert app_module._normalize_family("Fable") == "fable"
    assert app_module._normalize_family("claude-fable-5-1") == "fable"
    assert app_module._normalize_family("claude-opus-5") == "opus"
    # An unrecognised name degrades to "ungated", never to a denial.
    assert app_module._normalize_family("claude-brandnew-9") == ""
    assert app_module._normalize_family("") == ""


def test_model_families_include_plan_and_advisor_models() -> None:
    """A run needs every family it might reach, not just its primary model.

    `opus5-fable51-advisor` runs Opus but consults Fable mid-turn, so a slot
    without Fable fails partway through a turn rather than at spawn.
    """
    assert app_module._model_families_for_key("claude-fable-5-1") == {"fable"}
    assert app_module._model_families_for_key("claude-opus-5") == {"opus"}
    assert app_module._model_families_for_key("opus5-fable51-advisor") == {"opus", "fable"}
    # fableplan runs Opus 4.8 and plans on Fable 5.
    assert app_module._model_families_for_key("fableplan") == {"opus", "fable"}
    assert app_module._model_families_for_key("nonexistent-key") == set()


# ─── entitlement inference ────────────────────────────────────────────────


def test_scoped_bucket_marks_a_slot_entitled() -> None:
    app_module._save_entitlements(
        "cred:2", _profile("default_claude_max_5x", "team_tier_1"),
        _usage([_fable_bucket()]),
    )
    gated = app_module._gated_families(["cred:2"])
    assert gated == {"fable"}
    assert app_module._slot_entitlement_rank(
        "cred:2", {"fable"}, gated,
    ) == app_module._ENTITLEMENT_PROVEN


def test_missing_bucket_is_unknown_not_denied() -> None:
    """The whole design turns on this: absence must not become a refusal."""
    app_module._save_entitlements(
        "cred:2", _profile("default_claude_max_5x", "team_tier_1"),
        _usage([_fable_bucket()]),
    )
    app_module._save_entitlements(
        "cred:3", _profile("default_raven", "team_standard"), _usage([]),
    )
    gated = app_module._gated_families(["cred:2", "cred:3"])
    rank = app_module._slot_entitlement_rank("cred:3", {"fable"}, gated)
    assert rank == app_module._ENTITLEMENT_UNKNOWN
    assert rank != app_module._ENTITLEMENT_DENIED


def test_family_nobody_meters_is_ungated_for_everyone() -> None:
    """No account meters Opus separately, and every account runs it."""
    app_module._save_entitlements(
        "cred:2", _profile("default_claude_max_5x", "team_tier_1"),
        _usage([_fable_bucket()]),
    )
    app_module._save_entitlements(
        "cred:1", _profile("default_claude_ai"), _usage([]),
    )
    gated = app_module._gated_families(["cred:1", "cred:2"])
    assert "opus" not in gated
    for slot in ("cred:1", "cred:2"):
        assert app_module._slot_entitlement_rank(
            slot, {"opus"}, gated,
        ) == app_module._ENTITLEMENT_PROVEN


def test_spent_scoped_bucket_is_not_proof_of_usable_access() -> None:
    """Having the Fable bucket at 100% is entitlement without capacity."""
    app_module._save_entitlements(
        "cred:2", _profile("default_claude_max_5x", "team_tier_1"),
        _usage([_fable_bucket(percent=100)]),
    )
    gated = app_module._gated_families(["cred:2"])
    assert app_module._slot_entitlement_rank(
        "cred:2", {"fable"}, gated,
    ) == app_module._ENTITLEMENT_UNKNOWN


def test_observed_rejection_is_the_only_hard_denial() -> None:
    app_module._note_model_denial("cred:3", "claude-fable-5-1")
    assert app_module._denied_families("cred:3") == {"fable"}
    assert app_module._slot_entitlement_rank(
        "cred:3", {"fable"}, {"fable"},
    ) == app_module._ENTITLEMENT_DENIED
    # Scoped to the family that was refused; other models stay available.
    assert app_module._slot_entitlement_rank(
        "cred:3", {"opus"}, set(),
    ) == app_module._ENTITLEMENT_PROVEN


def test_expired_entitlement_read_is_ignored() -> None:
    app_module._save_entitlements(
        "cred:2", _profile("default_claude_max_5x", "team_tier_1"),
        _usage([_fable_bucket()]),
    )
    data = app_module._entitlement_cache()
    data["slots"]["cred:2"]["fetched_at"] = int(
        time.time() - app_module._ENTITLEMENT_TTL_SECONDS - 60
    )
    app_module._write_entitlement_cache(data)
    assert app_module._load_entitlement("cred:2") is None
    assert app_module._gated_families(["cred:2"]) == set()


# ─── plan-window health ───────────────────────────────────────────────────


def test_health_classifies_windows() -> None:
    _write_rate_limit("a", _healthy_window())
    _write_rate_limit("b", _spent_window())
    _write_rate_limit("c", _spent_window(overage="allowed"))
    assert app_module._slot_health_rank("a") == app_module._HEALTH_FREE
    assert app_module._slot_health_rank("b") == app_module._HEALTH_SPENT
    assert app_module._slot_health_rank("c") == app_module._HEALTH_PAYABLE
    assert app_module._slot_health_rank("never-seen") == app_module._HEALTH_UNKNOWN


def test_stale_allowed_is_unknown_not_healthy() -> None:
    """A five-hour window can go from 4% to spent inside one heavy session."""
    _write_rate_limit("a", _healthy_window(),
                      age_seconds=app_module._HEALTH_FRESH_SECONDS + 60)
    assert app_module._slot_health_rank("a") == app_module._HEALTH_UNKNOWN


def test_window_whose_reset_has_passed_reads_as_unknown() -> None:
    stale = _spent_window()
    stale["resetsAt"] = time.time() - 10
    _write_rate_limit("a", stale)
    assert app_module._slot_health_rank("a") == app_module._HEALTH_UNKNOWN


def test_reset_pending_accepts_both_timestamp_shapes() -> None:
    assert app_module._reset_pending(time.time() + 60) is True
    assert app_module._reset_pending(time.time() - 60) is False
    assert app_module._reset_pending("2099-01-01T00:00:00+00:00") is True
    assert app_module._reset_pending("2000-01-01T00:00:00Z") is False
    # Unparseable must not pin a slot into the exhausted class forever.
    assert app_module._reset_pending("not a date") is False
    assert app_module._reset_pending(None) is False


# ─── slot selection ───────────────────────────────────────────────────────


@pytest.fixture
def slots():
    """Four credential slots shaped like the real accounts, with creds on disk.

    Returns ``{"personal": "cred:N", ...}``. Homes get a ``.credentials.json``
    because both ``_slot_has_credentials`` and ``_resolve_account_for_run``
    refuse a slot whose home is empty.
    """
    made = {}
    for name in ("personal", "alex", "office"):
        cred = app_module._create_credential(SUB, f"failover-{name}")
        home = app_module._ensure_credential_home(SUB, cred["id"])
        (home / ".credentials.json").write_text("{}", encoding="utf-8")
        made[name] = f"cred:{cred['id']}"
    yield made
    for slot in made.values():
        cred_id = app_module._parse_cred_active(slot)
        app_module._state_db().execute(
            "DELETE FROM user_credential WHERE user_sub = ? AND id = ?",
            (SUB, cred_id),
        )


def _enable(ring: list[str], policy: str = "free_first") -> None:
    """Enable failover over an explicit ring (include_all off)."""
    app_module._set_failover_settings(SUB, True, policy, include_all=False)
    app_module._set_failover_ring(SUB, ring)


def _seed_real_shapes(slots) -> None:
    """Entitlements as measured: Alex meters Fable, Personal and Office don't."""
    app_module._save_entitlements(
        slots["alex"], _profile("default_claude_max_5x", "team_tier_1"),
        _usage([_fable_bucket()]),
    )
    app_module._save_entitlements(
        slots["personal"], _profile("default_claude_ai"), _usage([]),
    )
    app_module._save_entitlements(
        slots["office"], _profile("default_raven", "team_standard"), _usage([]),
    )


def test_disabled_never_substitutes(slots) -> None:
    _seed_real_shapes(slots)
    _write_rate_limit(slots["personal"], _spent_window())
    app_module._set_failover_settings(SUB, False, "free_first")
    app_module._set_failover_ring(SUB, [slots["alex"]])
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-fable-5-1",
    )
    assert chosen == slots["personal"]
    assert sub is None


def test_moves_off_a_spent_plan_window(slots) -> None:
    _enable([slots["personal"], slots["alex"]])
    _write_rate_limit(slots["personal"], _spent_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["alex"]
    assert sub["reason"] == "plan_limit"
    assert sub["from_slot"] == slots["personal"]


def test_moves_to_the_only_slot_that_meters_the_model(slots) -> None:
    """Fable picked on the Pro account: Alex is the one that can run it."""
    _seed_real_shapes(slots)
    _enable([slots["personal"], slots["office"], slots["alex"]])
    _write_rate_limit(slots["personal"], _healthy_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-fable-5-1",
    )
    assert chosen == slots["alex"]
    assert sub["reason"] == "model_unavailable"


def test_advisor_model_pulls_the_same_requirement(slots) -> None:
    """Opus 5 + Fable advisor still needs a Fable-capable credential."""
    _seed_real_shapes(slots)
    _enable([slots["personal"], slots["alex"]])
    _write_rate_limit(slots["personal"], _healthy_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, _ = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "opus5-fable51-advisor",
    )
    assert chosen == slots["alex"]


def test_requested_slot_wins_every_tie(slots) -> None:
    """Enabling failover must not reshuffle a user off the account they picked."""
    _seed_real_shapes(slots)
    _enable([slots["alex"], slots["personal"]])
    _write_rate_limit(slots["personal"], _healthy_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["personal"]
    assert sub is None


def test_never_refuses_a_turn_when_every_slot_looks_bad(slots) -> None:
    """All spent: stay put and let the turn try, rather than stranding it."""
    _enable([slots["personal"], slots["alex"], slots["office"]])
    for slot in slots.values():
        _write_rate_limit(slot, _spent_window())
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["personal"]
    assert sub is None


def test_unknown_outranks_spent_so_an_unchecked_slot_is_tried(slots) -> None:
    """Spawning is the only probe available, so unknown must stay attemptable."""
    _enable([slots["personal"], slots["office"]])
    _write_rate_limit(slots["personal"], _spent_window())
    # office has no cached window at all → unknown
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["office"]
    assert sub is not None


def test_slots_outside_the_ring_are_never_chosen(slots) -> None:
    """Ring membership is the opt-in that keeps a paid slot out of rotation."""
    _enable([slots["personal"]])
    _write_rate_limit(slots["personal"], _spent_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["personal"]
    assert sub is None


def test_prefer_current_keeps_a_payable_slot(slots) -> None:
    """Opting to pay on the current account beats moving to a free one."""
    _enable([slots["personal"], slots["alex"]], policy="prefer_current")
    _write_rate_limit(slots["personal"], _spent_window(overage="allowed"))
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["personal"]
    assert sub is None


def test_free_first_moves_off_a_payable_slot(slots) -> None:
    """The default: don't spend credits while another account has room."""
    _enable([slots["personal"], slots["alex"]], policy="free_first")
    _write_rate_limit(slots["personal"], _spent_window(overage="allowed"))
    _write_rate_limit(slots["alex"], _healthy_window())
    chosen, _ = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen == slots["alex"]


def test_denied_slot_is_skipped_but_ring_still_resolves(slots) -> None:
    _seed_real_shapes(slots)
    _enable([slots["personal"], slots["office"], slots["alex"]])
    for slot in slots.values():
        _write_rate_limit(slot, _healthy_window())
    app_module._note_model_denial(slots["office"], "claude-fable-5-1")
    chosen, _ = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-fable-5-1",
    )
    assert chosen == slots["alex"]


def test_offer_names_another_slot_after_a_mid_turn_failure(slots) -> None:
    _enable([slots["personal"], slots["alex"]])
    _write_rate_limit(slots["personal"], _spent_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    offer = app_module._failover_offer(
        SUB, slots["personal"], "claude-opus-5", "plan_limit",
    )
    assert offer is not None
    assert offer["to_slot"] == slots["alex"]
    assert offer["reason"] == "plan_limit"
    assert offer["to_label"] == "failover-alex"


def test_offer_is_withheld_when_the_alternative_is_also_spent(slots) -> None:
    """Don't send the user to an account that will fail the same way."""
    _enable([slots["personal"], slots["alex"]])
    for slot in slots.values():
        _write_rate_limit(slot, _spent_window())
    offer = app_module._failover_offer(
        SUB, slots["personal"], "claude-opus-5", "plan_limit",
    )
    assert offer is None


def test_ring_is_pruned_when_its_credential_is_deleted(slots) -> None:
    _enable([slots["personal"], slots["alex"]])
    assert slots["alex"] in app_module._failover_ring(SUB)
    app_module._delete_credential(SUB, app_module._parse_cred_active(slots["alex"]))
    assert slots["alex"] not in app_module._failover_ring(SUB)
    row = app_module._state_db().execute(
        "SELECT COUNT(*) FROM account_failover WHERE user_sub = ? AND slot = ?",
        (SUB, slots["alex"]),
    ).fetchone()
    assert row[0] == 0


def test_ring_write_is_atomic_and_replaces_wholesale(slots) -> None:
    _enable([slots["personal"], slots["alex"], slots["office"]])
    assert app_module._failover_ring(SUB) == [
        slots["personal"], slots["alex"], slots["office"],
    ]
    app_module._set_failover_ring(SUB, [slots["office"], slots["personal"]])
    assert app_module._failover_ring(SUB) == [slots["office"], slots["personal"]]


# ─── endpoints ────────────────────────────────────────────────────────────


def test_failover_endpoint_round_trips_settings_and_ring(client) -> None:
    """AUTH_MODE=none, so the caller is the anonymous sub the app assigns."""
    r = client.get("/api/account/failover")
    assert r.status_code == 200
    before = r.json()
    assert before["enabled"] is False
    assert before["spend_policy"] == "free_first"
    # 'shared' is always a slot the caller owns, so it can always be ringed.
    assert any(row["slot"] == "shared" for row in before["slots"])

    r = client.post(
        "/api/account/failover",
        data={"enabled": "true", "spend_policy": "prefer_current", "ring": "shared"},
    )
    assert r.status_code == 200
    after = r.json()
    assert after["enabled"] is True
    assert after["spend_policy"] == "prefer_current"
    assert after["ring"] == ["shared"]
    assert client.get("/api/account/failover").json()["ring"] == ["shared"]


def test_failover_endpoint_rejects_an_unknown_spend_policy(client) -> None:
    r = client.post("/api/account/failover", data={"spend_policy": "whatever"})
    assert r.status_code == 400


def test_failover_endpoint_drops_slots_the_caller_does_not_own(client) -> None:
    """A stale page listing a deleted credential saves the rest of the order."""
    r = client.post(
        "/api/account/failover",
        data={"enabled": "true", "ring": "cred:99999,shared"},
    )
    assert r.status_code == 200
    assert r.json()["ring"] == ["shared"]


def test_failover_rows_describe_state_in_words(client) -> None:
    """The page has to be readable aloud, not colour-coded."""
    rows = client.get("/api/account/failover").json()["slots"]
    row = next(r for r in rows if r["slot"] == "shared")
    assert row["health"] in {
        "has room", "not checked recently",
        "plan spent, credits available", "plan spent",
    }
    assert row["can_run_model"] in {"yes", "unknown", "no"}


def test_run_records_the_requested_slot_separately() -> None:
    """The session→slot binding must record the user's pick, not the substitute.

    Persisting a substitution would rewrite the user's standing choice the
    first time their account hit a limit, and they'd never drift back once it
    reset.
    """
    run = app_module.ActiveRun("r1", owner_sub=SUB, account_slot="cred:2")
    assert run.requested_account_slot == "cred:2"
    run.requested_account_slot = "cred:1"
    assert run.account_slot == "cred:2"
    assert run.requested_account_slot == "cred:1"


# ─── "use all my accounts" mode ───────────────────────────────────────────


def test_include_all_uses_every_subscription_slot_without_a_ring(slots) -> None:
    """The chat-page checkbox alone has to work — no ring configured."""
    app_module._set_failover_settings(SUB, True, "free_first", include_all=True)
    app_module._set_failover_ring(SUB, [])
    _seed_real_shapes(slots)
    _write_rate_limit(slots["personal"], _spent_window())
    _write_rate_limit(slots["alex"], _healthy_window())
    candidates = app_module._failover_candidates(SUB)
    assert slots["alex"] in candidates
    assert slots["office"] in candidates
    chosen, sub = app_module._select_account_slot(
        {"sub": SUB}, slots["personal"], "claude-opus-5",
    )
    assert chosen != slots["personal"]
    assert sub is not None


def test_include_all_excludes_api_key_slots(slots, monkeypatch) -> None:
    """A blanket checkbox must never be able to start per-token billing."""
    app_module._set_failover_settings(SUB, True, "free_first", include_all=True)
    monkeypatch.setattr(
        app_module, "_resolve_credential_mode",
        lambda slot, sub: "api_key" if slot == slots["office"] else "oauth",
    )
    candidates = app_module._failover_candidates(SUB)
    assert slots["office"] not in candidates
    assert slots["alex"] in candidates


def test_naming_a_ring_switches_off_include_all(client) -> None:
    r = client.post("/api/account/failover", data={"enabled": "true"})
    assert r.json()["include_all"] is True
    r = client.post("/api/account/failover", data={"ring": "shared"})
    body = r.json()
    assert body["include_all"] is False
    assert body["ring"] == ["shared"]
