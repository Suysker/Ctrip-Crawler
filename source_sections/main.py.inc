RECOVERABLE_FAILURES = frozenset({
    'worker_timeout', 'worker_crashed', 'ipc_closed', 'response_timeout',
    'network_error', 'browser_error', 'memory_limit', 'process_interrupted',
    'proxy_access_denied', 'proxy_unavailable', 'worker_startup_exhausted',
})
PROXY_FAILURES = frozenset({'proxy_access_denied', 'proxy_unavailable'})
LEGACY_RECOVERY_DEFAULTS = {'auto_recovery_rounds': 12, 'auto_recovery_cooldown': 300.0}


class StopRequested(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum
        super().__init__('Stop requested by signal ' + str(signum))


class ProjectLogFormatter(logging.Formatter):
    def __init__(self, timezone_name):
        super().__init__('%(asctime)s %(levelname)s %(message)s')
        self.zone = ZoneInfo(timezone_name)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.zone).isoformat(timespec='milliseconds')


class Runner:
    """Sequential parent control flow; all durable changes have one owner."""

    def __init__(self, config, *, worker_factory=None):
        self.config = copy.deepcopy(config)
        self.output = Path(config.get('output') or Path(__file__).resolve().parent / 'data').resolve()
        self.worker_factory = worker_factory or WorkerClient
        self.worker = None
        self.alias = None
        self.pool = None
        self.store = None
        self.control = {}
        self.control_path = None
        self.generation = 0
        self.completed_with_worker = 0
        self.stop_requested = False
        self.stop_signal = signal.SIGINT
        self.logger = logging.getLogger('ctrip')

    def save(self):
        atomic_json(self.control_path, self.control)

    def _record_failure(self, error, stage, task_index=None):
        current = self.control.get('current') or {}
        diagnostic = make_diagnostic(error, stage=stage, config=self.config)
        self.control['last_error'] = {
            'code': error.code, 'task_index': task_index if task_index is not None else current.get('index'),
            'at': utc_now(), 'attempt_id': current.get('attempt_id'),
            'worker_generation': self.generation, 'diagnostic': diagnostic,
        }
        if (current and self.config['auto_recovery_rounds']
                and (error.code in PROXY_FAILURES or
                     (stage == 'parent.prepare' and error.code in RECOVERABLE_FAILURES))):
            current['pending_recovery'] = {'code': error.code}
        self.logger.warning('operation_failed index=%s attempt=%s code=%s diagnostic=%s',
            self.control['last_error']['task_index'], current.get('attempt_count'), error.code,
            json.dumps(diagnostic, ensure_ascii=False, separators=(',', ':')))
        self.save()

    def _task_budget(self):
        current = self.control.get('current') or {}
        extra = current.get('extra_work_budget', 0.0)
        if (isinstance(extra, bool) or not isinstance(extra, (int, float))
                or not math.isfinite(extra) or extra < 0):
            raise CrawlerError('state_invalid', '追加的任务预算无效')
        total = self.config['task_budget'] + extra
        if not math.isfinite(total):
            raise CrawlerError('state_invalid', '任务累计预算超出有效范围')
        return total

    def _grant_manual_retry(self, token, reason):
        match = re.fullmatch(r'([0-9]+):([0-9]+)', token) if isinstance(token, str) else None
        if (match is None or not isinstance(reason, str) or not reason.strip() or len(reason) > 256):
            raise CrawlerError('config_invalid', '人工恢复需要 TASK:ATTEMPT 和不超过256字符的非空原因')
        try:
            index, count = map(int, match.groups())
        except ValueError as error:
            raise CrawlerError('config_invalid', '人工恢复任务标识无效') from error
        token = f'{index}:{count}'
        current = self.control.get('current')
        if (not isinstance(current, dict) or type(self.control.get('cursor')) is not int
                or type(current.get('index')) is not int
                or self.control['cursor'] != index or current['index'] != index
                or type(current.get('attempt_count')) is not int or current['attempt_count'] != count):
            raise CrawlerError('state_invalid', '人工恢复标识与当前任务及尝试次数不符')
        if self.control.get('staging') is not None or self.control.get('publish_intent') is not None:
            raise CrawlerError('state_invalid', '必须先恢复未完成的结果发布，不能同时追加任务额度')
        maximum, used = current.get('max_attempts'), current.get('work_used')
        if (type(maximum) is not int or maximum < 1 or count < 0
                or isinstance(used, bool) or not isinstance(used, (int, float))
                or not math.isfinite(used) or used < 0):
            raise CrawlerError('state_invalid', '当前任务预算记录无效')
        records = current.get('manual_recoveries', [])
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise CrawlerError('state_invalid', '人工恢复记录格式无效')
        if any(record.get('token') == token for record in records):
            return False
        budget = self._task_budget()
        if count < maximum and used < budget and self.control.get('pause_reason') != 'recovery_exhausted':
            raise CrawlerError('state_invalid', '当前任务仍有自动恢复额度，不能提前追加')
        self._record_recovery('manual_recoveries',
            {'token': token, 'reason': safe_message(reason, config=self.config)[:256]}, extend=True)
        return True

    def _record_recovery(self, history_key, details, *, extend, not_before=None):
        """Publish one recovery decision and its limits in the same state write."""
        current = self.control['current']
        budget = self._task_budget()
        records = current.get(history_key, [])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise CrawlerError('state_invalid', '恢复记录格式无效')
        increment = self.config['task_budget'] if extend else 0.0
        new_budget = budget + increment
        if not math.isfinite(new_budget):
            raise CrawlerError('state_invalid', '追加后的累计预算超出有效范围')
        maximum = current['max_attempts']
        new_maximum = maximum + (max(3, len(self.config['accounts']) or 1) if extend else 0)
        auth = self.control.get('auth')
        record = {**details, 'at': utc_now(),
                  'attempt_count': current['attempt_count'], 'work_used': current['work_used'],
                  'auth_used': auth.get('used') if isinstance(auth, dict) else current.get('auth_used'),
                  'old_max_attempts': maximum, 'new_max_attempts': new_maximum,
                  'old_task_budget': budget, 'new_task_budget': new_budget}
        candidate = copy.deepcopy(self.control)
        candidate['current'].update(max_attempts=new_maximum,
            extra_work_budget=current.get('extra_work_budget', 0.0) + increment)
        candidate['current'][history_key] = copy.deepcopy(records) + [record]
        candidate['current'].pop('pending_recovery', None)
        if history_key == 'manual_recoveries' and self.control.get('pause_reason') == 'recovery_exhausted':
            extra = current.get('extra_recovery_rounds', 0)
            candidate['current']['extra_recovery_rounds'] = extra + self.config['auto_recovery_rounds']
            record.update(old_recovery_rounds=self.config['auto_recovery_rounds'] + extra,
                          new_recovery_rounds=self.config['auto_recovery_rounds'] + candidate['current']['extra_recovery_rounds'])
            candidate.pop('pause_reason', None)
            candidate.pop('pause_diagnostic', None)
        if not_before is not None:
            candidate.update(status='waiting', next_attempt_at=not_before)
            if candidate.get('startup') and not candidate['startup'].get('complete'):
                candidate.pop('startup')
        previous = self.control
        self.control = candidate
        try:
            self.save()
        except BaseException:
            self.control = previous
            raise

    def _schedule_recovery(self, code):
        current = self.control['current']
        records = current.get('automatic_recoveries', [])
        if not isinstance(records, list):
            raise CrawlerError('state_invalid', '自动恢复记录格式无效')
        extra = current.get('extra_recovery_rounds', 0)
        if type(extra) is not int or extra < 0:
            raise CrawlerError('state_invalid', '追加的自动恢复额度无效')
        limit = self.config['auto_recovery_rounds'] + extra
        if len(records) >= limit:
            self.control.update(status='paused', pause_reason='recovery_exhausted')
            self.save()
            raise CrawlerError('recovery_exhausted', '当前任务的自动冷却恢复次数已耗尽，请检查最后的具体错误')
        exhausted = current['attempt_count'] >= current['max_attempts'] or current['work_used'] >= self._task_budget()
        until = (datetime.now(timezone.utc) + timedelta(seconds=self.config['auto_recovery_cooldown'])).isoformat()
        self._record_recovery('automatic_recoveries', {'error_code': code, 'not_before': until},
                              extend=exhausted, not_before=until)
        self.logger.warning('recovery_wait index=%s round=%s/%s code=%s until=%s',
                            current['index'], len(records) + 1, limit, code, until)

    def _last_task_failure(self):
        error = self.control.get('last_error') or {}
        current = self.control.get('current') or {}
        return error.get('code') if error.get('task_index') == current.get('index') else None

    def _check_stop(self):
        if self.stop_requested:
            raise StopRequested(self.stop_signal)

    def _disk_check(self):
        if shutil.disk_usage(self.output).free < self.config['disk_reserve_mb'] * 1024 * 1024:
            raise CrawlerError('storage_error', '输出目录可用空间不足，停止派发')

    def _site_deadline(self):
        path = self.output / '.site.json'
        state = read_json(path) if path.exists() else {'schema_version': SCHEMA_VERSION}
        if not isinstance(state, dict) or state.get('schema_version') != SCHEMA_VERSION:
            raise CrawlerError('state_invalid', '站点冷却状态格式不兼容')
        candidates = [value for value in (state.get('server_not_before'),
                                          self.control.get('server_not_before')) if value]
        try:
            deadline = max(candidates, key=parse_utc) if candidates else None
            if deadline:
                parse_utc(deadline)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CrawlerError('state_invalid', '站点冷却时间无效') from exc
        if deadline and deadline != state.get('server_not_before'):
            atomic_json(path, {'schema_version': SCHEMA_VERSION, 'server_not_before': deadline})
        return deadline

    def _site_gate(self):
        deadline = self._site_deadline()
        if deadline and parse_utc(deadline) > datetime.now(timezone.utc):
            raise CrawlerError('rate_limited', '站点的服务端等待尚未结束，新批次和登录不能绕过')

    def _handle_site_limit(self, error, *, allow_wait=True):
        try:
            seconds = float(error.retry_after) if error.retry_after is not None else 600.0
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError('retry_after')
        except (TypeError, ValueError) as exc:
            raise CrawlerError('protocol_changed', '服务端等待时间无效') from exc
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        previous = self._site_deadline()
        if previous:
            until = max((previous, until), key=parse_utc)
        # Publish output-wide policy first. A crash before progress.save cannot
        # allow another batch or an independent login to bypass the wait.
        atomic_json(self.output / '.site.json', {'schema_version': SCHEMA_VERSION,
                                               'server_not_before': until})
        self.control['server_not_before'] = until
        self.save()
        remaining = max(0, (parse_utc(until) - datetime.now(timezone.utc)).total_seconds())
        if not allow_wait or self.control.get('rate_limit_recoveries', 0) >= 1 or remaining > 600:
            raise CrawlerError('rate_limited', '站点已限流，最早恢复时间已保存')
        self.control['rate_limit_recoveries'] = 1
        self.save()
        self._wait_until(until)
        self._site_gate()

    def _session_file(self, alias):
        return self.pool.session_path(alias) if alias is not None else self.output / 'sessions' / 'anonymous.json'

    def _recover_owner(self, owner_path):
        """Retire only exact process identities recorded by this operation."""
        owner_path = Path(owner_path)
        if not owner_path.exists():
            return
        if not owner_path.resolve().is_relative_to(self.output):
            raise CrawlerError('process_cleanup', '进程归属记录不在当前输出目录内')
        record = read_json(owner_path)
        identities = record.get('processes')
        if (type(record.get('owner_pid')) is not int or record['owner_pid'] <= 0
                or not isinstance(identities, dict)):
            raise CrawlerError('process_cleanup', '进程归属记录不完整，拒绝推测清理目标')
        import psutil
        try:
            protected = {os.getpid(), *(process.pid for process in psutil.Process().parents())}
            matching = {}
            for raw_pid, created in identities.items():
                if (not re.fullmatch(r'[1-9][0-9]*', str(raw_pid)) or isinstance(created, bool)
                        or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0):
                    raise CrawlerError('process_cleanup', '进程身份记录无效')
                pid = int(raw_pid)
                try:
                    process = psutil.Process(pid)
                    if process.create_time() != created:
                        # The recorded resource ended; a reused PID belongs to
                        # another process and grants no cleanup authority.
                        continue
                    if pid in protected:
                        raise CrawlerError('process_cleanup', '归属记录指向当前父进程或其祖先，拒绝清理')
                    matching[pid] = created
                except psutil.NoSuchProcess:
                    continue
            # Inspect every entry before terminating any process. Inspection
            # denial is not evidence that a resource is absent.
            _terminate_owned_processes(matching)
            for pid, created in matching.items():
                try:
                    process = psutil.Process(pid)
                    if (process.create_time() == created and process.is_running()
                            and process.status() != psutil.STATUS_ZOMBIE):
                        raise CrawlerError('process_cleanup', '已记录的自有进程未能退出')
                except psutil.NoSuchProcess:
                    continue
        except psutil.AccessDenied as exc:
            raise CrawlerError('process_cleanup', '无法确认或清理已记录进程的权限') from exc
        atomic_json(owner_path, dict(record, processes={}, recovered_at=utc_now()))

    def _load_session(self, alias):
        path = self._session_file(alias)
        return read_json(path) if path.exists() else None

    def _save_identity(self, identity):
        current = self._load_session(self.alias)
        if current:
            if stable_hash(current['identity']) != stable_hash(identity):
                raise CrawlerError('session_incompatible', '浏览器身份发生变化，请使用独立输出目录重新认证')
            return
        snapshot = {'schema_version': SCHEMA_VERSION, 'session_identity_id': uuid.uuid4().hex,
                    'session_revision': 0, 'identity': identity, 'state': None}
        if self.alias is None:
            atomic_json(self._session_file(None), snapshot)
        else:
            self.pool.initialize_session(self.alias, snapshot)

    def _save_session(self, value):
        previous = self._load_session(self.alias)
        if not previous or stable_hash(previous['identity']) != stable_hash(value['identity']):
            raise CrawlerError('session_incompatible', '拒绝不匹配身份的认证快照')
        snapshot = dict(previous, state=value['state'], session_revision=previous['session_revision'] + 1,
                        updated_at=utc_now())
        if self.alias is None:
            atomic_json(self._session_file(None), snapshot)
        else:
            self.pool.commit_session(self.alias, snapshot, control_path=self.control_path, control=self.control)

    def _save_auth_candidate(self, value):
        previous = self._load_session(self.alias)
        if self.alias is None or not previous or not isinstance(value.get('state'), dict):
            raise CrawlerError('state_invalid', '认证候选状态没有合法身份')
        path = self.pool.session_path(self.alias).with_name('auth-candidate.json')
        atomic_json(path, {'schema_version': SCHEMA_VERSION, 'account_alias': self.alias,
                          'session_identity_id': previous['session_identity_id'],
                          'state': value['state'], 'confirmed': False,
                          'challenge_visible': bool(value.get('challenge_visible')), 'saved_at': utc_now()})
        if value.get('challenge_visible') and not self.control.get('challenge_notified'):
            self.logger.warning('需要在当前浏览器中完成手机验证；账号=%s，程序不会自动发送短信。', self.alias)
            self.control['challenge_notified'] = True
            self.save()

    def _close_worker(self):
        if self.worker is not None:
            worker, self.worker = self.worker, None
            worker.close()
        self.completed_with_worker = 0

    def _recover_budgets(self):
        interrupted = bool((self.control.get('current') or {}).get('reserved', 0))
        recover_attempt_budget(self.control)
        auth = self.control.setdefault('auth', {'used': 0.0, 'reserved': 0.0})
        auth['used'] += auth.get('reserved', 0)
        auth['reserved'] = 0
        startup = self.control.get('startup')
        if startup and startup.get('reserved'):
            if self.control.get('current'):
                self.control['current']['work_used'] += startup['reserved']
            startup['used'] = startup.get('used', 0) + startup['reserved']
            startup['reserved'] = 0
        self.save()
        if interrupted:
            self._record_failure(CrawlerError('process_interrupted',
                '上次已派发尝试未完成结算；保留次数并计入预留预算，终止原因未知'), 'parent.recovery')

    def _ensure_worker(self, alias):
        self._check_stop()
        self._site_gate()
        if self.worker is not None and self.alias == alias:
            return
        self._close_worker()
        self.alias = alias
        startup = self.control.get('startup')
        if not startup or startup.get('complete') or startup.get('alias') != alias:
            startup = {'id': uuid.uuid4().hex, 'alias': alias, 'attempts': 0, 'used': 0.0, 'reserved': 0.0, 'complete': False}
            self.control['startup'] = startup
        while startup['attempts'] < 3:
            self._check_stop()
            saved = self._load_session(alias)
            state = saved.get('state') if saved else None
            if self.config.get('explicit_login') and saved:
                candidate_path = self._session_file(alias).with_name('auth-candidate.json')
                if candidate_path.exists():
                    candidate = read_json(candidate_path)
                    if candidate.get('session_identity_id') != saved['session_identity_id'] or candidate.get('account_alias') != alias:
                        raise CrawlerError('session_incompatible', '认证候选状态与账号身份不符')
                    if parse_utc(candidate['saved_at']) > parse_utc(saved.get('updated_at', '0001-01-01T00:00:00+00:00')):
                        state = candidate['state']
            current = self.control.get('current')
            remaining = self._task_budget() - current['work_used'] if current else self.config['startup_timeout'] * 3 - startup['used']
            if remaining <= 0:
                raise CrawlerError('task_exhausted', '浏览器恢复时间预算已耗尽')
            timeout = min(self.config['startup_timeout'], remaining)
            self.generation = max(self.generation, self.control.get('generation', 0)) + 1
            self.control['generation'] = self.generation
            startup['attempts'] += 1
            startup['reserved'] = timeout
            self.save()
            began = time.monotonic()
            worker_config = dict(self.config, account_alias=alias)
            if alias is not None:
                worker_config['account'] = account_credentials(self.pool.config[alias])
            worker = self.worker_factory(worker_config, saved.get('identity') if saved else None,
                state, generation=self.generation, alias=alias,
                on_profile=self._save_identity, on_session=self._save_session,
                on_checkpoint=self._save_auth_candidate,
                owner_path=self.control_path.parent / 'owner.json')
            self.worker = worker
            try:
                worker.start(timeout=timeout)
                elapsed = min(timeout, time.monotonic() - began)
                startup['used'] += elapsed
                startup['reserved'] = 0
                startup['complete'] = True
                if current:
                    current['work_used'] += elapsed
                self.save()
                return
            except CrawlerError as exc:
                elapsed = min(timeout, time.monotonic() - began)
                startup['used'] += elapsed
                startup['reserved'] = 0
                if current:
                    current['work_used'] += elapsed
                self.save()
                self._record_failure(exc, 'worker.start')
                self._close_worker()
                if exc.code not in ('worker_timeout', 'worker_crashed', 'ipc_closed', 'browser_startup'):
                    raise
        raise CrawlerError('worker_startup_exhausted', '浏览器启动次数已耗尽，进度已保存')

    def _mark_account(self, state, reason, *, seconds=None):
        if self.alias is None:
            raise CrawlerError('needs_auth', '匿名会话需要认证，请配置账号池')
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat() if seconds else None
        self.pool.mark(self.alias, state, reason, cooldown_until=until,
                       control_path=self.control_path, control=self.control,
                       source={'run_id': self.control.get('run_id'), 'task_index': self.control.get('cursor'),
                               'attempt_id': (self.control.get('current') or {}).get('attempt_id')})

    def _choose_account(self):
        if not self.config['login_allowed']:
            return None
        self.pool.reconcile(control=self.control, control_path=self.control_path)
        alias = self.pool.select(self.alias or self.control.get('preferred_alias'), self.control.get('blocked_accounts', {}))
        if alias is None:
            reasons = {a: self.pool.record(a)['state'] for a in self.pool.config}
            self.control['pool_summary'] = reasons
            self.save()
            raise CrawlerError('account_pool_exhausted', '当前任务没有可用账号，请完成 --login 后恢复；任务预算不会重置')
        self.control['preferred_alias'] = alias
        self.save()
        return alias

    def _authenticate(self, *, explicit=False, manual=False):
        self._check_stop()
        self._site_gate()
        if self.alias is None:
            raise CrawlerError('needs_auth', '匿名运行无法自动登录')
        used = self.control.setdefault('auth_used_aliases', [])
        retry_identity = {'alias': self.alias, 'task_index': (self.control.get('current') or {}).get('index')}
        transport_retry = self.control.get('auth_transport_retry') == retry_identity
        if self.alias in used and not explicit and not transport_retry:
            self._mark_account('cooldown', 'authentication_budget', seconds=self.config['cooldown_seconds'])
            raise CrawlerError('account_unavailable', '此任务对该账号的认证机会已用完')
        auth = self.control.setdefault('auth', {'used': 0.0, 'reserved': 0.0})
        total = self.config['standalone_auth_timeout'] if explicit else self.config['auth_budget']
        timeout = self.config['standalone_auth_timeout'] if explicit else (
            self.config['challenge_timeout'] if manual else self.config['login_timeout'])
        timeout = min(timeout, total - auth['used'])
        if timeout <= 0:
            raise CrawlerError('auth_exhausted', '认证总预算已耗尽')
        if self.alias not in used:
            used.append(self.alias)
        self.control.pop('auth_transport_retry', None)
        auth.update(id=uuid.uuid4().hex, reserved=timeout, alias=self.alias)
        self.save()
        began = time.monotonic()
        account = account_credentials(self.pool.config[self.alias])
        try:
            value = self.worker.call('auth', {'account': account}, timeout=timeout, auth_id=auth['id'])
            if not isinstance(value, dict) or not value.get('authenticated'):
                raise CrawlerError('auth_failed', '浏览器未确认登录成功')
            snapshot = self._load_session(self.alias)
            if explicit:
                self.pool.commit_session(self.alias, snapshot, explicit_auth=True, auth_id=auth['id'],
                                         control_path=self.control_path, control=self.control)
            else:
                self.pool.mark(self.alias, 'ready', 'authenticated', control_path=self.control_path, control=self.control)
        except CrawlerError as exc:
            if exc.code in PROXY_FAILURES or exc.code == 'network_error':
                # Transport failure is not a rejected login. Preserve the used
                # time/history and permit one same-task continuation after the
                # caller's bounded network cooldown; never unblock an account.
                self.control['auth_transport_retry'] = retry_identity
            self._record_failure(exc, 'auth')
            if exc.code == 'rate_limited':
                self._handle_site_limit(exc, allow_wait=False)
            elif exc.code in ('credentials_invalid', 'account_disabled'):
                self._mark_account('disabled', exc.code)
            elif exc.code in ('needs_auth', 'challenge', 'auth_failed', 'auth_timeout', 'response_timeout', 'worker_timeout'):
                self._mark_account('cooldown', exc.code, seconds=self.config['cooldown_seconds'])
            raise
        finally:
            auth = self.control.setdefault('auth', auth)
            auth['used'] += min(timeout, time.monotonic() - began)
            auth['reserved'] = 0
            self.save()

    def _prepare_account(self):
        while True:
            self._check_stop()
            alias = self._choose_account()
            self._ensure_worker(alias)
            if alias is None or self.pool.record(alias)['state'] == 'ready':
                return
            try:
                self._authenticate()
                return
            except CrawlerError as exc:
                if exc.code not in ('account_unavailable', 'needs_auth', 'challenge', 'auth_failed', 'auth_timeout',
                                     'credentials_invalid', 'account_disabled', 'response_timeout', 'worker_timeout'):
                    raise
                self._close_worker()

    def _initialize(self, run_id, resume=False):
        self.store = FileStore(self.output, run_id)
        self.control_path = self.store.run_dir / 'initialization.json'
        self._recover_owner(self.store.run_dir / 'owner.json')
        if resume:
            self.control = read_json(self.control_path)
            saved_config = self.control.get('config')
            runtime_overrides = {'headless', 'challenge_policy', 'proxy_id', 'proxy_identity_version'}
            current_config = public_config(self.config)
            if (isinstance(saved_config, dict) and 'comfort_timeout' not in saved_config
                    and current_config.get('comfort_timeout') == 15.0):
                current_config.pop('comfort_timeout')
            for key, fallback in LEGACY_RECOVERY_DEFAULTS.items():
                if isinstance(saved_config, dict) and key not in saved_config:
                    current_config.pop(key, None)
                    self.config[key] = fallback
            if (not isinstance(saved_config, dict)
                    or {key: value for key, value in saved_config.items() if key not in runtime_overrides}
                    != {key: value for key, value in current_config.items() if key not in runtime_overrides}):
                raise CrawlerError('config_invalid', '初始化恢复配置与固定配置不同')
            binding = validate_proxy_binding(saved_config, self.config['proxy'],
                self.control.get('proxy_binding_override'),
                refresh=getattr(self, 'refresh_proxy_credentials', False),
                reason=getattr(self, 'recovery_reason', None))
            if binding != self.control.get('proxy_binding_override'):
                self.control['proxy_binding_override'] = binding
                self.save()
        else:
            self.control = {'schema_version': SCHEMA_VERSION, 'run_id': run_id,
                'operation_id': uuid.uuid4().hex, 'config': public_config(self.config),
                'dates': flight_dates(self.config), 'phase': 'initializing',
                'resolve_attempts': 0, 'resolve_used': 0.0, 'resolve_reserved': 0.0,
                'blocked_accounts': {}, 'auth_used_aliases': [], 'account_ack': {}}
            self.save()
        self.pool.reconcile(control=self.control, control_path=self.control_path)
        self._recover_budgets()
        self.control['resolve_used'] += self.control.get('resolve_reserved', 0)
        self.control['resolve_reserved'] = 0
        self.save()
        plan = None
        while self.control['resolve_attempts'] < 3:
            self._prepare_account()
            self._check_stop()
            timeout = min(self.config['attempt_timeout'], 180 - self.control['resolve_used'])
            if timeout <= 0:
                break
            self.control['resolve_attempts'] += 1
            self.control['resolve_reserved'] = timeout
            self.save()
            began = time.monotonic()
            try:
                self._site_gate()
                cities = self.worker.call('resolve', {'names': self.config['crawl_citys']}, timeout=timeout)
                if not isinstance(cities, list) or len(cities) != len(self.config['crawl_citys']):
                    raise CrawlerError('protocol_changed', '城市解析结果数量不一致')
                codes = [c['code'] for c in cities]
                if len(set(codes)) != len(codes) or any(not re.fullmatch(r'[A-Z]{3}', c) for c in codes):
                    raise CrawlerError('config_invalid', '城市身份重复或无效')
                for city in cities:
                    _files_safe_name(city['name'])
                plan = {'run_id': run_id, 'created_at': utc_now(), 'cities': cities,
                    'dates': self.control['dates'], 'config': public_config(self.config),
                    'options': {key: self.config[key] for key in ('direct_flight', 'comft_flight', 'rename_col')}}
                self.control['resolve_used'] += min(timeout, time.monotonic() - began)
                self.control['resolve_reserved'] = 0
                self.save()
                break
            except CrawlerError as exc:
                self.control['resolve_used'] += min(timeout, time.monotonic() - began)
                self.control['resolve_reserved'] = 0
                self.save()
                self._record_failure(exc, 'resolve')
                if exc.code == 'rate_limited':
                    self._handle_site_limit(exc)
                    continue
                if exc.code in ('needs_auth', 'challenge') and self.alias is not None:
                    self._mark_account('cooldown', exc.code, seconds=self.config['cooldown_seconds'])
                    self._close_worker()
                    continue
                raise
        if plan is None:
            raise CrawlerError('initialization_exhausted', '城市解析预算已耗尽')
        rate_limit_recoveries = self.control.get('rate_limit_recoveries', 0)
        try:
            progress = self.store.create(plan)
        except BaseException:
            # FileStore may already have published plan_complete before its
            # progress write failed. Never overwrite that recovery evidence
            # with the pre-publication in-memory initialization snapshot.
            self.control = read_json(self.control_path)
            raise
        self.control_path = self.store.progress_path
        self.control = progress
        self.control['rate_limit_recoveries'] = rate_limit_recoveries
        self.save()
        return plan

    def _resume(self, run_id):
        self.store = FileStore(self.output, run_id)
        if not self.store.plan_path.exists():
            return self._initialize(run_id, resume=True)
        self._recover_owner(self.store.run_dir / 'owner.json')
        plan, self.control = self.store.load()
        self.control_path = self.store.progress_path
        saved_config = plan['config']
        aliases = [a['alias'] for a in self.config['accounts']]
        if aliases != saved_config.get('account_aliases', []):
            raise CrawlerError('config_invalid', '恢复时账号池别名顺序不能改变')
        binding = validate_proxy_binding(saved_config, self.config['proxy'],
            self.control.get('proxy_binding_override'),
            refresh=getattr(self, 'refresh_proxy_credentials', False),
            reason=getattr(self, 'recovery_reason', None))
        if binding != self.control.get('proxy_binding_override'):
            self.control['proxy_binding_override'] = binding
            self.save()
        runtime_overrides = {'headless', 'challenge_policy'}
        for key, value in saved_config.items():
            if key in self.config and key not in runtime_overrides:
                self.config[key] = value
        self.config['comfort_timeout'] = saved_config.get('comfort_timeout', 15.0)
        for key, fallback in LEGACY_RECOVERY_DEFAULTS.items():
            self.config[key] = saved_config.get(key, fallback)
        self.pool.reconcile(control=self.control, control_path=self.control_path)
        self.store.recover(self.control)
        self._recover_budgets()
        return plan

    def _wait_until(self, value):
        self._check_stop()
        next_log = 0.0
        while value and parse_utc(value) > datetime.now(timezone.utc):
            remaining = (parse_utc(value) - datetime.now(timezone.utc)).total_seconds()
            self._check_stop()
            if time.monotonic() >= next_log:
                self.logger.info('waiting until=%s remaining_seconds=%.1f', value, remaining)
                next_log = time.monotonic() + 60
            time.sleep(max(0.0, min(0.25, remaining)))
        self._check_stop()

    def _collect_plan(self, plan):
        total = len(plan['cities']) * (len(plan['cities']) - 1) * len(plan['dates'])
        if self.control['cursor'] > total:
            raise CrawlerError('state_invalid', '恢复点超出固定计划')
        if self.control['cursor'] == total:
            self.control['status'] = 'completed'
            self.save()
            return 0
        if self.control.get('pause_reason') == 'recovery_exhausted':
            raise CrawlerError('recovery_exhausted', '自动恢复已耗尽，需要先处理故障并显式人工恢复')
        pending_wait = self.control.get('next_attempt_at')
        self.control['status'] = ('waiting' if pending_wait and parse_utc(pending_wait) > datetime.now(timezone.utc)
                                  else 'running')
        self.control.pop('pause_reason', None)
        self.control.pop('pause_diagnostic', None)
        self.save()
        self._site_gate()
        for task in iter_tasks(plan):
            if task['index'] < self.control['cursor']:
                continue
            self._check_stop()
            self._disk_check()
            if date.fromisoformat(task['departure_date']) < datetime.now(ZoneInfo(self.config['timezone'])).date():
                raise CrawlerError('expired', '当前任务出发日期已过去，固定计划不会自动改日')
            if self.control.get('current') is None:
                new = new_task_control(task['index'], len(self.config['accounts']) or 1)
                self.control.update(new)
                self.control['auth'] = {'used': 0.0, 'reserved': 0.0}
                self.save()
            while True:
                self._check_stop()
                current = self.control['current']
                pending = current.get('pending_recovery')
                if pending:
                    if not isinstance(pending, dict) or pending.get('code') not in RECOVERABLE_FAILURES:
                        raise CrawlerError('state_invalid', '未决恢复记录无效')
                    self._schedule_recovery(pending['code'])
                    current = self.control['current']
                code = self._last_task_failure()
                # A completed cooldown is consumed only by the next dispatch.
                # Restarting during the wait must not append a second round.
                if current['attempt_count'] >= current['max_attempts'] or current['work_used'] >= self._task_budget():
                    if code in RECOVERABLE_FAILURES and self.config['auto_recovery_rounds']:
                        self._schedule_recovery(code)
                    else:
                        raise CrawlerError('task_exhausted', '当前任务预算已耗尽；resume 不会重置预算')
                self._wait_until(self.control.get('next_attempt_at'))
                self.control['status'] = 'running'
                self._prepare_account()
                self._wait_until(self.control.get('next_search_at'))
                self._site_gate()
                current = reserve_attempt(self.control, self.alias, self.generation,
                                          self.config['attempt_timeout'], self._task_budget())
                self.control['next_search_at'] = (datetime.now(timezone.utc) + timedelta(seconds=self.config['crawl_interval'])).isoformat()
                self.save()
                began = time.monotonic()
                committed = False
                try:
                    task['account_alias'] = self.alias
                    task['binding_epoch'] = self.generation
                    result = self.worker.call('collect', {'task': task}, timeout=current['reserved'], attempt_id=current['attempt_id'])
                    settle_attempt(self.control, time.monotonic() - began)
                    self.save()
                    result.setdefault('columns', list(CSV_FIELDS))
                    self.store.publish(task, result, self.control)
                    committed = True
                    self.completed_with_worker += 1
                    self.logger.info('task_done index=%s/%s status=%s rows=%s price_status=%s',
                                     task['index'] + 1, total, result['status'], len(result['rows']), result['price_status'])
                    if self.completed_with_worker >= self.config['recycle_tasks']:
                        self._close_worker()
                    break
                except CrawlerError as exc:
                    if (committed or self.control.get('current') is None
                            or self.control.get('staging') or self.control.get('publish_intent')):
                        raise
                    settle_attempt(self.control, time.monotonic() - began)
                    self._record_failure(exc, 'collect', task['index'])
                    if exc.code in ('needs_auth', 'challenge') and self.alias is not None:
                        if self.config['challenge_policy'] == 'wait_then_switch':
                            try:
                                self._authenticate(manual=True)
                                continue
                            except CrawlerError as auth_error:
                                if auth_error.code not in ('needs_auth', 'auth_timeout', 'auth_failed', 'worker_timeout', 'response_timeout', 'account_unavailable'):
                                    raise
                        self._mark_account('cooldown', exc.code, seconds=self.config['cooldown_seconds'])
                        self._close_worker()
                        continue
                    if exc.code in ('credentials_invalid', 'account_disabled') and self.alias is not None:
                        self._mark_account('disabled', exc.code)
                        self._close_worker()
                        continue
                    if exc.code == 'rate_limited':
                        self._handle_site_limit(exc)
                        continue
                    if exc.code not in RECOVERABLE_FAILURES:
                        raise
                    self._close_worker()
                    if exc.code in PROXY_FAILURES:
                        if not self.config['auto_recovery_rounds']:
                            raise
                        self._schedule_recovery(exc.code)
                        continue
                    count = self.control['current']['attempt_count']
                    if count >= self.control['current']['max_attempts'] or self.control['current']['work_used'] >= self._task_budget():
                        continue
                    delays = self.config.get('retry_delays', [5, 15])
                    delay = delays[min(count - 1, len(delays) - 1)]
                    self.control['current']['work_used'] += delay
                    self.control['next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    self.save()
        self.control['status'] = 'completed'
        self.save()
        return 0

    def run(self, *, resume=None, login=None, run_id=None, retry_current=None,
            refresh_proxy_credentials=False, recovery_reason=None):
        action = retry_current is not None or refresh_proxy_credentials
        if run_id is not None and (resume is not None or login is not None):
            raise CrawlerError('config_invalid', '固定批次入口不能与 resume 或 login 同时使用')
        if action and (not resume or login is not None):
            raise CrawlerError('config_invalid', '人工恢复选项只能配合 --resume 使用')
        if action and (not isinstance(recovery_reason, str) or not recovery_reason.strip() or len(recovery_reason) > 256):
            raise CrawlerError('config_invalid', '人工恢复必须提供不超过256字符的 --recovery-reason')
        if recovery_reason is not None and not action:
            raise CrawlerError('config_invalid', '恢复原因必须对应一个明确的人工恢复操作')
        self.refresh_proxy_credentials = refresh_proxy_credentials
        self.recovery_reason = safe_message(recovery_reason, config=self.config)[:256] if action else None
        from filelock import FileLock, Timeout
        self.output.mkdir(parents=True, exist_ok=True)
        try:
            lock = FileLock(str(self.output / '.run.lock'), timeout=0)
            with lock:
                self.pool = AccountPool(self.output, self.config['accounts'])
                self.pool.reconcile()
                self._disk_check()
                self._site_gate()
                try:
                    if login:
                        if login not in self.pool.config:
                            raise CrawlerError('config_invalid', '未知的账号别名')
                        record = self.pool.record(login)
                        if record.get('server_not_before') and parse_utc(record['server_not_before']) > datetime.now(timezone.utc):
                            raise CrawlerError('rate_limited', '账号的服务端等待尚未结束')
                        self.config['headless'] = False
                        self.config['challenge_policy'] = 'wait_then_switch'
                        self.config['explicit_login'] = True
                        self.control_path = self.pool.session_path(login).parent / 'auth_control.json'
                        self._recover_owner(self.control_path.parent / 'owner.json')
                        self.control = {'operation_id': uuid.uuid4().hex, 'auth_used_aliases': [],
                                        'blocked_accounts': {}, 'account_ack': {}}
                        self.save()
                        self._ensure_worker(login)
                        self._authenticate(explicit=True, manual=True)
                        return 0
                    selected_id = (run_id if run_id is not None else
                        resume or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8])
                    store = FileStore(self.output, selected_id)
                    existing = store.plan_path.exists() or store.initialization_path.exists()
                    self.logger.info('run=%s output=%s pid=%s parent_pid=%s',
                                     selected_id, self.output, os.getpid(), os.getppid())
                    plan = self._resume(selected_id) if resume or (run_id and existing) else self._initialize(selected_id)
                    if retry_current is not None:
                        granted = self._grant_manual_retry(retry_current, self.recovery_reason)
                        if granted:
                            self.control.pop('pause_reason', None)
                        self.logger.warning('manual_retry token=%s granted=%s reason=%s',
                            retry_current, granted, self.recovery_reason)
                    while True:
                        try:
                            return self._collect_plan(plan)
                        except CrawlerError as error:
                            if (error.code not in RECOVERABLE_FAILURES
                                    or not self.config['auto_recovery_rounds']
                                    or not self.control.get('current')
                                    or self.control.get('staging') or self.control.get('publish_intent')):
                                raise
                            self._record_failure(error, 'parent.prepare')
                            self._close_worker()
                            self._schedule_recovery(error.code)
                except BaseException as exc:
                    if self.control_path and self.control_path.exists():
                        self.control['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'paused'
                        self.control['pause_reason'] = getattr(exc, 'code', type(exc).__name__)
                        self.control['pause_diagnostic'] = make_diagnostic(exc, stage='parent.run', config=self.config)
                        current = self.control.get('current') or {}
                        self.logger.warning('run_paused code=%s index=%s attempts=%s/%s work_used=%s/%s diagnostic=%s',
                            self.control['pause_reason'], current.get('index'), current.get('attempt_count'),
                            current.get('max_attempts'), current.get('work_used'), self._task_budget(),
                            json.dumps(self.control['pause_diagnostic'], ensure_ascii=False, separators=(',', ':')))
                        self.save()
                    raise
                finally:
                    self._close_worker()
        except Timeout as exc:
            raise CrawlerError('output_locked', '输出目录正被另一个实例使用') from exc


def _resume_cli_config(config, run_id, output):
    """Read frozen non-secret settings before validating local credentials."""
    store = FileStore(output, run_id)
    if store.plan_path.exists():
        plan = read_json(store.plan_path)
        check = dict(plan)
        digest = check.pop('plan_hash', None)
        if (plan.get('run_id') != run_id or plan.get('schema_version') != SCHEMA_VERSION
                or digest != stable_hash(check)):
            raise CrawlerError('state_invalid', '恢复计划的版本、身份或摘要不符')
        frozen = plan.get('config')
    else:
        initialization = read_json(store.initialization_path)
        if initialization.get('run_id') != run_id or initialization.get('schema_version') != SCHEMA_VERSION:
            raise CrawlerError('state_invalid', '初始化恢复记录的版本或身份不符')
        frozen = initialization.get('config')
    if not isinstance(frozen, dict) or not isinstance(frozen.get('account_aliases'), list):
        raise CrawlerError('state_invalid', '恢复记录缺少规范配置或账号别名')
    local_accounts = config['accounts']
    if not isinstance(local_accounts, list) or any(not isinstance(item, dict) for item in local_accounts):
        raise CrawlerError('config_invalid', '本地账号配置必须为列表')
    by_alias = {}
    for account in local_accounts:
        alias = account.get('alias')
        if alias in by_alias:
            raise CrawlerError('config_invalid', '本地账号别名不能重复')
        by_alias[alias] = account
    accounts = []
    for alias in frozen['account_aliases']:
        if alias not in by_alias:
            raise CrawlerError('config_invalid', '恢复此批次需要在脚本中配置账号别名 ' + str(alias))
        accounts.append(copy.deepcopy(by_alias[alias]))
    for key, value in frozen.items():
        if key in config and key not in ('accounts', 'proxy', 'output'):
            config[key] = copy.deepcopy(value)
    config['accounts'] = accounts
    config['comfort_timeout'] = frozen.get('comfort_timeout', 15.0)
    for key, fallback in LEGACY_RECOVERY_DEFAULTS.items():
        config[key] = frozen.get(key, fallback)
    return config


def command_line(argv=None):
    parser = argparse.ArgumentParser(description='携程航班采集：Camoufox、独立单文件、账号池与可恢复文件输出',
        epilog='第三方安装：python -m pip install ' + ' '.join(f'{k}=={v}' for k, v in RUNTIME_REQUIREMENTS.items()) + '\n浏览器：python -m camoufox fetch ' + BROWSER_BUILD)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--resume', metavar='RUN_ID')
    operation.add_argument('--run-id', metavar='RUN_ID', help='幂等创建或恢复固定批次，适用于进程托管')
    operation.add_argument('--login', metavar='ALIAS')
    operation.add_argument('--check-environment', action='store_true')
    parser.add_argument('--retry-current', metavar='TASK:ATTEMPT',
                        help='随 --resume 为已耗尽的指定当前任务追加一轮有限额度')
    parser.add_argument('--refresh-proxy-credentials', action='store_true',
                        help='随 --resume 显式迁移旧批次代理凭据绑定；须确认连接配置未变')
    parser.add_argument('--recovery-reason', metavar='TEXT', help='人工恢复原因，必填且不超过256字符')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cities', nargs='+', metavar='CITY')
    parser.add_argument('--begin-date')
    parser.add_argument('--days', type=int)
    parser.add_argument('--anonymous', action='store_true', help='显式匿名运行；需要认证时停止')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--headed', action='store_true')
    mode.add_argument('--headless', action='store_true')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args(argv)
    check_environment()
    if args.check_environment:
        browser_environment_check()
        print('运行依赖与指定 Camoufox 构建检查通过。')
        return 0
    config = copy.deepcopy(DEFAULT_CONFIG)
    if args.output:
        config['output'] = str(args.output.resolve())
    output = Path(config['output'] or Path(__file__).resolve().parent / 'data').resolve()
    fixed_store = FileStore(output, args.run_id) if args.run_id is not None else None
    resuming = args.resume or (args.run_id if fixed_store and
        (fixed_store.plan_path.exists() or fixed_store.initialization_path.exists()) else None)
    if resuming:
        if args.resume and (args.cities or args.begin_date is not None or args.days is not None):
            raise CrawlerError('config_invalid', '恢复使用固定城市和日期；修改业务条件请创建新批次')
        output = Path(config['output'] or Path(__file__).resolve().parent / 'data').resolve()
        config = _resume_cli_config(config, resuming, output)
        if args.anonymous and config['login_allowed']:
            raise CrawlerError('config_invalid', '恢复不能将账号批次改为匿名批次')
    if args.cities and not resuming:
        config['crawl_citys'] = args.cities
    if args.begin_date and not resuming:
        config['begin_date'] = args.begin_date
    if args.days is not None and not resuming:
        config['crawl_days'] = args.days
    if args.anonymous:
        config['login_allowed'] = False
        config['accounts'] = []
    if args.headed:
        config['headless'] = False
    if args.headless:
        config['headless'] = True
    if args.login:
        config['headless'] = False
        config['challenge_policy'] = 'wait_then_switch'
    config = validate_config(config)
    browser_environment_check()
    output = Path(config['output'] or Path(__file__).resolve().parent / 'data').resolve()
    output.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('ctrip')
    logger.setLevel(logging.INFO)
    formatter = ProjectLogFormatter(config['timezone'])
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating = logging.handlers.RotatingFileHandler(output / 'crawler.log', maxBytes=10 * 1024 * 1024,
                                                    backupCount=9, encoding='utf-8')
    rotating.setFormatter(formatter)
    logger.handlers[:] = [console, rotating]
    runner = Runner(config)
    interrupts = 0

    def stop(signum, frame):
        nonlocal interrupts
        interrupts += 1
        if interrupts == 1:
            runner.stop_requested = True
            runner.stop_signal = signum
            logger.warning('stop_requested signal=%s pid=%s；停止派发，等待当前有界操作结束。', signum, os.getpid())
        else:
            raise StopRequested(signum)

    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = runner.run(resume=args.resume, login=args.login, run_id=args.run_id, retry_current=args.retry_current,
                            refresh_proxy_credentials=args.refresh_proxy_credentials,
                            recovery_reason=args.recovery_reason)
        logger.info('finished status=%s stats=%s', result, runner.control.get('stats', {}))
        return result
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()


def main(argv=None):
    try:
        return command_line(argv)
    except StopRequested as exc:
        print(f'运行已中断，signal={exc.signum}；固定批次可继续恢复。', file=sys.stderr)
        return 128 + exc.signum
    except KeyboardInterrupt:
        print('运行已中断，使用 --resume 恢复固定批次。', file=sys.stderr)
        return 130
    except CrawlerError as exc:
        print(f'{exc.code}: {safe_message(exc)}', file=sys.stderr)
        if exc.code in ('config_invalid', 'config_error', 'unsupported_mode', 'environment_invalid', 'browser_missing', 'browser_unavailable'):
            return 2
        if exc.code in ('needs_auth', 'account_pool_exhausted', 'rate_limited', 'protocol_changed', 'expired', 'session_incompatible',
                        'task_exhausted', 'recovery_exhausted', 'proxy_access_denied', 'proxy_unavailable', 'initialization_exhausted'):
            return 3
        if exc.code.startswith(('storage', 'ipc', 'process', 'worker_startup')) or exc.code in ('output_locked', 'state_invalid', 'worker_error'):
            return 4
        return 1 if exc.code in RECOVERABLE_FAILURES else 4
    except Exception as exc:
        diagnostic = make_diagnostic(exc, stage='cli', config=DEFAULT_CONFIG)
        print('unexpected_error: ' + json.dumps(diagnostic, ensure_ascii=False, separators=(',', ':')),
              file=sys.stderr)
        return 4


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
