"""Keep historical secret paths out of review artifacts, including renamed files."""
import subprocess

import pytest

from roundtable import core


def git(repo, *arguments, stdin=None, raw=False):
    output = subprocess.run(['git', '-C', str(repo), *arguments], check=True,
                            capture_output=True, input=stdin).stdout
    return output if raw else output.decode('utf-8').strip()


def repository(tmp_path, *, include_secret=True):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.com')
    git(tmp_path, 'config', 'core.hooksPath', '/dev/null')
    (tmp_path / 'code.py').write_text('value = 1\n')
    if include_secret:
        (tmp_path / 'café.key').write_text('SECRET_SENTINEL\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    return git(tmp_path, 'rev-parse', 'HEAD')


def test_historical_quoted_secret_deletion_never_reaches_artifact(tmp_path):
    base = repository(tmp_path)
    (tmp_path / 'café.key').unlink()
    (tmp_path / 'code.py').write_text('value = 2\n')
    git(tmp_path, 'add', '-A')
    git(tmp_path, 'commit', '-qm', 'update')
    tid = core.roundtable_create('historical deletion')['thread_id']
    core.roundtable_bind_diff(tid, str(tmp_path), base)
    content = core.roundtable_get_artifact(tid, 'working-diff')['content']
    assert 'SECRET_SENTINEL' not in content
    assert '+value = 2' in content


def test_rename_from_excluded_path_is_excluded_as_a_pair(tmp_path):
    base = repository(tmp_path)
    git(tmp_path, 'mv', 'café.key', 'public.txt')
    (tmp_path / 'public.txt').write_text('SECRET_SENTINEL\nextra line\n')
    (tmp_path / 'code.py').write_text('value = 3\n')
    git(tmp_path, 'add', '-A')
    git(tmp_path, 'commit', '-qm', 'rename')
    tid = core.roundtable_create('historical rename')['thread_id']
    core.roundtable_bind_diff(tid, str(tmp_path), base)
    assert 'SECRET_SENTINEL' not in core.roundtable_get_artifact(tid, 'working-diff')['content']


def test_receipts_do_not_alias_different_snapshots_with_identical_visible_diff(tmp_path):
    repository(tmp_path)
    (tmp_path / 'code.py').write_text('value = 2\n')
    (tmp_path / 'café.key').write_text('SECRET_FIRST\n')
    first = core.capture_review_diff(str(tmp_path))
    (tmp_path / 'café.key').write_text('SECRET_SECOND\n')
    second = core.capture_review_diff(str(tmp_path))
    assert first['content'] == second['content']
    assert first['capture_id'] != second['capture_id']


@pytest.mark.parametrize('filename', ['café.key', 'line\nbreak.key', 'bracket[one].key', 'quoted".key'])
def test_raw_deleted_secret_paths_do_not_leak(tmp_path, filename):
    parent = repository(tmp_path, include_secret=False)
    code_entry = git(tmp_path, 'ls-tree', '-z', parent, '--', 'code.py', raw=True)
    baseline_index = git(tmp_path, 'ls-files', '--stage', '-z', raw=True)
    assert git(tmp_path, 'ls-files', '-z', raw=True) == b'code.py\0'
    # Some historical Git names cannot exist on Windows. Store those names
    # in a tree without checking them out or adding them to the index.
    secret = git(tmp_path, 'hash-object', '-w', '--stdin', stdin=b'HISTORICAL_SECRET\n')
    name = filename.encode('utf-8')
    tree = git(tmp_path, 'mktree', '-z', stdin=(
        code_entry + b'100644 blob ' + secret.encode('ascii') + b'\t' + name + b'\0'
    ))
    base = git(tmp_path, 'commit-tree', tree, '-p', parent, stdin=b'secret\n')
    git(tmp_path, 'update-ref', 'HEAD', base)
    assert git(tmp_path, 'ls-files', '--stage', '-z', raw=True) == baseline_index
    assert git(tmp_path, 'diff', '--cached', '--name-status', '-z', raw=True) == b'D\0' + name + b'\0'
    (tmp_path / 'code.py').write_text('value = 4\n')
    fields = git(tmp_path, 'diff', '--name-status', '-z', base, '--', raw=True).removesuffix(b'\0').split(b'\0')
    assert set(zip(fields[::2], fields[1::2], strict=True)) == {(b'D', name), (b'M', b'code.py')}
    tid = core.roundtable_create('raw deletion')['thread_id']
    core.roundtable_bind_diff(tid, str(tmp_path), base)
    content = core.roundtable_get_artifact(tid, 'working-diff')['content']
    assert 'HISTORICAL_SECRET' not in content
    assert '+value = 4' in content


def test_literal_pathspec_preserves_metacharacter_source_name(tmp_path):
    repository(tmp_path)
    (tmp_path / 'source[one].py').write_text('literal_source = 1\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'source')
    (tmp_path / 'source[one].py').write_text('literal_source = 2\n')
    tid = core.roundtable_create('literal path')['thread_id']
    core.roundtable_bind_diff(tid, str(tmp_path))
    assert '+literal_source = 2' in core.roundtable_get_artifact(tid, 'working-diff')['content']
