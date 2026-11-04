# File publication and finite account-pool persistence.
import copy as _files_copy
import csv as _files_csv
import hashlib as _files_hashlib
import json as _files_json
import os as _files_os
import re as _files_re
import uuid as _files_uuid
from datetime import datetime as _files_datetime, timezone as _files_timezone
from pathlib import Path as _FilesPath
from zoneinfo import ZoneInfo as _FilesZoneInfo


def _files_fail(message):
    raise CrawlerError('storage_error', message)


def _files_sync_directory(path):
    if _files_os.name == 'nt':
        return
    descriptor = _files_os.open(str(path), _files_os.O_RDONLY | getattr(_files_os, 'O_DIRECTORY', 0))
    try:
        _files_os.fsync(descriptor)
    finally:
        _files_os.close(descriptor)


def read_json(path):
    try:
        value = _files_json.loads(_FilesPath(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            _files_fail(f'Expected a JSON object: {path}')
        return value
    except (OSError, ValueError) as exc:
        raise CrawlerError('storage_error', f'Cannot read JSON {path}: {exc}') from exc


def atomic_json(path, data):
    path = _FilesPath(path)
    temporary = path.with_name(f'.{path.name}.{_files_uuid.uuid4().hex}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open('x', encoding='utf-8', newline='\n') as handle:
            _files_json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            handle.write('\n')
            handle.flush()
            _files_os.fsync(handle.fileno())
        _files_os.replace(temporary, path)
        _files_sync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        raise CrawlerError('storage_error', f'Cannot atomically write {path}: {exc}') from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def safe_path(root, relative):
    root = _FilesPath(root).resolve()
    raw = str(relative).replace('\\', '/')
    if not raw or raw.startswith('/') or ':' in raw or '..' in raw.split('/'):
        _files_fail(f'Unsafe relative path: {relative}')
    path = (root / raw).resolve()
    if path == root or not path.is_relative_to(root):
        _files_fail(f'Path outside output directory: {relative}')
    return path


def _files_digest(path):
    digest = _files_hashlib.sha256()
    try:
        with _FilesPath(path).open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError as exc:
        raise CrawlerError('storage_error', f'Cannot read result {path}: {exc}') from exc
    return digest.hexdigest()


def _files_safe_name(value):
    if not isinstance(value, str) or not value or value in ('.', '..'):
        _files_fail('Missing safe result name')
    if _files_re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith((' ', '.')):
        _files_fail(f'Unsafe result name: {value}')
    return value


def _files_initial_progress(run_id, initialization=None):
    initialization = initialization or {}
    recoveries = initialization.get('rate_limit_recoveries', 0)
    if type(recoveries) is not int or recoveries not in (0, 1):
        _files_fail('Invalid initialization site recovery budget')
    not_before = initialization.get('server_not_before')
    if not_before is not None:
        try:
            parse_utc(not_before)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CrawlerError('storage_error', 'Invalid initialization site waiting time') from exc
    return {
        'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'cursor': 0,
        'current': None, 'staging': None, 'publish_intent': None,
        'stats': {'succeeded': 0, 'empty': 0, 'price_partial': 0,
                  'comfort_partial': 0, 'comfort_unavailable': 0}, 'status': 'pending',
        'blocked_accounts': {}, 'auth_used_aliases': [], 'account_ack': {},
        'rate_limit_recoveries': recoveries, 'server_not_before': not_before,
    }


def _files_check_price_quality(record, error_code):
    status, reason = record.get('price_status'), record.get('price_reason')
    allowed = ('not_applicable',) if record.get('status') == 'empty' else ('complete', 'partial')
    valid_reason = (isinstance(reason, list) and all(isinstance(item, str) and item for item in reason)
                    and reason == sorted(set(reason)))
    if status not in allowed or not valid_reason or bool(reason) != (status == 'partial'):
        raise CrawlerError(error_code, 'Missing or inconsistent price quality metadata')


class FileStore:
    def __init__(self, output, run_id):
        self.output = _FilesPath(output).resolve()
        if not isinstance(run_id, str) or not _files_re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
            _files_fail('Invalid run identifier')
        self.run_id = run_id
        self.run_dir = safe_path(self.output, f'.runs/{run_id}')
        self.plan_path = self.run_dir / 'run.json'
        self.progress_path = self.run_dir / 'progress.json'
        self.initialization_path = self.run_dir / 'initialization.json'

    def create(self, plan):
        if self.plan_path.exists() or self.progress_path.exists():
            _files_fail('Run already exists; use resume')
        if plan.get('run_id') != self.run_id:
            _files_fail('Plan run identifier differs')
        saved = _files_copy.deepcopy(plan)
        saved['schema_version'] = SCHEMA_VERSION
        saved.pop('plan_hash', None)
        saved['plan_hash'] = stable_hash(saved)
        initialization = read_json(self.initialization_path) if self.initialization_path.exists() else {}
        initialization.update(phase='plan_complete', progress_initialized=False, resolved_plan_hash=saved['plan_hash'])
        atomic_json(self.initialization_path, initialization)
        atomic_json(self.plan_path, saved)
        progress = _files_initial_progress(self.run_id, initialization)
        self.save_progress(progress)
        initialization['progress_initialized'] = True
        atomic_json(self.initialization_path, initialization)
        return progress

    def load(self):
        plan = read_json(self.plan_path)
        check = dict(plan)
        expected = check.pop('plan_hash', None)
        if plan.get('run_id') != self.run_id or plan.get('schema_version') != SCHEMA_VERSION or expected != stable_hash(check):
            _files_fail('Plan schema, identity or checksum differs')
        if not self.progress_path.exists():
            initialization = read_json(self.initialization_path)
            if initialization.get('phase') != 'plan_complete' or initialization.get('progress_initialized') is not False or initialization.get('resolved_plan_hash') != expected:
                _files_fail('Missing progress cannot be recreated after task initialization')
            progress = _files_initial_progress(self.run_id, initialization)
            self.save_progress(progress)
        else:
            progress = read_json(self.progress_path)
        if progress.get('run_id') != self.run_id or progress.get('schema_version') != SCHEMA_VERSION:
            _files_fail('Progress schema or identity differs')
        if not isinstance(progress.get('cursor'), int) or progress['cursor'] < 0:
            _files_fail('Invalid progress cursor')
        if self.initialization_path.exists():
            initialization = read_json(self.initialization_path)
            if initialization.get('progress_initialized') is False:
                initialization['progress_initialized'] = True
                atomic_json(self.initialization_path, initialization)
        return plan, progress

    def save_progress(self, progress):
        if progress.get('run_id') != self.run_id:
            _files_fail('Progress belongs to another run')
        atomic_json(self.progress_path, progress)

    def _relative(self, path):
        return path.relative_to(self.output).as_posix()

    def _check_stage(self, record, progress):
        current = progress.get('current') or {}
        if record.get('run_id') != self.run_id or record.get('task_index') != progress['cursor'] or record.get('attempt_id') != current.get('attempt_id'):
            _files_fail('Publication does not belong to current attempt')
        temporary = safe_path(self.output, record['temp_path'])
        target = safe_path(self.output, record['target_path'])
        if temporary.parent != target.parent or not temporary.name.startswith('.') or not temporary.name.endswith('.partial.csv'):
            _files_fail('Invalid owned staging path')
        return temporary, target

    def publish(self, task, observation, progress):
        try:
            if task.get('run_id') != self.run_id or task.get('index') != progress['cursor']:
                _files_fail('Result task does not match current cursor')
            current = progress.get('current') or {}
            attempt_id = current.get('attempt_id')
            if not attempt_id:
                _files_fail('Cannot publish without registered attempt')
            if progress.get('staging') or progress.get('publish_intent'):
                _files_fail('Recover outstanding publication before another result')
            if observation.get('status') not in ('succeeded', 'empty'):
                _files_fail('Only successful or verified empty observations can publish')
            rows, columns = observation.get('rows'), observation.get('columns')
            if not isinstance(rows, list) or not isinstance(columns, list) or not columns or len(columns) != len(set(columns)):
                _files_fail('Result rows or CSV columns missing')
            if observation['status'] == 'empty' and rows:
                _files_fail('Empty result contains rows')
            if observation['status'] == 'succeeded' and not rows:
                _files_fail('Successful result must contain rows')
            _files_check_price_quality(observation, 'data_invalid')
            plan = read_json(self.plan_path)
            timezone_name = plan.get('config', {}).get('timezone', 'Asia/Shanghai')
            observed = parse_utc(observation['observed_at'])
            if observed.tzinfo is None:
                _files_fail('Observation time must include timezone')
            day = observed.astimezone(_FilesZoneInfo(timezone_name)).date().isoformat()
            departure = _files_datetime.strptime(task['departure_date'], '%Y-%m-%d').date().isoformat()
            name = f"{_files_safe_name(task['origin']['name'])}-{_files_safe_name(task['destination']['name'])}.csv"
            target = safe_path(self.output, f'{departure}/{day}/{name}')
            receipt_path = target.with_suffix('.meta.json')
            if target.exists() or receipt_path.exists():
                _files_fail(f'Result path already exists: {target}')
            result_id = _files_uuid.uuid4().hex
            temporary = target.with_name(f'.{self.run_id}.{attempt_id}.{result_id}.partial.csv')
            stage = {
                'run_id': self.run_id, 'task_index': task['index'], 'task_key': task['key'],
                'attempt_id': attempt_id, 'result_id': result_id, 'observed_at': observation['observed_at'],
                'temp_path': self._relative(temporary), 'target_path': self._relative(target),
            }
            progress['staging'] = stage
            self.save_progress(progress)
            target.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open('x', encoding='utf-8', newline='') as handle:
                writer = _files_csv.DictWriter(handle, fieldnames=columns, extrasaction='raise', lineterminator='\n')
                writer.writeheader()
                writer.writerows(rows)
                handle.flush()
                _files_os.fsync(handle.fileno())
            if target.exists() or receipt_path.exists():
                _files_fail('Output changed during publication')
            intent = dict(stage, schema_version=SCHEMA_VERSION, sha256=_files_digest(temporary),
                          row_count=len(rows), status=observation['status'], columns=columns,
                          comfort_status=observation.get('comfort_status', 'disabled'),
                          comfort_reason=observation.get('comfort_reason'),
                          price_status=observation['price_status'], price_reason=observation['price_reason'],
                          manual_recoveries=_files_copy.deepcopy(current.get('manual_recoveries')),
                          automatic_recoveries=_files_copy.deepcopy(current.get('automatic_recoveries')),
                          comfort_summary=_files_copy.deepcopy(observation.get('comfort_summary')),
                          account_alias=task.get('account_alias'), binding_epoch=task.get('binding_epoch'))
            progress['publish_intent'] = intent
            self.save_progress(progress)
            self._finish_publication(progress)
            return progress
        except CrawlerError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CrawlerError('storage_error', f'Cannot publish result: {exc}') from exc

    def _finish_publication(self, progress):
        intent = progress['publish_intent']
        for key in ('manual_recoveries', 'automatic_recoveries'):
            if intent.get(key) != (progress.get('current') or {}).get(key):
                _files_fail('Recovery evidence differs from the current task')
        temporary, target = self._check_stage(intent, progress)
        meta_path = target.with_suffix('.meta.json')
        if target.exists():
            if _files_digest(target) != intent['sha256']:
                _files_fail('Published CSV differs from recorded intent')
        else:
            if meta_path.exists():
                _files_fail('Receipt exists without its CSV')
            if not temporary.exists() or _files_digest(temporary) != intent['sha256']:
                _files_fail('Staging CSV cannot complete publication')
            _files_os.replace(temporary, target)
            _files_sync_directory(target.parent)
        if meta_path.exists():
            receipt = read_json(meta_path)
            for key in ('run_id', 'task_index', 'task_key', 'attempt_id', 'result_id', 'sha256', 'row_count',
                        'target_path', 'comfort_status', 'comfort_reason', 'price_status', 'price_reason',
                        'manual_recoveries', 'automatic_recoveries', 'comfort_summary'):
                if receipt.get(key) != intent.get(key):
                    _files_fail('Receipt differs from current publication')
        else:
            atomic_json(meta_path, dict(intent, committed_at=utc_now()))
        candidate = _files_copy.deepcopy(progress)
        candidate['cursor'] += 1
        stats = candidate.setdefault('stats', {})
        stats[intent['status']] = stats.get(intent['status'], 0) + 1
        if intent['price_status'] == 'partial':
            stats['price_partial'] = stats.get('price_partial', 0) + 1
        if intent['status'] == 'succeeded' and intent.get('comfort_status') in ('partial', 'unavailable'):
            key = 'comfort_' + intent['comfort_status']
            stats[key] = stats.get(key, 0) + 1
        candidate.update(current=None, staging=None, publish_intent=None, blocked_accounts={}, auth_used_aliases=[], status='pending')
        for key in ('attempt_count', 'spent_seconds', 'reserved_seconds', 'auth_spent_seconds', 'auth_reserved_seconds'):
            candidate[key] = 0
        self.save_progress(candidate)
        progress.clear()
        progress.update(candidate)
        if temporary.exists():
            temporary.unlink()

    def recover(self, progress):
        try:
            intent = progress.get('publish_intent')
            if intent:
                _files_check_price_quality(intent, 'state_invalid')
                temporary, target = self._check_stage(intent, progress)
                if target.exists():
                    self._finish_publication(progress)
                    return True
                if target.with_suffix('.meta.json').exists():
                    _files_fail('Receipt exists but its CSV is missing')
                if temporary.exists() and _files_digest(temporary) == intent.get('sha256'):
                    self._finish_publication(progress)
                    return True
                if temporary.exists():
                    temporary.unlink()
                progress.update(staging=None, publish_intent=None)
                self.save_progress(progress)
                return False
            stage = progress.get('staging')
            if stage:
                temporary, target = self._check_stage(stage, progress)
                if target.exists() or target.with_suffix('.meta.json').exists():
                    _files_fail('Unexpected result beside unfinished staging')
                if temporary.exists():
                    temporary.unlink()
                progress['staging'] = None
                self.save_progress(progress)
            return False
        except CrawlerError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CrawlerError('storage_error', f'Cannot recover publication: {exc}') from exc


class AccountPool:
    def __init__(self, output, accounts):
        self.output = _FilesPath(output).resolve()
        self.path = self.output / 'accounts.json'
        self.config = {}
        identifiers = set()
        for account in accounts:
            alias = account.get('alias')
            if not isinstance(alias, str) or not _files_re.fullmatch(r'[A-Za-z0-9_-]+', alias):
                raise CrawlerError('config_error', 'Invalid account alias')
            login = str(account.get('username', '')).strip()
            identity_hash = stable_hash(login)
            if alias in self.config or identity_hash in identifiers:
                raise CrawlerError('config_error', 'Duplicate account alias or login')
            identifiers.add(identity_hash)
            self.config[alias] = dict(account)
        self.data = read_json(self.path) if self.path.exists() else {'schema_version': SCHEMA_VERSION, 'accounts': {}}
        if self.data.get('schema_version') != SCHEMA_VERSION or not isinstance(self.data.get('accounts'), dict):
            _files_fail('Account state schema differs')
        changed = False
        for alias, account in self.config.items():
            identity_hash = stable_hash(str(account.get('username', '')).strip())
            record = self.data['accounts'].get(alias)
            if record is not None and record.get('login_hash') != identity_hash:
                raise CrawlerError('config_error', f'Account alias {alias} is bound to another login')
            if record is None:
                self.data['accounts'][alias] = {
                    'alias': alias, 'login_hash': identity_hash, 'state': 'needs_auth', 'reason': 'not_authenticated',
                    'pool_revision': 0, 'session_revision': 0, 'session_identity_id': None,
                    'cooldown_until': None, 'server_not_before': None, 'last_used_at': None,
                    'last_explicit_login': None, 'mutation': None,
                }
                changed = True
        if changed:
            self._save()

    def _save(self):
        atomic_json(self.path, self.data)

    def record(self, alias):
        if alias not in self.config:
            raise CrawlerError('config_error', 'Unknown account alias')
        return _files_copy.deepcopy(self.data['accounts'][alias])

    def session_path(self, alias):
        self.record(alias)
        return safe_path(self.output, f'sessions/{alias}/session.json')

    def load_session(self, alias):
        path = self.session_path(alias)
        return read_json(path) if path.exists() else None

    def initialize_session(self, alias, snapshot):
        existing = self.load_session(alias)
        if existing is not None:
            return existing
        self._validate_snapshot(alias, snapshot, initial=True)
        atomic_json(self.session_path(alias), snapshot)
        return _files_copy.deepcopy(snapshot)

    def _validate_snapshot(self, alias, snapshot, initial=False):
        if not isinstance(snapshot, dict) or not snapshot.get('session_identity_id') or not isinstance(snapshot.get('session_revision'), int):
            _files_fail('Session identity or revision missing')
        if snapshot['session_revision'] < 0:
            _files_fail('Negative session revision')
        existing = self.load_session(alias)
        if existing:
            if snapshot['session_identity_id'] != existing.get('session_identity_id'):
                _files_fail('Session identity changed without explicit reset')
            if snapshot['session_revision'] <= existing.get('session_revision', -1):
                if snapshot == existing:
                    return
                _files_fail('Session revision did not increase')

    def _future(self, value):
        return bool(value and parse_utc(value) > parse_utc(utc_now()))

    def _blocked(self, alias, blocked):
        if alias not in blocked:
            return False
        entry = blocked[alias] if isinstance(blocked, dict) else None
        evidence = self.data['accounts'][alias].get('last_explicit_login')
        if not isinstance(entry, dict) or not evidence:
            return True
        return evidence.get('pool_revision', -1) <= entry.get('pool_revision', 0)

    def select(self, preferred=None, blocked=None):
        blocked = blocked or {}
        aliases = [alias for alias, config in self.config.items() if config.get('enabled', True)]
        if preferred in aliases:
            index = aliases.index(preferred)
            aliases = aliases[index:] + aliases[:index]
        for alias in aliases:
            record = self.data['accounts'][alias]
            if self._blocked(alias, blocked) or record['state'] == 'disabled':
                continue
            if self._future(record.get('server_not_before')) or self._future(record.get('cooldown_until')):
                continue
            return alias
        return None

    def _control_ref(self, control_path):
        path = _FilesPath(control_path).resolve()
        if not path.is_relative_to(self.output):
            _files_fail('Account mutation control outside output')
        return path.relative_to(self.output).as_posix()

    def _apply_mutation(self, alias, mutation, control=None, control_path=None):
        reference = mutation.get('control_path')
        if not reference:
            return
        path = safe_path(self.output, reference)
        state = read_json(path)
        acknowledgements = state.setdefault('account_ack', {})
        if acknowledgements.get(alias) != mutation['id']:
            effect = mutation['effect']
            blocked = state.setdefault('blocked_accounts', {})
            if effect.get('block'):
                blocked[alias] = {key: effect.get(key) for key in ('reason', 'session_identity_id', 'session_revision', 'pool_revision')}
            acknowledgements[alias] = mutation['id']
            try:
                atomic_json(path, state)
            except OSError as exc:
                raise CrawlerError('storage_error', f'Cannot reconcile account control: {exc}') from exc
        if control is not None and control_path is not None and path == _FilesPath(control_path).resolve():
            control.clear()
            control.update(state)

    def reconcile(self, control=None, control_path=None):
        changed = False
        for alias, record in self.data['accounts'].items():
            mutation = record.get('mutation')
            if mutation and not mutation.get('confirmed'):
                self._apply_mutation(alias, mutation, control, control_path)
                mutation['confirmed'] = True
                changed = True
        if changed:
            self._save()
        if control is not None:
            state = read_json(control_path) if control_path is not None else _files_copy.deepcopy(control)
            blocked = state.setdefault('blocked_accounts', {})
            cleared = [alias for alias in blocked if alias in self.config and not self._blocked(alias, blocked)]
            for alias in cleared:
                blocked.pop(alias)
            if cleared:
                if control_path is not None:
                    atomic_json(control_path, state)
                control.clear()
                control.update(state)
                changed = True
        return changed

    def mark(self, alias, state, reason, *, cooldown_until=None, server_not_before=None,
             control_path=None, control=None, source=None):
        if state not in ('ready', 'needs_auth', 'cooldown', 'disabled'):
            raise CrawlerError('config_error', 'Unknown account state')
        self.reconcile(control, control_path)
        record = self.data['accounts'][alias]
        record.update(state=state, reason=reason, pool_revision=record['pool_revision'] + 1)
        if cooldown_until is not None:
            record['cooldown_until'] = cooldown_until
        if server_not_before is not None:
            previous = record.get('server_not_before')
            record['server_not_before'] = max(filter(None, (previous, server_not_before)), key=parse_utc)
        mutation = {
            'id': _files_uuid.uuid4().hex, 'confirmed': control_path is None,
            'control_path': self._control_ref(control_path) if control_path is not None else None,
            'source': dict(source or {}),
            'effect': {'block': state in ('cooldown', 'disabled'), 'reason': reason,
                       'session_identity_id': record.get('session_identity_id'),
                       'session_revision': record.get('session_revision', 0), 'pool_revision': record['pool_revision']},
        }
        record['mutation'] = mutation
        self._save()
        if control_path is not None:
            self._apply_mutation(alias, mutation, control, control_path)
            mutation['confirmed'] = True
            self._save()
        return self.record(alias)

    def commit_session(self, alias, snapshot, *, explicit_auth=False, auth_id=None,
                       control_path=None, control=None):
        self.reconcile(control, control_path)
        if explicit_auth and not auth_id:
            _files_fail('Explicit authentication requires auth_id')
        self._validate_snapshot(alias, snapshot)
        atomic_json(self.session_path(alias), snapshot)
        record = self.data['accounts'][alias]
        record.update(session_identity_id=snapshot['session_identity_id'], session_revision=snapshot['session_revision'])
        if explicit_auth:
            if not auth_id:
                _files_fail('Explicit authentication requires auth_id')
            record['cooldown_until'] = None
            record['last_explicit_login'] = {
                'auth_id': auth_id, 'session_identity_id': snapshot['session_identity_id'],
                'session_revision': snapshot['session_revision'], 'pool_revision': record['pool_revision'] + 1,
            }
            result = self.mark(alias, 'ready', 'explicit_login', control_path=control_path, control=control,
                               source={'auth_id': auth_id})
        elif record['state'] == 'ready':
            self._save()
            result = self.record(alias)
        else:
            # Session refresh is data, not evidence that a blocked account was explicitly authenticated.
            self._save()
            result = self.record(alias)
        self.reconcile(control, control_path)
        return result
