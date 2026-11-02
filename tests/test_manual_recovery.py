"""Explicit retry grants preserve exhausted budgets and publication evidence."""
import copy

import pytest

from section_loader import load_sections


@pytest.fixture
def app():
    return load_sections('imports', 'config', 'files', 'processing', 'worker', 'main')


@pytest.fixture
def make_runner(app, tmp_path):
    def create(account_count=1, exhausted_by='attempts'):
        accounts = [{'alias': f'a{i}', 'username': f'local-user-{i}', 'password': f'local-password-{i}'}
                    for i in range(account_count)]
        cfg = dict(app.DEFAULT_CONFIG, output=str(tmp_path), accounts=accounts,
                   login_allowed=bool(accounts), disk_reserve_mb=0,
                   proxy={'server': 'http://local-proxy:3128', 'username': 'proxy-user', 'password': 'proxy-secret'})
        store = app.FileStore(tmp_path, 'manual')
        plan = {'run_id': 'manual', 'cities': [{'name': '上海', 'code': 'SHA'}, {'name': '香港', 'code': 'HKG'}],
                'dates': ['2099-01-01'], 'options': {'direct_flight': True}, 'config': app.public_config(cfg)}
        progress = store.create(plan)
        progress.update(app.new_task_control(0, account_count or 1))
        current = progress['current']
        current.update(attempt_count=current['max_attempts'] if exhausted_by == 'attempts' else 1,
                       work_used=49.25 if exhausted_by == 'attempts' else cfg['task_budget'],
                       attempt_id='previous-attempt', worker_generation=1, auth_used=12.0)
        progress.update(status='paused', pause_reason='task_exhausted',
                        auth={'used': 42.0, 'reserved': 0.0}, auth_used_aliases=['a0'],
                        blocked_accounts={'a0': {'reason': 'challenge'}},
                        server_not_before='2098-12-31T00:00:00+00:00',
                        last_error={'code': 'browser_error', 'task_index': 0})
        store.save_progress(progress)
        runner = app.Runner(cfg)
        runner.store, runner.control_path, runner.control = store, store.progress_path, progress
        return runner, store, plan
    return create


def observation():
    return {'rows': [{'航班号': 'LOCAL100', '票价': '100'}], 'columns': ['航班号', '票价'],
            'observed_at': '2098-12-31T20:00:00+00:00', 'status': 'succeeded',
            'price_status': 'complete', 'price_reason': [], 'comfort_status': 'not_requested'}


@pytest.mark.parametrize('account_count', [0, 1, 4])
def test_grant_adds_finite_caps_once_without_resetting_existing_state(app, make_runner, monkeypatch, account_count):
    runner, store, _ = make_runner(account_count=account_count)
    original = copy.deepcopy(runner.control)
    original_plan = store.plan_path.read_bytes()
    saves = []
    real_save = runner.save
    monkeypatch.setattr(runner, 'save', lambda: (saves.append(True), real_save())[-1])
    old = original['current']
    token = f"0:{old['attempt_count']}"

    assert runner._grant_manual_retry(token, 'operator reviewed the failure') is True

    _, restored = store.load()
    current = restored['current']
    assert len(saves) == 1
    assert current['max_attempts'] == old['max_attempts'] + max(3, account_count or 1)
    assert current['extra_work_budget'] == runner.config['task_budget']
    assert runner._task_budget() == runner.config['task_budget'] * 2
    assert {key: current[key] for key in old if key != 'max_attempts'} == {
        key: old[key] for key in old if key != 'max_attempts'}
    assert {key: restored[key] for key in original if key != 'current'} == {
        key: original[key] for key in original if key != 'current'}
    record = current['manual_recoveries'][0]
    assert record['token'] == token and record['reason'] == 'operator reviewed the failure'
    assert record['attempt_count'] == old['attempt_count'] and record['work_used'] == old['work_used']
    assert record['auth_used'] == original['auth']['used']
    assert (record['old_max_attempts'], record['new_max_attempts']) == (old['max_attempts'], current['max_attempts'])
    assert (record['old_task_budget'], record['new_task_budget']) == (300, 600)
    assert app.parse_utc(record['at']).tzinfo is not None
    assert store.plan_path.read_bytes() == original_plan


def test_time_exhaustion_can_receive_a_grant_and_later_grants_accumulate(app, make_runner):
    runner, _, _ = make_runner(exhausted_by='work')
    assert runner._grant_manual_retry('0:1', 'time exhausted') is True
    first_record = copy.deepcopy(runner.control['current']['manual_recoveries'][0])
    runner.control['current'].update(attempt_count=4, work_used=600)
    runner.save()
    assert runner._grant_manual_retry('0:4', 'additional reviewed recovery') is True
    current = runner.control['current']
    assert current['attempt_count'] == 4 and current['work_used'] == 600
    assert current['extra_work_budget'] == 600 and runner._task_budget() == 900
    assert current['max_attempts'] == 9
    assert len(current['manual_recoveries']) == 2 and current['manual_recoveries'][0] == first_record


def test_repeated_token_is_an_idempotent_no_write_after_reload(app, make_runner, monkeypatch):
    runner, store, _ = make_runner()
    runner._grant_manual_retry('0:3', 'reviewed')
    persisted = store.progress_path.read_bytes()
    _, runner.control = store.load()
    monkeypatch.setattr(runner, 'save', lambda: pytest.fail('An idempotent grant must not save again'))
    assert runner._grant_manual_retry('0:3', 'same operation retried') is False
    assert store.progress_path.read_bytes() == persisted
    assert len(runner.control['current']['manual_recoveries']) == 1


def test_old_token_is_rejected_after_a_new_attempt_is_dispatched(app, make_runner):
    runner, store, _ = make_runner()
    runner._grant_manual_retry('0:3', 'reviewed')
    app.reserve_attempt(runner.control, 'a0', 2, 90, runner._task_budget())
    app.settle_attempt(runner.control, 1)
    runner.control['current']['work_used'] = runner._task_budget()
    runner.save()
    before = store.progress_path.read_bytes()
    with pytest.raises(app.CrawlerError) as error:
        runner._grant_manual_retry('0:3', 'stale instruction')
    assert error.value.code == 'state_invalid'
    assert store.progress_path.read_bytes() == before


@pytest.mark.parametrize('token,reason,code', [
    ('bad', 'reviewed', 'config_invalid'), ('0:3:4', 'reviewed', 'config_invalid'),
    ('1:3', 'reviewed', 'state_invalid'), ('0:2', 'reviewed', 'state_invalid'),
    ('0:3', '', 'config_invalid'), ('0:3', '  ', 'config_invalid'),
    ('0:3', 'x' * 257, 'config_invalid'), ('0:3', None, 'config_invalid'),
])
def test_invalid_authorization_does_not_change_the_checkpoint(app, make_runner, token, reason, code):
    runner, store, _ = make_runner()
    before = store.progress_path.read_bytes()
    with pytest.raises(app.CrawlerError) as error:
        runner._grant_manual_retry(token, reason)
    assert error.value.code == code
    assert store.progress_path.read_bytes() == before


@pytest.mark.parametrize('invalid_state', ['budget_remaining', 'different_current', 'no_current', 'staging', 'publish_intent'])
def test_grant_requires_the_exhausted_current_task_without_publication(app, make_runner, invalid_state):
    runner, store, _ = make_runner()
    if invalid_state == 'budget_remaining':
        runner.control['current']['attempt_count'] = 2
    elif invalid_state == 'different_current':
        runner.control['current']['index'] = 1
    elif invalid_state == 'no_current':
        runner.control['current'] = None
    else:
        runner.control[invalid_state] = {'attempt_id': 'previous-attempt'}
    runner.save()
    before = store.progress_path.read_bytes()
    token = '0:2' if invalid_state == 'budget_remaining' else '0:3'
    with pytest.raises(app.CrawlerError) as error:
        runner._grant_manual_retry(token, 'reviewed')
    assert error.value.code == 'state_invalid'
    assert store.progress_path.read_bytes() == before


def test_failed_atomic_grant_restores_memory_and_can_be_retried(app, make_runner, monkeypatch):
    runner, store, _ = make_runner()
    previous_control = runner.control
    before = store.progress_path.read_bytes()
    real_save = runner.save
    monkeypatch.setattr(runner, 'save', lambda: (_ for _ in ()).throw(app.CrawlerError('storage_error', 'injected save failure')))
    with pytest.raises(app.CrawlerError, match='injected'):
        runner._grant_manual_retry('0:3', 'reviewed')
    assert runner.control is previous_control
    assert 'manual_recoveries' not in runner.control['current']
    assert store.progress_path.read_bytes() == before
    monkeypatch.setattr(runner, 'save', real_save)
    assert runner._grant_manual_retry('0:3', 'reviewed') is True


def test_recovery_reason_is_redacted_before_persistence(app, make_runner):
    runner, store, _ = make_runner()
    runner._grant_manual_retry('0:3', 'reviewed proxy-secret local-password-0 https://user:pass@local-proxy/private?token=x')
    raw = store.progress_path.read_text(encoding='utf-8')
    reason = runner.control['current']['manual_recoveries'][0]['reason']
    assert 'proxy-secret' not in raw and 'local-password-0' not in raw and 'user:pass@' not in raw
    assert 'reviewed' in reason and len(reason) <= 256


def interrupt_after_meta(app, runner, store, plan, monkeypatch):
    task = next(app.iter_tasks(plan))
    real_save = store.save_progress
    def fail_checkpoint(progress):
        if progress['cursor'] == 1:
            raise app.CrawlerError('storage_error', 'injected checkpoint interruption')
        return real_save(progress)
    monkeypatch.setattr(store, 'save_progress', fail_checkpoint)
    with pytest.raises(app.CrawlerError, match='injected'):
        store.publish(task, observation(), runner.control)
    monkeypatch.setattr(store, 'save_progress', real_save)
    _, restored = store.load()
    return restored, next(store.output.rglob('*.meta.json'))


def test_manual_recovery_evidence_survives_publish_interruption_and_counts_once(app, make_runner, monkeypatch):
    runner, store, plan = make_runner()
    runner._grant_manual_retry('0:3', 'reviewed')
    records = copy.deepcopy(runner.control['current']['manual_recoveries'])
    app.reserve_attempt(runner.control, 'a0', 2, 90, runner._task_budget())
    app.settle_attempt(runner.control, 1)
    runner.save()
    restored, receipt_path = interrupt_after_meta(app, runner, store, plan, monkeypatch)
    assert restored['publish_intent']['manual_recoveries'] == records
    assert app.read_json(receipt_path)['manual_recoveries'] == records
    assert store.recover(restored) is True
    assert restored['cursor'] == 1 and restored['stats']['succeeded'] == 1
    assert store.recover(restored) is False
    assert restored['stats']['succeeded'] == 1


@pytest.mark.parametrize('tamper', ['intent', 'receipt'])
def test_recovery_rejects_changed_manual_evidence(app, make_runner, monkeypatch, tamper):
    runner, store, plan = make_runner()
    runner._grant_manual_retry('0:3', 'reviewed')
    restored, receipt_path = interrupt_after_meta(app, runner, store, plan, monkeypatch)
    if tamper == 'intent':
        restored['publish_intent']['manual_recoveries'] = None
        store.save_progress(restored)
    else:
        receipt = app.read_json(receipt_path)
        receipt['manual_recoveries'] = None
        app.atomic_json(receipt_path, receipt)
    with pytest.raises(app.CrawlerError):
        store.recover(restored)
    assert restored['cursor'] == 0 and restored['stats']['succeeded'] == 0


def test_old_publication_without_manual_fields_still_recovers(app, make_runner, monkeypatch):
    runner, store, plan = make_runner()
    restored, receipt_path = interrupt_after_meta(app, runner, store, plan, monkeypatch)
    restored['publish_intent'].pop('manual_recoveries', None)
    store.save_progress(restored)
    receipt = app.read_json(receipt_path)
    receipt.pop('manual_recoveries', None)
    app.atomic_json(receipt_path, receipt)
    assert store.recover(restored) is True
    assert restored['cursor'] == 1
