"""Panel review: other AIs check a chat's plan and changes before they land.

When panel review is on for a chat, the chat's Claude needs a review panel's
approval twice: for its plan before it changes files in a git repository, and
for the actual changes before it commits them. The panel is the roundtable's
coding panel (GPT, Gemini and a second Claude by default), reading the
repository read-only. Claude makes every change itself; the panel only reviews.

When it's off, Claude can still ask for a review of big or risky work. The
user is asked first, on a card; a yes covers that stage's rounds until it
resolves or the user says something in the chat. Nothing is gated then: the
panel's answer is advice, the user's word in the chat settles a disagreement,
and there's no decision card.

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

import review_contract

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
# Between a parked panel answer and the user's message that carries it, so a
# reopened chat can show the two apart.
PARKED_SEPARATOR = "\n\n[The user's message follows.]\n\n"

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
            # A submodule or nested repository: what this repository would
            # commit is the commit it points at.
            try:
                r = _git(str(p), "rev-parse", "HEAD")
                return "dir:" + r.stdout.decode().strip() if r.returncode == 0 else "dir"
            except (OSError, subprocess.TimeoutExpired):
                return "dir"
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        # The executable bit is part of what git commits.
        return h.hexdigest() + (":x" if os.access(p, os.X_OK) else "")
    except FileNotFoundError:
        return "deleted"


def staged_differs(root: str) -> list[str]:
    """Paths whose staged content isn't what's on disk.

    The panel reviews files as they are on disk, and ``git commit`` commits
    what's staged, so a path staged at one version and edited (or restored)
    since would commit something the panel never saw.
    """
    def names(*args: str) -> set[str]:
        r = _git(root, "diff", *args, "--name-only", "-z")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or "git diff failed")
        return {n.decode("utf-8", "surrogateescape") for n in r.stdout.split(b"\0") if n}

    return sorted(names("--cached") & names())


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
# Shell words that start or end a compound command rather than run one.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}",
             "fi", "done", "esac"}
# Commands that run the rest of their arguments as a command.
_WRAPPERS = {"sudo", "env", "command", "nice", "nohup", "time", "exec", "builtin",
             "timeout", "stdbuf", "ionice"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# What may share a Bash call with a commit: nothing here changes files, so
# the gate's check of the repository still holds when the commit runs.
_SAFE_WITH_COMMIT = {"cd", "pushd", "popd", "echo", "printf", "true", "false", ":",
                     "tail", "head", "wc", "test", "[", "sleep"}
_GIT_SAFE_WITH_COMMIT = {"add", "commit", "status", "log", "diff", "show", "rev-parse",
                         "push", "branch", "describe", "shortlog", "ls-files", "tag",
                         "fetch", "remote"}
_GIT_OPTS_WITH_VALUE = {
    "-c", "--namespace", "--super-prefix", "--config-env", "--exec-path", "--list-cmds",
}
_PUNCT = ";&|()<>"
_SEPARATOR_CHARS = set(";&|()")
_HARMLESS_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


@dataclasses.dataclass
class CommandCheck:
    """What a Bash command does that the commit gate cares about."""

    # Where it runs `git commit`; None when it commits somewhere the gate
    # can't work out (it then refuses and asks for `git -C <repo> commit`).
    commits: Optional[list[str]]
    # Anything else in the same call that could change files: run before the
    # commit, it would slip past a check made before the command started.
    other: list[str]


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
    it won't tokenize (unbalanced quotes). Redirection operators stay in the
    argv as their own tokens."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=_PUNCT)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t and set(t) <= _SEPARATOR_CHARS:
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


def _redirections(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split redirections out of ``argv``: the words left, and the files the
    command writes to (``/dev/null`` and fd copies like ``2>&1`` don't count)."""
    words: list[str] = []
    writes: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok and set(tok) <= set("<>&|") and ("<" in tok or ">" in tok):
            target = argv[i + 1] if i + 1 < len(argv) else ""
            if ">" in tok and not tok.endswith("&") and target not in _HARMLESS_TARGETS:
                writes.append(target)
            if words and words[-1].isdigit():
                words.pop()  # the fd number in front of 2>
            i += 2
            continue
        words.append(tok)
        i += 1
    return words, writes


def _unwrap(argv: list[str]) -> tuple[list[str], dict[str, str], bool]:
    """Skip shell keywords, VAR=value assignments and wrappers like sudo.

    Returns the command's own argv, the assignments, and whether a wrapper
    hid a command this module doesn't follow. Wrappers take options with
    values (``sudo -u matt``, ``timeout 60``), so rather than parse each one,
    jump to the first word that starts a command we follow.
    """
    env: dict[str, str] = {}
    i = 0
    while i < len(argv) and (argv[i] in _KEYWORDS or _ENV_ASSIGN.match(argv[i])):
        if _ENV_ASSIGN.match(argv[i]):
            key, _, value = argv[i].partition("=")
            env[key] = value
        i += 1
    argv = argv[i:]
    if argv and argv[0] in _WRAPPERS:
        for j, word in enumerate(argv):
            if _ENV_ASSIGN.match(word):
                key, _, value = word.partition("=")
                env[key] = value
            elif (os.path.basename(word) in ("git", *_SHELLS)
                  or word in ("cd", "pushd", "eval")):
                return argv[j:], env, False
        return [], env, True
    return argv, env, False


def _shell_script(argv: list[str]) -> Optional[str]:
    """The script a shell runs with -c (in any flag cluster: -c, -ec, -xc)."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            return argv[i + 1] if i + 1 < len(argv) else None
        if not tok.startswith(("-", "+")):
            return None
        i += 1
    return None


def _git_dir_root(base: Optional[str], git_dir: str) -> Optional[str]:
    p = _resolve(base, git_dir)
    if p is None:
        return None
    return os.path.dirname(p) if os.path.basename(p) == ".git" else p


def _git_command(argv: list[str], where: Optional[str], env: dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    """git's subcommand and the directory it runs in."""
    if "GIT_WORK_TREE" in env:
        where = _resolve(where, env["GIT_WORK_TREE"])
    elif "GIT_DIR" in env:
        where = _git_dir_root(where, env["GIT_DIR"])
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--work-tree") and i + 1 < len(argv):
            where = _resolve(where, argv[i + 1])
            i += 2
        elif a.startswith("--work-tree="):
            where = _resolve(where, a.split("=", 1)[1])
            i += 1
        elif a == "--git-dir" and i + 1 < len(argv):
            where = _git_dir_root(where, argv[i + 1])
            i += 2
        elif a.startswith("--git-dir="):
            where = _git_dir_root(where, a.split("=", 1)[1])
            i += 1
        elif a in _GIT_OPTS_WITH_VALUE and i + 1 < len(argv):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, where
    return None, where


def check_command(command: str, cwd: str) -> CommandCheck:
    """Where ``command`` runs ``git commit``, and what else it does.

    ``cwd`` is the shell's directory when the command starts, which the hook
    input carries (it follows earlier ``cd`` commands; verified on 2.1.296).
    This follows the shell forms an agent writes (``&&`` chains, ``cd``,
    ``git -C``, ``if``/``for`` bodies, ``bash -c``, ``eval``, wrappers like
    ``sudo`` and ``env``). It's there to hold a cooperating agent to the
    review, not to stop one trying to get round it: a script that commits,
    a git alias or ``xargs git commit`` aren't followed.
    """
    text = _strip_heredocs(command.replace("\\\n", " "))
    segs = _segments(text)
    if segs is None:
        return CommandCheck(None if _LOOSE_COMMIT.search(text) else [], [])
    here: Optional[str] = cwd or None
    found: list[str] = []
    other: list[str] = []
    unplaced = False
    for raw in segs:
        argv, env, hidden = _unwrap(raw)
        if hidden:
            other.append(raw[0])
            continue
        if not argv:
            continue
        argv, writes = _redirections(argv)
        other.extend(f"output to {w}" for w in writes)
        if not argv:
            continue
        head = os.path.basename(argv[0])
        if argv[0] in ("cd", "pushd"):
            target = argv[1] if len(argv) > 1 else "~"
            here = None if target == "-" else _resolve(here, target)
            continue
        if argv[0] == "popd":
            here = None
            continue
        if head in _SHELLS or argv[0] == "eval":
            script = " ".join(argv[1:]) if argv[0] == "eval" else _shell_script(argv)
            if script is None:
                other.append(argv[0])
                continue
            inner = check_command(script, here or "")
            if inner.commits is None:
                unplaced = True
            else:
                found.extend(inner.commits)
            other.extend(inner.other)
            continue
        if head == "git":
            sub, where = _git_command(argv, here, env)
            if sub == "commit":
                if where is None:
                    unplaced = True
                else:
                    found.append(where)
            elif sub not in _GIT_SAFE_WITH_COMMIT:
                other.append(f"git {sub}" if sub else "git")
            continue
        if argv[0] not in _SAFE_WITH_COMMIT:
            other.append(argv[0])
    return CommandCheck(None if unplaced else found, other)


def commit_targets(command: str, cwd: str) -> Optional[list[str]]:
    """Where ``command`` runs ``git commit`` (see check_command)."""
    return check_command(command, cwd).commits


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
    # repo root -> {path: digest}: what the panel was last shown, approved or
    # not. "Go ahead anyway" approves this, never what's on disk by then.
    last_reviewed: dict = dataclasses.field(default_factory=dict)
    # Set when a round's answer found no live chat to deliver to; the next
    # message the user sends carries it in front.
    pending_message: Optional[str] = None
    # With panel review off: the stages the user said yes to on Claude's
    # card, still running. A yes ends when its stage resolves, when the user
    # sends a message of their own, or when the switch flips.
    granted: list = dataclasses.field(default_factory=list)
    # repo -> stage -> participant -> findings, including explicit resolutions.
    review_ledger: dict = dataclasses.field(default_factory=dict)
    # User requirements remain citable when later rounds have no new user turn.
    review_requirements: dict = dataclasses.field(default_factory=dict)

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
        state.last_reviewed = dict(state.last_reviewed or {})
        if not isinstance(state.review_ledger, dict):
            state.review_ledger = {}
        if not isinstance(state.review_requirements, dict):
            state.review_requirements = {}
        granted = state.granted if isinstance(state.granted, list) else []
        state.granted = [s for s in STAGES if s in granted]
        return state

    def end_cycle(self) -> None:
        """A task's changes are committed: the next change starts with a plan."""
        self.plan_ok = False
        self.rounds = {s: 0 for s in STAGES}
        self.review_ledger = {}
        self.review_requirements = {}


# ─── What the panel and Claude see ───────────────────────────────────────────

TOOL_DESCRIPTION = (
    "The user's review panel: GPT, Gemini and a second Claude, run through the "
    "roundtable. They check your work at two points: stage \"plan\", before you "
    "change files in a git repository by any means, where you send your plan; and "
    "stage \"changes\", when the changes are made and before you commit, where you "
    "send a summary and the panel reviews the repository's actual diff. When panel "
    "review is on in this chat (the user's messages say so), both are required and "
    "you can't skip them. When it's off, you can still ask for a review of big or "
    "risky work: give a one-sentence reason, the user is asked first, and if they "
    "say no, carry on without it. A call that starts a round returns at once: end "
    "your turn right after it. The panel's answer arrives as your next message, "
    "usually within a few minutes, and the user can talk to you while you wait. "
    "When they want changes, fix what you agree with, push back with reasons on "
    "what you don't (they read your reasons), and send it again. After a few rounds "
    "without agreement the user decides."
)

# Appended to every Claude chat's system prompt where the panel exists. The
# tool is deferred, so its description is unseen until ToolSearch loads it,
# and with panel review off nothing else mentions it. Keep it static: it sits
# in the prompt prefix every chat shares in the cache.
SYSTEM_HINT = (
    "claude-web has a review panel (GPT, Gemini and a second Claude) that can check "
    "your plan before you change files in a git repository, or your changes before "
    f"you commit, through the {TOOL_NAME} tool (load it with ToolSearch). When panel "
    "review is on, the user's messages say so and you must use it. When it's off, "
    "use it only when a second opinion is clearly worth a few minutes: a big or "
    "risky change, such as a rewrite, anything touching security, privacy or data "
    "that can't be recovered, or a large diff. Not for small fixes, tests, docs or "
    "notes. The tool asks the user before anything is sent, so don't ask them in "
    "the chat first."
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
                "The user's requirements, proposed steps or actual changes, and "
                "assumptions to check. Separate observed facts from hypotheses. "
                "On later rounds address each finding id with the relevant code "
                "change, test result, or factual correction. Do not ask reviewers "
                "to endorse your conclusion."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "When panel review is off: one sentence for the user on why this "
                "needs a second look. It's read out on the card that asks them."
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
        f"You're an independent code reviewer for {user_label}. "
        f"{AUTHOR} proposes and implements work; review its claims as hypotheses. "
        "You can read the repository but must never edit it.\n"
        + review_contract.CONTRACT
    )


def round_prompt(
    stage: str, round_no: int, max_rounds: int, repo: str,
    plan_reviewed: bool, diff_note: str = "",
) -> str:
    budget = (
        "Budget about 10 tool calls: read what the work touches, not the whole tree."
    )
    ending = (
        "Return the evidence-backed JSON report specified in the review contract. "
        "An approval needs inspected source evidence; keep unanswered objections open."
    )
    if stage == "plan":
        return (
            f"Round {round_no} of {max_rounds} on {AUTHOR}'s PLAN for {repo}. Nothing "
            "has been changed yet. Check the plan against the repository: wrong "
            "assumptions, missed cases, risky steps, a simpler way that does the same "
            f"job. {diff_note + ' ' if diff_note else ''}{budget} {ending}"
        )
    against = (
        "Check the requirements and implementation afresh. Earlier plan approval "
        "does not establish that the approach or code is correct."
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


def submitted_text(
    stage: str, round_no: int, max_rounds: int, labels: list[str],
    advisory: bool = False, user_label: str = "the user",
) -> str:
    """What the tool tells Claude when a round starts. ``advisory``: panel
    review is off, so the review is advice and holds nothing back."""
    who = _join(labels) or "the panel"
    hold = "change files" if stage == "plan" else "commit"
    wait = (
        "Panel review is off in this chat, so this review is advice: if "
        f"{user_label} tells you to go ahead before they answer, go ahead."
        if advisory else
        f"Don't {hold} until they approve."
    )
    return (
        f"Sent to the panel (round {round_no} of {max_rounds} on your {stage}): {who} "
        f"{'is' if len(labels) == 1 else 'are'} reviewing it. Their answer will arrive "
        "as your next message, usually within a few minutes. End your turn now: tell "
        f"the user in a sentence that it's with the panel. {wait}"
    )


# ─── Asking first, with panel review off ─────────────────────────────────────

ASK_SEND = "Yes, send it"
ASK_KEEP_ON = "Yes, and keep panel review on"
ASK_SKIP = "No, skip it"
ASK_ANSWERS = {ASK_SEND: "send", ASK_KEEP_ON: "keep_on", ASK_SKIP: "skip"}
# Long enough for a sentence; the card is read out in full.
ASK_REASON_CHARS = 300


def ask_card(stage: str, repo_name: str, reason: str, again: bool) -> list[dict]:
    """The question card for Claude's request, in Claude's words.

    ``again``: this stage already went to the panel and the user has said
    something since, so this asks to send the revised version back.
    Keeping panel review on is offered for a plan only: turned on during a
    changes review, the edit gate (no plan approved) would stop Claude fixing
    the very points the panel raised.
    """
    what = "plan for" if stage == "plan" else "changes in"
    if again:
        question = f"Can I send my revised {what} {repo_name} back to the review panel?"
    elif stage == "plan":
        question = f"Can I send my plan for {repo_name} to the review panel?"
    else:
        question = f"Can I send my changes in {repo_name} to the review panel before I commit?"
    reason = " ".join((reason or "").split())[:ASK_REASON_CHARS]
    if reason:
        question += " " + reason
    options = [{
        "label": ASK_SEND,
        "description": (
            "GPT, Gemini and a second Claude review it, usually within a few minutes. "
            "Panel review stays off."
        ),
    }]
    if stage == "plan":
        options.append({
            "label": ASK_KEEP_ON,
            "description": (
                "The same, and panel review stays on for this chat: every plan and "
                "every set of changes goes to them before files change or anything "
                "is committed."
            ),
        })
    options.append({
        "label": ASK_SKIP,
        "description": "Nothing is sent. Claude carries on without the panel.",
    })
    return [{"question": question, "header": "Panel", "multiSelect": False,
             "options": options}]


def ask_declined(how: str, user_label: str, other: str = "", minutes: int = 0) -> str:
    """What the tool tells Claude when nothing went to the panel.

    ``how``: skip (they said no), other (they typed their own answer),
    skipped (they skipped the card), timeout, or interrupted.
    """
    carry_on = "Carry on without the panel, and don't ask again for this same work."
    if how == "other":
        return (
            f"{user_label} didn't choose to send it, so nothing was sent. They wrote: "
            f"\"{other}\". Carry on without the panel; if that needs an answer, reply "
            "to them in the chat."
        )
    if how == "timeout":
        return (
            f"{user_label} didn't answer within {minutes} minute{'s' if minutes != 1 else ''}, "
            "so nothing was sent. Carry on without the panel, and mention in your "
            "reply that you'd wanted a review and why."
        )
    if how == "interrupted":
        return f"The turn was stopped before {user_label} answered, so nothing was sent."
    if how == "skipped":
        return f"{user_label} skipped the question, so nothing was sent. {carry_on}"
    return f"{user_label} said no to a panel review this time, so nothing was sent. {carry_on}"


def ask_busy(user_label: str) -> str:
    return (
        f"You already asked {user_label} about sending something to the panel. "
        "Wait for that answer."
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


def commit_not_alone(other: list[str]) -> str:
    shown = ", ".join(dict.fromkeys(other))
    return (
        "Panel review is on in this chat, so a commit has to run on its own: this "
        f"command also runs {shown}, which could change files after the panel's check "
        "and before the commit. Do that first, then commit in a separate command "
        "(git add, git commit, and git status or git log if you like)."
    )


def commit_staged_differs(root: str, paths: list[str]) -> str:
    shown = ", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "")
    return (
        f"Panel review is on in this chat, and in {root} what's staged differs from "
        f"what's on disk for: {shown}. The panel reviews the files as they are on disk, "
        "and git commit commits what's staged, so stage them again (git add) or "
        "unstage them before committing."
    )


def commit_check_failed(root: str, error: Exception) -> str:
    return (
        f"Panel review is on in this chat, and claude-web couldn't read {root} to "
        f"check the commit against the panel's approval ({type(error).__name__}: "
        f"{error}). Nothing was committed. Try again in a moment; if git is busy "
        "(an index.lock), wait for it to finish."
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
    "approve": "approves", "changes": "wants changes", "unclear": "review not verified",
}


def reply_heading(label: str, verdict: str) -> str:
    return f"{label}: {_VERDICT_WORDS.get(verdict, verdict)}"


def result_message(
    stage: str, round_no: int, max_rounds: int, outcome: str,
    replies: list[dict], errors: list[dict], user_label: str,
    advisory: bool = False,
) -> str:
    """The panel's answer, delivered to Claude as its next message.

    ``outcome`` is approved, changes, unavailable, go_ahead (the user overrode
    a deadlock), more_rounds, stop, undecided (the question card timed out),
    or unresolved (objections after the last round with panel review off).
    ``advisory``: panel review is off, so nothing waits on the panel and the
    user's word in the chat is what counts.
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
        "unresolved": "they still had objections after the last round, and with panel "
                      "review off in this chat, the review ends here",
    }[outcome]
    if advisory and outcome == "unavailable":
        phrase = ("nobody on the panel could answer, so there's no review this time; "
                  f"{user_label} has been told")
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
        "unresolved": (
            f"Next: tell {user_label} in a sentence or two what the panel still objects "
            "to and what you'd do about it, then wait for them to decide."
        ),
    }[outcome]
    if advisory:
        advice = (
            "Panel review is off in this chat, so this is advice: if "
            f"{user_label} tells you to go ahead, go ahead."
        )
        approved_next = (
            "Next: make the changes. Panel review is off in this chat, so a review of "
            "the changes is your call: ask for one (stage \"changes\") if they turn out "
            f"big or risky; {user_label} is asked first."
            if stage == "plan" else
            "Next: commit these changes."
        )
        nxt = {
            "approved": approved_next,
            "go_ahead": approved_next,
            "unavailable": "Next: carry on without the panel.",
            "changes": (
                "Next: fix what you agree with and push back on what you don't (they'll "
                f"read your reasons), then send it again (stage \"{stage}\") and end your "
                f"turn. {advice}"
            ),
            "more_rounds": (
                "Next: answer their points, then send it again (stage "
                f"\"{stage}\") and end your turn. {advice}"
            ),
            "undecided": (
                f"Next: stop here and wait for {user_label} to tell you what they want."
            ),
        }.get(outcome, nxt)
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
    review_ledger: Optional[dict] = None


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
    effort: str = EFFORT, review_ledger: Optional[dict] = None,
    requirements: Optional[str] = None,
) -> RoundResult:
    """Run one review round through the roundtable library ``core``.

    Creates the chat's thread on first use, binds the repository read-only,
    attaches the working diff for a changes round, posts the user's recent
    messages and Claude's message, then asks every participant in parallel.
    Each blocking library call runs in a worker thread. ``emit`` gets the
    events the chat shows; it's called on the event loop.
    Malformed or incomplete reports get one private correction attempt in the
    same round. A failed correction retains the unverified report and findings.
    """
    labels = {p: core.PARTICIPANTS[p]["label"] for p in participants}
    tid = thread_id
    if tid is not None:
        # Closed from the Roundtable page, or gone: every post to it would
        # fail and the panel would step aside on every round from then on.
        row = await asyncio.to_thread(core._thread_row, tid)
        if row is None or row.get("closed_at"):
            tid = None
    if tid is None:
        created = await asyncio.to_thread(
            core.roundtable_create, topic=topic, participants=list(participants),
            house_rules=house_rules(user_label),
        )
        tid = int(created["thread_id"])
        on_thread(tid)
    ledger = json.loads(json.dumps(review_ledger or {}))
    # Adopt still-open objections from pre-contract threads on an in-flight
    # stage. Once a stage has a ledger, it is authoritative, including {}.
    if review_ledger is None and round_no > 1 and hasattr(core, "_thread_messages"):
        latest = {}
        for turn in await asyncio.to_thread(core._thread_messages, tid):
            if turn["speaker"] in labels.values():
                latest[turn["speaker"]] = turn["content"]
        for participant, label in labels.items():
            old = latest.get(label, "")
            if parse_verdict(old) == "changes":
                ledger[participant] = [{"id": "F1", "status": "open", "claim": old,
                                        "evidence": []}]
    reviewed_state = None
    diff_note = ""
    repo_bound = False
    try:
        await asyncio.to_thread(core.roundtable_bind_repo, tid, repo, "readonly")
        repo_bound = True
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
    context = core._effective_tool_context(tid) if repo_bound else None
    if requirements is None:
        requirements = "\n\n".join(notes)
    loop = asyncio.get_running_loop()
    shown: set[str] = set()
    assessments: dict[str, dict] = {}
    raw_replies: dict[str, str] = {}
    provider_errors: dict[str, str] = {}

    def show(p: str, verdict: str, text: str, *, from_thread: bool = False) -> None:
        if p not in shown:
            shown.add(p)
            event = {
                "type": "panel_reply", "stage": stage, "round": round_no,
                "participant": labels[p], "verdict": verdict, "text": text,
            }
            if from_thread:
                loop.call_soon_threadsafe(emit, event)
            else:
                emit(event)

    async def ask(people: list[str], prompts: dict[str, str], prompt: str = "", *,
                  correcting: bool = False) -> None:
        assessed: set[str] = set()

        def landed(p: str, text: Optional[str], error: Optional[str]) -> None:
            # Called on the library's worker thread, then again as a fallback
            # for cores without callbacks. Assess and display each reply once.
            if p not in people or p in assessed:
                return
            assessed.add(p)
            if error is not None:
                provider_errors[p] = error
                if p not in assessments:
                    show(p, "error", error, from_thread=True)
                return
            raw_replies[p] = text or ""
            read_line = evidence_reader(core, context, labels[p], message, requirements)
            assessment = review_contract.assess(
                raw_replies[p], ledger.get(p, []), stage=stage, read_line=read_line,
            )
            assessments[p] = assessment
            ledger[p] = assessment["ledger"]
            # A malformed report gets one private correction attempt before
            # being shown. Valid peers still stream immediately.
            if correcting or not assessment["correction_error"]:
                show(p, assessment["verdict"], assessment["text"], from_thread=True)

        result = await asyncio.to_thread(
            core.roundtable_ask_parallel, tid, people, prompt=prompt,
            effort=effort, tool_use_context=context, on_result=landed,
            independent=True, house_rules=house_rules(user_label), participant_prompts=prompts,
        )
        for p in people:
            if p in (result.get("responses") or {}):
                await asyncio.to_thread(landed, p, str(result["responses"][p] or ""), None)
            elif p in (result.get("errors") or {}):
                await asyncio.to_thread(landed, p, None, str(result["errors"][p]))

    await ask(list(participants), {p: review_contract.reviewer_prompt(
            message, requirements, ledger.get(p, []),
        ) for p in participants},
        round_prompt(stage, round_no, max_rounds, repo, plan_reviewed, diff_note),
    )
    retry = [p for p in participants if p in assessments and assessments[p]["correction_error"]]
    if retry:
        prompts = {p: review_contract.correction_prompt(
            message, requirements, ledger.get(p, []), stage=stage,
            error=assessments[p]["correction_error"], reply=raw_replies[p],
        ) for p in retry}
        try:
            await ask(retry, prompts, correcting=True)
        except Exception as exc:  # A failed correction must keep the rejected report and its objections.
            for p in retry:
                if p not in shown:
                    provider_errors[p] = f"Report correction failed: {type(exc).__name__}: {exc}"

    replies: list[dict] = []
    errors: list[dict] = []
    for p in participants:
        if p in assessments:
            assessment = assessments[p]
            replies.append({"participant": p, "label": labels[p],
                            "verdict": assessment["verdict"], "text": assessment["text"]})
            show(p, assessment["verdict"], assessment["text"])
        if p in provider_errors:
            err = provider_errors[p]
            errors.append({"participant": p, "label": labels[p], "error": err})
            show(p, "error", err)
    # An unavailable reviewer cannot silently withdraw its earlier objection.
    open_findings = any(f["status"] == "open" for rows in ledger.values() for f in rows)
    for p, rows in ledger.items():
        if p not in assessments and any(f["status"] == "open" for f in rows):
            replies.append({"participant": p, "label": labels.get(p, p), "verdict": "changes",
                            "text": "Earlier objections remain unresolved:\n" + "\n".join(
                                f"{f['id']}: {f['claim']}" for f in rows if f["status"] == "open")})
    return RoundResult(
        thread_id=tid, outcome="changes" if open_findings else outcome_of(replies), replies=replies,
        errors=errors, reviewed_state=reviewed_state, review_ledger=ledger,
    )


def evidence_reader(core: Any, context: Any, label: str, proposal: str, requirements: str):
    """Read permission-gated lines, caching windows for bounded quote relocation."""
    texts = {"@proposal": proposal.splitlines(), "@requirements": requirements.splitlines()}
    cache = {}
    tools = None
    if context is not None and context.working_directory is not None:
        tools = core._RepoTools(Path(context.working_directory), context.permission_callback, label)

    def read_line(source: str, line: int) -> Optional[str]:
        key = (source, line)
        if key not in cache:
            value = None
            if source in texts:
                lines = texts[source]
                value = lines[line - 1] if 0 < line <= len(lines) else None
            elif tools is not None and not source.startswith("@") and not Path(source).is_absolute():
                radius = review_contract.CITATION_SEARCH_RADIUS
                lo, hi = max(1, line - radius), line + radius
                # One authorized read serves the whole search, including missing
                # lines and denied reads; a typo must not trigger 81 tool calls.
                excerpt = tools.execute("Read", {"path": source, "line": line, "context": radius})
                cache.update({(source, n): None for n in range(lo, hi + 1)})
                if excerpt.startswith("["):
                    # Error messages can echo a model-supplied filename. They
                    # are never source, even if that filename embeds newlines.
                    return None
                for row in excerpt.split("\n"):
                    number, sep, content = row.partition(": ")
                    if sep and number.isascii() and number.isdecimal() and lo <= int(number) <= hi:
                        cache[(source, int(number))] = content
                value = cache[key]
            cache[key] = value
        return cache[key]
    return read_line
