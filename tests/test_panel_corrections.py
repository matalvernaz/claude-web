"""Report correction through real roundtable storage and permission-gated reads."""
import asyncio
import copy
import json
import subprocess

import pytest

import panel
import review_contract
from roundtable import core


def refs(source="access.py", quote="return owner == user"):
    return [{"source": source, "line": 1, "quote": quote}]


def report(**changes):
    return {"summary": "Ownership is checked.", "checks": [{
        "scenario": "A different user requests a record",
        "result": "The owner comparison denies access", "evidence": refs(),
    }], "findings": [], "resolutions": [], "limitations": [], "verdict": "approve", **changes}


@pytest.fixture
def review(tmp_path, monkeypatch):
    (tmp_path / "access.py").write_text("return owner == user\n", encoding="utf-8")
    monkeypatch.setattr(core, "_participant_provider_available", lambda key: True)
    monkeypatch.setattr(core, "_resolve_participant", lambda key: dict(core.PARTICIPANTS[key]))
    monkeypatch.setattr(core, "_REPO_ROOT_ALLOWLIST", [str(tmp_path)])

    def run(scripts, **kwargs):
        calls, events = [], []
        scripts = copy.deepcopy(scripts)
        stage = kwargs.pop("stage", "plan")
        if stage == "changes":
            for args in (("init", "-q"), ("add", "access.py"), ("commit", "-qm", "Initial guard")):
                subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                                "-c", "commit.gpgsign=false", *args],
                               cwd=tmp_path, check=True, capture_output=True)
            (tmp_path / "access.py").write_text("return owner == user\n# reviewed change\n", encoding="utf-8")

        def turn(thread, info, messages, instruction, tool_use_context, **options):
            label = info["label"]
            calls.append((label, messages, instruction))
            tools = core._RepoTools(tmp_path, tool_use_context.permission_callback, label)
            assert tools.execute("Read", {"path": "access.py"}).startswith("1: return owner == user")
            reply = scripts[label].pop(0)
            if isinstance(reply, Exception):
                raise reply
            return core.ProviderResult(text=reply if isinstance(reply, str) else json.dumps(reply))

        monkeypatch.setattr(core, "_run_turn", turn)
        result = asyncio.run(panel.run_round(
            core, stage=stage, repo=str(tmp_path), message="Keep the owner check.",
            notes=["Only owners can access a record."], thread_id=None,
            participants=["gpt-5", "gemini-pro"], round_no=1, max_rounds=3,
            plan_reviewed=False, user_label="Matt", topic="Ownership", emit=events.append,
            **kwargs,
        ))
        return result, calls, events

    return run


def invalid_report(kind):
    obj = report()
    if kind == "json":
        # Regression: GPT omitted the opening quote of a source excerpt.
        return json.dumps(obj).replace('"quote": "return owner', '"quote": return owner')
    if kind == "citation":
        obj["checks"][0]["evidence"] = refs(quote="return Owner == user")
    if kind == "proposal":
        # Regression: Gemini cited only the author's proposed step for one check.
        obj["checks"].append({"scenario": "The proposal preserves the guard",
                              "result": "The author proposes the right thing",
                              "evidence": refs("@proposal", "Keep the owner check.")})
    if kind == "empty":
        obj["checks"] = []
    return obj


def test_changes_correction_requires_current_repository_evidence_and_keeps_its_snapshot(review):
    obj = report()
    obj["checks"][0]["evidence"] = (
        refs("@proposal", "Keep the owner check.")
        + refs("@requirements", "Only owners can access a record.")
    )
    result, calls, _ = review({"GPT Sol": [report()], "Gemini Pro": [obj, report()]}, stage="changes")
    assert result.outcome == "approved"
    assert set(result.reviewed_state) == {"access.py"}
    assert "repository evidence is required" in calls[-1][2]
    assert "Your changes review report" in calls[-1][2]


@pytest.mark.parametrize("kind, error", [
    ("json", "Expecting value"),
    ("citation", "citation does not match accessible source: access.py:1"),
    ("proposal", "a plan check needs repository evidence or both requirement and proposal"),
    ("empty", "Approval has no source-backed check"),
])
def test_only_the_invalid_reviewer_corrects_its_report_in_the_same_round(review, kind, error):
    corrected = report()
    if kind == "json":
        corrected["resolutions"] = [{"id": "F1", "status": "refuted",
                                     "reason": "The unreadable report only approved; inspected guard denies nonowners",
                                     "evidence": refs()}]
    result, calls, events = review({
        "GPT Sol": [report()], "Gemini Pro": [invalid_report(kind), corrected],
    })
    assert result.outcome == "approved"
    assert [c[0] for c in calls].count("GPT Sol") == 1
    assert [c[0] for c in calls].count("Gemini Pro") == 2
    label, messages, prompt = calls[-1]
    assert label == "Gemini Pro" and error in prompt
    assert "not a request to approve" in prompt
    assert "1: Keep the owner check." in prompt
    assert "1: Only owners can access a record." in prompt
    assert not any(m["speaker"] == "GPT Sol" for m in messages)
    # The raw rejected and corrected reports remain in the audit transcript.
    assert len([m for m in core._thread_messages(result.thread_id) if m["speaker"] == label]) == 2
    assert [e["type"] for e in events] == ["panel_round", "panel_reply", "panel_reply"]
    assert all(e["round"] == 1 for e in events)
    assert all(e["verdict"] == "approve" for e in events if e["type"] == "panel_reply")


def test_correction_does_not_turn_into_an_unbounded_retry_or_accept_bad_evidence(review):
    result, calls, events = review({
        "GPT Sol": [report()],
        "Gemini Pro": [invalid_report("citation"), invalid_report("citation")],
    })
    assert result.outcome == "changes"
    assert len(calls) == 3
    gemini = next(r for r in result.replies if r["participant"] == "gemini-pro")
    assert gemini["verdict"] == "unclear" and "Review not verified" in gemini["text"]
    assert len([e for e in events if e.get("participant") == "Gemini Pro"]) == 1


def test_a_correction_failure_keeps_the_unverified_reply_instead_of_approving(review):
    result, calls, events = review({
        "GPT Sol": [report()],
        "Gemini Pro": [invalid_report("json"), RuntimeError("provider unavailable")],
    })
    assert result.outcome == "changes"
    assert result.errors[0]["participant"] == "gemini-pro"
    assert "provider unavailable" in result.errors[0]["error"]
    assert next(r for r in result.replies if r["participant"] == "gemini-pro")["verdict"] == "unclear"
    assert len(calls) == 3 and len(events) == 3


@pytest.mark.parametrize("resolved", [False, True])
def test_an_objection_in_a_rejected_report_still_belongs_to_its_reviewer(review, resolved):
    bad = invalid_report("citation")
    bad["findings"] = [{"claim": "Nonowners can access the record", "evidence": refs()}]
    fixed = report()
    if resolved:
        fixed["resolutions"] = [{"id": "F1", "status": "refuted",
                                  "reason": "The owner comparison denies other users", "evidence": refs()}]
    result, calls, _ = review({"GPT Sol": [report()], "Gemini Pro": [bad, fixed]})
    assert result.outcome == ("approved" if resolved else "changes")
    assert result.review_ledger["gemini-pro"][0]["status"] == ("refuted" if resolved else "open")
    assert '"id": "F1"' in calls[-1][2]
    assert "Nonowners can access the record" in calls[-1][2]


def test_missing_resolution_gets_feedback_and_can_be_resolved_without_author_resubmission(review):
    previous = [{"id": "F1", "status": "open", "claim": "Nonowners can access the record",
                 "evidence": refs()}]
    corrected = report(resolutions=[{"id": "F1", "status": "refuted",
                       "reason": "The owner comparison denies other users", "evidence": refs()}])
    result, calls, _ = review({"GPT Sol": [report()], "Gemini Pro": [report(), corrected]},
                             review_ledger={"gemini-pro": previous})
    assert result.outcome == "approved"
    assert "Earlier objections were not all addressed" in calls[-1][2]
    assert previous[0]["status"] == "open"  # Never mutate the caller's saved ledger.


@pytest.mark.parametrize("verdict", ["changes", "abstain"])
def test_valid_objections_and_honest_abstentions_are_not_retried(review, verdict):
    response = report(verdict=verdict)
    if verdict == "changes":
        response["findings"] = [{"claim": "The caller supplies the wrong owner", "evidence": refs()}]
    else:
        response.update(checks=[], limitations=["Cannot inspect the caller"])
    result, calls, _ = review({"GPT Sol": [report()], "Gemini Pro": [response]})
    assert result.outcome == "changes" and len(calls) == 2


def test_a_crashing_correction_request_cannot_make_the_panel_step_aside(review, monkeypatch):
    original = core.roundtable_ask_parallel

    def ask(tid, participants, **kwargs):
        if len(participants) == 1:
            raise RuntimeError("correction request crashed")
        return original(tid, participants, **kwargs)

    monkeypatch.setattr(core, "roundtable_ask_parallel", ask)
    result, _, events = review({"GPT Sol": [report()], "Gemini Pro": [invalid_report("json")]})
    assert result.outcome == "changes"
    assert "correction request crashed" in result.errors[0]["error"]
    assert len(events) == 3


@pytest.mark.parametrize("resolved", [False, True])
def test_malformed_objection_requires_explicit_reconciliation_in_the_correction(review, resolved):
    bad = report(verdict="changes", findings=[{"claim": "Nonowners can access the record", "evidence": refs()}])
    raw = json.dumps(bad).replace('"quote": "return owner', '"quote": return owner', 1)
    corrected = report()
    if resolved:
        corrected["resolutions"] = [{"id": "F1", "status": "refuted",
                                     "reason": "The unreadable report claimed missing ownership; the guard denies nonowners",
                                     "evidence": refs()}]
    result, calls, _ = review({"GPT Sol": [report()], "Gemini Pro": [raw, corrected]})
    assert result.outcome == ("approved" if resolved else "changes")
    assert result.review_ledger["gemini-pro"][0]["status"] == ("refuted" if resolved else "open")
    assert result.review_ledger["gemini-pro"][0]["unreconciled_report"] == raw
    assert "Nonowners can access the record" in calls[-1][2]
    assert '"id": "F1"' in calls[-1][2]


@pytest.mark.parametrize("callbacks", [False, True])
@pytest.mark.parametrize("phase", ["initial", "correction"])
def test_validator_faults_retain_the_report_and_ledger_without_becoming_outages(review, monkeypatch, callbacks, phase):
    previous = [{"id": "F1", "status": "open", "claim": "Nonowners can access the record", "evidence": refs()}]
    failing = report(summary="Simulate a validator fault", resolutions=[{
        "id": "F1", "status": "refuted", "reason": "The guard denies other users", "evidence": refs(),
    }])
    raw = json.dumps(failing)
    original_assess = review_contract.assess

    def faulty_assess(text, old, **kwargs):
        if "Simulate a validator fault" in text:
            old[0]["status"] = "refuted"  # Partial mutation before the fault must roll back.
            raise RuntimeError("Injected validator failure")
        return original_assess(text, old, **kwargs)

    monkeypatch.setattr(review_contract, "assess", faulty_assess)
    if not callbacks:
        original_ask = core.roundtable_ask_parallel

        def ask(*args, **kwargs):
            kwargs["on_result"] = None
            return original_ask(*args, **kwargs)

        monkeypatch.setattr(core, "roundtable_ask_parallel", ask)
    scripts = [failing] if phase == "initial" else [invalid_report("citation"), failing]
    result, calls, events = review({"GPT Sol": [report()], "Gemini Pro": scripts},
                                  review_ledger={"gemini-pro": previous})
    assert result.outcome == "changes"
    assert result.errors == []  # Local validation failure must not be classified as provider outage.
    gemini = next(r for r in result.replies if r["participant"] == "gemini-pro")
    assert gemini["verdict"] == "unclear"
    assert "Report validator failed: RuntimeError: Injected validator failure" in gemini["text"]
    assert raw in gemini["text"]
    ledger = result.review_ledger["gemini-pro"]
    assert ledger[0] == previous[0] and previous[0]["status"] == "open"
    assert ledger[1]["unreconciled_report"] == raw and ledger[1]["status"] == "open"
    assert len(calls) == (2 if phase == "initial" else 3)
    assert len([e for e in events if e.get("participant") == "Gemini Pro"]) == 1
    assert next(e for e in events if e.get("participant") == "Gemini Pro")["verdict"] == "unclear"


def test_all_validator_faults_cannot_make_the_panel_step_aside(review, monkeypatch):
    def faulty(*args, **kwargs):
        raise RuntimeError("Validator unavailable")

    monkeypatch.setattr(review_contract, "assess", faulty)
    result, calls, events = review({"GPT Sol": [report()], "Gemini Pro": [report()]})
    assert result.outcome == "changes" and result.errors == []
    assert len(calls) == 2 and len(result.replies) == 2
    assert all(r["verdict"] == "unclear" for r in result.replies)
    assert all(rows[0]["status"] == "open" for rows in result.review_ledger.values())
    assert len(events) == 3


@pytest.mark.parametrize("old_objection", [False, True])
def test_actual_provider_outages_keep_their_existing_behavior(review, old_objection):
    old = {"gemini-pro": [{"id": "F1", "status": "open", "claim": "Missing owner check",
                           "evidence": refs()}]} if old_objection else {}
    result, calls, events = review({
        "GPT Sol": [RuntimeError("provider unavailable")],
        "Gemini Pro": [RuntimeError("provider unavailable")],
    }, review_ledger=old)
    assert result.outcome == ("changes" if old_objection else "unavailable")
    assert result.review_ledger == old
    assert len(result.errors) == 2 and len(calls) == 2 and len(events) == 3
