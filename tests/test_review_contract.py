"""Approval and objection regressions, using real permission-gated source reads."""
import copy
import json
from types import SimpleNamespace

import pytest

import panel
import review_contract
from roundtable import core


@pytest.fixture
def reader(tmp_path):
    (tmp_path / "access.py").write_text("def allowed(owner, user):\n    return owner == user\n")
    ctx = SimpleNamespace(working_directory=tmp_path, permission_callback=lambda *args: "allow")
    return panel.evidence_reader(core, ctx, "Reviewer", "Keep the owner check.", "Only owners can access a record.")


def evidence(source="access.py", line=2, quote="    return owner == user"):
    return [{"source": source, "line": line, "quote": quote}]


def report(**changes):
    return {"summary": "The owner check is enforced.",
            "checks": [{"scenario": "A different user requests the record",
                        "result": "The owner comparison returns false", "evidence": evidence()}],
            "findings": [], "resolutions": [], "limitations": [], "verdict": "approve", **changes}


def previous():
    return [{"id": "F1", "status": "open", "claim": "Nonowners can access the record",
             "evidence": evidence()}]


def assess(reader, payload, old=None, stage="changes"):
    return review_contract.assess(json.dumps(payload), old or [], stage=stage, read_line=reader)


def test_clean_work_can_pass_without_inventing_a_finding(reader):
    result = assess(reader, report())
    assert result["verdict"] == "approve"
    assert result["ledger"] == []
    assert "access.py:2" in result["text"]
    assert '"checks"' not in result["text"]


def test_empty_approval_is_not_a_review(reader):
    assert assess(reader, report(checks=[]))["verdict"] == "unclear"


def test_new_code_plan_can_be_checked_against_requirements(reader):
    obj = report()
    obj["checks"][0]["evidence"] = (
        evidence("@requirements", 1, "Only owners can access a record.")
        + evidence("@proposal", 1, "Keep the owner check.")
    )
    assert assess(reader, obj, stage="plan")["verdict"] == "approve"
    assert assess(reader, obj, stage="changes")["verdict"] == "unclear"
    obj["checks"][0]["evidence"] = evidence("@proposal", 1, "Keep the owner check.")
    assert assess(reader, obj, stage="plan")["verdict"] == "unclear"


def test_unchanged_evidence_cannot_be_called_an_implementation_fix(reader):
    result = assess(reader, report(resolutions=[{
        "id": "F1", "status": "fixed", "reason": "The author says the code was changed.",
        "evidence": evidence(),
    }]), previous())
    assert result["ledger"][0]["status"] == "open"
    assert result["validation_error"]


@pytest.mark.parametrize("refs", [
    evidence(quote="return True"), evidence(line=100),
    evidence(source="missing.py"), evidence(source="../access.py"),
    evidence(source=".env"), evidence(source="@proposal", line=1, quote="Keep the owner check."),
    evidence(source=" @proposal", line=1, quote="Keep the owner check."),
])
def test_invented_or_nonrepository_approval_evidence_fails(reader, refs):
    obj = report()
    obj["checks"][0]["evidence"] = refs
    assert assess(reader, obj)["verdict"] == "unclear"


def test_omission_does_not_resolve_an_objection(reader):
    result = assess(reader, report(), previous())
    assert result["verdict"] == "changes"
    assert result["ledger"][0]["status"] == "open"
    assert "F1" in result["text"]


def test_confident_rebuttal_without_evidence_does_not_resolve(reader):
    result = assess(reader, report(resolutions=[{
        "id": "F1", "status": "refuted", "reason": "The author says it is intentional.", "evidence": [],
    }]), previous())
    assert result["verdict"] == "unclear"
    assert result["ledger"][0]["status"] == "open"


def test_a_source_backed_correction_resolves_the_objection(reader):
    result = assess(reader, report(resolutions=[{
        "id": "F1", "status": "refuted", "reason": "The explicit owner comparison denies other users.",
        "evidence": evidence(),
    }]), previous())
    assert result["verdict"] == "approve"
    assert result["ledger"][0]["status"] == "refuted"


def test_revised_proposal_can_fix_a_plan_but_cannot_prove_code_was_fixed(reader):
    obj = report(resolutions=[{
        "id": "F1", "status": "fixed", "reason": "The revised plan preserves the check.",
        "evidence": evidence("@proposal", 1, "Keep the owner check."),
    }])
    assert assess(reader, obj, previous(), stage="plan")["verdict"] == "approve"
    assert assess(reader, obj, previous(), stage="changes")["ledger"][0]["status"] == "open"
    obj["resolutions"][0]["status"] = "refuted"
    assert assess(reader, obj, previous(), stage="plan")["ledger"][0]["status"] == "open"


def test_a_late_invalid_resolution_rolls_back_all_resolutions(reader):
    old = previous()
    old.append({**copy.deepcopy(old[0]), "id": "F2", "claim": "Other concern"})
    obj = report(resolutions=[
        {"id": "F1", "status": "refuted", "reason": "Owner guard already present", "evidence": evidence()},
        {"id": "F2", "status": "fixed", "reason": "Trust me", "evidence": evidence(quote="bogus")},
    ])
    assert assess(reader, obj, old)["ledger"] == old


def test_a_reviewer_cannot_close_a_finding_it_was_not_given(reader):
    obj = report(resolutions=[{"id": "F999", "status": "refuted", "reason": "Not my finding",
                              "evidence": evidence()}])
    assert assess(reader, obj, previous())["ledger"] == previous()


def test_a_new_objection_overrides_an_approve_label(reader):
    obj = report(findings=[{"claim": "Owner data can be missing before this comparison", "evidence": evidence()}])
    assert assess(reader, obj)["verdict"] == "changes"


def test_explicit_abstention_is_never_approval(reader):
    assert assess(reader, report(verdict="abstain", limitations=["Cannot inspect the caller"]))["verdict"] == "unclear"


def test_a_denied_read_and_a_symlink_escape_cannot_validate_evidence(tmp_path):
    (tmp_path / "access.py").write_text("return True\n")
    ctx = SimpleNamespace(working_directory=tmp_path, permission_callback=lambda *args: "deny")
    denied = panel.evidence_reader(core, ctx, "Reviewer", "", "")
    assert denied("access.py", 1) is None
    outer = tmp_path.parent / "outside.txt"
    outer.write_text("return True\n")
    try:
        (tmp_path / "link.py").symlink_to(outer)
    except OSError:
        pytest.skip("symlinks unavailable")
    ctx.permission_callback = lambda *args: "allow"
    assert panel.evidence_reader(core, ctx, "Reviewer", "", "")("link.py", 1) is None


def test_finding_state_survives_reload_and_resets_after_commit():
    state = panel.PanelState(review_ledger={"/repo": {"plan": {"gemini-pro": previous()}}},
                             review_requirements={"/repo": ["Keep the owner check."]})
    restored = panel.PanelState.from_json(state.to_json())
    assert restored.review_ledger == state.review_ledger
    assert restored.review_requirements == state.review_requirements
    restored.end_cycle()
    assert restored.review_ledger == {}
    assert restored.review_requirements == {}
