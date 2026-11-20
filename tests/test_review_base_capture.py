"""Keep historical secret paths out of review artifacts, including renamed files."""
import subprocess

import pytest

from roundtable import core


def git(repo, *arguments):
    return subprocess.run(['git', '-C', str(repo), *arguments], check=True,
                          capture_output=True).stdout.decode().strip()


def repository(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.com')
    git(tmp_path, 'config', 'core.hooksPath', '/dev/null')
    (tmp_path / 'code.py').write_text('value = 1\n')
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
    base = repository(tmp_path)
    (tmp_path / filename).write_text('HISTORICAL_SECRET\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'secret')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / filename).unlink()
    (tmp_path / 'code.py').write_text('value = 4\n')
    tid = core.roundtable_create('raw deletion')['thread_id']
    core.roundtable_bind_diff(tid, str(tmp_path), base)
    assert 'HISTORICAL_SECRET' not in core.roundtable_get_artifact(tid, 'working-diff')['content']


def test_literal_pathspec_preserves_metacharacter_source_name(tmp_path):
    repository(tmp_path)
    (tmp_path / 'source[one].py').write_text('literal_source = 1\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'source')
    (tmp_path / 'source[one].py').write_text('literal_source = 2\n')
    tid = core.roundtable_create('literal path')['thread_id']
    core.roundtable_bind_diff(tid, str(tmp_path))
    assert '+literal_source = 2' in core.roundtable_get_artifact(tid, 'working-diff')['content']
