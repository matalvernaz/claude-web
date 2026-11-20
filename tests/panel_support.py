"""Valid provider replies for tests of panel routing, gates, and lifecycle."""
import json
from pathlib import Path
from types import SimpleNamespace

import panel


class SourceTools:
    def __init__(self, root, callback, label):
        self.root = Path(root)

    def execute(self, name, args):
        return "1: " + source_line(self.root)


def source_line(root):
    path = Path(root) / "a.py"
    return path.read_text().splitlines()[0] if path.is_file() else "a = 1"


def context(root):
    return SimpleNamespace(working_directory=root, permission_callback=lambda *args: "allow")


def report(text, root, prompt=""):
    """Translate a scripted vote into an evidence-bearing provider response."""
    if text.lstrip().startswith("{"):
        return text
    change = panel.parse_verdict(text) == "changes"
    refs = [{"source": "a.py", "line": 1, "quote": source_line(root)}]
    old = json.loads(prompt.split("Your outstanding findings (address every id in resolutions):\n")[-1]) if prompt else []
    return json.dumps({
        "summary": text.splitlines()[0],
        "checks": [{"scenario": "Read the assignment", "result": "The value matches the proposal", "evidence": refs}],
        "findings": [{"claim": "The assignment is incorrect", "evidence": refs}] if change else [],
        "resolutions": [{"id": f["id"], "status": "open" if change else "refuted",
                         "reason": "The current assignment establishes the value", "evidence": refs} for f in old],
        "limitations": [], "verdict": "changes" if change else "approve",
    })


class CaptureSupport:
    """Use real Git capture while keeping review publication and inference scripted."""

    @staticmethod
    def capture_review_diff(root, base='HEAD'):
        from roundtable.core import capture_review_diff
        return capture_review_diff(root, base)

    def publish_review_diff(self, thread_id, capture):
        import review_capture
        review_capture.validate(capture)
        return self.roundtable_bind_diff(thread_id, capture['root'], capture['base'])
