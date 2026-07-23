"""FastAPI/SSE coverage for task-aware roundtable assistant workflows."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import app as app_module


def _git_repo(path):
    path.mkdir()
    (path / "service.py").write_text("enabled = False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "initial"],
        cwd=path, check=True,
    )
    (path / "service.py").write_text("enabled = True\n", encoding="utf-8")
    return path


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for record in body.split("\n\n"):
        event = None
        data = None
        for line in record.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        if event and data is not None:
            events.append((event, data))
    return events


def test_roundtable_page_exposes_task_modes(client, monkeypatch):
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda: True)
    response = client.get("/roundtable")
    assert response.status_code == 200
    assert 'id="assistant-task"' in response.text
    assert '<option value="debug"' in response.text
    assert '<option value="review"' in response.text
    assert 'id="capture-working-diff"' in response.text
    assert 'id="verify-review"' in response.text


def test_roundtable_assistant_rejects_unknown_task(client, monkeypatch):
    monkeypatch.setattr(app_module.roundtable_core, "_participant_provider_available", lambda *_: True)
    response = client.post(
        "/api/roundtable/assistant",
        data={"prompt": "do it", "task": "invent-a-mode"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 400
    assert "Unknown coding task" in response.json()["detail"]


def test_review_sse_captures_diff_and_emits_verified_ledger(
    client, tmp_path, monkeypatch,
):
    rt = app_module.roundtable_core
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(rt, "_participant_provider_available", lambda *_: True)
    monkeypatch.setattr(app_module, "_resolve_project_path", lambda key: repo)

    def fake_structured(thread_id, participants, schema, prompt="", effort=""):
        assert "Coding workflow: Review changes" in prompt
        assert schema["additionalProperties"] is False
        return {
            "results": {
                "gemini-pro": {
                    "summary": "one finding",
                    "findings": [{
                        "claim": "the flag changes startup behavior",
                        "file": "service.py", "line": 1,
                        "proof": "enabled = True", "severity": "high",
                        "category": "correctness",
                    }],
                },
                "gpt-5": {"summary": "clean", "findings": []},
            },
            "errors": {},
        }

    def fake_converge(thread_id, findings, verifier, transport):
        assert verifier == "claude-opus"
        assert transport == "auto"
        return {
            "ledger": [{
                "claim": findings[0]["claim"], "file": "service.py", "line": 1,
                "proof": findings[0]["proof"], "severity": "high",
                "verifier": "Claude Opus", "verdict": "confirmed",
                "evidence": "line 1 sets enabled to True",
            }],
            "summary": {"confirmed": 1, "refuted": 0, "unresolved": 0},
        }

    monkeypatch.setattr(rt, "roundtable_ask_structured", fake_structured)
    monkeypatch.setattr(rt, "roundtable_converge", fake_converge)
    monkeypatch.setattr(rt, "roundtable_ask", lambda *a, **k: "Verified review result")

    response = client.post(
        "/api/roundtable/assistant",
        data={
            "prompt": "Review this change",
            "task": "review",
            "project_key": "test-project",
            "capture_working_diff": "true",
            "verify_review": "true",
            "diff_base": "HEAD",
        },
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert names[:2] == ["stream", "created"]
    assert "grounded" in names
    assert "verify_start" in names
    assert "verify_done" in names
    assert names[-1] == "done"

    grounded = next(data for name, data in events if name == "grounded")
    assert grounded["repo_bound"] is True
    assert grounded["diff"]["artifact_version"] == 1
    verified = next(data for name, data in events if name == "verify_done")
    assert verified["summary"] == {
        "confirmed": 1, "refuted": 0, "unresolved": 0,
    }
    done = events[-1][1]
    assert done["task"] == "review"
    assert done["verification"]["ledger"][0]["reviewer"] == "Gemini Pro"
    assert done["synthesis"] == "Verified review result"


def test_frontend_posts_task_and_explicit_review_booleans():
    source = (Path(__file__).parents[1] / "static" / "roundtable.js").read_text(
        encoding="utf-8",  # Windows defaults to cp1252 and chokes on the JS
    )
    assert 'formData.append("task", taskSelect.value)' in source
    assert '"capture_working_diff", captureWorkingDiff.checked ? "true" : "false"' in source
    assert 'formData.append("verify_review", verifyReview.checked ? "true" : "false")' in source
