"""Panel review: other AIs check a chat's plan and changes before they land.

When panel review is on for a chat, the chat's Claude needs a review panel's
approval twice: for its plan before it changes files in a git repository, and
for the actual changes before it commits them. The panel is the roundtable's
coding panel (GPT, Gemini and a second Claude by default), reading the
repository read-only. Claude makes every change itself; the panel only reviews.

How it fits together:

- Gates. app.py registers PreToolUse hooks on every Claude run. Hooks fire in
  every permission mode, bypassPermissions included (verified on CLI 2.1.296),
  so they also work in chats that never prompt. The edit gate stops Edit and
  Write in a git repository until a plan is approved; the commit gate stops
  ``git commit`` until the panel has seen the repository exactly as it now
  stands. Edits made through the shell slip past the edit gate, which is why
  every message the user sends in a panel chat carries a short instruction,
  and why the commit gate is the one that can't be skipped.
- Rounds. Claude calls the in-process ``review`` tool, which starts a round in
  the background and returns at once, telling Claude to end its turn. The
  panel's answer is delivered as the next message. Neither a hook's
  ``additionalContext`` nor a mid-turn query reaches the model as the user's
  words (the first is distrusted as injected text, the second is dropped), so
  turn boundaries are the only place the user can join in, and ending the turn
  at each round gives them one every few minutes.
- The user decides. After MAX_ROUNDS rounds on one stage without approval,
  they get a question card: go ahead anyway, give the panel more rounds, or
  stop.

This module is stdlib-only and imports nothing from app.py, like
codex_provider. It holds the repository checks, the commit parser, verdict
parsing, the per-chat state record, one review round against the roundtable
library (passed in, so tests can use a fake) and the words the panel and
Claude see. app.py owns runs, events, delivery and the question card.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

MAX_ROUNDS = max(1, int(os.getenv("CLAUDE_WEB_PANEL_MAX_ROUNDS", "3")))
EFFORT = os.getenv("CLAUDE_WEB_PANEL_EFFORT", "medium")
# Comma-separated roundtable participant keys; empty uses the roundtable's
# default coding panel.
PARTICIPANTS = [
    p.strip() for p in os.getenv("CLAUDE_WEB_PANEL_PARTICIPANTS", "").split(",")
    if p.strip()
]

SERVER_NAME = "panel"
TOOL_NAME = f"mcp__{SERVER_NAME}__review"
STAGES = ("plan", "changes")
AUTHOR = "Claude (author)"
# Every message the panel sends Claude starts with this, so a reopened chat
# can show it as the panel's answer rather than as something the user typed.
MESSAGE_MARKER = "[Review panel]"
# Opens the instruction put in front of the user's messages in a panel chat;
# a reopened chat strips everything up to the blank line after it.
USER_PREFIX_MARKER = "[Panel review is on in this chat."

_GIT_TIMEOUT = 20


# ─── The repository ──────────────────────────────────────────────────────────

def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, timeout=_GIT_TIMEOUT,
    )


def repo_root(path: str) -> Optional[str]:
    """The git work tree holding ``path``, or None outside any repository.

    ``path`` is absolute and need not exist yet (a file Write is about to
    create): the nearest existing parent decides.
    """
    p = Path(path)
    if not p.is_absolute():
        return None
    d = p if p.is_dir() else p.parent
    while not d.exists():
        if d.parent == d:
            return None
        d = d.parent
    try:
        r = _git(str(d), "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "surrogateescape").strip() or None


def working_state(root: str) -> dict[str, str]:
    """Every path in ``root`` that differs from HEAD, mapped to its content.

    Staged, unstaged and untracked changes all count (ignored files don't);
    each path maps to a digest of what is on disk now, or "deleted". Two calls
    return the same dict exactly when nothing git could commit has changed in
    between, which is what lets one approval cover a commit split in two.
    """
    r = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if r.returncode != 0:
        raise RuntimeError(
            r.stderr.decode("utf-8", "replace").strip() or "git status failed"
        )
    fields = r.stdout.split(b"\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:].decode("utf-8", "surrogateescape"))
        # With -z a rename or copy is "XY new\0old\0": the old path is the
        # next field, and it is gone from the working tree.
        if b"R" in entry[:2] or b"C" in entry[:2]:
            if i < len(fields) and fields[i]:
                paths.append(fields[i].decode("utf-8", "surrogateescape"))
            i += 1
    return {rel: _digest(Path(root) / rel) for rel in paths}


def _digest(p: Path) -> str:
    try:
        if p.is_symlink():
            return "link:" + os.readlink(p)
        if p.is_dir():
            # A submodule or nested repository: its own commits are not
            # this repository's changes to review.
            return "dir"
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return "deleted"


def head_commit(root: str) -> Optional[str]:
    """The commit HEAD points at, or None (no commits yet, or git failed)."""
    try:
        r = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip() or None


def unapproved(approved: dict[str, str], current: dict[str, str]) -> list[str]:
    """Paths whose current content isn't what the panel approved."""
    return sorted(p for p, digest in current.items() if approved.get(p) != digest)


# ─── Spotting a commit in a shell command ────────────────────────────────────

_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LOOSE_COMMIT = re.compile(r"(?<![\w./-])git\b[^\n;&|]*?\bcommit\b")
# Commands that run the rest of their arguments as a command.
_WRAPPERS = {"sudo", "env", "command", "nice", "nohup", "time", "exec", "builtin"}
_GIT_OPTS_WITH_VALUE = {
    "-c", "--git-dir", "--namespace", "--super-prefix", "--config-env",
    "--exec-path", "--list-cmds",
}
_PUNCT = ";&|()"


def _strip_heredocs(command: str) -> str:
    """Drop here-document bodies: a commit message written with
    ``git commit -F - <<'EOF'`` can say anything, including 'git commit'."""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            term = m.group(2)
            while i < len(lines) and lines[i].strip() != term:
                i += 1
            i += 1
    return "\n".join(out)


def _segments(command: str) -> Optional[list[list[str]]]:
    """Split a shell command into simple commands (argv lists), or None when
    it won't tokenize (unbalanced quotes)."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=_PUNCT)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t and all(c in _PUNCT for c in t):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _resolve(base: Optional[str], target: str) -> Optional[str]:
    target = os.path.expanduser(target)
    if os.path.isabs(target):
        return os.path.normpath(target)
    if base is None:
        return None
    return os.path.normpath(os.path.join(base, target))


def _unwrap(argv: list[str]) -> list[str]:
    """Skip leading VAR=value assignments and wrappers like sudo, env, nice.

    Wrappers take options with values (``sudo -u matt``), so rather than
    parse each one, jump to the first word that starts a command we care
    about.
    """
    if not argv:
        return argv
    first = argv[0]
    if _ENV_ASSIGN.match(first) or first in _WRAPPERS or first == "timeout":
        for i, a in enumerate(argv):
            if os.path.basename(a) == "git" or a in ("cd", "pushd", "bash", "sh", "zsh"):
                return argv[i:]
        return []
    return argv


def commit_targets(command: str, cwd: str) -> Optional[list[str]]:
    """Directories where ``command`` would run ``git commit``.

    Returns [] when it doesn't commit, and None when it seems to but the
    directory can't be worked out (a ``cd -``, quoting the tokenizer can't
    follow); the caller then refuses and asks for ``git -C <repo> commit``.
    ``cwd`` is the shell's directory when the command starts, which the hook
    input carries (it follows earlier ``cd`` commands; verified on 2.1.296).
    """
    text = _strip_heredocs(command.replace("\\\n", " "))
    segs = _segments(text)
    if segs is None:
        return None if _LOOSE_COMMIT.search(text) else []
    here: Optional[str] = cwd or None
    found: list[str] = []
    for raw in segs:
        argv = _unwrap(raw)
        if not argv:
            continue
        head = argv[0]
        if head in ("cd", "pushd"):
            target = argv[1] if len(argv) > 1 else "~"
            here = None if target == "-" else _resolve(here, target)
            continue
        if head in ("bash", "sh", "zsh") and len(argv) >= 3 and argv[1] in ("-c", "-lc", "-ec"):
            inner = commit_targets(argv[2], here or "")
            if inner is None:
                return None
            found.extend(inner)
            continue
        if os.path.basename(head) != "git":
            continue
        where = here
        sub = None
        i = 1
        while i < len(argv):
            a = argv[i]
            if a in ("-C", "--work-tree") and i + 1 < len(argv):
                where = _resolve(where, argv[i + 1])
                i += 2
            elif a.startswith("--work-tree="):
                where = _resolve(where, a.split("=", 1)[1])
                i += 1
            elif a in _GIT_OPTS_WITH_VALUE and i + 1 < len(argv):
                i += 2
            elif a.startswith("-"):
                i += 1
            else:
                sub = a
                break
        if sub == "commit":
            if where is None:
                return None
            found.append(where)
    return found


# ─── Verdicts ────────────────────────────────────────────────────────────────

_VERDICT = re.compile(
    r"VERDICT\s*[:\-]\s*[*_`]*\s*(APPROVED?|CHANGES(?:\s+NEEDED)?|REQUEST(?:ING)?\s+CHANGES)",
    re.IGNORECASE,
)


def parse_verdict(text: str) -> str:
    """"approve", "changes", or "unclear" from a reply's last VERDICT line."""
    found = _VERDICT.findall(text or "")
    if not found:
        return "unclear"
    return "approve" if found[-1].upper().startswith("APPROVE") else "changes"


# ─── Per-chat state ──────────────────────────────────────────────────────────

@dataclasses.dataclass
class PanelState:
    """What app.py keeps per session in the ``session_panel`` table."""

    enabled: bool = False
    thread_id: Optional[int] = None
    plan_ok: bool = False
    rounds: dict = dataclasses.field(default_factory=lambda: {"plan": 0, "changes": 0})
    # repo root -> {path: digest}: the working state the panel last approved.
    approvals: dict = dataclasses.field(default_factory=dict)
    # Set when a round's answer found no live chat to deliver to; the next
    # message the user sends carries it in front.
    pending_message: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "PanelState":
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        fields = {f.name for f in dataclasses.fields(cls)}
        state = cls(**{k: v for k, v in data.items() if k in fields})
        state.rounds = {s: int((state.rounds or {}).get(s, 0)) for s in STAGES}
        state.approvals = dict(state.approvals or {})
        return state

    def end_cycle(self) -> None:
        """A task's changes are committed: the next change starts with a plan."""
        self.plan_ok = False
        self.rounds = {s: 0 for s in STAGES}


# ─── What the panel and Claude see ───────────────────────────────────────────

TOOL_DESCRIPTION = (
    "The user's review panel: GPT, Gemini and a second Claude, run through the "
    "roundtable. When panel review is on in this chat they check your work at two "
    "points, and you can't skip them: (1) stage \"plan\": before you change files in "
    "a git repository, by any means, send your plan; (2) stage \"changes\": when the "
    "changes are made and before you commit, send a summary, and the panel reviews "
    "the repository's actual diff. Each call starts one round in the background and "
    "returns at once. End your turn right after calling it: the panel's answer "
    "arrives as your next message, usually within a few minutes, and the user can "
    "talk to you while you wait. When they want changes, fix what you agree with, "
    "push back with reasons on what you don't (they read your reasons), and send it "
    "again. After a few rounds without agreement the user decides."
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "stage": {
            "type": "string", "enum": list(STAGES),
            "description": "\"plan\" before changing files; \"changes\" before committing.",
        },
        "repo": {
            "type": "string",
            "description": "Absolute path of the git repository the work is in.",
        },
        "message": {
            "type": "string",
            "description": (
                "Your plan, or what you changed and why. From the second round on, "
                "also answer the panel's points: what you changed, and why you "
                "disagree with any you didn't take."
            ),
        },
    },
    "required": ["stage", "repo", "message"],
}


def user_prefix() -> str:
    """The instruction in front of each message the user sends in a panel chat.

    It rides the user's own message because that's the one channel Claude
    reliably follows: the hooks' deny reasons only fire at Edit/Write and
    commit, and a ``sed -i`` in the shell edits files without either.
    """
    return (
        f"{USER_PREFIX_MARKER} Before you change any file in a git repository, by any "
        "means including the shell, send your plan to the review panel with the "
        f"{TOOL_NAME} tool (stage \"plan\"; load it with ToolSearch if needed) and end "
        "your turn. Before you commit, send the changes the same way (stage "
        "\"changes\"). The panel's answers arrive as messages.]\n\n"
    )


def strip_user_prefix(text: str) -> str:
    if not text.startswith(USER_PREFIX_MARKER):
        return text
    _, sep, rest = text.partition("]\n\n")
    return rest if sep else text


def house_rules(user_label: str) -> str:
    return (
        f"You're on a review panel for {user_label}, who owns this project. "
        f"{AUTHOR} is the AI doing the work in {user_label}'s chat: it makes every "
        "change itself and sends you its plan before editing and its changes before "
        "committing. You review; you never edit. You can read the repository with "
        "your tools.\n"
        "- Check claims against the actual code and cite file:line.\n"
        "- Raise only what matters: bugs, wrong assumptions, missed cases, risky or "
        "unnecessary changes, missing tests. No style nits or matters of taste.\n"
        f"- When {AUTHOR} pushes back on a point, accept good reasoning or explain "
        "concretely why it's still wrong. Don't repeat points that are settled.\n"
        f"- {user_label} may join in. What {user_label} asks for wins over your "
        "preferences.\n"
        "- Keep it short: a sentence or two of summary, then numbered points if you "
        "have any.\n"
        "- End every reply with exactly one line, VERDICT: APPROVE or VERDICT: "
        "CHANGES NEEDED. APPROVE means good enough to go ahead; minor notes can ride "
        "along."
    )


def round_prompt(
    stage: str, round_no: int, max_rounds: int, repo: str,
    plan_reviewed: bool, diff_note: str = "",
) -> str:
    budget = (
        "Budget about 10 tool calls: read what the work touches, not the whole tree."
    )
    ending = "End with exactly one line: VERDICT: APPROVE or VERDICT: CHANGES NEEDED."
    if stage == "plan":
        return (
            f"Round {round_no} of {max_rounds} on {AUTHOR}'s PLAN for {repo}. Nothing "
            "has been changed yet. Check the plan against the repository: wrong "
            "assumptions, missed cases, risky steps, a simpler way that does the same "
            f"job. {diff_note + ' ' if diff_note else ''}{budget} {ending}"
        )
    against = (
        "Check them against the plan approved earlier in this thread."
        if plan_reviewed else
        "No plan was reviewed before these changes were made, so judge the approach "
        "as well as the code."
    )
    return (
        f"Round {round_no} of {max_rounds} on {AUTHOR}'s CHANGES in {repo}, before "
        f"they're committed. {diff_note or 'The working diff is attached as an artifact.'} "
        f"{against} Look for bugs, regressions, missing tests and anything that "
        f"doesn't match what was asked. {budget} {ending}"
    )


def submitted_text(stage: str, round_no: int, max_rounds: int, labels: list[str]) -> str:
    who = _join(labels) or "the panel"
    hold = "change files" if stage == "plan" else "commit"
    return (
        f"Sent to the panel (round {round_no} of {max_rounds} on your {stage}): {who} "
        f"{'is' if len(labels) == 1 else 'are'} reviewing it. Their answer will arrive "
        "as your next message, usually within a few minutes. End your turn now: tell "
        "the user in a sentence that it's with the panel. Don't "
        f"{hold} until they approve."
    )


def edit_denied(root: str, subagent: bool) -> str:
    if subagent:
        return (
            "Panel review is on in this chat and the panel hasn't approved a plan yet, "
            f"so files in {root} can't be changed. Report back to the main agent: it has "
            "to get the plan approved before anyone edits."
        )
    return (
        "Panel review is on in this chat, and the panel hasn't approved a plan yet. "
        f"Before you change files in {root}, send your plan with the {TOOL_NAME} tool "
        "(stage \"plan\"; load it with ToolSearch if needed) and end your turn. Their "
        "answer arrives as your next message. Reading files is fine meanwhile."
    )


def commit_denied(root: str, paths: list[str]) -> str:
    shown = ", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "")
    return (
        "Panel review is on in this chat, and the panel hasn't approved the changes "
        f"in {root} as they stand now: {shown}. Send them with the {TOOL_NAME} tool "
        "(stage \"changes\") and end your turn; commit after they approve. If some of "
        "those files are noise that shouldn't be committed, say so to the panel."
    )


def commit_unclear() -> str:
    return (
        "Panel review is on in this chat, and claude-web couldn't tell which "
        "repository this commit runs in. Run it as `git -C /absolute/path/to/repo "
        "commit ...` so the panel's approval can be checked."
    )


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_VERDICT_WORDS = {
    "approve": "approves", "changes": "wants changes", "unclear": "no clear verdict",
}


def reply_heading(label: str, verdict: str) -> str:
    return f"{label}: {_VERDICT_WORDS.get(verdict, verdict)}"


def result_message(
    stage: str, round_no: int, max_rounds: int, outcome: str,
    replies: list[dict], errors: list[dict], user_label: str,
) -> str:
    """The panel's answer, delivered to Claude as its next message.

    ``outcome`` is approved, changes, unavailable, go_ahead (the user overrode
    a deadlock), more_rounds, stop, or undecided (the question card timed out).
    """
    phrase = {
        "approved": "approved",
        "changes": "they want changes",
        "unavailable": "nobody on the panel could answer, so the panel stepped "
                       f"aside and this counts as approved; {user_label} has been told",
        "go_ahead": f"they still had objections, and {user_label} decided to go ahead",
        "more_rounds": f"they still had objections, and {user_label} wants you and "
                       f"the panel to keep going for up to {max_rounds} more rounds",
        "stop": f"they still had objections, and {user_label} asked you to stop here",
        "undecided": f"they still had objections, and {user_label} hasn't decided yet",
    }[outcome]
    parts = [
        f"{MESSAGE_MARKER} This message is from claude-web's review panel, not typed "
        f"by {user_label}.",
        f"Round {round_no} of {max_rounds} on your {stage}: {phrase}.",
    ]
    for r in replies:
        parts.append(f"### {reply_heading(r['label'], r['verdict'])}\n{r['text'].strip()}")
    for e in errors:
        parts.append(f"### {e['label']}: couldn't answer\n{e['error']}")
    approved_next = (
        "Next: make the changes. Before you commit, send them to the panel (stage "
        "\"changes\") and end your turn."
        if stage == "plan" else
        "Next: commit these changes as they are. Change anything first and it needs "
        "another round."
    )
    hold = "change files" if stage == "plan" else "commit"
    nxt = {
        "approved": approved_next,
        "unavailable": approved_next,
        "go_ahead": approved_next,
        "changes": (
            "Next: fix what you agree with and push back on what you don't (they'll "
            f"read your reasons), then send it again (stage \"{stage}\") and end your "
            f"turn. Don't {hold} until they approve."
        ),
        "more_rounds": (
            "Next: answer their points, then send it again (stage "
            f"\"{stage}\") and end your turn. Don't {hold} until they approve."
        ),
        "stop": (
            f"Next: don't change files or commit. Tell {user_label} where things stand "
            "and wait for them."
        ),
        "undecided": (
            f"Next: stop here and wait for {user_label}. When they answer, send it to "
            f"the panel again (stage \"{stage}\") and they'll be asked to decide."
        ),
    }[outcome]
    parts.append(nxt)
    return "\n\n".join(parts)


DECISION_QUESTION = "The panel still has objections. What now?"
DECISIONS = {
    "Go ahead anyway": "go_ahead",
    "Keep discussing": "more_rounds",
    "Stop": "stop",
}


def decision_card(stage: str, rounds: int, max_rounds: int) -> list[dict]:
    """The question card the user gets when a stage deadlocks."""
    what = "plan" if stage == "plan" else "changes"
    return [{
        "question": (
            f"The panel still has objections to Claude's {what} after {rounds} "
            f"round{'s' if rounds != 1 else ''}. What now?"
        ),
        "header": "Panel",
        "multiSelect": False,
        "options": [
            {"label": "Go ahead anyway",
             "description": (
                 "Treat it as approved: Claude makes the changes."
                 if stage == "plan" else
                 "Treat it as approved: Claude commits these changes."
             )},
            {"label": "Keep discussing",
             "description": f"Give them up to {max_rounds} more rounds."},
            {"label": "Stop",
             "description": "Claude stops here and waits for you."},
        ],
    }]


# ─── One round ───────────────────────────────────────────────────────────────

@dataclasses.dataclass
class RoundResult:
    thread_id: int
    outcome: str  # approved, changes, unavailable
    replies: list[dict]
    errors: list[dict]
    # For a changes round, the working state the panel was shown, which is
    # what an approval covers.
    reviewed_state: Optional[dict] = None


def outcome_of(replies: list[dict]) -> str:
    if not replies:
        return "unavailable"
    return "approved" if all(r["verdict"] == "approve" for r in replies) else "changes"


async def run_round(
    core: Any, *, stage: str, repo: str, message: str, notes: list[str],
    thread_id: Optional[int], participants: list[str], round_no: int,
    max_rounds: int, plan_reviewed: bool, user_label: str, topic: str,
    emit: Callable[[dict], None],
    on_thread: Callable[[int], None] = lambda tid: None,
    effort: str = EFFORT,
) -> RoundResult:
    """Run one review round through the roundtable library ``core``.

    Creates the chat's thread on first use, binds the repository read-only,
    attaches the working diff for a changes round, posts the user's recent
    messages and Claude's message, then asks every participant in parallel.
    Each blocking library call runs in a worker thread. ``emit`` gets the
    events the chat shows; it's called on the event loop.
    """
    labels = {p: core.PARTICIPANTS[p]["label"] for p in participants}
    tid = thread_id
    if tid is None:
        created = await asyncio.to_thread(
            core.roundtable_create, topic=topic, participants=list(participants),
            house_rules=house_rules(user_label),
        )
        tid = int(created["thread_id"])
        on_thread(tid)
    reviewed_state = None
    diff_note = ""
    try:
        await asyncio.to_thread(core.roundtable_bind_repo, tid, repo, "readonly")
    except (ValueError, RuntimeError) as exc:
        # A deployment can fence repositories off (ROUNDTABLE_REPO_ROOTS).
        # Review from what's posted rather than not at all.
        diff_note = (
            f"The repository couldn't be opened for you ({exc}): judge from what's "
            "posted in this thread."
        )
    if stage == "changes":
        reviewed_state = await asyncio.to_thread(working_state, repo)
        try:
            diff = await asyncio.to_thread(core.roundtable_bind_diff, tid, repo, "HEAD")
            if diff.get("truncated"):
                diff_note = (
                    "The working diff is attached as an artifact, cut short because "
                    "it's large: read the files for the rest."
                )
        except (ValueError, RuntimeError) as exc:
            diff_note = (diff_note + " " if diff_note else "") + (
                f"The working diff couldn't be attached ({exc}): read the files "
                "with your tools if you can."
            )
    for note in notes:
        await asyncio.to_thread(core.roundtable_post, tid, note, user_label)
    await asyncio.to_thread(core.roundtable_post, tid, message, AUTHOR)
    emit({
        "type": "panel_round", "stage": stage, "round": round_no,
        "max_rounds": max_rounds, "repo": repo, "thread_id": tid,
        "participants": [labels[p] for p in participants],
    })
    loop = asyncio.get_running_loop()
    shown: set[str] = set()

    def _reply_event(p: str, text: Optional[str], error: Optional[str]) -> dict:
        return {
            "type": "panel_reply", "stage": stage, "round": round_no,
            "participant": labels[p],
            "verdict": "error" if error is not None else parse_verdict(text or ""),
            "text": error if error is not None else (text or ""),
        }

    def _landed(p: str, text: Optional[str], error: Optional[str]) -> None:
        # On the library's thread: hand each reply to the loop as it lands,
        # so the chat shows GPT at three minutes rather than everyone at six.
        if p in labels and p not in shown:
            shown.add(p)
            loop.call_soon_threadsafe(emit, _reply_event(p, text, error))

    result = await asyncio.to_thread(
        core.roundtable_ask_parallel, tid, list(participants),
        prompt=round_prompt(stage, round_no, max_rounds, repo, plan_reviewed, diff_note),
        effort=effort, tool_use_context=core._effective_tool_context(tid),
        on_result=_landed,
    )
    replies: list[dict] = []
    errors: list[dict] = []
    for p in participants:
        if p in (result.get("responses") or {}):
            text = str(result["responses"][p] or "")
            replies.append({"participant": p, "label": labels[p],
                            "verdict": parse_verdict(text), "text": text})
            if p not in shown:
                emit(_reply_event(p, text, None))
        elif p in (result.get("errors") or {}):
            err = str(result["errors"][p])
            errors.append({"participant": p, "label": labels[p], "error": err})
            if p not in shown:
                emit(_reply_event(p, None, err))
    return RoundResult(
        thread_id=tid, outcome=outcome_of(replies), replies=replies,
        errors=errors, reviewed_state=reviewed_state,
    )
