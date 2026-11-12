"""The chat's task list, rebuilt from Claude's task tool calls and what the CLI answered.

Claude Code keeps the task list to itself. All that reaches claude-web is the
task tools Claude calls (TaskCreate, TaskUpdate, TaskList, TaskGet) and the
result of each one. This module turns that traffic back into the list, the same
way for a chat that is running, one that has just been started again, and one
that is only being read back from its saved conversation.

It goes by results, not calls: a task exists once the CLI says it made it, and a
change counts once the CLI says it took. The CLI's own task files are left alone
on purpose. Where they live depends on settings this app cannot see, and the CLI
already has a second way of storing them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


TASK_TOOL_NAMES = frozenset({"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"})
# The older tool, which sends the whole list in one call and needs no result.
WHOLE_LIST_TOOL_NAME = "TodoWrite"

STATUS_PENDING = "pending"
STATUS_DELETED = "deleted"

# The fields of a task that an update may change, by the name the tool uses.
UPDATABLE_FIELDS = ("subject", "description", "activeForm")

# The CLI's wording, used only when a result arrives without its structured
# form. Every result seen so far (2,114 on the homelab, 2026-11) carried one.
_CREATED_TEXT = re.compile(r"^\s*Task #(\d+)\s+created", re.IGNORECASE)
_UPDATED_TEXT = re.compile(r"^\s*Updated task #(\d+)", re.IGNORECASE)
_NOT_FOUND_TEXT = re.compile(r"\bnot found\b", re.IGNORECASE)


def _text_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _numeric_order(task_id: str) -> tuple[int, int]:
    """Sort key that puts task 9 before task 10, and anything unnumbered last."""
    if task_id.isdigit():
        return (0, int(task_id))
    return (1, 0)


class TaskLedger:
    """One chat's task list, fed one tool call and one tool result at a time."""

    def __init__(self) -> None:
        # Task number -> the task. The number is the CLI's own id, as a string.
        self.tasks: dict[str, dict[str, str]] = {}
        # Calls still waiting for their result: tool call id -> (tool, input).
        self.pending_calls: dict[str, tuple[str, dict]] = {}
        # Calls already dealt with, so a message that arrives twice (a resumed
        # chat can repeat old ones) does not count twice.
        self.settled_call_ids: set[str] = set()

    def note_call(self, call_id: str, tool_name: str, tool_input: Any) -> None:
        """Remember a task tool call until its result says what happened."""
        if tool_name not in TASK_TOOL_NAMES:
            return
        if not call_id:
            return
        if call_id in self.settled_call_ids:
            return
        if call_id in self.pending_calls:
            return
        cleaned_input = dict(tool_input) if isinstance(tool_input, dict) else {}
        self.pending_calls[call_id] = (tool_name, cleaned_input)

    def note_result(
        self,
        call_id: str,
        text: str,
        is_error: bool,
        structured: Any = None,
    ) -> bool:
        """Apply the result of a remembered call. True when the list changed.

        ``structured`` is the tool's own result object when the CLI sent one
        (the SDK's ``tool_use_result``, the transcript's ``toolUseResult``).
        ``text`` is what the model was shown, and is the fallback.
        """
        call = self.pending_calls.pop(call_id, None)
        if call is None:
            return False
        self.settled_call_ids.add(call_id)
        if is_error:
            return False
        tool_name, tool_input = call
        result = structured if isinstance(structured, dict) else {}
        if tool_name == "TaskCreate":
            return self._created(tool_input, text or "", result)
        if tool_name == "TaskUpdate":
            return self._updated(tool_input, text or "", result)
        if tool_name == "TaskList":
            return self._listed(result)
        return self._fetched(tool_input, result)

    def _created(self, tool_input: dict, text: str, result: dict) -> bool:
        saved = result.get("task")
        if not isinstance(saved, dict):
            saved = {}
        # The number only ever comes from the CLI, never from the call.
        task_id = str(saved.get("id") or "")
        if not task_id:
            match = _CREATED_TEXT.match(text)
            if match is None:
                return False
            task_id = match.group(1)
        # Numbers only go up within one list, and the CLI makes tasks one at a
        # time. Being handed a number that is not above every number we hold
        # means the CLI is on a fresh list. That is what a chat got when it was
        # branched off another before claude-web carried lists across: its
        # saved conversation shows the old chat's tasks, and Claude Code gave
        # it an empty list. The old tasks are gone.
        if self._is_not_above_known_numbers(task_id):
            self.tasks.clear()
        # What the CLI saved can differ from what was asked for: it repairs
        # malformed calls and a hook may rewrite them.
        subject = _text_or_empty(saved.get("subject")) or _text_or_empty(tool_input.get("subject"))
        description = _text_or_empty(saved.get("description")) or _text_or_empty(tool_input.get("description"))
        self.tasks[task_id] = {
            "subject": subject,
            "description": description,
            "activeForm": _text_or_empty(tool_input.get("activeForm")),
            "status": STATUS_PENDING,
        }
        return True

    def _is_not_above_known_numbers(self, task_id: str) -> bool:
        if task_id in self.tasks:
            return True
        if not task_id.isdigit():
            return False
        known_numbers = [int(known_id) for known_id in self.tasks if known_id.isdigit()]
        if not known_numbers:
            return False
        return int(task_id) <= max(known_numbers)

    def _updated(self, tool_input: dict, text: str, result: dict) -> bool:
        # Work out whether the change took, and for which task.
        if "success" in result:
            succeeded = result.get("success") is True
            task_id = str(result.get("taskId") or tool_input.get("taskId") or "")
            refusal = _text_or_empty(result.get("error")) or text
        else:
            match = _UPDATED_TEXT.match(text)
            succeeded = match is not None
            task_id = match.group(1) if match else str(tool_input.get("taskId") or "")
            refusal = text
        if not task_id:
            return False
        if not succeeded:
            # "Not found" means the CLI has no such task, so neither do we.
            # Any other refusal (a hook blocking a completion, say) leaves the
            # task exactly as it was.
            if _NOT_FOUND_TEXT.search(refusal):
                return self.tasks.pop(task_id, None) is not None
            return False

        status_change = result.get("statusChange")
        new_status = _text_or_empty(tool_input.get("status"))
        if isinstance(status_change, dict) and _text_or_empty(status_change.get("to")):
            new_status = status_change["to"]
        if new_status == STATUS_DELETED:
            return self.tasks.pop(task_id, None) is not None

        changed = False
        task = self.tasks.get(task_id)
        if task is None:
            # A task we never saw being made. Its name stays empty unless this
            # update carries one; the page says so instead of showing a blank.
            task = {"subject": "", "description": "", "activeForm": "", "status": STATUS_PENDING}
            self.tasks[task_id] = task
            changed = True
        if new_status and task["status"] != new_status:
            task["status"] = new_status
            changed = True
        for field_name in UPDATABLE_FIELDS:
            new_value = tool_input.get(field_name)
            if not isinstance(new_value, str):
                continue
            if task[field_name] != new_value:
                task[field_name] = new_value
                changed = True
        return changed

    def _listed(self, result: dict) -> bool:
        """TaskList answers with the whole list, so the ledger becomes that list."""
        listed = result.get("tasks")
        if not isinstance(listed, list):
            return False
        rebuilt: dict[str, dict[str, str]] = {}
        for item in listed:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "")
            if not task_id:
                continue
            # The listing has no description or doing-phrase. Keep ours.
            known = self.tasks.get(task_id, {})
            rebuilt[task_id] = {
                "subject": _text_or_empty(item.get("subject")) or known.get("subject", ""),
                "description": known.get("description", ""),
                "activeForm": known.get("activeForm", ""),
                "status": _text_or_empty(item.get("status")) or known.get("status", STATUS_PENDING),
            }
        if rebuilt == self.tasks:
            return False
        self.tasks = rebuilt
        return True

    def _fetched(self, tool_input: dict, result: dict) -> bool:
        """TaskGet answers with one task as the CLI holds it, or with nothing."""
        if "task" not in result:
            return False
        saved = result["task"]
        if saved is None:
            missing_id = str(tool_input.get("taskId") or "")
            return self.tasks.pop(missing_id, None) is not None
        if not isinstance(saved, dict):
            return False
        task_id = str(saved.get("id") or tool_input.get("taskId") or "")
        if not task_id:
            return False
        known = self.tasks.get(task_id, {})
        refreshed = {
            "subject": _text_or_empty(saved.get("subject")) or known.get("subject", ""),
            "description": _text_or_empty(saved.get("description")) or known.get("description", ""),
            "activeForm": known.get("activeForm", ""),
            "status": _text_or_empty(saved.get("status")) or known.get("status", STATUS_PENDING),
        }
        if refreshed == known:
            return False
        self.tasks[task_id] = refreshed
        return True

    def as_todos(self) -> list[dict[str, str]]:
        """The list in the shape the browser's Tasks section reads.

        ``content`` is the task's name; that key is what TodoWrite called it
        and what pages loaded before this change still read.
        """
        ordered_ids = sorted(self.tasks, key=_numeric_order)
        rows = []
        for task_id in ordered_ids:
            task = self.tasks[task_id]
            rows.append({
                "id": task_id,
                "content": task["subject"],
                "activeForm": task["activeForm"],
                "status": task["status"],
                "description": task["description"],
            })
        return rows


@dataclass
class TranscriptTasks:
    """What a saved conversation says its task list is."""

    ledger: TaskLedger = field(default_factory=TaskLedger)
    # The list as the browser reads it. Not always the ledger's: a chat that
    # used TodoWrite last shows that list.
    todos: list[dict] = field(default_factory=list)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(_text_or_empty(part.get("text")))
        return "".join(parts)
    return ""


def _written_at(entry: dict) -> Optional[float]:
    """When a saved-conversation entry was written, in seconds since the epoch."""
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _may_matter(line: bytes, ledger: TaskLedger) -> bool:
    """Cheap test that lets most of a long conversation go by unparsed.

    Deliberately blind to how the JSON is spaced: it only looks for the tool
    names and for the ids of calls still waiting on a result.
    """
    if b"Task" in line or b"TodoWrite" in line:
        return True
    for call_id in ledger.pending_calls:
        if call_id.encode() in line:
            return True
    return False


def tasks_from_transcript(
    path: Optional[Path], history_from: Optional[float] = None,
) -> TranscriptTasks:
    """Rebuild a chat's task list from its saved conversation (the CLI's jsonl).

    Only Claude's own turns count. Helper agents keep their own lists, and the
    file's other entry kinds repeat the same calls without their results.

    ``history_from`` (seconds since the epoch) is for a chat that was branched
    off another and given a list of its own. Its file begins with a copy of
    the other chat's conversation, original times and all, and none of that is
    about this chat's list: entries written before ``history_from`` are passed
    over, and so is one whose time cannot be read.
    """
    found = TranscriptTasks()
    if path is None:
        return found
    ledger = found.ledger
    whole_list: Optional[list] = None
    whole_list_is_newest = False
    try:
        handle = path.open("rb")
    except OSError:
        return found
    with handle:
        for line in handle:
            if not _may_matter(line, ledger):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_kind = entry.get("type")
            if entry_kind not in ("assistant", "user"):
                continue
            if entry.get("isSidechain"):
                continue
            if history_from is not None:
                written_at = _written_at(entry)
                if written_at is None or written_at < history_from:
                    continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue

            if entry_kind == "assistant":
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name")
                    tool_input = block.get("input")
                    if tool_name == WHOLE_LIST_TOOL_NAME:
                        todos = tool_input.get("todos") if isinstance(tool_input, dict) else None
                        whole_list = todos if isinstance(todos, list) else []
                        whole_list_is_newest = True
                        continue
                    ledger.note_call(_text_or_empty(block.get("id")), tool_name, tool_input)
                continue

            results = [
                block for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            # The structured result belongs to the entry, not to a block, so it
            # is only trusted when the entry holds a single result.
            structured = entry.get("toolUseResult") if len(results) == 1 else None
            for block in results:
                changed = ledger.note_result(
                    _text_or_empty(block.get("tool_use_id")),
                    _result_text(block.get("content")),
                    bool(block.get("is_error")),
                    structured,
                )
                if changed:
                    whole_list_is_newest = False

    if whole_list_is_newest and whole_list is not None:
        found.todos = whole_list
    else:
        found.todos = ledger.as_todos()
    return found
