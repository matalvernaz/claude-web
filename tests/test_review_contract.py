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


@pytest.mark.parametrize("verdict", ["approve", "changes"])
def test_exact_quote_with_approximate_line_preserves_the_review(tmp_path, verdict):
    """Regression: both a finding and its later resolution became 'unclear'
    because Gemini read unnumbered source and estimated citation locations.
    """
    quote = "return 50 if saved is None else saved"
    (tmp_path / "volume.py").write_text("# surrounding source\n" * 184 + quote + "\n")
    reads = []

    def allow(label, tool, args):
        reads.append((tool, args))
        return "allow"

    ctx = SimpleNamespace(working_directory=tmp_path, permission_callback=allow)
    reader = panel.evidence_reader(core, ctx, "Reviewer", "", "")
    refs = evidence("volume.py", 178, quote)
    obj = report(checks=[{"scenario": "Saved mute value", "result": "Zero remains zero", "evidence": refs}],
                 verdict=verdict)
    if verdict == "changes":
        obj["findings"] = [{"claim": "A concrete defect requiring attention", "evidence": refs}]
        old = []
    else:
        old = [{"id": "F1", "status": "open", "claim": "Zero was replaced by 50",
                "evidence": [], "unverified": True}]
        obj["resolutions"] = [{"id": "F1", "status": "fixed", "reason": "Only None uses the fallback",
                               "evidence": refs}]
    result = assess(reader, obj, old)
    assert result["verdict"] == verdict
    assert result["validation_error"] is None
    assert result["report"]["checks"][0]["evidence"][0]["line"] == 185
    assert refs[0]["line"] == 178  # The caller's original report stays intact.
    assert "volume.py:178 to line 185" in result["text"]
    assert len(reads) == 1  # Search uses one permission-gated window, not 81 reads.
    if verdict == "changes":
        assert result["ledger"][0]["evidence"][0]["line"] == 185
    else:
        assert result["ledger"][0]["status"] == "fixed"


@pytest.mark.parametrize("lines, cited, quote", [
    (["return owner == user", "# here", "return owner == user"], 2, "return owner == user"),
    (["return owner == user", "# here"], 2, "return"),
    (["return owner != user", "# here"], 2, "return owner == user"),
    (["# source"] * 100 + ["return owner == user"], 2, "return owner == user"),
])
def test_relocation_rejects_ambiguous_short_changed_or_distant_quotes(tmp_path, lines, cited, quote):
    (tmp_path / "access.py").write_text("\n".join(lines))
    ctx = SimpleNamespace(working_directory=tmp_path, permission_callback=lambda *args: "allow")
    reader = panel.evidence_reader(core, ctx, "Reviewer", "", "")
    obj = report()
    obj["checks"][0]["evidence"] = evidence(line=cited, quote=quote)
    result = assess(reader, obj)
    assert result["verdict"] == "unclear"
    assert result["validation_error"]
    assert "Reviewer verdict: APPROVE (unverified)." in result["text"]


def test_relocation_still_respects_denied_reads(tmp_path):
    (tmp_path / "access.py").write_text("# header\nreturn owner == user\n")
    denied = []

    def deny(*args):
        denied.append(args)
        return "deny"

    reader = panel.evidence_reader(core, SimpleNamespace(working_directory=tmp_path,
                                   permission_callback=deny), "Reviewer", "", "")
    obj = report()
    obj["checks"][0]["evidence"] = evidence(line=1)
    assert assess(reader, obj)["verdict"] == "unclear"
    assert len(denied) == 1


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
    evidence(source="missing.py\n2: return owner == user\n"),
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


def test_read_errors_are_never_treated_as_numbered_source(reader):
    assert reader("missing.py\n2: return owner == user\n", 2) is None


def test_finding_state_survives_reload_and_resets_after_commit():
    state = panel.PanelState(review_ledger={"/repo": {"plan": {"gemini-pro": previous()}}},
                             review_requirements={"/repo": ["Keep the owner check."]})
    restored = panel.PanelState.from_json(state.to_json())
    assert restored.review_ledger == state.review_ledger
    assert restored.review_requirements == state.review_requirements
    restored.end_cycle()
    assert restored.review_ledger == {}
    assert restored.review_requirements == {}


def malformed_report(payload):
    # Peer reproduction: a missing opening quote makes the whole report unreadable.
    return json.dumps(payload).replace('"quote": "', '"quote": ', 1)


@pytest.mark.parametrize("verdict", ["approve", "changes"])
def test_an_unreadable_report_cannot_be_silently_replaced_by_approval(reader, verdict):
    obj = report(verdict=verdict)
    if verdict == "changes":
        obj["findings"] = [{"claim": "Nonowners can access the record", "evidence": evidence()}]
    raw = malformed_report(obj)
    first = review_contract.assess(raw, [], stage="changes", read_line=reader)
    assert first["verdict"] == "unclear"
    marker, = first["ledger"]
    assert marker["status"] == "open" and marker["unreconciled_report"] == raw
    assert marker["evidence"] == [] and marker["unverified"]
    second = assess(reader, report(), first["ledger"])
    assert second["verdict"] == "changes" and second["ledger"] == first["ledger"]
    # Repeated malformed output must not inflate the ledger with duplicate markers.
    assert review_contract.assess(raw, first["ledger"], stage="changes", read_line=reader)["ledger"] == first["ledger"]
    state = panel.PanelState(review_ledger={"/repo": {"changes": {"gpt-5": first["ledger"]}}})
    assert panel.PanelState.from_json(state.to_json()).review_ledger == state.review_ledger


@pytest.mark.parametrize("with_evidence", [False, True])
@pytest.mark.parametrize("status", ["fixed", "refuted"])
def test_a_malformed_approval_is_repairable_with_explicit_evidenced_reconciliation(reader, with_evidence, status):
    raw = malformed_report(report())
    first = review_contract.assess(raw, [], stage="changes", read_line=reader)
    obj = report(resolutions=[{
        "id": "F1", "status": status,
        "reason": "The unreadable report only approved the owner guard; the comparison still denies nonowners",
        "evidence": evidence() if with_evidence else [],
    }])
    result = assess(reader, obj, first["ledger"])
    assert result["verdict"] == ("approve" if with_evidence else "unclear")
    assert result["ledger"][0]["status"] == (status if with_evidence else "open")


def test_reconciling_an_unreadable_report_can_restate_a_still_open_defect(reader):
    raw = malformed_report(report(verdict="changes", findings=[{
        "claim": "The caller supplies another user's owner", "evidence": evidence(),
    }]))
    first = review_contract.assess(raw, [], stage="changes", read_line=reader)
    corrected = report(verdict="changes", findings=[{
        "claim": "The caller supplies another user's owner", "evidence": evidence(),
    }], resolutions=[{
        "id": "F1", "status": "fixed", "reason": "The unreadable report's objection is restated in findings",
        "evidence": evidence(),
    }])
    result = assess(reader, corrected, first["ledger"])
    assert result["verdict"] == "changes"
    assert [f["status"] for f in result["ledger"]] == ["fixed", "open"]
    assert result["ledger"][1]["claim"] == corrected["findings"][0]["claim"]


@pytest.mark.parametrize("raw", ["null", "[]", '{"findings": {"claim": "Missing owner check"}}'])
def test_unreadable_findings_are_kept_verbatim_until_reconciled(reader, raw):
    first = review_contract.assess(raw, [], stage="changes", read_line=reader)
    assert first["ledger"][0]["unreconciled_report"] == raw
    assert assess(reader, report(), first["ledger"])["verdict"] == "changes"


@pytest.mark.parametrize("unreadable", [False, True])
def test_a_resolved_objection_repeated_in_a_rejected_report_must_be_reconciled_again(reader, unreadable):
    obj = report(verdict="changes", findings=[{
        "claim": "Nonowners can access the record", "evidence": evidence(),
    }])
    if unreadable:
        raw = malformed_report(obj)
    else:
        obj["checks"][0]["evidence"] = evidence(quote="nonexistent code")
        raw = json.dumps(obj)
    first = review_contract.assess(raw, [], stage="changes", read_line=reader)
    resolved = assess(reader, report(resolutions=[{
        "id": "F1", "status": "refuted", "reason": "The owner guard denies nonowners", "evidence": evidence(),
    }]), first["ledger"])
    assert resolved["verdict"] == "approve" and resolved["ledger"][0]["status"] == "refuted"
    repeated = review_contract.assess(raw, resolved["ledger"], stage="changes", read_line=reader)
    assert [f["status"] for f in repeated["ledger"]] == ["refuted", "open"]
    assert repeated["ledger"][1]["id"] == "F2"
    assert assess(reader, report(), repeated["ledger"])["verdict"] == "changes"
