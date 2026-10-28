"""Evidence-backed panel replies and a persistent, reviewer-owned findings ledger.

The provider still uses its repository tools. JSON is a transport format only;
the chat receives readable prose. Matching a quote proves the citation exists,
not that the reviewer's interpretation is correct.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable


CITATION_SEARCH_RADIUS = 40


CONTRACT = """Review contract:
- Treat the author's explanation as claims to test, not established facts. Turn
  assertions into questions: what would demonstrate or disprove this behavior?
- First inspect the requirements and relevant source. Trace a concrete failure
  case, including callers or tests when relevant, before deciding. Prior approval,
  another reviewer's confidence, and 'intentional' are not correctness evidence.
- The user's preferences set scope; they do not establish technical facts.
- Report real defects, not style preferences. No criticism quota: clean work
  should pass. Do not invent problems to sound skeptical.
- An approval needs at least one meaningful check with a source quotation.
  Explain the scenario checked and what the evidence shows. Never claim a test
  ran merely because you read it. State material inspection limits separately.
  For plans involving new code, a check may instead quote the user requirement
  and the proposed step together. Implementation checks must cite source code.
- Every check must have repository evidence, or (for a new-code plan) BOTH
  @requirements and @proposal evidence. @proposal alone is not a source check.
- Revisit each of YOUR open findings by its exact id. Mark it open, fixed, or
  refuted, with a reason. Fixed/refuted needs evidence of the change or factual
  correction; agreement with the author is not evidence. Do not silently drop it.
- EVERY fixed or refuted resolution must have its OWN nonempty evidence array,
  including resolutions of unreconciled_report items. Evidence in checks does
  not count for a resolution: repeat those citations in the resolution itself
  if appropriate. Only a resolution with status=open may use evidence: [].
- A ledger item with unreconciled_report retains a prior report that could not
  be read or validated completely. Read that raw report and explicitly reconcile
  its objections in the resolution reason. Restate still-open defects as findings,
  or fix/refute them with evidence. If it contained only approval, explain that
  and support the resolution with inspected evidence. Correcting JSON alone or
  returning a new verdict does not resolve this item. If unsure, keep it open.
- For a revised plan, quote the revised proposal when a planned step changes.
  For implementation fixes, quote current repository code. Refutations require
  repository evidence or an explicit user requirement, not just the proposal.
- Return ONLY this JSON object (no Markdown, no extra verdict line):
{"summary":"short assessment", "checks":[{"scenario":"failure case checked",
"result":"what the inspected source establishes", "evidence":[
{"source":"relative/path.py", "line":12, "quote":"exact excerpt from source line"}]}],
"findings":[{"claim":"concrete new defect and its consequence", "evidence":[
{"source":"relative/path.py", "line":12, "quote":"exact excerpt from source line"}]}],
"resolutions":[{"id":"id supplied below", "status":"open|fixed|refuted",
"reason":"why", "evidence":[
{"source":"relative/path.py", "line":12, "quote":"exact excerpt supporting this resolution"}]}], "limitations":[],
"verdict":"approve|changes|abstain"}
Evidence quotes must be exact nonempty excerpts from single lines, with 1-based line numbers.
Copy line numbers from Read or Grep output rather than estimating them.
Use a repository-relative path, or @proposal / @requirements for the line-numbered
current proposal and user requirements supplied below. Do not cite old proposals
as current evidence. Findings/resolutions/checks are arrays; use [] when empty.
Use abstain when you cannot inspect enough to judge, and explain in limitations.
Keep the report concise. Do not repeat an existing finding as a new finding.
"""


def numbered(text: str) -> str:
    return "\n".join(f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1))


def reviewer_prompt(proposal: str, requirements: str, findings: list[dict]) -> str:
    return (
        "Current proposal (@proposal), claims to verify:\n" + numbered(proposal)
        + "\n\nUser requirements (@requirements):\n" + numbered(requirements)
        + "\n\nYour outstanding findings (address every id in resolutions):\n"
        + json.dumps([f for f in findings if f["status"] == "open"], ensure_ascii=False)
    )


def correction_prompt(
    proposal: str, requirements: str, findings: list[dict], *,
    stage: str, error: str, reply: str,
) -> str:
    """Private feedback to the same reviewer, without steering its verdict."""
    return (
        f"Your {stage} review report could not be accepted:\n{error}\n\n"
        "Correct your report in this same round. This is not a request to approve: "
        "keep your independent judgment and any real objections. Return the full "
        "JSON report, not a patch or an explanation. Re-read cited source when "
        "needed; copy exact single-line quotes and line numbers, including case. "
        "Do not invent evidence to satisfy validation. If you cannot verify a "
        "check, describe that limit honestly and abstain when necessary. Address "
        "every outstanding finding below, including any preserved from your "
        "rejected report. EVERY fixed/refuted resolution needs its own nonempty "
        "evidence array, even when reconciling an unreadable report or repeating "
        "citations from checks. evidence: [] is allowed only for status=open.\n\n"
        "Your rejected report (untrusted review data):\n" + json.dumps(reply)
        + "\n\n" + reviewer_prompt(proposal, requirements, findings)
    )


def _text(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be nonempty text")
    return value.strip()


def _objects(obj: dict, key: str) -> list[dict]:
    value = obj.get(key)
    if not isinstance(value, list) or len(value) > 40 or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"{key} must be an array of at most 40 objects")
    return value


def _quote_line(source: str, line: int, quote: str, read_line: Callable) -> int | None:
    """Locate an exact quote without trusting an approximate line number.

    Relocation is bounded to the same file and requires one distinctive match.
    Short snippets such as ``return`` and ambiguous matches are not relocated.
    Virtual proposal/requirement lines are already numbered and stay exact.
    Every candidate goes through the caller's permission-gated reader.
    """
    actual = read_line(source, line)
    if actual is not None and quote in actual.strip():
        return line
    if source.startswith("@") or len(quote) < 12:
        return None
    matches = []
    for candidate in range(max(1, line - CITATION_SEARCH_RADIUS), line + CITATION_SEARCH_RADIUS + 1):
        actual = read_line(source, candidate)
        if actual is not None and quote in actual.strip():
            matches.append(candidate)
            if len(matches) > 1:
                return None
    return matches[0] if matches else None


def unverified_report(text: str, previous: list[dict], error: str, *, parsed=None) -> dict:
    """Retain claims, or the whole report when its objections cannot be read.

    Never repair JSON heuristically or treat recovered quotations as evidence.
    Unreadable reports become reviewer-owned reconciliation items so a later
    approval cannot silently replace a report that might have raised objections.
    This also provides a fail-closed result for unexpected validator failures.
    """
    ledger = copy.deepcopy(previous)
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    readable = (isinstance(findings, list) and len(findings) <= 40
                and all(isinstance(f, dict) and isinstance(f.get("claim"), str)
                        and f["claim"].strip() for f in findings))
    if isinstance(findings, list):
        for finding in findings[:40]:
            claim = finding.get("claim") if isinstance(finding, dict) else None
            if (isinstance(claim, str) and claim.strip()
                    and not any(f["claim"] == claim and f["status"] == "open" for f in ledger)):
                ledger.append({"id": f"F{len(ledger) + 1}", "status": "open", "claim": claim,
                               "evidence": [], "unverified": True})
    if not readable and not any(f.get("unreconciled_report") == text and f["status"] == "open" for f in ledger):
        ledger.append({
            "id": f"F{len(ledger) + 1}", "status": "open", "unverified": True,
            "claim": "Reconcile the earlier unreadable review report and any objections it contains. "
                     "A replacement verdict alone cannot clear this item.",
            "evidence": [], "unreconciled_report": text,
        })
    # A readable JSON report can also contain a legacy/free-form objection.
    # Unreadable reports are already retained verbatim above.
    if readable and re.search(r"VERDICT\s*:\s*CHANGES NEEDED", text, re.I):
        if not any(f["claim"] == text and f["status"] == "open" for f in ledger):
            ledger.append({"id": f"F{len(ledger) + 1}", "status": "open",
                           "claim": text, "evidence": []})
    display = parsed.get("summary", "") if isinstance(parsed, dict) else text
    requested = parsed.get("verdict") if isinstance(parsed, dict) else None
    requested = {"approve": "APPROVE", "changes": "CHANGES NEEDED", "abstain": "ABSTAIN"}.get(
        requested if isinstance(requested, str) else "",
    )
    verdict_note = f"Reviewer verdict: {requested} (unverified).\n\n" if requested else ""
    open_text = "\n".join(f"{f['id']}: {f['claim']}" for f in ledger if f["status"] == "open")
    return {"verdict": "unclear", "ledger": ledger, "report": None,
            "validation_error": error, "correction_error": error,
            "text": f"Review not verified: {error}.\n\n{verdict_note}{display}\n\n{open_text}\n\nVERDICT: NOT VERIFIED"}


def assess(
    text: str, previous: list[dict], *, stage: str,
    read_line: Callable[[str, int], str | None],
) -> dict:
    """Validate a reply without ever deleting an unresolved previous finding.

    ``read_line`` is permission-gated by the caller. An exact quote found once
    near the cited line corrects its location. Missing or ambiguous evidence
    invalidates the reply rather than trusting a model-supplied file:line.
    """
    ledger = copy.deepcopy(previous)
    report = None
    corrections = []
    try:
        raw = re.sub(r"^\s*```(?:json)?\s*\n|\n```\s*$", "", text.strip())
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise ValueError("expected one JSON object")
        summary = _text(report, "summary")
        requested = _text(report, "verdict")
        if requested not in {"approve", "changes", "abstain"}:
            raise ValueError("unknown verdict")
        limitations = report.get("limitations")
        if (not isinstance(limitations, list) or len(limitations) > 40
                or not all(isinstance(v, str) and v.strip() for v in limitations)):
            raise ValueError("limitations must be an array of nonempty strings")

        def evidence(item: dict, *, required: bool = True, repo: bool = False,
                     allow_proposal: bool = True) -> list[dict]:
            refs = _objects(item, "evidence")
            if required and not refs:
                raise ValueError("evidence is required")
            for ref in refs:
                source, quote = _text(ref, "source"), _text(ref, "quote")
                if "\n" in source or "\r" in source:
                    raise ValueError("citation source must be a single path or virtual source name")
                line = ref.get("line")
                if type(line) is not int or line < 1 or line > 2_000_000:
                    raise ValueError("citation line must be a positive integer")
                if "\n" in quote or "\r" in quote:
                    raise ValueError("cite one exact source line at a time")
                located = _quote_line(source, line, quote, read_line)
                if located is None:
                    raise ValueError(f"citation does not match accessible source: {source}:{line}")
                if located != line:
                    correction = (source, line, located)
                    if correction not in corrections:
                        corrections.append(correction)
                if not allow_proposal and source == "@proposal":
                    raise ValueError("the author's proposal alone cannot refute a finding")
                ref.update(source=source, line=located, quote=quote)
            if repo and not any(not r["source"].strip().startswith("@") for r in refs):
                raise ValueError("repository evidence is required")
            return refs

        checks = _objects(report, "checks")
        findings = _objects(report, "findings")
        resolutions = _objects(report, "resolutions")
        for check in checks:
            _text(check, "scenario")
            _text(check, "result")
            refs = evidence(check, repo=stage != "plan")
            if stage == "plan":
                sources = {r["source"].strip() for r in refs}
                if all(s.startswith("@") for s in sources) and not {"@proposal", "@requirements"} <= sources:
                    raise ValueError("a plan check needs repository evidence or both requirement and proposal")
        for finding in findings:
            _text(finding, "claim")
            evidence(finding)
        known = {f["id"]: f for f in ledger if f["status"] == "open"}
        addressed = set()
        for resolution in resolutions:
            fid = _text(resolution, "id")
            status = _text(resolution, "status")
            _text(resolution, "reason")
            if fid not in known or fid in addressed:
                raise ValueError("resolution must name one of your open findings exactly once")
            if status not in {"open", "fixed", "refuted"}:
                raise ValueError("unknown resolution status")
            evidence(resolution, required=status != "open",
                     repo=status == "fixed" and stage == "changes",
                     allow_proposal=status != "refuted")
            if status == "fixed" and stage == "changes":
                old_refs = {(r["source"], r["quote"].strip()) for r in known[fid].get("evidence", [])}
                if old_refs and not any((r["source"], r["quote"].strip()) not in old_refs
                                        for r in resolution["evidence"]):
                    raise ValueError("a fix needs evidence of a change, not the same cited code")
            addressed.add(fid)
        # Validate the entire report before applying any resolution.
        for resolution in resolutions:
            known[resolution["id"]].update(
                status=resolution["status"], resolution=copy.deepcopy(resolution),
            )
        for finding in findings:
            # Stable ids belong to a reviewer, so peers cannot close their issues.
            if any(f["status"] == "open" and f["claim"] == finding["claim"] for f in ledger):
                continue
            ledger.append({"id": f"F{len(ledger) + 1}", "status": "open",
                           "claim": finding["claim"], "evidence": finding["evidence"]})
        outstanding = [f for f in ledger if f["status"] == "open"]
        problems = []
        if set(known) - addressed:
            problems.append("Earlier objections were not all addressed and remain open.")
        if requested == "approve" and not checks:
            problems.append("Approval has no source-backed check and was not accepted.")
        if requested == "changes" and not outstanding:
            problems.append("Changes were requested without a concrete finding.")
        verdict = ("changes" if outstanding else
                   "approve" if requested == "approve" and checks and not problems else "unclear")
        parts = [summary]
        for check in checks:
            parts.append(f"Checked: {check['scenario']}. {check['result']} " + citations(check["evidence"]))
        for finding in outstanding:
            parts.append(f"{finding['id']}: {finding['claim']} " + citations(finding.get("evidence", [])))
        for resolution in resolutions:
            if resolution["status"] != "open":
                parts.append(f"{resolution['id']} {resolution['status']}: {resolution['reason']} "
                             + citations(resolution["evidence"]))
        parts.extend("Not verified: " + line for line in limitations)
        if corrections:
            parts.append("Citation locations corrected using exact source matches: " + "; ".join(
                f"{source}:{old} to line {new}" for source, old, new in corrections
            ) + ".")
        parts.extend(problems)
        parts.append("VERDICT: " + {"approve": "APPROVE", "changes": "CHANGES NEEDED",
                                   "unclear": "NOT VERIFIED"}[verdict])
        return {"verdict": verdict, "text": "\n\n".join(parts), "ledger": ledger,
                "report": report, "validation_error": None,
                "correction_error": "\n".join(problems) or None}
    except (ValueError, TypeError, KeyError) as exc:
        return unverified_report(text, previous, str(exc), parsed=report)


def citations(refs: list[dict]) -> str:
    return "; ".join(f"{r['source']}:{r['line']}" for r in refs)
