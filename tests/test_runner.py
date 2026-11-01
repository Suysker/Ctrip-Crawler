from pathlib import Path
import time
import os
import subprocess
import sys
import pytest
from section_loader import load_sections


def app_module():
    return load_sections('imports', 'config', 'files', 'processing', 'worker', 'main')


def config(app, tmp_path, accounts=None):
    return dict(app.DEFAULT_CONFIG, output=str(tmp_path), accounts=accounts or [],
                login_allowed=bool(accounts), crawl_citys=['上海', '香港'],
                begin_date='2099-01-01', crawl_days=1, crawl_interval=0,
                retry_delays=[0, 0], disk_reserve_mb=0)


def test_finished_resume_does_not_create_browser(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    plan = {'run_id': 'done', 'cities': [{'name': '上海', 'code': 'SHA'},
            {'name': '香港', 'code': 'HKG'}], 'dates': ['2099-01-01'],
            'options': {'direct_flight': True}, 'config': app.public_config(cfg)}
    store = app.FileStore(tmp_path, 'done')
    progress = store.create(plan)
    progress['cursor'] = 2
    store.save_progress(progress)
    def forbidden(*args, **kwargs):
        raise AssertionError('Completed run must not launch a browser')
    assert app.Runner(cfg, worker_factory=forbidden).run(resume='done') == 0


@pytest.mark.parametrize('stop_kind', ['start', 'startup_failure', 'auth', 'resolve', 'collect_challenge'])
def test_graceful_stop_finishes_current_operation_without_dispatching_another(tmp_path, stop_kind):
    app = app_module()
    accounts = ([{'alias': alias, 'username': 'fixture-' + alias, 'password': 'fixture'}
                 for alias in ('one', 'two')] if stop_kind in ('auth', 'collect_challenge') else [])
    cfg = config(app, tmp_path, accounts)
    events = []
    holder = {}

    class Worker:
        def __init__(self, cfg, identity, state, **kw):
            self.alias = kw['alias']
            self.identity = identity or {'fixture': self.alias or 'anonymous'}
            self.profile, self.session = kw['on_profile'], kw['on_session']

        def start(self, timeout=None):
            events.append(('start', self.alias, holder['runner'].stop_requested))
            self.profile(self.identity)
            if stop_kind in ('start', 'startup_failure'):
                holder['runner'].stop_requested = True
                if stop_kind == 'startup_failure':
                    raise app.CrawlerError('browser_startup', 'fixture startup failure')

        def close(self):
            pass

        def call(self, kind, value, **unused):
            events.append((kind, self.alias, holder['runner'].stop_requested))
            if kind == 'auth':
                self.session({'identity': self.identity, 'state': {'cookies': []}})
                if stop_kind == 'auth':
                    holder['runner'].stop_requested = True
                return {'authenticated': True}
            if kind == 'resolve':
                if stop_kind == 'resolve':
                    holder['runner'].stop_requested = True
                return [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}]
            if stop_kind == 'collect_challenge' and self.alias == 'one':
                holder['runner'].stop_requested = True
                raise app.CrawlerError('challenge', 'fixture challenge')
            return {'rows': [{'航班号': 'TEST100'}], 'status': 'succeeded',
                    'observed_at': '2098-12-31T20:00:00+00:00', 'comfort_status': 'not_requested',
                    'price_status': 'complete', 'price_reason': []}

    runner = app.Runner(cfg, worker_factory=Worker)
    holder['runner'] = runner
    with pytest.raises(KeyboardInterrupt):
        runner.run()
    assert not [event for event in events if event[2]], events
    assert not list(tmp_path.rglob('*.csv'))


def test_account_challenge_switches_without_changing_task(tmp_path):
    app = app_module()
    accounts = [{'alias': 'one', 'username': 'user-one', 'password': 'dummy'},
                {'alias': 'two', 'username': 'user-two', 'password': 'dummy'}]
    cfg = config(app, tmp_path, accounts)
    attempts = []

    class FakeWorker:
        def __init__(self, cfg, identity, state, **kw):
            self.alias = kw['alias']
            self.profile = kw['on_profile']
            self.session = kw['on_session']
            self.generation = kw['generation']
        def start(self, timeout=None):
            self.profile({'fixture': self.alias})
        def close(self):
            pass
        def call(self, kind, value, **kw):
            if kind == 'resolve':
                return [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}]
            if kind == 'auth':
                self.session({'identity': {'fixture': self.alias}, 'state': {'cookies': []}})
                return {'authenticated': True}
            task = value['task']
            attempts.append((task['key'], self.alias))
            if self.alias == 'one':
                raise app.CrawlerError('needs_auth', 'verification required')
            return {'rows': [{'航班号': 'TEST100'}], 'status': 'succeeded',
                    'observed_at': '2098-12-31T20:00:00+00:00', 'comfort_status': 'not_requested', 'price_status': 'complete', 'price_reason': []}

    assert app.Runner(cfg, worker_factory=FakeWorker).run() == 0
    assert attempts[0][0] == attempts[1][0]
    assert attempts[0][1] == 'one' and attempts[1][1] == 'two'
    pool = app.AccountPool(tmp_path, accounts)
    assert pool.record('one')['state'] == 'cooldown'
    assert len(list(tmp_path.rglob('*.csv'))) == 2


def test_exhausted_task_is_not_reset_on_resume(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    plan = {'run_id': 'failed', 'cities': [{'name': '上海', 'code': 'SHA'},
            {'name': '香港', 'code': 'HKG'}], 'dates': ['2099-01-01'],
            'options': {'direct_flight': True}, 'config': app.public_config(cfg)}
    store = app.FileStore(tmp_path, 'failed')
    progress = store.create(plan)
    progress.update(app.new_task_control(0, 1))
    progress['current']['attempt_count'] = 3
    store.save_progress(progress)
    def forbidden(*args, **kwargs):
        raise AssertionError('No remaining budget must not launch a browser')
    with pytest.raises(app.CrawlerError) as error:
        app.Runner(cfg, worker_factory=forbidden).run(resume='failed')
    assert error.value.code == 'task_exhausted'


def test_authentication_challenges_rotate_through_more_than_three_accounts(tmp_path):
    app = app_module()
    accounts = [{'alias': f'a{i}', 'username': f'user-{i}', 'password': 'fixture'} for i in range(4)]
    cfg = config(app, tmp_path, accounts)
    authenticated = []

    class Worker:
        def __init__(self, cfg, identity, state, **kw):
            self.alias = kw['alias']
            self.profile, self.session = kw['on_profile'], kw['on_session']
        def start(self, timeout=None):
            self.profile({'fixture': self.alias})
        def close(self):
            pass
        def call(self, kind, value, **kw):
            if kind == 'auth':
                authenticated.append(self.alias)
                if self.alias != 'a3':
                    raise app.CrawlerError('challenge', 'manual SMS required')
                self.session({'identity': {'fixture': self.alias}, 'state': {'cookies': []}})
                return {'authenticated': True}
            if kind == 'resolve':
                return [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}]
            return {'rows': [{'航班号': 'TEST100'}], 'status': 'succeeded',
                    'observed_at': '2098-12-31T20:00:00+00:00', 'comfort_status': 'not_requested', 'price_status': 'complete', 'price_reason': []}

    assert app.Runner(cfg, worker_factory=Worker).run() == 0
    assert authenticated == ['a0', 'a1', 'a2', 'a3']
    pool = app.AccountPool(tmp_path, accounts)
    assert all(pool.record(f'a{i}')['state'] == 'cooldown' for i in range(3))
    assert pool.record('a3')['state'] == 'ready'


def successful_worker(app, events, *, fail_close=False, rate_limits=(), on_collect=None):
    limits = list(rate_limits)

    class FakeWorker:
        def __init__(self, cfg, identity, state, **kw):
            self.identity = identity or {'fixture': 'stable'}
            self.profile = kw['on_profile']
            self.session = kw['on_session']

        def start(self, timeout=None):
            self.profile(self.identity)

        def close(self):
            if fail_close:
                raise app.CrawlerError('process_cleanup', 'cleanup failed after commit')

        def call(self, kind, value, **unused):
            events.append(kind)
            if kind == 'resolve':
                return [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}]
            if kind == 'auth':
                self.session({'identity': self.identity, 'state': {'cookies': []}})
                return {'authenticated': True}
            if on_collect:
                on_collect()
            if limits:
                raise app.CrawlerError('rate_limited', 'site waiting required', retry_after=limits.pop(0))
            return {'rows': [{'航班号': 'TEST100'}], 'status': 'succeeded',
                    'observed_at': '2098-12-31T20:00:00+00:00', 'comfort_status': 'not_requested', 'price_status': 'complete', 'price_reason': []}

    return FakeWorker


def test_startup_retry_reloads_identity_that_was_saved_before_failure(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    received = []

    class StartupWorker:
        def __init__(self, cfg, identity, state, **kw):
            received.append(identity)
            self.identity = identity
            self.profile = kw['on_profile']

        def start(self, timeout=None):
            self.profile(self.identity or {'fixture': len(received)})
            if len(received) == 1:
                raise app.CrawlerError('browser_startup', 'temporary startup problem')

        def close(self):
            pass

    runner = app.Runner(cfg, worker_factory=StartupWorker)
    runner.control_path = tmp_path / 'control.json'
    runner.pool = app.AccountPool(tmp_path, [])
    try:
        runner._ensure_worker(None)
        assert received == [None, {'fixture': 1}]
    finally:
        runner._close_worker()


def test_cleanup_failure_after_commit_does_not_reenter_attempt_settlement(tmp_path):
    app = app_module()
    cfg = dict(config(app, tmp_path), recycle_tasks=1)
    events = []
    worker = successful_worker(app, events, fail_close=True)
    with pytest.raises(app.CrawlerError) as error:
        app.Runner(cfg, worker_factory=worker).run()
    assert error.value.code == 'process_cleanup'
    progress = app.read_json(next(tmp_path.rglob('progress.json')))
    assert progress['cursor'] == 1
    assert progress['current'] is None
    assert len(list(tmp_path.rglob('*.csv'))) == 1
    assert events.count('collect') == 1


@pytest.mark.parametrize('operation', ['new', 'login', 'resume'])
def test_output_site_wait_cannot_be_bypassed_by_another_operation(tmp_path, operation):
    app = app_module()
    accounts = [{'alias': 'one', 'username': 'one', 'password': 'dummy'}]
    cfg = config(app, tmp_path, accounts)
    if operation == 'resume':
        app.FileStore(tmp_path, 'waiting').create({'run_id': 'waiting',
            'cities': [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}],
            'dates': ['2099-01-01'], 'options': {'direct_flight': True}, 'config': app.public_config(cfg)})
    until = (app.datetime.now(app.timezone.utc) + app.timedelta(seconds=120)).isoformat()
    app.atomic_json(tmp_path / '.site.json', {'schema_version': app.SCHEMA_VERSION, 'server_not_before': until})

    def forbidden(*args, **kwargs):
        raise AssertionError('Site wait must prevent browser operations')

    with pytest.raises(app.CrawlerError) as error:
        app.Runner(cfg, worker_factory=forbidden).run(
            login='one' if operation == 'login' else None,
            resume='waiting' if operation == 'resume' else None)
    assert error.value.code == 'rate_limited'


def test_short_site_wait_retries_once_without_resetting_task_budget(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    events = []
    started = time.monotonic()
    runner = app.Runner(cfg, worker_factory=successful_worker(app, events, rate_limits=[0.04]))
    assert runner.run() == 0
    assert time.monotonic() - started < 3
    assert events.count('collect') == 3
    assert runner.control['rate_limit_recoveries'] == 1
    assert (tmp_path / '.site.json').exists()


def test_second_site_wait_pauses_instead_of_recovering_again(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    events = []
    runner = app.Runner(cfg, worker_factory=successful_worker(app, events, rate_limits=[0.04, 0.04]))
    with pytest.raises(app.CrawlerError) as error:
        runner.run()
    assert error.value.code == 'rate_limited'
    assert events.count('collect') == 2
    assert runner.control['rate_limit_recoveries'] == 1
    assert runner.control['current']['attempt_count'] == 2


def test_interrupt_during_site_wait_saves_wait_and_stops_network(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    events = []
    runner = None

    def request_stop():
        runner.stop_requested = True

    runner = app.Runner(cfg, worker_factory=successful_worker(app, events, rate_limits=[1], on_collect=request_stop))
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        runner.run()
    assert time.monotonic() - started < 2
    assert events.count('collect') == 1
    state = app.read_json(tmp_path / '.site.json')
    assert app.parse_utc(state['server_not_before']) > app.datetime.now(app.timezone.utc)


def test_plan_publication_failure_keeps_recoverable_initialization_marker(tmp_path, monkeypatch):
    app = app_module()
    cfg = config(app, tmp_path)
    events = []
    original_save = app.FileStore.save_progress
    injected = False

    def fail_first_progress(store, progress):
        nonlocal injected
        if not injected:
            injected = True
            raise app.CrawlerError('storage_error', 'injected progress publication failure')
        return original_save(store, progress)

    monkeypatch.setattr(app.FileStore, 'save_progress', fail_first_progress)
    first = app.Runner(cfg, worker_factory=successful_worker(app, events))
    with pytest.raises(app.CrawlerError) as error:
        first.run()
    assert error.value.code == 'storage_error'
    plan_path = next(tmp_path.rglob('run.json'))
    initialization = app.read_json(plan_path.with_name('initialization.json'))
    assert initialization['phase'] == 'plan_complete'
    assert initialization['progress_initialized'] is False
    resumed = app.Runner(cfg, worker_factory=successful_worker(app, events))
    assert resumed.run(resume=plan_path.parent.name) == 0
    assert events.count('resolve') == 1
    assert len(list(tmp_path.rglob('*.csv'))) == 2


@pytest.mark.parametrize('mode_args,expected_headless', [([], False), (['--headless'], True)])
def test_resume_cli_loads_frozen_anonymous_config_before_validation(tmp_path, monkeypatch, mode_args, expected_headless):
    app = app_module()
    cfg = dict(config(app, tmp_path), headless=False)
    plan = {'run_id': 'anonymous', 'cities': [{'name': '上海', 'code': 'SHA'},
            {'name': '香港', 'code': 'HKG'}], 'dates': ['2099-01-01'],
            'options': {'direct_flight': True}, 'config': app.public_config(cfg)}
    app.FileStore(tmp_path, 'anonymous').create(plan)
    monkeypatch.setattr(app, 'check_environment', lambda: None)
    monkeypatch.setattr(app, 'browser_environment_check', lambda: None, raising=False)
    observed = []

    def capture_run(runner, **operation):
        observed.append(runner.config)
        assert operation['resume'] == 'anonymous'
        return 0

    monkeypatch.setattr(app.Runner, 'run', capture_run)
    assert app.command_line(['--resume', 'anonymous', '--output', str(tmp_path), *mode_args]) == 0
    assert observed[0]['login_allowed'] is False
    assert observed[0]['accounts'] == []
    assert observed[0]['begin_date'] == '2099-01-01'
    assert observed[0]['headless'] is expected_headless


def test_explicit_login_enables_manual_challenge_wait_even_with_switch_default(tmp_path):
    app = app_module()
    accounts = [{'alias': 'one', 'username': 'one', 'password': 'dummy'}]
    cfg = config(app, tmp_path, accounts)
    base = successful_worker(app, [])

    class LoginWorker(base):
        def __init__(self, worker_config, *args, **kwargs):
            assert worker_config['headless'] is False
            assert worker_config['challenge_policy'] == 'wait_then_switch'
            super().__init__(worker_config, *args, **kwargs)

    assert app.Runner(cfg, worker_factory=LoginWorker).run(login='one') == 0
    evidence = app.AccountPool(tmp_path, accounts).record('one')['last_explicit_login']
    assert evidence and evidence['auth_id']


def test_initialization_resume_allows_display_override_without_replanning(tmp_path):
    app = app_module()
    original = dict(config(app, tmp_path), headless=False)
    store = app.FileStore(tmp_path, 'initializing')
    app.atomic_json(store.initialization_path, {
        'schema_version': app.SCHEMA_VERSION, 'run_id': 'initializing', 'operation_id': 'op',
        'config': app.public_config(original), 'dates': ['2099-01-01'], 'phase': 'initializing',
        'resolve_attempts': 0, 'resolve_used': 0.0, 'resolve_reserved': 0.0,
        'blocked_accounts': {}, 'auth_used_aliases': [], 'account_ack': {},
    })
    cfg = dict(original, headless=True)
    runner = app.Runner(cfg, worker_factory=successful_worker(app, []))
    assert runner.run(resume='initializing') == 0
    assert app.read_json(store.plan_path)['dates'] == ['2099-01-01']


def completed_store(app, cfg, output):
    store = app.FileStore(output, 'leftover')
    progress = store.create({'run_id': 'leftover',
        'cities': [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}],
        'dates': ['2099-01-01'], 'options': {'direct_flight': True}, 'config': app.public_config(cfg)})
    progress['cursor'] = 2
    store.save_progress(progress)
    return store


def test_resume_reaps_only_recorded_live_process_with_matching_identity(tmp_path):
    import psutil
    app = app_module()
    cfg = config(app, tmp_path)
    store = completed_store(app, cfg, tmp_path)
    owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        app.atomic_json(store.run_dir / 'owner.json', {'owner_pid': os.getpid(),
            'processes': {str(owned.pid): psutil.Process(owned.pid).create_time()}})
        assert app.Runner(cfg).run(resume='leftover') == 0
        assert owned.wait(timeout=2) is not None
        assert unrelated.poll() is None
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_reused_pid_does_not_authorize_killing_current_process(tmp_path):
    import psutil
    app = app_module()
    cfg = config(app, tmp_path)
    store = completed_store(app, cfg, tmp_path)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        app.atomic_json(store.run_dir / 'owner.json', {'owner_pid': os.getpid(),
            'processes': {str(unrelated.pid): psutil.Process(unrelated.pid).create_time() - 1}})
        assert app.Runner(cfg).run(resume='leftover') == 0
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_owner_record_cannot_target_current_parent_or_bypass_inspection_denial(tmp_path, monkeypatch):
    import psutil
    app = app_module()
    cfg = config(app, tmp_path)
    store = completed_store(app, cfg, tmp_path)
    path = store.run_dir / 'owner.json'
    app.atomic_json(path, {'owner_pid': os.getpid(), 'processes': {str(os.getpid()): psutil.Process().create_time()}})
    with pytest.raises(app.CrawlerError) as error:
        app.Runner(cfg).run(resume='leftover')
    assert error.value.code == 'process_cleanup'
    inaccessible_pid = 123456789
    app.atomic_json(path, {'owner_pid': os.getpid(), 'processes': {str(inaccessible_pid): 1.0}})
    real_process = psutil.Process

    class AccessDeniedProcess(real_process):
        def __init__(self, pid=None):
            if pid == inaccessible_pid:
                raise psutil.AccessDenied(pid)
            super().__init__(pid)

    monkeypatch.setattr(psutil, 'Process', AccessDeniedProcess)
    with pytest.raises(app.CrawlerError) as error:
        app.Runner(cfg).run(resume='leftover')
    assert error.value.code == 'process_cleanup'


def test_explicit_login_reaps_previous_owned_process_before_browser_start(tmp_path):
    import psutil
    app = app_module()
    accounts = [{'alias': 'one', 'username': 'one', 'password': 'dummy'}]
    cfg = config(app, tmp_path, accounts)
    owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    path = tmp_path / 'sessions' / 'one' / 'owner.json'
    app.atomic_json(path, {'owner_pid': os.getpid(),
        'processes': {str(owned.pid): psutil.Process(owned.pid).create_time()}})
    base = successful_worker(app, [])

    class LoginWorker(base):
        def start(self, timeout=None):
            assert owned.poll() is not None
            super().start(timeout)

    try:
        assert app.Runner(cfg, worker_factory=LoginWorker).run(login='one') == 0
    finally:
        if owned.poll() is None:
            owned.kill()
        owned.wait(timeout=3)


def test_price_partial_tasks_advance_with_visible_quality_summary(tmp_path, caplog):
    app = app_module()
    cfg = config(app, tmp_path)
    base = successful_worker(app, [])

    class PartialPriceWorker(base):
        def call(self, kind, value, **kwargs):
            result = super().call(kind, value, **kwargs)
            if kind == 'collect':
                result.update(price_status='partial', price_reason=['currency_missing', 'tax_missing'])
            return result

    caplog.set_level(app.logging.INFO, logger='ctrip')
    runner = app.Runner(cfg, worker_factory=PartialPriceWorker)
    assert runner.run() == 0
    assert runner.control['cursor'] == 2
    assert runner.control['stats']['price_partial'] == 2
    assert runner.control['stats']['succeeded'] == 2
    assert sum('price_status=partial' in record.message for record in caplog.records) == 2
