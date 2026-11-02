# Edit this block when using the downloaded standalone script.
DEFAULT_CONFIG = {
    'crawl_citys': ['上海', '香港', '东京'],
    'begin_date': None,
    'end_date': None,
    'start_interval': 1,
    'crawl_days': 60,
    'days_interval': 1,
    'direct_flight': True,
    'comft_flight': False,
    'rename_col': True,
    'login_allowed': True,
    'accounts': [],  # {'alias': 'account_1', 'username': '你的账号', 'password': '你的密码', 'enabled': True}
    'challenge_policy': 'switch',  # switch or wait_then_switch
    'headless': True,
    'proxy': None,  # {'server': 'socks5://host:port', 'username': '...', 'password': '...'}
    'output': None,  # defaults to ./data next to this file
    'timezone': 'Asia/Shanghai',
    'browser_build': BROWSER_BUILD,
    'action_timeout': 10.0,
    'navigation_timeout': 30.0,
    'attempt_timeout': 90.0,
    'comfort_timeout': 15.0,
    'task_budget': 300.0,
    'auth_budget': 600.0,
    'login_timeout': 60.0,
    'challenge_timeout': 120.0,
    'standalone_auth_timeout': 900.0,
    'startup_timeout': 60.0,
    'close_timeout': 10.0,
    'crawl_interval': 2.0,
    'cooldown_seconds': 1800.0,
    'recycle_tasks': 100,
    'memory_limit_mb': 2048,
    'disk_reserve_mb': 256,
    'max_response_bytes': 32 * 1024 * 1024,
    'max_result_bytes': 16 * 1024 * 1024,
    'max_candidates': 64,
    'max_rows': 20000,
}


def flight_dates(config, today=None):
    today = today or datetime.now(ZoneInfo(config['timezone'])).date()
    try:
        start = date.fromisoformat(config['begin_date']) if config['begin_date'] else today + timedelta(days=config['start_interval'])
        end = date.fromisoformat(config['end_date']) if config['end_date'] else start + timedelta(days=config['crawl_days'] - 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CrawlerError('config_invalid', '日期配置无效') from exc
    if config['days_interval'] <= 0 or config['crawl_days'] <= 0:
        raise CrawlerError('config_invalid', '天数和日间隔必须大于零')
    if end < start:
        raise CrawlerError('config_invalid', '结束日期不能早于起始日期')
    return [(start + timedelta(days=i)).isoformat()
            for i in range(0, (end - start).days + 1, config['days_interval'])]


def validate_config(config):
    config = copy.deepcopy(config)
    integer_minima = {
        'start_interval': None, 'crawl_days': 1, 'days_interval': 1,
        'recycle_tasks': 1, 'memory_limit_mb': 1, 'disk_reserve_mb': 0,
        'max_response_bytes': 1, 'max_result_bytes': 1, 'max_candidates': 1, 'max_rows': 1,
    }
    for key, minimum in integer_minima.items():
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
            raise CrawlerError('config_invalid', key + ' 必须为有效整数')
    if not isinstance(config['crawl_citys'], list) or any(not isinstance(c, str) or not c.strip() for c in config['crawl_citys']):
        raise CrawlerError('config_invalid', 'crawl_citys 必须是非空城市名称列表')
    config['crawl_citys'] = list(dict.fromkeys(c.strip() for c in config['crawl_citys']))
    if len(config['crawl_citys']) < 2:
        raise CrawlerError('config_invalid', '至少需要两个不同城市')
    if not config['direct_flight']:
        raise CrawlerError('unsupported_mode', '当前 CSV 合同仅支持直飞；不将转机行程截成第一段')
    if config['challenge_policy'] not in ('switch', 'wait_then_switch'):
        raise CrawlerError('config_invalid', 'challenge_policy 必须为 switch 或 wait_then_switch')
    if config['challenge_policy'] == 'wait_then_switch' and config['headless']:
        raise CrawlerError('config_invalid', '人工验证等待需要 --headed')
    if config['browser_build'] != BROWSER_BUILD:
        raise CrawlerError('config_invalid', '浏览器构建与此脚本锁定版本不一致')
    aliases, principals = set(), set()
    enabled = []
    for account in config['accounts']:
        if not isinstance(account, dict):
            raise CrawlerError('config_invalid', 'accounts 项必须包含 alias、username 与凭据来源')
        alias, username = account.get('alias', ''), account.get('username', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', alias) or not isinstance(username, str) or not username.strip():
            raise CrawlerError('config_invalid', '账号别名或登录标识无效')
        if alias in aliases or username.strip().casefold() in principals:
            raise CrawlerError('config_invalid', '账号别名和登录标识不能重复')
        aliases.add(alias)
        principals.add(username.strip().casefold())
        if account.get('enabled', True):
            enabled.append(account)
    config['accounts'] = enabled
    if config['login_allowed'] and not enabled:
        raise CrawlerError('config_invalid', '请在顶部 accounts 配置账号池，或明确使用 --anonymous')
    if not config['login_allowed'] and enabled:
        raise CrawlerError('config_invalid', '匿名模式不能同时配置启用账号')
    nonnegative_times = {'crawl_interval', 'cooldown_seconds', 'comfort_timeout'}
    for key in ('attempt_timeout', 'task_budget', 'auth_budget', 'startup_timeout', 'close_timeout',
                'action_timeout', 'navigation_timeout', 'login_timeout', 'challenge_timeout',
                'standalone_auth_timeout', 'crawl_interval', 'cooldown_seconds', 'comfort_timeout'):
        value = config[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or (value < 0 if key in nonnegative_times else value <= 0)):
            raise CrawlerError('config_invalid', key + ' 必须为有限的有效时间值')
    try:
        ZoneInfo(config['timezone'])
    except (KeyError, ValueError) as exc:
        raise CrawlerError('config_invalid', '时区不可用，请检查 tzdata 安装和时区名称') from exc
    proxy = config['proxy']
    if proxy is not None:
        try:
            if not isinstance(proxy, dict) or not isinstance(proxy.get('server'), str):
                raise ValueError('proxy')
            parsed = urlparse(proxy['server'])
            if parsed.username is not None or parsed.password is not None:
                raise CrawlerError('config_invalid', '代理凭据必须放在独立的 username/password 字段中')
            if (parsed.scheme not in ('http', 'https', 'socks5') or not parsed.hostname
                    or any(character.isspace() for character in parsed.hostname)
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError('proxy')
        except (TypeError, ValueError) as exc:
            raise CrawlerError('config_invalid', 'proxy 必须包含有效 HTTP/HTTPS/SOCKS5 主机和端口') from exc
    flight_dates(config)
    return config


def proxy_identity(proxy):
    """Bind connection settings; authentication material may be refreshed."""
    if proxy is None:
        return None
    return stable_hash({key: value for key, value in proxy.items()
                        if key not in ('username', 'password')})


def validate_proxy_binding(saved_config, proxy, override=None, *, refresh=False, reason=None):
    """Validate v2 connections or explicitly migrate a legacy full-config hash.

    A legacy hash cannot prove its original endpoint. The explicit migration
    records the operator's confirmation; an existing migration cannot rebind.
    The caller alone publishes a newly returned override to its control file.
    """
    if not isinstance(saved_config, dict) or 'proxy_id' not in saved_config:
        raise CrawlerError('state_invalid', '恢复记录缺少代理身份')
    version = saved_config.get('proxy_identity_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise CrawlerError('state_invalid', '代理身份版本不兼容')
    connection_id = proxy_identity(proxy)
    frozen_id = saved_config['proxy_id']
    if version == 2:
        if override is not None:
            raise CrawlerError('state_invalid', '新版代理身份不能附加旧版迁移记录')
        if connection_id != frozen_id:
            raise CrawlerError('config_invalid', '恢复时代理连接配置不能静默改变')
        return None
    if override is not None:
        if (not isinstance(override, dict) or override.get('proxy_identity_version') != 2
                or frozen_id is None or override.get('legacy_proxy_id') != frozen_id
                or not isinstance(override.get('proxy_id'), str) or not override['proxy_id']):
            raise CrawlerError('state_invalid', '代理迁移凭据与原始固定计划不符')
        try:
            parse_utc(override['migrated_at'])
        except (KeyError, AttributeError, TypeError, ValueError, CrawlerError) as error:
            raise CrawlerError('state_invalid', '代理迁移凭据缺少有效确认时间') from error
        if override.get('proxy_id') != connection_id:
            raise CrawlerError('config_invalid', '恢复时代理连接配置不能静默改变')
        return override
    if refresh:
        if frozen_id is None or proxy is None:
            raise CrawlerError('config_invalid', '刷新代理凭据不能添加或移除代理连接')
        return {'proxy_identity_version': 2, 'proxy_id': connection_id,
                'legacy_proxy_id': frozen_id, 'migrated_at': utc_now(), 'reason': reason}
    legacy_id = stable_hash(proxy) if proxy else None
    if legacy_id != frozen_id:
        raise CrawlerError('config_invalid', '旧批次代理认证已变化；确认连接相同时请显式刷新代理凭据')
    return None


def public_config(config):
    excluded = {'accounts', 'proxy', 'output'}
    value = {key: copy.deepcopy(item) for key, item in config.items() if key not in excluded and key not in ('envelope',)}
    value['account_aliases'] = [a['alias'] for a in config['accounts']]
    value['proxy_identity_version'] = 2
    value['proxy_id'] = proxy_identity(config['proxy'])
    return value


def account_credentials(account):
    if account is None:
        return None
    return dict(account)


def iter_tasks(plan):
    index = 0
    for origin in plan['cities']:
        for destination in reversed(plan['cities']):
            if origin['code'] == destination['code']:
                continue
            for day in plan['dates']:
                identity = {'origin': origin['code'], 'destination': destination['code'],
                            'departure_date': day, 'options': plan['options']}
                yield {'index': index, 'key': stable_hash(identity), 'run_id': plan['run_id'],
                       'origin': origin, 'destination': destination, 'departure_date': day}
                index += 1


def new_task_control(index, account_count):
    return {'cursor': index, 'current': {'index': index, 'attempt_count': 0,
            'max_attempts': max(3, account_count), 'work_used': 0.0, 'reserved': 0.0,
            'auth_used': 0.0, 'auth_reserved': 0.0, 'auth_used_aliases': []},
            'blocked_accounts': {}, 'auth_used_aliases': [], 'state': 'idle'}


def reserve_attempt(progress, alias, generation, timeout, total_budget=300):
    current = progress['current']
    if current['attempt_count'] >= current['max_attempts'] or current['work_used'] >= total_budget:
        raise CrawlerError('task_exhausted', '当前任务自动恢复预算已耗尽')
    current.update(attempt_id=uuid.uuid4().hex, account_alias=alias,
                   worker_generation=generation, reserved=min(timeout, total_budget - current['work_used']),
                   started_at=utc_now())
    current['attempt_count'] += 1
    progress['state'] = 'running'
    return current


def settle_attempt(progress, elapsed):
    current = progress['current']
    current['work_used'] += min(max(0, elapsed), current.get('reserved', 0))
    current['reserved'] = 0.0
    progress['state'] = 'idle'


def recover_attempt_budget(progress):
    current = progress.get('current')
    if current:
        current['work_used'] += current.pop('reserved', 0.0)
        current['reserved'] = 0.0
        current['auth_used'] = current.get('auth_used', 0.0) + current.pop('auth_reserved', 0.0)
        current['auth_reserved'] = 0.0
    progress['state'] = 'interrupted'


def check_environment():
    failures = []
    if sys.version_info < (3, 12):
        failures.append('需要 Python 3.12 或以上')
    for package, expected in RUNTIME_REQUIREMENTS.items():
        try:
            actual = importlib.metadata.version(package)
            if actual != expected:
                failures.append(f'{package}: 需要 {expected}，当前 {actual}')
        except importlib.metadata.PackageNotFoundError:
            failures.append(f'{package}=={expected}: 未安装')
    if failures:
        raise CrawlerError('environment_invalid', '\n'.join(failures) + '\n安装命令见脚本顶部或 --help。')
