"""Review older commits without granting commit permission or losing open objections."""
import os

import pytest

import app as app
import panel
from roundtable import core
from tests.test_panel_app import chat as chat, repo as repo, FakeCore, APPROVE_ALL, _review, _finish_round, _git

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Panel review is not enabled on Windows")


async def test_committed_branch_reaches_reviewers_without_commit_approval(chat, repo, monkeypatch):
    run, events, delivered = chat
    base = panel.head_commit(str(repo))
    (repo / 'a.py').write_text('a = 2\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'branch')
    fake = FakeCore([APPROVE_ALL])
    monkeypatch.setattr(app, 'roundtable_core', fake)
    text, error = await _review(run, stage='changes', repo=str(repo), base=base, message='Review the committed branch')
    assert not error and base in text
    await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert fake.asks == 1
    assert not state.approvals and not state.last_reviewed
    assert state.comparisons[str(repo)]['base_oid'] == base
    assert not state.comparisons[str(repo)]['commit_eligible']
    assert 'does not approve pending edits' in delivered[-1]
    assert 'Next: commit these changes' not in delivered[-1]
    assert next(event for event in events if event['type'] == 'panel_round')['comparison']['base_oid'] == base


async def test_empty_and_invalid_comparisons_do_not_consume_rounds(chat, repo, monkeypatch):
    run, _, _ = chat
    fake = FakeCore([])
    monkeypatch.setattr(app, 'roundtable_core', fake)
    for base in ['HEAD', '', '--output=/tmp/nope', 'HEAD..HEAD', 'missing-ref']:
        text, error = await _review(run, stage='changes', repo=str(repo), base=base, message='Check changes')
        assert error and 'did not start' in text
        assert app._panel_load(run.session_id).rounds['changes'] == 0
    assert fake.asks == 0 and not fake.posts


@pytest.mark.parametrize('advisory', [False, True])
async def test_publication_failure_preserves_objections_and_requirements(chat, repo, monkeypatch, advisory):
    run, _, delivered = chat
    (repo / 'a.py').write_text('a = 2\n')

    class Broken(FakeCore):
        def publish_review_diff(self, *arguments):
            raise RuntimeError('Publication failed after preflight')

    fake = Broken([APPROVE_ALL])
    monkeypatch.setattr(app, 'roundtable_core', fake)
    state = app._panel_load(run.session_id)
    state.enabled = not advisory
    state.plan_ok = True
    state.granted = ['changes'] if advisory else []
    state.review_requirements[str(repo)] = ['Preserve this requirement']
    state.review_ledger[str(repo)] = {'changes': {'gpt-5': [{'id': 'F1', 'status': 'open', 'claim': 'Original problem', 'evidence': []}]}}
    before = state.review_ledger
    app._panel_save(run.session_id, run.owner_sub, state)
    text, error = await _review(run, stage='changes', repo=str(repo), message='Changes')
    assert not error
    await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert fake.asks == 0
    assert state.review_ledger == before
    assert state.review_requirements[str(repo)] == ['Preserve this requirement']
    assert state.plan_ok and not state.approvals and not state.last_reviewed
    assert state.rounds['changes'] == 0
    assert 'no approval' in delivered[-1].lower()
    if advisory:
        assert state.granted == ['changes']


@pytest.mark.parametrize('base_ref', ['HEAD', 'main'])
async def test_explicit_base_resolving_to_head_is_commit_eligible(chat, repo, monkeypatch, base_ref):
    run, _, _ = chat
    _git(repo, 'branch', '-M', 'main')
    (repo / 'a.py').write_text('a = 2\n')
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([APPROVE_ALL]))
    _, error = await _review(run, stage='changes', repo=str(repo), base=base_ref, message='Pending edits')
    assert not error
    await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert state.approvals[str(repo)] == panel.working_state(str(repo))
    assert state.comparisons[str(repo)]['commit_eligible']


async def test_review_only_go_ahead_keeps_head_objection_through_outage(chat, repo, monkeypatch):
    run, _, delivered = chat
    base = panel.head_commit(str(repo))
    (repo / 'a.py').write_text('a = 2\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'branch')
    # The pending revert is invisible against BASE. It must get no approval from this receipt.
    (repo / 'a.py').write_text('a = 1\n')
    (repo / 'other.py').write_text('other = 1\n')
    capture = core.capture_review_diff(str(repo), base)
    state = app._panel_load(run.session_id)
    state.thread_id = 77
    state.comparisons[str(repo)] = {key: capture[key] for key in ('base', 'base_oid', 'commit_sha', 'commit_eligible', 'capture_id', 'truncated')}
    finding = {'id': 'F1', 'status': 'open', 'claim': 'Pending revert is wrong', 'evidence': []}
    state.review_ledger[str(repo)] = {'changes': {'gpt-5': [finding]}}
    state.rounds['changes'] = panel.MAX_ROUNDS
    app._panel_save(run.session_id, run.owner_sub, state)
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([]))

    async def go(*args, **kwargs):
        return 'go_ahead'

    monkeypatch.setattr(app, '_panel_ask_user', go)
    text, error = await _review(run, stage='changes', repo=str(repo), base='HEAD', message='New scope')
    assert not error and 'NOT reviewed' in text
    await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert state.review_ledger[str(repo)]['changes']['gpt-5'][0]['status'] == 'open'
    assert not state.approvals and not state.last_reviewed
    assert 'does not approve pending edits' in delivered[-1]
    # Ground a HEAD round with no available reviewers. The prior objection must still block it.
    capture = core.capture_review_diff(str(repo))
    capture['pending_state'] = panel.working_state(str(repo))
    await app._panel_round_task(run, run.session_id, 'changes', str(repo), 'HEAD after outage', [], [], 1, capture=capture)
    state = app._panel_load(run.session_id)
    assert not state.approvals
    assert state.review_ledger[str(repo)]['changes']['gpt-5'][0]['status'] == 'open'


@pytest.mark.parametrize('provider, function', [('openai', '_call_openai'), ('gemini', '_call_gemini'), ('anthropic', '_call_anthropic_router')])
def test_current_diff_survives_history_trimming_at_provider_transport(monkeypatch, provider, function):
    seen = []

    def transport(model, system, transcript, instruction, *arguments, **kwargs):
        seen.append((transcript, instruction))
        return core.ProviderResult(text='result')

    monkeypatch.setattr(core, function, transport)
    monkeypatch.setattr(core, 'PROMPT_CHAR_CAP', 16000)
    protected = 'BEGIN_CURRENT_DIFF\n' + 'diff content\n' * 300 + 'END_CURRENT_DIFF'
    history = [{'speaker': 'orchestrator', 'content': 'old ' * 10000},
               {'speaker': 'orchestrator', 'content': protected},
               {'speaker': 'orchestrator', 'content': 'new ' * 10000}]
    for instruction in ['Review this', 'Correct your report']:
        core._run_turn({'topic': 'Review', 'participants': ['test']},
                       {'provider': provider, 'label': 'Test', 'model': 'test'}, history,
                       instruction, review_context=protected)
    assert len(seen) == 2
    for transcript, instruction in seen:
        assert protected in instruction
        assert protected not in transcript
        assert len(transcript) + len(instruction) < 16000


def test_over_budget_context_is_a_grounding_failure_not_a_provider_outage(monkeypatch):
    import review_capture
    monkeypatch.setattr(core, '_participant_provider_available', lambda name: True)
    tid = core.roundtable_create('budget test', participants=['gpt-5'])['thread_id']
    monkeypatch.setattr(core, 'PROMPT_CHAR_CAP', 3000)
    monkeypatch.setattr(core, '_resolve_participant', lambda name: {**core.PARTICIPANTS[name], 'model': 'test'})
    with pytest.raises(review_capture.CaptureError, match='budget'):
        core.roundtable_ask_parallel(tid, ['gpt-5'], review_context='current capture')


async def test_concurrent_preflights_launch_only_one_review(chat, repo, monkeypatch):
    import asyncio
    import threading
    run, _, _ = chat
    (repo / 'a.py').write_text('a = 2\n')
    entered, release = threading.Event(), threading.Event()

    class Concurrent(FakeCore):
        def capture_review_diff(self, root, base='HEAD'):
            entered.set()
            assert release.wait(timeout=3)
            return super().capture_review_diff(root, base)

    fake = Concurrent([APPROVE_ALL])
    monkeypatch.setattr(app, 'roundtable_core', fake)
    first = asyncio.create_task(_review(run, stage='changes', repo=str(repo), message='Changes'))
    async with asyncio.timeout(3):
        while not entered.is_set():
            await asyncio.sleep(0.001)
    second = await _review(run, stage='changes', repo=str(repo), message='Changes')
    release.set()
    await first
    await _finish_round(run)
    assert fake.asks == 1
    assert 'already being prepared' in second[0]


async def test_renamed_secret_destination_is_blocked_in_every_review_read_path(chat, repo, monkeypatch):
    run, _, _ = chat
    (repo / 'secret.key').write_text('PRIVATE_SENTINEL\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'secret base')
    _git(repo, 'mv', 'secret.key', 'public.txt')
    (repo / 'public.txt').write_text('PRIVATE_SENTINEL\nextra line\n')
    (repo / 'a.py').write_text('a = 2\n')

    class Reading(FakeCore):
        def roundtable_ask_parallel(self, *arguments, **kwargs):
            context = kwargs['tool_use_context']
            assert context.permission_callback.enforce_read_hooks
            tools = core._RepoTools(repo, context.permission_callback, 'Test')
            assert 'PRIVATE_SENTINEL' not in tools.execute('Read', {'path': 'public.txt'})
            assert context.permission_callback('Claude', 'Read', {'file_path': str(repo / 'public.txt')}) == 'deny'
            assert context.permission_callback('Claude', 'Grep', {'pattern': 'PRIVATE'}) == 'deny'
            assert context.permission_callback('Claude', 'Glob', {'pattern': '*'}) == 'deny'
            assert 'PRIVATE_SENTINEL' not in kwargs['review_context']
            return super().roundtable_ask_parallel(*arguments, **kwargs)

    fake = Reading([APPROVE_ALL])
    monkeypatch.setattr(app, 'roundtable_core', fake)
    _, error = await _review(run, stage='changes', repo=str(repo), message='Review source changes')
    assert not error
    await _finish_round(run)
    assert fake.asks == 1


def test_claude_read_filter_runs_even_when_native_read_is_already_allowed(monkeypatch, tmp_path):
    import asyncio
    sdk = core._import_agent_sdk()
    observed = []

    def permission(label, name, arguments):
        return 'deny' if arguments.get('file_path') == str(tmp_path / 'public.txt') else 'allow'

    permission.enforce_read_hooks = True

    async def query(prompt, options):
        # Deliberately do NOT call can_use_tool: native allowed_tools can skip it.
        hook = options.hooks['PreToolUse'][0].hooks[0]
        result = await hook({'tool_name': 'Read', 'tool_input': {'file_path': str(tmp_path / 'public.txt')}}, 'read-1', {})
        observed.append(result)
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text='Read was denied')], model='test')

    monkeypatch.setattr(sdk, 'query', query)
    context = core.ToolUseContext(permission, working_directory=tmp_path, allowed_tools=['Read'])
    # The production helper owns its event loop, as it does on a roundtable worker thread.
    assert not asyncio._get_running_loop()
    core._call_anthropic_sdk_with_tools('test', 'Review', '', 'Read source', 'low', False, context, participant_label='Claude')
    assert observed[0]['hookSpecificOutput']['permissionDecision'] == 'deny'


async def test_interrupted_decision_recovers_its_matching_head_snapshot(chat, repo, monkeypatch):
    import asyncio
    import contextlib
    from tests.test_panel_app import OBJECT
    run, _, _ = chat
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([OBJECT, OBJECT]))
    (repo / 'a.py').write_text('a = 2\n')
    await _review(run, stage='changes', repo=str(repo), message='First HEAD')
    await _finish_round(run)
    old = app._panel_load(run.session_id).last_reviewed[str(repo)]
    (repo / 'a.py').write_text('a = 3\n')
    expected = panel.working_state(str(repo))
    state = app._panel_load(run.session_id)
    state.rounds['changes'] = panel.MAX_ROUNDS - 1
    app._panel_save(run.session_id, run.owner_sub, state)
    entered = asyncio.Event()

    async def interrupted(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(app, '_panel_ask_user', interrupted)
    await _review(run, stage='changes', repo=str(repo), message='Second HEAD')
    await asyncio.wait_for(entered.wait(), 2)
    task = app._PANEL_ROUNDS[run.session_id]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert old != expected

    async def go(*args, **kwargs):
        return 'go_ahead'

    monkeypatch.setattr(app, '_panel_ask_user', go)
    await _review(run, stage='changes', repo=str(repo), message='Recover decision')
    await _finish_round(run)
    assert app._panel_load(run.session_id).approvals[str(repo)] == expected


async def test_missing_decision_snapshot_reports_failure_instead_of_dying(chat, repo, monkeypatch):
    import review_capture
    run, _, delivered = chat
    (repo / 'a.py').write_text('a = 2\n')
    capture = core.capture_review_diff(str(repo))
    state = app._panel_load(run.session_id)
    state.comparisons[str(repo)] = review_capture.receipt(capture)
    state.rounds['changes'] = panel.MAX_ROUNDS
    app._panel_save(run.session_id, run.owner_sub, state)
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([]))

    async def go(*args, **kwargs):
        return 'go_ahead'

    monkeypatch.setattr(app, '_panel_ask_user', go)
    await _review(run, stage='changes', repo=str(repo), message='Recover missing snapshot')
    await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert not state.approvals
    assert 'fresh review' in delivered[-1].lower()


async def test_pending_head_decision_survives_a_later_review_only_receipt(chat, repo, monkeypatch):
    import review_capture
    run, _, delivered = chat
    base = panel.head_commit(str(repo))
    (repo / 'a.py').write_text('a = 2\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'branch')
    (repo / 'a.py').write_text('a = 3\n')
    snapshot = panel.working_state(str(repo))
    head_receipt = review_capture.receipt(core.capture_review_diff(str(repo)))
    branch_receipt = review_capture.receipt(core.capture_review_diff(str(repo), base))
    state = app._panel_load(run.session_id)
    state.record_comparison(str(repo), head_receipt, snapshot)
    state.decision_reviews[str(repo)] = head_receipt['capture_id']
    state.record_comparison(str(repo), branch_receipt, None)
    state.review_ledger[str(repo)] = {'changes': {'gpt-5': [{'id': 'F1', 'status': 'open', 'claim': 'HEAD dispute', 'evidence': []}]}}
    state.rounds['changes'] = panel.MAX_ROUNDS
    app._panel_save(run.session_id, run.owner_sub, state)
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([]))

    async def go(*args, **kwargs):
        return 'go_ahead'

    monkeypatch.setattr(app, '_panel_ask_user', go)
    text, error = await _review(run, stage='changes', repo=str(repo), base=base, message='Recover HEAD decision')
    assert not error and head_receipt['capture_id'] in text
    assert branch_receipt['capture_id'] not in text
    await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert state.approvals[str(repo)] == snapshot
    assert state.review_ledger[str(repo)]['changes']['gpt-5'][0]['status'] == 'overridden_by_user'
    assert head_receipt['capture_id'] in delivered[-1]


async def test_ordinary_secret_exclusion_keeps_normal_head_search_permissions(chat, repo, monkeypatch):
    run, _, _ = chat
    (repo / '.env').write_text('EXCLUDED_SENTINEL=old\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'base with env')
    (repo / '.env').write_text('EXCLUDED_SENTINEL=new\n')
    (repo / 'a.py').write_text('a = 2\n')

    class Reading(FakeCore):
        def roundtable_ask_parallel(self, *arguments, **kwargs):
            callback = kwargs['tool_use_context'].permission_callback
            assert callback('Claude', 'Grep', {'pattern': 'a ='}) == 'allow'
            assert callback('Claude', 'Glob', {'pattern': '*.py'}) == 'allow'
            assert 'EXCLUDED_SENTINEL' not in kwargs['review_context']
            return super().roundtable_ask_parallel(*arguments, **kwargs)

    fake = Reading([APPROVE_ALL])
    monkeypatch.setattr(app, 'roundtable_core', fake)
    await _review(run, stage='changes', repo=str(repo), message='Review ordinary source changes')
    await _finish_round(run)
    assert fake.asks == 1


@pytest.mark.parametrize('selected_review_only', [False, True])
async def test_decision_card_and_go_ahead_target_only_the_selected_repository(chat, repo, monkeypatch, selected_review_only):
    import review_capture
    from tests.test_panel_app import _card, _answer
    run, events, _ = chat
    other = repo.parent / 'other'
    other.mkdir()
    (other / 'a.py').write_text('a = 1\n')
    _git(other, 'init', '-q')
    _git(other, 'add', '.')
    _git(other, 'commit', '-qm', 'other initial')
    state = app._panel_load(run.session_id)
    receipts, snapshots = {}, {}
    for path, review_only in [(other, not selected_review_only), (repo, selected_review_only)]:
        base = panel.head_commit(str(path))
        (path / 'a.py').write_text('a = 2\n')
        _git(path, 'add', '.')
        _git(path, 'commit', '-qm', 'branch')
        (path / 'a.py').write_text('a = 3\n')
        root = str(path)
        snapshots[root] = None if review_only else panel.working_state(root)
        receipts[root] = review_capture.receipt(core.capture_review_diff(root, base if review_only else 'HEAD'))
        state.record_comparison(root, receipts[root], snapshots[root])
        state.decision_reviews[root] = receipts[root]['capture_id']
        state.review_ledger[root] = {'changes': {'gpt-5': [{'id': 'F1', 'status': 'open', 'claim': 'Original dispute', 'evidence': []}]}}
    state.rounds['changes'] = panel.MAX_ROUNDS
    app._panel_save(run.session_id, run.owner_sub, state)
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([]))
    await _review(run, stage='changes', repo=str(repo), message='Decide this repository only')
    card = await _card(events)
    try:
        question = card['questions'][0]['question']
        assert str(repo) in question and receipts[str(repo)]['capture_id'] in question
        assert str(other) not in question and receipts[str(other)]['capture_id'] not in question
        description = card['questions'][0]['options'][0]['description']
        if selected_review_only:
            assert 'still need a HEAD changes review' in description
        else:
            assert 'commits these changes' in description
    finally:
        _answer(card, 'Go ahead anyway')
        await _finish_round(run)
    state = app._panel_load(run.session_id)
    assert state.decision_reviews[str(other)] == receipts[str(other)]['capture_id']
    assert state.review_ledger[str(other)]['changes']['gpt-5'][0]['status'] == 'open'
    assert str(other) not in state.approvals
    selected_status = state.review_ledger[str(repo)]['changes']['gpt-5'][0]['status']
    if selected_review_only:
        assert selected_status == 'open' and str(repo) not in state.approvals
    else:
        assert selected_status == 'overridden_by_user'
        assert state.approvals[str(repo)] == snapshots[str(repo)]


@pytest.mark.parametrize('plan_answer', ['go_ahead', 'more_rounds', 'stop'])
async def test_plan_answer_preserves_an_interrupted_changes_decision(chat, repo, monkeypatch, plan_answer):
    import asyncio
    import contextlib
    import review_capture
    from tests.test_panel_app import OBJECT
    run, _, _ = chat
    base = panel.head_commit(str(repo))
    (repo / 'a.py').write_text('a = 2\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'branch')
    (repo / 'a.py').write_text('a = 3\n')
    snapshot = panel.working_state(str(repo))
    state = app._panel_load(run.session_id)
    state.rounds['changes'] = panel.MAX_ROUNDS - 1
    app._panel_save(run.session_id, run.owner_sub, state)
    monkeypatch.setattr(app, 'roundtable_core', FakeCore([OBJECT]))
    entered = asyncio.Event()

    async def interrupted(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(app, '_panel_ask_user', interrupted)
    await _review(run, stage='changes', repo=str(repo), message='HEAD dispute')
    await asyncio.wait_for(entered.wait(), 2)
    task = app._PANEL_ROUNDS[run.session_id]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    state = app._panel_load(run.session_id)
    pending = state.decision_reviews[str(repo)]
    newer = review_capture.receipt(core.capture_review_diff(str(repo), base))
    state.record_comparison(str(repo), newer, None)
    state.rounds['plan'] = panel.MAX_ROUNDS
    app._panel_save(run.session_id, run.owner_sub, state)

    async def decide(sid, stage, number, **kwargs):
        return plan_answer if stage == 'plan' else 'go_ahead'

    monkeypatch.setattr(app, '_panel_ask_user', decide)
    await _review(run, stage='plan', repo=str(repo), message='Intervening plan decision')
    await _finish_round(run)
    assert app._panel_load(run.session_id).decision_reviews[str(repo)] == pending
    text, error = await _review(run, stage='changes', repo=str(repo), message='Recover HEAD decision')
    assert not error and pending in text and newer['capture_id'] not in text
    await _finish_round(run)
    assert app._panel_load(run.session_id).approvals[str(repo)] == snapshot
