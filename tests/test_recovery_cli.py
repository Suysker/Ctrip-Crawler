import pytest

from test_runner import app_module, config, successful_worker


def exhausted(app, cfg, path):
    store = app.FileStore(path, 'recover')
    progress = store.create({'run_id': 'recover', 'cities': [{'code': 'SHA', 'name': '上海'}, {'code': 'HKG', 'name': '香港'}],
                             'dates': ['2099-01-01'], 'options': {'direct_flight': True}, 'config': app.public_config(cfg)})
    progress.update(app.new_task_control(0, 1))
    progress['current'].update(attempt_count=3, work_used=49.25)
    progress.update(status='paused', pause_reason='task_exhausted')
    store.save_progress(progress)
    return store


def test_authorized_retry_resumes_original_plan_and_records_history_in_receipt(tmp_path):
    app = app_module()
    cfg = config(app, tmp_path)
    store = exhausted(app, cfg, tmp_path)
    plan_before = store.plan_path.read_bytes()
    calls = []
    base = successful_worker(app, calls)

    class Worker(base):
        def call(self, kind, value, **kw):
            if kind == 'collect' and value['task']['index'] == 0:
                progress = app.read_json(store.progress_path)
                assert progress['status'] == 'running'
                assert progress['current']['attempt_count'] == 4
                assert progress['current']['work_used'] >= 49.25
            return super().call(kind, value, **kw)

    assert app.Runner(cfg, worker_factory=Worker).run(
        resume='recover', retry_current='0:3', recovery_reason='parser corrected') == 0
    assert store.plan_path.read_bytes() == plan_before
    receipt = next(app.read_json(p) for p in tmp_path.glob('*/*/*.meta.json') if app.read_json(p)['task_index'] == 0)
    assert receipt['manual_recoveries'][0]['token'] == '0:3'
    assert app.read_json(store.progress_path)['cursor'] == 2
    assert calls.count('collect') == 2


@pytest.mark.parametrize('options', [
    {'retry_current': '0:3'},
    {'refresh_proxy_credentials': True},
    {'resume': 'recover', 'retry_current': '0:3'},
    {'resume': 'recover', 'refresh_proxy_credentials': True},
    {'resume': 'recover', 'recovery_reason': 'not an action'},
])
def test_recovery_options_require_resume_and_explicit_reason(tmp_path, options):
    app = app_module()
    runner = app.Runner(config(app, tmp_path))
    with pytest.raises(app.CrawlerError) as error:
        runner.run(**options)
    assert error.value.code == 'config_invalid'
    assert not list(tmp_path.iterdir())


def test_cli_forwards_explicit_recovery_without_using_new_run_defaults(tmp_path, monkeypatch):
    app = app_module()
    exhausted(app, config(app, tmp_path), tmp_path)
    monkeypatch.setattr(app, 'check_environment', lambda: None)
    monkeypatch.setattr(app, 'browser_environment_check', lambda: None, raising=False)
    received = []

    def run(runner, **options):
        received.append(options)
        return 0

    monkeypatch.setattr(app.Runner, 'run', run)
    assert app.command_line(['--resume', 'recover', '--output', str(tmp_path), '--retry-current', '0:3',
                             '--refresh-proxy-credentials', '--recovery-reason', 'verified proxy and parser']) == 0
    assert received[0]['retry_current'] == '0:3'
    assert received[0]['refresh_proxy_credentials'] is True
    assert received[0]['recovery_reason'] == 'verified proxy and parser'
