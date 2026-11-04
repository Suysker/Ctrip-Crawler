"""Unattended recovery preserves progress and never grants unbounded retries."""
import copy
import signal

import pytest

from test_runner import app_module, config, successful_worker


def make_worker(app, events, failure):
    base = successful_worker(app, events)

    class Worker(base):
        def call(self, kind, value, **kw):
            if kind == 'collect':
                failure(value['task'])
            return super().call(kind, value, **kw)
    return Worker


@pytest.mark.parametrize('code,failures', [('response_timeout', 3), ('proxy_access_denied', 1), ('proxy_unavailable', 1)])
def test_transient_failure_waits_and_finishes_without_manual_grant(tmp_path, code, failures):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=2, auto_recovery_cooldown=0.01)
    calls = []

    def failure(task):
        calls.append(task['index'])
        if len(calls) <= failures:
            raise app.CrawlerError(code, 'controlled temporary outage')

    runner = app.Runner(cfg, worker_factory=make_worker(app, [], failure))
    waits = []
    wait = runner._wait_until
    def observe_wait(value):
        if runner.control.get('status') == 'waiting':
            waits.append(copy.deepcopy(app.read_json(runner.control_path)))
        wait(value)
    runner._wait_until = observe_wait
    assert runner.run(run_id='fixed') == 0
    assert waits and waits[0]['current']['automatic_recoveries']
    assert calls == [0] * (failures + 1) + [1]
    receipts = [app.read_json(p) for p in tmp_path.rglob('*.meta.json')]
    first = next(x for x in receipts if x['task_index'] == 0)
    assert first['automatic_recoveries'][0]['error_code'] == code
    assert first['manual_recoveries'] is None
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*.csv')}
    assert app.Runner(cfg, worker_factory=lambda *a, **k: pytest.fail('completed run relaunched worker')).run(run_id='fixed') == 0
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*.csv')} == before


def test_persistent_proxy_denial_stops_and_restart_cannot_refill_rounds(tmp_path):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=2, auto_recovery_cooldown=0.01)
    calls = []
    def failure(task):
        calls.append(task['index'])
        raise app.CrawlerError('proxy_access_denied', 'still forbidden')
    worker = make_worker(app, [], failure)
    for _ in range(2):
        with pytest.raises(app.CrawlerError) as error:
            app.Runner(cfg, worker_factory=worker).run(run_id='fixed')
        assert error.value.code == 'recovery_exhausted'
    assert calls == [0, 0, 0]
    state = app.read_json(tmp_path / '.runs/fixed/progress.json')
    assert len(state['current']['automatic_recoveries']) == 2
    assert state['last_error']['code'] == 'proxy_access_denied'
    assert not list(tmp_path.rglob('*.csv'))


def test_restart_during_cooldown_keeps_wait_and_does_not_grant_twice(tmp_path):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=2, auto_recovery_cooldown=0.01)
    def failure(task):
        raise app.CrawlerError('proxy_unavailable', 'outage')
    runner = app.Runner(cfg, worker_factory=make_worker(app, [], failure))
    original_wait = runner._wait_until
    def interrupt(value):
        if runner.control.get('status') == 'waiting':
            raise KeyboardInterrupt
        original_wait(value)
    runner._wait_until = interrupt
    with pytest.raises(KeyboardInterrupt):
        runner.run(run_id='fixed')
    before = app.read_json(runner.control_path)
    assert len(before['current']['automatic_recoveries']) == 1
    plan_before = (tmp_path / '.runs/fixed/run.json').read_bytes()
    assert app.Runner(cfg, worker_factory=successful_worker(app, [])).run(run_id='fixed') == 0
    receipt = next(app.read_json(p) for p in tmp_path.rglob('*.meta.json') if app.read_json(p)['task_index'] == 0)
    assert receipt['automatic_recoveries'] == before['current']['automatic_recoveries']
    assert (tmp_path / '.runs/fixed/run.json').read_bytes() == plan_before


def test_manual_grant_is_resumable_if_process_stops_immediately_after_save(tmp_path):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=1, auto_recovery_cooldown=0.01)
    def failure(task):
        raise app.CrawlerError('proxy_access_denied', 'persistent outage')
    runner = app.Runner(cfg, worker_factory=make_worker(app, [], failure))
    with pytest.raises(app.CrawlerError) as error:
        runner.run(run_id='fixed')
    assert error.value.code == 'recovery_exhausted'
    token = f"0:{runner.control['current']['attempt_count']}"
    assert runner._grant_manual_retry(token, 'proxy repaired')
    # No further command-loop mutations: emulate termination after the durable grant.
    assert app.Runner(cfg, worker_factory=successful_worker(app, [])).run(run_id='fixed') == 0
    receipt = next(app.read_json(p) for p in tmp_path.rglob('*.meta.json') if app.read_json(p)['task_index'] == 0)
    assert len(receipt['automatic_recoveries']) == 1
    assert receipt['manual_recoveries'][0]['new_recovery_rounds'] == 2


@pytest.mark.parametrize('at_limit', [False, True])
def test_restart_between_failure_and_recovery_decision_cannot_bypass_policy(tmp_path, at_limit):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=1, auto_recovery_cooldown=0.01)
    def failure(task):
        raise app.CrawlerError('proxy_access_denied', 'proxy refused')
    runner = app.Runner(cfg, worker_factory=make_worker(app, [], failure))
    schedule = runner._schedule_recovery
    def interrupted_decision(code):
        count = len(runner.control['current'].get('automatic_recoveries', []))
        if not at_limit or count == 1:
            raise KeyboardInterrupt
        schedule(code)
    runner._schedule_recovery = interrupted_decision
    with pytest.raises(KeyboardInterrupt):
        runner.run(run_id='fixed')
    if at_limit:
        def forbidden(*args, **kwargs):
            pytest.fail('No browser operation is allowed after the durable round limit')
        with pytest.raises(app.CrawlerError) as error:
            app.Runner(cfg, worker_factory=forbidden).run(run_id='fixed')
        assert error.value.code == 'recovery_exhausted'
    else:
        assert app.Runner(cfg, worker_factory=successful_worker(app, [])).run(run_id='fixed') == 0
        receipt = next(app.read_json(p) for p in tmp_path.rglob('*.meta.json') if app.read_json(p)['task_index'] == 0)
        assert len(receipt['automatic_recoveries']) == 1


def test_protocol_failure_is_not_automatically_retried(tmp_path):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=2, auto_recovery_cooldown=0.01)
    calls = []
    def failure(task):
        calls.append(task['index'])
        raise app.CrawlerError('protocol_changed', 'unrecognized contract')
    with pytest.raises(app.CrawlerError) as error:
        app.Runner(cfg, worker_factory=make_worker(app, [], failure)).run(run_id='fixed')
    assert error.value.code == 'protocol_changed' and calls == [0]


def test_task_preparation_proxy_failure_uses_same_persistent_recovery(tmp_path):
    app = app_module()
    cfg = dict(config(app, tmp_path), auto_recovery_rounds=1, auto_recovery_cooldown=0.01)
    runner = app.Runner(cfg, worker_factory=successful_worker(app, []))
    prepare = runner._prepare_account
    failed = []
    def prepare_with_outage():
        if runner.control.get('current') and not failed:
            failed.append(True)
            raise app.CrawlerError('proxy_unavailable', 'proxy failed before collection')
        prepare()
    runner._prepare_account = prepare_with_outage
    assert runner.run(run_id='fixed') == 0
    assert failed
    receipt = next(app.read_json(p) for p in tmp_path.rglob('*.meta.json') if app.read_json(p)['task_index'] == 0)
    assert receipt['automatic_recoveries'][0]['error_code'] == 'proxy_unavailable'


def test_proxy_failure_during_auth_does_not_exhaust_the_account(tmp_path):
    app = app_module()
    accounts = [{'alias': 'one', 'username': 'fixture-user', 'password': 'fixture-password'}]
    cfg = dict(config(app, tmp_path, accounts), auto_recovery_rounds=1, auto_recovery_cooldown=0.01)
    app.FileStore(tmp_path, 'fixed').create({'run_id': 'fixed',
        'cities': [{'name': 'A', 'code': 'AAA'}, {'name': 'B', 'code': 'BBB'}],
        'dates': ['2099-01-01'], 'options': {'direct_flight': True}, 'config': app.public_config(cfg)})
    base = successful_worker(app, [])
    auth_calls = []
    class Worker(base):
        def call(self, kind, value, **kw):
            if kind == 'auth':
                auth_calls.append(value['account']['alias'])
                if len(auth_calls) == 1:
                    raise app.CrawlerError('proxy_access_denied', 'CONNECT refused')
            return super().call(kind, value, **kw)
    runner = app.Runner(cfg, worker_factory=Worker)
    assert runner.run(run_id='fixed') == 0
    assert auth_calls == ['one', 'one']
    assert runner.pool.record('one')['state'] == 'ready'
    receipt = next(app.read_json(p) for p in tmp_path.rglob('*.meta.json') if app.read_json(p)['task_index'] == 0)
    assert receipt['automatic_recoveries'][0]['error_code'] == 'proxy_access_denied'


def test_sigterm_records_requested_exit_and_restores_handlers(tmp_path, monkeypatch, capsys):
    app = app_module()
    monkeypatch.setattr(app, 'check_environment', lambda: None)
    monkeypatch.setattr(app, 'browser_environment_check', lambda: None, raising=False)
    original = signal.getsignal(signal.SIGTERM)
    def run(runner, **kw):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        runner._check_stop()
    monkeypatch.setattr(app.Runner, 'run', run)
    assert app.main(['--anonymous', '--output', str(tmp_path), '--run-id', 'fixed']) == 143
    assert signal.getsignal(signal.SIGTERM) == original


@pytest.mark.parametrize('code,expected', [('worker_error', 4), ('recovery_exhausted', 3), ('browser_missing', 2)])
def test_service_does_not_restart_known_fatal_failures(monkeypatch, code, expected):
    app = app_module()
    def fail(argv):
        raise app.CrawlerError(code, 'failure requiring intervention')
    monkeypatch.setattr(app, 'command_line', fail)
    assert app.main([]) == expected


def test_legacy_plan_recovery_defaults_do_not_follow_edited_script_defaults(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    saved = app.public_config(cfg)
    saved.pop('auto_recovery_rounds')
    saved.pop('auto_recovery_cooldown')
    store = app.FileStore(tmp_path, 'legacy')
    store.create({'run_id': 'legacy', 'cities': [], 'dates': [], 'options': {}, 'config': saved})
    before = store.plan_path.read_bytes()
    app.DEFAULT_CONFIG.update(auto_recovery_rounds=0, auto_recovery_cooldown=1)
    restored = app._resume_cli_config(dict(cfg, auto_recovery_rounds=0), 'legacy', tmp_path)
    runner = app.Runner(restored)
    runner.pool = app.AccountPool(tmp_path, [])
    runner._resume('legacy')
    assert runner.config['auto_recovery_rounds'] == 12
    assert runner.config['auto_recovery_cooldown'] == 300
    assert store.plan_path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('auto_recovery_rounds', -1), ('auto_recovery_rounds', True), ('auto_recovery_cooldown', 0)])
def test_recovery_config_rejects_unbounded_or_invalid_settings(tmp_path, field, value):
    app = app_module()
    cfg = dict(config(app, tmp_path), **{field: value})
    with pytest.raises(app.CrawlerError):
        app.validate_config(cfg)
