"""Exercise live Codex catalog completeness, metadata and per-input choices."""
import copy
import asyncio
import contextlib

import pytest

import codex_provider


@pytest.fixture(autouse=True)
def isolated_settings_state(monkeypatch, tmp_path):
    import app
    monkeypatch.setattr(app, "_STATE_DB", None)
    monkeypatch.setattr(app, "STATE_DB_PATH", tmp_path / "settings.db")
    monkeypatch.setattr(app, "CODEX_PERSONAL_HOMES_DIR", tmp_path / "homes")
    monkeypatch.setattr(app, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app, "ACTIVE_RUNS_BY_SESSION", {})
    yield
    if app._STATE_DB is not None:
        app._STATE_DB.close()


MODEL = {
    'id': 'gpt-6.2-sol', 'model': 'gpt-6.2-sol', 'displayName': 'GPT-6.2-Sol',
    'description': 'Coding and everyday work.', 'isDefault': True,
    'defaultReasoningEffort': 'low',
    'supportedReasoningEfforts': [{'reasoningEffort': 'low', 'description': 'Quick'},
                                 {'reasoningEffort': 'ultra', 'description': 'Delegates'}],
    'serviceTiers': [{'id': 'priority', 'name': 'Fast', 'description': 'Increased usage'}],
    'inputModalities': ['text', 'image'],
}


async def test_catalog_follows_pages_and_keeps_metadata():
    server = codex_provider.CodexAppServer()
    calls = []

    async def request(method, params):
        calls.append((method, params))
        if not params.get('cursor'):
            return {'data': [MODEL], 'nextCursor': 'page-two'}
        return {'data': [{**MODEL, 'id': 'gpt-6.2-astra', 'model': 'gpt-6.2-astra',
                          'isDefault': False}], 'nextCursor': None}

    server.request = request
    models = await server.models()
    assert len(models) == 2
    assert models[0]['service_tiers'] == MODEL['serviceTiers']
    assert models[0]['effort_descriptions']['ultra'] == 'Delegates'
    assert models[0]['input_modalities'] == ['text', 'image']
    assert models[0]['efforts'] == ['low', 'ultra']
    assert calls[-1][1]['cursor'] == 'page-two'
    assert await server.models() == models
    assert len(calls) == 2


async def test_failed_later_page_never_publishes_partial_catalog():
    server = codex_provider.CodexAppServer()
    server._models_cache = [{'key': 'previous'}]
    server._models_cached_at = -codex_provider.MODELS_CACHE_TTL_S
    old = copy.deepcopy(server._models_cache)

    async def request(method, params):
        if params.get('cursor'):
            raise codex_provider.CodexError('page failed')
        return {'data': [MODEL], 'nextCursor': 'next'}

    server.request = request
    with pytest.raises(codex_provider.CodexError, match='page failed'):
        await server.models()
    assert server._models_cache == old


async def test_repeated_cursor_fails_without_replacing_cache():
    server = codex_provider.CodexAppServer()

    async def request(method, params):
        return {'data': [MODEL], 'nextCursor': 'same'}

    server.request = request
    with pytest.raises(codex_provider.CodexError, match='cursor'):
        await server.models()
    assert server._models_cache is None


def test_selection_resets_default_effort_and_standard_tier():
    model = {'key': 'sol', 'model': 'sol', 'efforts': ['low', 'ultra'],
             'default_effort': 'low', 'service_tiers': MODEL['serviceTiers']}
    selection = codex_provider.model_selection([model], 'sol', '', 'default')
    assert selection.turn_params() == {'model': 'sol', 'effort': 'low', 'serviceTier': 'default'}
    assert codex_provider.model_selection([model], 'sol', 'ultra', 'priority').tier == 'priority'
    with pytest.raises(ValueError, match='speed'):
        codex_provider.model_selection([model], 'sol', '', 'ultrafast')
    with pytest.raises(ValueError, match='effort'):
        codex_provider.model_selection([model], 'sol', 'max', 'default')


@pytest.fixture
def model_driver(client, monkeypatch):
    import asyncio
    import app as app
    monkeypatch.setattr(app, '_panel_available', lambda: False)
    monkeypatch.setattr(app, '_notify_turn_complete', lambda run: None)
    monkeypatch.setattr(codex_provider, 'availability', lambda *args, **kwargs: {'available': True, 'reason': None})

    class Server:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.calls = []

        async def request(self, method, params=None, **kwargs):
            self.calls.append((method, params))
            if method.startswith('thread/'):
                return {'thread': {'id': 'settings-thread'}}
            if method == 'turn/start':
                return {'turn': {'id': str(len(self.calls))}}
            return {}

        def subscribe(self, sid):
            return self.queue

        def set_request_handler(self, *args):
            pass

        def unsubscribe(self, *args):
            pass

    server = Server()

    async def get_server(*args, **kwargs):
        return server

    async def close(*args):
        pass

    monkeypatch.setattr(app, '_codex_server_for_account', get_server)
    monkeypatch.setattr(codex_provider.CodexAppServer, 'close_key', close)
    run = app.ActiveRun('settings-run', owner_sub='anonymous', account_slot='shared')
    run.provider = 'codex'
    return app, server, run


async def wait_until(predicate):
    import asyncio
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def start_driver(app, server, run):
    import asyncio
    selected = codex_provider.ModelSelection('sol', 'sol', '', 'low')
    task = asyncio.create_task(app._codex_driver(run, initial_text='first', initial_images=[],
        resume_thread_id=None, model_id='sol', effort='', cwd=app.DEFAULT_CWD,
        personality_append='', first_prompt_title='test', account={'server_key': 'shared'}, selection=selected))
    run.task = task
    await wait_until(lambda: run.codex_active is not None)
    return task


def starts(server):
    return [params for method, params in server.calls if method == 'turn/start']


async def test_two_waiting_messages_keep_their_speed_and_effort(model_driver):
    app, server, run = model_driver
    task = await start_driver(app, server, run)
    try:
        fast = codex_provider.ModelSelection('sol', 'sol', 'ultra', 'ultra', 'priority')
        standard = codex_provider.ModelSelection('sol', 'sol', '', 'low', 'default')
        assert await app._inject_user_input(run, 'second', [], 0, 0, codex_selection=fast) is None
        await wait_until(lambda: run._deferred_user_item is not None)
        assert await app._inject_user_input(run, 'third', [], 0, 0, codex_selection=standard) is None
        run.codex_pending = fast  # A later picker change cannot rewrite either accepted item.
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await wait_until(lambda: len(starts(server)) == 2)
        assert starts(server)[1]['serviceTier'] == 'priority'
        assert starts(server)[1]['effort'] == 'ultra'
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await wait_until(lambda: len(starts(server)) == 3)
        assert starts(server)[2]['serviceTier'] == 'default'
        assert starts(server)[2]['effort'] == 'low'
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_expired_held_input_cannot_launch_after_lost_input(model_driver, monkeypatch):
    import asyncio
    app, server, run = model_driver
    monkeypatch.setattr(app, 'USER_INPUT_DELIVERY_TIMEOUT', 0.02)
    task = await start_driver(app, server, run)
    try:
        fast = codex_provider.ModelSelection('sol', 'sol', '', 'low', 'priority')
        await app._inject_user_input(run, 'must expire', [], 0, 0, queue_id='expired', codex_selection=fast)
        await wait_until(lambda: any('lost_input' in event for event in run.events))
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await asyncio.sleep(0.03)
        assert len(starts(server)) == 1
        assert app._codex_settings_record(run.session_id)['applied']['service_tier'] == 'default'
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_held_head_counts_toward_capacity_and_rejection_changes_no_settings(model_driver, monkeypatch):
    app, server, run = model_driver
    task = await start_driver(app, server, run)
    monkeypatch.setattr(app, 'MAX_USER_INPUT_QUEUE', 1)
    try:
        fast = codex_provider.ModelSelection('sol', 'sol', '', 'low', 'priority')
        await app._inject_user_input(run, 'held', [], 0, 0, codex_selection=fast)
        await wait_until(lambda: run._deferred_user_item is not None)
        before = app._codex_settings_record(run.session_id)
        rejected = codex_provider.ModelSelection('other', 'other', '', 'low')
        assert await app._inject_user_input(run, 'full', [], 0, 0, codex_selection=rejected) == 'queue_full'
        assert run.codex_pending == fast
        assert app._codex_settings_record(run.session_id) == before
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_finished_conversations_restore_their_own_settings(client):
    import app
    for sid, tier, effort in [('standard-chat', 'default', ''), ('fast-chat', 'priority', 'ultra')]:
        run = app.ActiveRun('run-' + sid, owner_sub='anonymous')
        run.session_id = sid
        app._record_codex_session(sid, 'anonymous', '', '', 'sol')
        app._save_codex_settings(run, codex_provider.ModelSelection('sol', 'sol', effort, effort or 'low', tier), 'applied')
    assert app._codex_session_settings('standard-chat')['service_tier'] == 'default'
    assert app._codex_session_settings('fast-chat')['effort'] == 'ultra'


def test_bound_signed_out_account_does_not_fall_back_to_shared(client, monkeypatch):
    import app
    from fastapi import HTTPException
    monkeypatch.setattr(app, '_session_account_slot', lambda sid: 'cred:7')
    monkeypatch.setattr(app, '_get_codex_credential', lambda *args: {'id': 7, 'label': 'Mine'})
    monkeypatch.setattr(app, '_codex_credential_is_configured', lambda *args: False)
    monkeypatch.setattr(codex_provider, 'availability', lambda *args, **kwargs: {'available': True})
    with pytest.raises(HTTPException) as error:
        app._resolve_codex_account_for_run({'sub': 'anonymous'}, session_id='saved')
    assert error.value.status_code == 503


async def test_refresh_uses_disposable_reader_and_leaves_control_alive(client, monkeypatch):
    import asyncio
    import app
    from types import SimpleNamespace
    control = SimpleNamespace(_login_started_at=0, _models_lock=asyncio.Lock(), _models_cache=None)
    monkeypatch.setattr(codex_provider, 'availability', lambda *args, **kwargs: {'available': True})
    monkeypatch.setattr(codex_provider.CodexAppServer, 'get_cached', lambda key: control)
    keys, closed = [], []

    class Reader:
        async def models(self):
            return [{'key': 'sol'}]

    async def get(account, *, server_key=None):
        keys.append(server_key)
        return Reader()

    async def close(key):
        closed.append(key)

    monkeypatch.setattr(app, '_codex_server_for_account', get)
    monkeypatch.setattr(codex_provider.CodexAppServer, 'close_key', close)
    result = await app.api_codex_models_refresh(account_slot='shared', user={'sub': 'anonymous'})
    assert result['account_slot'] == 'shared'
    assert len(keys) == 1 and keys[0].startswith('shared:read:models:')
    assert closed == keys
    assert control._models_cache == [{'key': 'sol'}]


@pytest.mark.skipif(__import__('os').getenv('CLAUDE_WEB_TEST_CODEX_CLI') != '1', reason='Opt in to the installed CLI loopback contract')
async def test_installed_cli_resets_fast_and_effort_on_the_actual_wire(tmp_path, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append({'service_tier': body.get('service_tier'), 'effort': (body.get('reasoning') or {}).get('effort')})
            item = {'type': 'message', 'id': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'Done', 'annotations': []}]}
            response = {'id': 'response', 'object': 'response', 'status': 'completed', 'output': [item]}
            events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                      {'type': 'response.completed', 'response': response}]
            data = ''.join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *arguments):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv('CODEX_MODELS_TEST_KEY', 'loopback-only')
    server = codex_provider.CodexAppServer(key='models-contract', home=tmp_path / 'codex')
    try:
        await server._start()
        started = await server.request('thread/start', {'cwd': str(tmp_path), 'ephemeral': True, 'model': 'gpt-6.2-sol',
            'approvalPolicy': 'never', 'sandbox': 'danger-full-access', 'config': {'model_provider': 'test',
            'features.code_mode': False, 'model_providers.test': {'name': 'Contract',
            'base_url': f'http://127.0.0.1:{http.server_port}', 'wire_api': 'responses', 'env_key': 'CODEX_MODELS_TEST_KEY'}}})
        sid = started['thread']['id']
        queue = server.subscribe(sid)
        for effort, tier in [('high', 'priority'), ('low', 'default'), ('high', 'priority'), ('low', 'default')]:
            await server.request('turn/start', {'threadId': sid, 'input': [{'type': 'text', 'text': 'Say Done'}],
                                               'effort': effort, 'serviceTier': tier})
            while True:
                event = await asyncio.wait_for(queue.get(), 20)
                if event['method'] == 'turn/completed':
                    assert not event.get('params', {}).get('turn', {}).get('error')
                    break
        assert captured == [{'service_tier': 'priority', 'effort': 'high'}, {'service_tier': None, 'effort': 'low'}] * 2
    finally:
        server.shutdown()
        if server.proc:
            await server.proc.wait()
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)


async def test_transmitted_input_cannot_expire_and_claim_it_was_lost(model_driver, monkeypatch):
    app, server, run = model_driver
    monkeypatch.setattr(app, 'USER_INPUT_DELIVERY_TIMEOUT', 0.02)
    entered, release = asyncio.Event(), asyncio.Event()
    original = server.request

    async def delayed(method, params=None, **kwargs):
        if method == 'turn/steer':
            entered.set()
            await release.wait()
        return await original(method, params, **kwargs)

    server.request = delayed
    task = await start_driver(app, server, run)
    try:
        await app._inject_user_input(run, 'still delivering', [], 0, 0, codex_selection=run.codex_active)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.04)
        assert not any('lost_input' in event for event in run.events)
        release.set()
        await wait_until(lambda: any(event.get('type') == 'user_prompt' for event in run.events))
        assert not any('lost_input' in event for event in run.events)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_refresh_refuses_waiting_sign_in_without_starting_a_reader(client, monkeypatch):
    import time
    import app
    from types import SimpleNamespace
    from fastapi import HTTPException
    monkeypatch.setattr(codex_provider, 'availability', lambda *args, **kwargs: {'available': True})
    monkeypatch.setattr(codex_provider.CodexAppServer, 'get_cached', lambda key: SimpleNamespace(_login_started_at=time.time()))

    async def forbidden(*args, **kwargs):
        pytest.fail('A waiting sign-in must not spawn or terminate any server')

    monkeypatch.setattr(app, '_codex_server_for_account', forbidden)
    monkeypatch.setattr(codex_provider.CodexAppServer, 'close_key', forbidden)
    with pytest.raises(HTTPException) as error:
        await app.api_codex_models_refresh(account_slot='shared', user={'sub': 'anonymous'})
    assert error.value.status_code == 409


@pytest.mark.parametrize('fork_fails', [False, True])
async def test_cross_account_fork_never_rebinds_or_changes_source(model_driver, monkeypatch, fork_fails):
    import uuid
    from starlette.requests import Request
    from starlette.datastructures import FormData
    from starlette.responses import JSONResponse
    app, server, _ = model_driver
    source = 'source-' + uuid.uuid4().hex
    child = 'child-' + uuid.uuid4().hex
    credential = app._create_codex_credential('anonymous', 'Destination-' + uuid.uuid4().hex)
    slot = 'cred:' + str(credential['id'])
    home = app._codex_credential_home_path('anonymous', credential['id'])
    home.mkdir(parents=True)
    (home / 'auth.json').write_text('{}')
    app._record_codex_session(source, 'anonymous', '', 'Source', 'sol')
    app._bind_session_account(source, 'anonymous', 'shared')
    old_run = app.ActiveRun('source-run', owner_sub='anonymous', account_slot='shared')
    old_run.session_id = source
    app._save_codex_settings(old_run, codex_provider.ModelSelection('sol', 'sol', '', 'low'), 'applied')
    original_settings = app._codex_settings_record(source)
    selected = {'key': 'sol', 'model': 'sol', 'efforts': ['low'], 'default_effort': 'low',
                'service_tiers': MODEL['serviceTiers']}

    async def models():
        return [selected]

    server.models = models
    original = server.request

    async def request(method, params=None, **kwargs):
        if method == 'thread/fork':
            assert app._session_account_slot(source) == 'shared'
            if fork_fails:
                raise RuntimeError('Fork failed')
            return {'thread': {'id': child}}
        if method == 'turn/start':
            server.queue.put_nowait({'method': codex_provider.SERVER_EXITED_METHOD})
        return await original(method, params, **kwargs)

    server.request = request
    monkeypatch.setattr(app, '_stream_run_response', lambda run, **kwargs: JSONResponse({'run_id': run.run_id}))
    request_obj = Request({'type': 'http', 'method': 'POST', 'path': '/api/chat', 'headers': []})
    request_obj._form = FormData({'model': 'sol', 'effort': '', 'service_tier': 'priority'})
    response = await app.api_chat(request_obj, message='Fork', session_id=source, project='', model='sol',
        advisor='', effort='', service_tier='priority', permission_mode='default', fork=True,
        personality_id=None, account_slot=slot, queue_id='', provider='codex', panel_review='', user={'sub': 'anonymous'})
    import json
    run = app.ACTIVE_RUNS[json.loads(response.body)['run_id']]
    await asyncio.wait_for(run.task, 2)
    assert app._session_account_slot(source) == 'shared'
    assert app._codex_settings_record(source) == original_settings
    if fork_fails:
        assert app._session_account_slot(child) is None
        assert not app._codex_settings_record(child)
    else:
        assert app._session_account_slot(child) == slot
        assert app._codex_settings_record(child)['applied']['service_tier'] == 'priority'


async def test_unavailable_account_can_open_settings_without_borrowing_shared(client, monkeypatch):
    import app
    from fastapi import HTTPException
    app._record_codex_session('unavailable-chat', 'anonymous', '', '', 'sol')
    app._bind_session_account('unavailable-chat', 'anonymous', 'cred:7')

    def unavailable(*args, **kwargs):
        raise HTTPException(503, 'Sign in to this account')

    async def forbidden(*args, **kwargs):
        pytest.fail('History must not borrow another account')

    monkeypatch.setattr(app, '_resolve_codex_account_for_run', unavailable)
    monkeypatch.setattr(app, '_codex_server_for_account', forbidden)
    result = await app._codex_session_payload('unavailable-chat', app._codex_session_row('unavailable-chat'), {'sub': 'anonymous'})
    assert result['account_slot'] == 'cred:7'
    assert result['history_error'] == 'Sign in to this account'
    assert result['messages'] == []


async def test_cancelled_admission_does_not_release_another_tasks_credential_lock(model_driver, monkeypatch):
    from starlette.requests import Request
    from starlette.datastructures import FormData
    app, _, _ = model_driver
    requested = asyncio.Event()

    class Lock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def acquire(self):
            self.calls += 1
            if self.calls == 2:
                requested.set()
            return await super().acquire()

    lock = Lock()
    await lock.acquire()
    monkeypatch.setattr(app, '_codex_credential_lock', lambda *args: lock)

    async def catalog(*args):
        return [{'key': 'sol', 'model': 'sol', 'efforts': ['low'], 'default_effort': 'low'}]

    monkeypatch.setattr(app, '_codex_catalog', catalog)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/api/chat', 'headers': []})
    request._form = FormData({'model': 'sol'})
    admission = asyncio.create_task(app.api_chat(request, message='Start', session_id='', project='', model='sol',
        advisor='', effort='', service_tier='default', permission_mode='default', fork=False,
        personality_id=None, account_slot='shared', queue_id='', provider='codex', panel_review='', user={'sub': 'anonymous'}))
    entered, release = asyncio.Event(), asyncio.Event()

    async def other_waiter():
        async with lock:
            entered.set()
            await release.wait()

    third = None
    try:
        await asyncio.wait_for(requested.wait(), 2)
        third = asyncio.create_task(other_waiter())
        admission.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await admission
        await asyncio.sleep(0.02)
        assert lock.locked() and not entered.is_set()
        lock.release()
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        admission.cancel()
        if lock.locked() and not entered.is_set():
            lock.release()
        release.set()
        if third is not None:
            await third


@pytest.mark.parametrize('newer_intent', [None, 'picker', 'submission'])
async def test_expiry_restores_surviving_default_before_an_omitted_settings_send(model_driver, monkeypatch, newer_intent):
    from starlette.requests import Request
    from starlette.datastructures import FormData
    app, server, run = model_driver
    app.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app, 'USER_INPUT_DELIVERY_TIMEOUT', 0.05)
    monkeypatch.setattr(app, '_resolve_personality_for_run', lambda *args, **kwargs: {'id': None, 'append': ''})

    async def models(*args):
        return [{'key': 'sol', 'model': 'sol', 'efforts': ['low'], 'default_effort': 'low', 'service_tiers': MODEL['serviceTiers']}]

    monkeypatch.setattr(app, '_codex_catalog', models)
    task = await start_driver(app, server, run)
    try:
        fast = codex_provider.ModelSelection('sol', 'sol', '', 'low', 'priority')
        await app._inject_user_input(run, 'expire this Fast message', [], 0, 0, queue_id='expired-fast', codex_selection=fast)
        await wait_until(lambda: run._deferred_user_item is not None)
        if newer_intent == 'picker':
            await app.api_chat_set_model(session_id=run.session_id, model='sol', effort='', service_tier='priority', user={'sub': 'anonymous'})
        elif newer_intent == 'submission':
            monkeypatch.setattr(app, 'USER_INPUT_DELIVERY_TIMEOUT', 1)
            await app._inject_user_input(run, 'newer Fast message', [], 0, 0, codex_selection=fast)
        await wait_until(lambda: any(event.get('queue_id') == 'expired-fast' and 'lost_input' in event for event in run.events))
        expected = 'priority' if newer_intent is not None else 'default'
        assert run.codex_pending.tier == expected
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/chat/send', 'headers': []})
        request._form = FormData()
        observed = []

        async def admit(*args, **kwargs):
            observed.append(kwargs['codex_selection'])
            return None

        monkeypatch.setattr(app, '_inject_user_input', admit)
        response = await app.api_chat_send(request, run.run_id, message='Omitted settings', provider='codex', personality_id=None,
                                           account_slot='', steer=False, queue_id='', user={'sub': 'anonymous'})
        assert response.status_code == 202
        assert observed[0].tier == expected
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_recalling_a_held_fast_message_restores_the_inherited_default(model_driver):
    app, server, run = model_driver
    app.ACTIVE_RUNS[run.run_id] = run
    task = await start_driver(app, server, run)
    try:
        fast = codex_provider.ModelSelection('sol', 'sol', '', 'low', 'priority')
        await app._inject_user_input(run, 'recall this', [], 0, 0, queue_id='recalled-fast', codex_selection=fast)
        await wait_until(lambda: run._deferred_user_item is not None)
        response = await app.api_chat_cancel_queued(run.run_id, queue_id='recalled-fast', user={'sub': 'anonymous'})
        assert response['cancelled']
        assert run.codex_pending.tier == 'default'
        await wait_until(lambda: not run.codex_delivery_pending)
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await wait_until(lambda: run.between_turns)
        assert len(starts(server)) == 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize('rpc_fails', [False, True])
async def test_queued_compaction_cannot_expire_after_rpc_commitment(model_driver, monkeypatch, rpc_fails):
    app, server, run = model_driver
    app.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app, 'USER_INPUT_DELIVERY_TIMEOUT', 0.03)
    entered, release = asyncio.Event(), asyncio.Event()
    original_request = server.request

    async def request(method, params=None, **kwargs):
        if method == 'thread/compact/start':
            server.calls.append((method, params))
            entered.set()
            await release.wait()
            if rpc_fails:
                raise codex_provider.CodexError('Compaction response lost')
            return {}
        return await original_request(method, params, **kwargs)

    monkeypatch.setattr(server, 'request', request)
    task = await start_driver(app, server, run)
    try:
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await wait_until(lambda: run.between_turns)
        assert await app._inject_user_input(run, '/compact', [], 0, 0, queue_id='compact-pending', codex_selection=run.codex_active) is None
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.08)
        assert not any('lost_input' in event for event in run.events)
        recalled = await app.api_chat_cancel_queued(run.run_id, queue_id='compact-pending', user={'sub': 'anonymous'})
        assert recalled['reason'] == 'already_delivered' and not recalled['cancelled']
        assert 'compact-pending' in run.committed_input_ids
        release.set()
        await wait_until(lambda: not run.codex_delivery_pending)
        assert not any('lost_input' in event for event in run.events)
        if rpc_fails:
            assert any('may have been received' in event.get('message', '') for event in run.events)
        else:
            assert any(event.get('type') == 'user_prompt' and event.get('queue_id') == 'compact-pending' for event in run.events)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_queued_compaction_respects_the_write_lock_and_recall(model_driver):
    app, server, run = model_driver
    app.ACTIVE_RUNS[run.run_id] = run
    task = await start_driver(app, server, run)
    try:
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await wait_until(lambda: run.between_turns)
        async with run.client_write_lock:
            await app._inject_user_input(run, '/compact', [], 0, 0, queue_id='compact-recalled', codex_selection=run.codex_active)
            await asyncio.sleep(0.03)
            assert not any(method == 'thread/compact/start' for method, _ in server.calls)
            recalled = await app.api_chat_cancel_queued(run.run_id, queue_id='compact-recalled', user={'sub': 'anonymous'})
            assert recalled['cancelled']
        await wait_until(lambda: not run.codex_delivery_pending)
        assert not any(method == 'thread/compact/start' for method, _ in server.calls)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_changed_steering_settings_offer_next_turn_queue_without_admission(model_driver, monkeypatch):
    import json
    from starlette.requests import Request
    from starlette.datastructures import FormData
    app, server, run = model_driver
    app.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app, '_resolve_personality_for_run', lambda *args, **kwargs: {'id': None, 'append': ''})

    async def catalog(*args):
        return [{'key': 'sol', 'model': 'sol', 'efforts': ['low', 'ultra'], 'default_effort': 'low', 'service_tiers': MODEL['serviceTiers']}]

    monkeypatch.setattr(app, '_codex_catalog', catalog)
    task = await start_driver(app, server, run)
    before = app._codex_settings_record(run.session_id)
    try:
        for steer in [True, False]:
            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/chat/send', 'headers': []})
            request._form = FormData({'model': 'sol', 'effort': 'ultra', 'service_tier': 'priority'})
            response = await app.api_chat_send(request, run.run_id, message='Use Fast next', provider='codex', personality_id=None,
                account_slot='', steer=steer, queue_id='changed-settings', user={'sub': 'anonymous'})
            if steer:
                assert response.status_code == 409 and json.loads(response.body)['error'] == 'codex_settings_require_queue'
                assert app._codex_settings_record(run.session_id) == before
                assert not run.codex_delivery_pending
            else:
                assert response.status_code == 202
        await wait_until(lambda: run._deferred_user_item is not None)
        assert len(starts(server)) == 1 and run.accepting_input
        await server.queue.put({'method': 'turn/completed', 'params': {'turn': {}}})
        await wait_until(lambda: len(starts(server)) == 2)
        assert starts(server)[1]['serviceTier'] == 'priority' and starts(server)[1]['effort'] == 'ultra'
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize('invalid_field, invalid_value', [('model', 'retired'), ('effort', 'ultra'), ('service_tier', 'priority'), ('images', None)])
async def test_unavailable_codex_selection_rejects_without_admission_or_settings_changes(model_driver, monkeypatch, invalid_field, invalid_value):
    import json
    from starlette.requests import Request
    from starlette.datastructures import FormData
    app, server, run = model_driver
    app.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app, '_resolve_personality_for_run', lambda *args, **kwargs: {'id': None, 'append': ''})

    async def catalog(*args):
        return [{'key': 'sol', 'model': 'sol', 'efforts': ['low'], 'default_effort': 'low', 'service_tiers': [], 'input_modalities': ['text']}]

    submitted_image = False

    async def read_images(*args):
        return ([{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='}}] if submitted_image else []), []

    monkeypatch.setattr(app, '_codex_catalog', catalog)
    monkeypatch.setattr(app, '_read_uploaded_images', read_images)
    task = await start_driver(app, server, run)
    try:
        before = app._codex_settings_record(run.session_id)
        pending = run.codex_pending
        for invalid in [True, False]:
            values = {'model': 'sol', 'effort': '', 'service_tier': 'default'}
            submitted_image = invalid and invalid_field == 'images'
            if invalid and invalid_field != 'images':
                values[invalid_field] = invalid_value
            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/chat/send', 'headers': []})
            request._form = FormData(values)
            response = await app.api_chat_send(request, run.run_id, message='Continue', provider='codex', personality_id=None,
                account_slot='', steer=True, queue_id='rejected' if invalid else 'valid-after-rejection', user={'sub': 'anonymous'})
            if invalid:
                assert response.status_code == 400
                body = json.loads(response.body)
                assert body['error'] == 'codex_selection_unavailable' and body['detail']
                assert app._codex_settings_record(run.session_id) == before and run.codex_pending == pending
                assert not run.codex_delivery_pending and run.user_input_queue.empty()
                assert run.accepting_input and not run.done and not task.done()
            else:
                assert response.status_code == 202
        await wait_until(lambda: any(event.get('queue_id') == 'valid-after-rejection' and event.get('type') == 'user_prompt' for event in run.events))
        assert any(method == 'turn/steer' for method, _ in server.calls)
        assert len(starts(server)) == 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
