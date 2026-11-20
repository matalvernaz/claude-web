"""Capture one immutable, path-filtered Git comparison without publishing or approving it."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Callable


class CaptureError(ValueError):
    """Grounding failures must not be mistaken for unavailable reviewers."""


def git(root: str, *arguments: str) -> bytes:
    result = subprocess.run(['git', '-C', root, *arguments], capture_output=True, timeout=20)
    if result.returncode:
        raise CaptureError(result.stderr.decode('utf-8', 'replace').strip() or 'Git comparison failed')
    return result.stdout


def resolve(root: str, base: str) -> str:
    if not isinstance(base, str) or not base.strip() or base.startswith('-') or '..' in base:
        raise CaptureError('Base must be one branch or commit, not options or a revision range.')
    return git(root, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()


def changes(root: str, base: str) -> list[tuple[str, ...]]:
    fields = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-status', '-z',
                 '-M', '-C', '--find-copies-harder', '-l1000', base, '--').split(b'\0')
    result = []
    offset = 0
    while offset < len(fields) and fields[offset]:
        status = fields[offset]
        count = 2 if status[:1] in (b'R', b'C') else 1
        paths = fields[offset + 1:offset + 1 + count]
        if len(paths) != count or not all(paths):
            raise CaptureError('Git returned incomplete changed paths')
        result.append(tuple(os.fsdecode(path) for path in paths))
        offset += count + 1
    return result


def digest(root: str, relative: str) -> str:
    path = Path(root) / relative
    if path.is_symlink():
        return 'link:' + os.readlink(path)
    if not path.exists():
        return 'deleted'
    if path.is_dir():
        return 'dir:' + git(str(path), 'rev-parse', 'HEAD').decode().strip()
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest() + (':x' if os.access(path, os.X_OK) else '')


def state(root: str, base: str) -> dict:
    head = resolve(root, 'HEAD')
    comparisons = changes(root, base) + changes(root, head)
    untracked = [os.fsdecode(path) for path in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if path]
    paths = sorted({path for pair in comparisons for path in pair} | set(untracked))
    return {'head': head, 'pairs': comparisons, 'untracked': untracked,
            'status': git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').hex(),
            'digests': {path: digest(root, path) for path in paths}}


def validate(capture: dict) -> None:
    if state(capture['root'], capture['base_oid']) != capture['state']:
        raise CaptureError('Repository changed while preparing the review. Submit it again.')


def capture(root: str, base: str, excluded: Callable[[Path], bool], *, limit: int = 200000,
            untracked_limit: int = 50) -> dict:
    root = str(Path(root).resolve())
    try:
        base_oid = resolve(root, base)
        before = state(root, base_oid)
    except (OSError, subprocess.TimeoutExpired, CaptureError) as exc:
        raise CaptureError(f'Cannot capture git work tree against {base!r}: {exc}') from exc
    pairs = changes(root, base_oid)
    blocked = {path for pair in before['pairs'] for path in pair if any(excluded(Path(part)) for part in pair)}
    tracked = sorted({path for pair in pairs for path in pair} - blocked)
    untracked = [path for path in before['untracked'] if path not in blocked and not excluded(Path(path))]
    shown = untracked[:untracked_limit]
    raw = b''
    if tracked:
        raw = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', base_oid, '--',
                  *[':(literal)' + path for path in tracked])
    parts = []
    if raw.strip():
        parts.append(raw.decode('utf-8', 'replace'))
    for path in shown:
        absolute = Path(root) / path
        # Never dereference an untracked symlink and copy a file from outside the repository.
        if absolute.is_symlink():
            parts.append(f'Untracked symlink {path!r} -> {os.readlink(absolute)!r}')
            continue
        result = subprocess.run(['git', '-C', root, 'diff', '--no-ext-diff', '--no-textconv',
                                 '--no-index', '--', '/dev/null', path], capture_output=True, timeout=20)
        if result.returncode not in (0, 1):
            raise CaptureError('Cannot capture untracked file ' + repr(path))
        parts.append(result.stdout.decode('utf-8', 'replace'))
    if not any(part.strip() for part in parts):
        raise CaptureError(f'Working tree is clean against {base!r}: no eligible changes. Choose an earlier base to review committed work.')
    header = f'# Working diff vs {base!r} ({base_oid}); HEAD = {before["head"]}\n'
    if blocked:
        header += '# Excluded secret/VCS paths: ' + ', '.join(repr(path) for path in sorted(blocked)) + '\n'
    if len(untracked) > len(shown):
        header += f'# {len(untracked) - len(shown)} untracked files omitted by the file limit.\n'
    text = header + '\n'.join(parts)
    truncated = len(text) > limit
    if truncated:
        notice = '\n[Diff abbreviated. Read the source files for the remaining changes.]\n'
        room = max(0, limit - len(header) - len(notice))
        text = header + '\n'.join(parts)[:room] + notice
    result = {'root': root, 'base': base, 'base_oid': base_oid, 'commit_sha': before['head'],
              'commit_eligible': base_oid == before['head'], 'content': text, 'state': before,
              'files_changed': len(tracked), 'files_excluded': len(blocked), 'excluded_paths': sorted(blocked),
              'dynamic_excluded_paths': sorted(path for path in blocked if not excluded(Path(path))),
              'untracked_included': len(shown), 'untracked_skipped': len(untracked) - len(shown), 'truncated': truncated}
    result['capture_id'] = hashlib.sha256((base_oid + before['head'] + text + json.dumps(before, sort_keys=True)).encode()).hexdigest()
    validate(result)
    return result


def receipt(capture: dict) -> dict:
    return {key: capture[key] for key in ('base', 'base_oid', 'commit_sha', 'commit_eligible', 'capture_id', 'truncated')}
