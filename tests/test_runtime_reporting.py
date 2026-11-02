import logging
import types
from datetime import datetime, timezone

import pytest

from test_runner import app_module, config, successful_worker


def test_cli_preflight_unexpected_error_retains_safe_diagnostic(monkeypatch, capsys):
    app = app_module()
    app.DEFAULT_CONFIG['accounts'] = [{'alias': 'example', 'username': 'private-login', 'password': 'private-password'}]

    def fail(argv):
        raise AttributeError('preflight failed for private-login private-password')

    monkeypatch.setattr(app, 'command_line', fail)
    assert app.main([]) == 4
    text = capsys.readouterr().err
    assert 'unexpected_error' in text and 'AttributeError' in text
    assert 'cli' in text and 'preflight failed' in text
    assert 'test_runtime_reporting.py' in text and 'fail' in text
    assert 'private-login' not in text and 'private-password' not in text


def test_log_timezone_is_explicit_and_independent_of_host_localtime(monkeypatch):
    app = app_module()
    record = logging.LogRecord('test', logging.WARNING, __file__, 1, 'same event', (), None)
    record.created = datetime(2026, 11, 1, 18, 24, 27, 609000, tzinfo=timezone.utc).timestamp()
    assert app.ProjectLogFormatter('Asia/Shanghai').format(record).startswith('2026-11-02T02:24:27.609+08:00 ')
    assert app.ProjectLogFormatter('UTC').format(record).startswith('2026-11-01T18:24:27.609+00:00 ')


@pytest.mark.parametrize('phase', ['startup', 'auth', 'collect'])
def test_diagnostics_survive_pause_and_retry_without_leaking_credentials(tmp_path, caplog, phase):
    app = app_module()
    accounts = [{'alias': 'one', 'username': 'sample-login', 'password': 'sample-login-password'}] if phase == 'auth' else []
    cfg = dict(config(app, tmp_path, accounts), proxy={
        'server': 'http://proxy.invalid:3128', 'username': 'sample-proxy-user', 'password': 'sample-proxy-password'})
    base = successful_worker(app, [])

    def failure():
        return app.CrawlerError('browser_startup' if phase == 'startup' else 'browser_error', 'Error', diagnostic={
            'exception_type': 'Error', 'stage': 'browser.' + phase,
            'message': 'ERR_PROXY_CONNECTION_FAILED http://sample-proxy-user:sample-proxy-password@proxy.invalid:3128/path?token=hidden',
            'frames': [{'file': 'browser.py.inc', 'line': 10, 'function': phase}]})

    class Worker(base):
        def start(self, timeout=None):
            super().start(timeout)
            if phase == 'startup':
                raise failure()

        def call(self, kind, value, **kw):
            if kind == phase:
                raise failure()
            return super().call(kind, value, **kw)

    runner = app.Runner(cfg, worker_factory=Worker)
    with pytest.raises(app.CrawlerError):
        runner.run()
    persisted = app.read_json(runner.control_path)
    diagnostic = persisted['last_error']['diagnostic']
    assert diagnostic['exception_type'] == 'Error'
    assert diagnostic['stage'] == 'browser.' + phase
    assert 'ERR_PROXY_CONNECTION_FAILED' in diagnostic['message']
    assert diagnostic['frames'][0]['function'] == phase
    assert 'ERR_PROXY_CONNECTION_FAILED' in caplog.text
    combined = app.json.dumps(persisted) + caplog.text
    assert 'sample-proxy-password' not in combined
    assert 'sample-proxy-user' not in combined
    assert 'token=hidden' not in combined
    assert persisted['pause_diagnostic']['exception_type']


def test_final_failed_attempt_does_not_reserve_an_unused_backoff(tmp_path, monkeypatch):
    app = app_module()
    monkeypatch.setattr(app, 'time', types.SimpleNamespace(monotonic=lambda: 100.0))
    cfg = dict(config(app, tmp_path), retry_delays=[2, 7])

    def fail():
        raise app.CrawlerError('network_error', 'temporary fixture failure')

    runner = app.Runner(cfg, worker_factory=successful_worker(app, [], on_collect=fail))
    monkeypatch.setattr(runner, '_wait_until', lambda value: None)
    with pytest.raises(app.CrawlerError) as caught:
        runner.run()
    assert caught.value.code == 'task_exhausted'
    assert runner.control['current']['attempt_count'] == 3
    assert runner.control['current']['work_used'] == 9


def test_legacy_resume_retains_original_comfort_window(tmp_path):
    app = app_module()
    original = config(app, tmp_path)
    frozen = app.public_config(original)
    frozen.pop('comfort_timeout')
    store = app.FileStore(tmp_path, 'legacy')
    store.create({'run_id': 'legacy', 'cities': [{'code': 'SHA', 'name': '上海'}, {'code': 'HKG', 'name': '香港'}],
                  'dates': ['2099-01-01'], 'options': {'direct_flight': True}, 'config': frozen})
    loaded = app._resume_cli_config(dict(original, comfort_timeout=90), 'legacy', tmp_path)
    assert loaded['comfort_timeout'] == 15
