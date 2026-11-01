import datetime as dt
import pytest
from section_loader import load_sections


@pytest.fixture
def app():
    return load_sections('imports', 'config')


def test_dates_freeze_today_and_cross_year(app):
    cfg = dict(app.DEFAULT_CONFIG, begin_date=None, end_date=None,
               start_interval=0, crawl_days=3, days_interval=1)
    assert app.flight_dates(cfg, dt.date(2026, 12, 31)) == [
        '2026-12-31', '2027-01-01', '2027-01-02']


def test_end_date_controls_span_and_step(app):
    cfg = dict(app.DEFAULT_CONFIG, begin_date='2027-01-01',
               end_date='2027-01-06', crawl_days=2, days_interval=2)
    assert app.flight_dates(cfg) == ['2027-01-01', '2027-01-03', '2027-01-05']


def test_invalid_date_range_is_not_empty_success(app):
    cfg = dict(app.DEFAULT_CONFIG, begin_date='2027-01-05', end_date='2027-01-01')
    with pytest.raises(app.CrawlerError, match='结束日期'):
        app.flight_dates(cfg)


def test_task_order_and_identity_are_repeatable(app):
    plan = {'run_id': 'r', 'cities': [{'name': '上海', 'code': 'SHA'},
            {'name': '中国香港', 'code': 'HKG'}], 'dates': ['2027-01-01', '2027-01-02'],
            'options': {'direct_flight': True}}
    tasks = list(app.iter_tasks(plan))
    assert len(tasks) == 4
    assert [t['index'] for t in tasks] == list(range(4))
    assert tasks[0]['origin']['code'] == 'SHA'
    assert tasks[2]['origin']['code'] == 'HKG'
    assert tasks == list(app.iter_tasks(plan))
    assert tasks[0]['key'] != tasks[1]['key']


def test_pool_budget_allows_four_accounts_without_resetting(app):
    progress = app.new_task_control(0, 4)
    for i in range(3):
        app.reserve_attempt(progress, 'a' + str(i), i, 30)
        app.settle_attempt(progress, 2)
    app.reserve_attempt(progress, 'a3', 3, 30)
    assert progress['current']['attempt_count'] == 4
    with pytest.raises(app.CrawlerError):
        app.reserve_attempt(progress, 'a0', 4, 30)


def test_crash_consumes_reserved_time_once(app):
    p = app.new_task_control(0, 2)
    app.reserve_attempt(p, 'a', 1, 90)
    app.recover_attempt_budget(p)
    assert p['current']['work_used'] == 90
    app.recover_attempt_budget(p)
    assert p['current']['work_used'] == 90


@pytest.mark.parametrize('key,value', [
    ('crawl_days', 1.5), ('days_interval', 1.5), ('start_interval', True),
    ('max_rows', -1), ('max_response_bytes', 0), ('max_result_bytes', -1),
    ('max_candidates', 1.5), ('memory_limit_mb', -1), ('disk_reserve_mb', -1),
    ('recycle_tasks', True),
])
def test_invalid_integral_controls_are_rejected_before_runtime(app, key, value):
    cfg = dict(app.DEFAULT_CONFIG, login_allowed=False, **{key: value})
    with pytest.raises(app.CrawlerError) as caught:
        app.validate_config(cfg)
    assert caught.value.code == 'config_invalid'


@pytest.mark.parametrize('key,value', [
    ('crawl_interval', float('nan')), ('cooldown_seconds', float('inf')),
    ('login_timeout', -1), ('challenge_timeout', float('nan')),
    ('standalone_auth_timeout', 0), ('attempt_timeout', True),
])
def test_invalid_time_budget_is_rejected_before_runtime(app, key, value):
    cfg = dict(app.DEFAULT_CONFIG, login_allowed=False, **{key: value})
    with pytest.raises(app.CrawlerError) as caught:
        app.validate_config(cfg)
    assert caught.value.code == 'config_invalid'


@pytest.mark.parametrize('server', ['http://', 'socks5://', 'https://host:bad', 'http://host:-1'])
def test_proxy_needs_a_valid_host_and_port(app, server):
    cfg = dict(app.DEFAULT_CONFIG, login_allowed=False, proxy={'server': server})
    with pytest.raises(app.CrawlerError) as caught:
        app.validate_config(cfg)
    assert caught.value.code == 'config_invalid'
