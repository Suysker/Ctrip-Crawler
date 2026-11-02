"""Proxy connection identity and explicit migration of legacy frozen secrets."""
import copy
import json

import pytest

from section_loader import load_sections


@pytest.fixture
def app():
    return load_sections('imports', 'config', 'files', 'processing', 'worker', 'main')


def proxy(password='old-secret', username='old-user', **options):
    return {'server': 'http://proxy.invalid:3128', 'username': username,
            'password': password, **options}


def config(app, output=None, current_proxy=None):
    return dict(app.DEFAULT_CONFIG, login_allowed=False, accounts=[], proxy=current_proxy,
                output=str(output) if output else None, begin_date='2099-01-01',
                crawl_citys=['Alpha', 'Beta'], crawl_days=1, disk_reserve_mb=0)


def legacy_config(app, cfg):
    saved = app.public_config(cfg)
    saved.pop('proxy_identity_version', None)
    saved['proxy_id'] = app.stable_hash(cfg['proxy']) if cfg['proxy'] else None
    return saved


def test_new_public_identity_allows_credentials_rotation_but_not_connection_changes(app):
    original = config(app, current_proxy=proxy())
    saved = app.public_config(original)
    assert saved['proxy_identity_version'] == 2
    rotated = proxy(password='new-secret', username='new-user')
    assert app.public_config(config(app, current_proxy=rotated))['proxy_id'] == saved['proxy_id']
    assert app.validate_proxy_binding(saved, rotated) is None
    assert 'old-secret' not in json.dumps(saved)
    assert 'old-user' not in json.dumps(saved)
    for changed in (dict(rotated, server='http://other.invalid:3128'), dict(rotated, bypass='localhost')):
        with pytest.raises(app.CrawlerError) as error:
            app.validate_proxy_binding(saved, changed, refresh=True)
        assert error.value.code == 'config_invalid'


def test_legacy_identity_remains_strict_until_explicit_credentials_refresh(app):
    original = proxy()
    saved = legacy_config(app, config(app, current_proxy=original))
    assert app.validate_proxy_binding(saved, original) is None
    changed = proxy(password='new-secret')
    with pytest.raises(app.CrawlerError) as error:
        app.validate_proxy_binding(saved, changed)
    assert error.value.code == 'config_invalid'
    before = copy.deepcopy(saved)
    override = app.validate_proxy_binding(saved, changed, refresh=True, reason='same connection, new credentials')
    assert override['proxy_identity_version'] == 2
    assert override['proxy_id'] == app.proxy_identity(changed)
    assert override['legacy_proxy_id'] == saved['proxy_id']
    assert override['reason'] == 'same connection, new credentials'
    app.parse_utc(override['migrated_at'])
    assert saved == before
    assert 'new-secret' not in json.dumps(override)
    assert 'old-secret' not in json.dumps(override)
    assert app.validate_proxy_binding(saved, proxy(password='later', username='later'), override) == override


def test_migration_confirmation_cannot_be_reused_to_change_a_known_connection(app):
    saved = legacy_config(app, config(app, current_proxy=proxy()))
    override = app.validate_proxy_binding(saved, proxy(password='new-secret'), refresh=True)
    for changed in (dict(proxy(), server='http://other.invalid:3128'), dict(proxy(), bypass='localhost'), None):
        with pytest.raises(app.CrawlerError) as error:
            app.validate_proxy_binding(saved, changed, override, refresh=True)
        assert error.value.code == 'config_invalid'
    damaged = dict(override, legacy_proxy_id='unrelated-proof')
    with pytest.raises(app.CrawlerError) as error:
        app.validate_proxy_binding(saved, proxy(), damaged)
    assert error.value.code == 'state_invalid'


def test_credentials_migration_does_not_authorize_adding_or_removing_a_proxy(app):
    direct = legacy_config(app, config(app))
    assert app.validate_proxy_binding(direct, None) is None
    for saved, current in ((direct, proxy()),
                           (legacy_config(app, config(app, current_proxy=proxy())), None)):
        with pytest.raises(app.CrawlerError) as error:
            app.validate_proxy_binding(saved, current, refresh=True)
        assert error.value.code == 'config_invalid'


@pytest.mark.parametrize('missing_id', [True, False])
def test_incomplete_migration_record_cannot_authorize_removing_a_proxy(app, missing_id):
    saved = legacy_config(app, config(app, current_proxy=proxy()))
    override = app.validate_proxy_binding(saved, proxy(password='new-secret'), refresh=True)
    if missing_id:
        override.pop('proxy_id')
    else:
        override['proxy_id'] = None
    with pytest.raises(app.CrawlerError) as error:
        app.validate_proxy_binding(saved, None, override, refresh=True)
    assert error.value.code == 'state_invalid'


def test_proxy_server_cannot_hide_credentials_in_the_connection_identity(app):
    cfg = config(app, current_proxy={'server': 'http://user:secret@proxy.invalid:3128'})
    with pytest.raises(app.CrawlerError) as error:
        app.validate_config(cfg)
    assert error.value.code == 'config_invalid'


def seed_plan(app, output, saved_config):
    store = app.FileStore(output, 'legacy')
    plan = {'run_id': 'legacy', 'cities': [{'name': 'Alpha', 'code': 'AAA'},
            {'name': 'Beta', 'code': 'BBB'}], 'dates': ['2099-01-01'],
            'options': {'direct_flight': True}, 'config': saved_config}
    progress = store.create(plan)
    progress.update(app.new_task_control(0, 2))
    progress['current'].update(attempt_count=3, work_used=49.25)
    store.save_progress(progress)
    return store


def resume_parent(app, cfg, *, refresh=False):
    runner = app.Runner(cfg)
    runner.pool = app.AccountPool(runner.output, cfg['accounts'])
    runner.refresh_proxy_credentials = refresh
    runner.recovery_reason = 'same connection, new credentials'
    return runner


def test_legacy_plan_migration_changes_only_progress_not_plan_or_task_budget(app, tmp_path):
    cfg = config(app, tmp_path, proxy())
    store = seed_plan(app, tmp_path, legacy_config(app, cfg))
    original_plan = store.plan_path.read_bytes()
    rotated = config(app, tmp_path, proxy(password='new-secret'))
    with pytest.raises(app.CrawlerError) as error:
        resume_parent(app, rotated)._resume('legacy')
    assert error.value.code == 'config_invalid'
    parent = resume_parent(app, rotated, refresh=True)
    parent._resume('legacy')
    saved = app.read_json(store.progress_path)
    assert saved['proxy_binding_override']['proxy_id'] == app.proxy_identity(rotated['proxy'])
    assert saved['proxy_binding_override']['reason'] == 'same connection, new credentials'
    assert saved['current']['attempt_count'] == 3
    assert saved['current']['work_used'] == 49.25
    assert store.plan_path.read_bytes() == original_plan
    resume_parent(app, config(app, tmp_path, proxy(password='later')))._resume('legacy')
    assert store.plan_path.read_bytes() == original_plan
    changed = config(app, tmp_path, dict(proxy(), bypass='localhost'))
    with pytest.raises(app.CrawlerError) as error:
        resume_parent(app, changed, refresh=True)._resume('legacy')
    assert error.value.code == 'config_invalid'


def test_v2_plan_resume_accepts_new_credentials_without_an_override(app, tmp_path):
    cfg = config(app, tmp_path, proxy())
    store = seed_plan(app, tmp_path, app.public_config(cfg))
    rotated = config(app, tmp_path, proxy(password='new-secret', username='new-user'))
    parent = resume_parent(app, rotated)
    parent._resume('legacy')
    assert 'proxy_binding_override' not in app.read_json(store.progress_path)


@pytest.mark.parametrize('missing_comfort_default', [False, True])
def test_initialization_legacy_migration_preserves_frozen_config_and_other_identity(app, tmp_path, missing_comfort_default):
    original = config(app, tmp_path, proxy())
    if missing_comfort_default:
        original['comfort_timeout'] = 15.0
    store = app.FileStore(tmp_path, 'legacy')
    frozen = legacy_config(app, original)
    if missing_comfort_default:
        frozen.pop('comfort_timeout', None)
    app.atomic_json(store.initialization_path, {
        'schema_version': app.SCHEMA_VERSION, 'run_id': 'legacy', 'operation_id': 'op',
        'config': frozen, 'dates': ['2099-01-01'], 'phase': 'initializing',
        'resolve_attempts': 0, 'resolve_used': 0.0, 'resolve_reserved': 0.0,
        'blocked_accounts': {}, 'auth_used_aliases': [], 'account_ack': {}})
    rotated = config(app, tmp_path, proxy(password='new-secret'))
    if missing_comfort_default:
        rotated['comfort_timeout'] = 15.0
    with pytest.raises(app.CrawlerError) as error:
        resume_parent(app, rotated)._initialize('legacy', resume=True)
    assert error.value.code == 'config_invalid'
    parent = resume_parent(app, rotated, refresh=True)
    class BeforeNetwork(Exception):
        pass
    parent._prepare_account = lambda: (_ for _ in ()).throw(BeforeNetwork())
    with pytest.raises(BeforeNetwork):
        parent._initialize('legacy', resume=True)
    initialization = app.read_json(store.initialization_path)
    assert initialization['config'] == frozen
    assert initialization['proxy_binding_override']['legacy_proxy_id'] == frozen['proxy_id']
    drifted = dict(rotated, attempt_timeout=91)
    with pytest.raises(app.CrawlerError) as error:
        resume_parent(app, drifted, refresh=True)._initialize('legacy', resume=True)
    assert error.value.code == 'config_invalid'
