def _enable_child_subreaper():
    """Keep orphaned owned browser descendants reapable by this supervisor."""
    if sys.platform != 'linux':
        return
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise CrawlerError('process_cleanup', '无法建立 Linux 自有子进程回收边界')


class _ParentConnection:
    """Closing a POSIX pipe must also interrupt another thread blocked in recv."""

    def __init__(self, connection):
        self._connection = connection

    def send_bytes(self, *args, **kwargs):
        return self._connection.send_bytes(*args, **kwargs)

    def recv_bytes(self, *args, **kwargs):
        return self._connection.recv_bytes(*args, **kwargs)

    def close(self):
        if self._connection.closed:
            return
        if os.name == 'posix':
            import socket
            # multiprocessing's POSIX duplex Pipe is a socketpair.  Shutdown
            # wakes the reader thread; merely closing its FD can leave the
            # underlying socket referenced by a blocked recv indefinitely.
            try:
                duplicate = socket.fromfd(self._connection.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    duplicate.shutdown(socket.SHUT_RDWR)
                finally:
                    duplicate.close()
            except OSError:
                pass  # Already disconnected; close the original handle below.
        self._connection.close()


def _terminate_owned_processes(identities, timeout=2.0):
    import psutil
    processes = []
    for pid, created in reversed(list(identities.items())):
        try:
            process = psutil.Process(int(pid))
            if process.create_time() == created:
                processes.append(process)
                process.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise CrawlerError('process_cleanup', '无法终止当前运行拥有的进程') from exc
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=timeout)
    if alive:
        raise CrawlerError('process_cleanup', '自有浏览器进程未能在关闭期限内退出')


def _windows_worker_job():
    """Bind browser descendants to worker lifetime, even if Python is killed."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        if job:
            kernel.CloseHandle(job)
        raise CrawlerError('process_cleanup', '无法建立 Windows 子进程生命周期边界')
    # Keep the raw handle open until this process exits; closing kills this job.
    return job


def worker_main(connection, config, identity, state):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    job = None
    messages = queue.Queue(maxsize=2)
    disconnected = threading.Event()
    envelope = config['envelope']
    send_lock = threading.Lock()

    def send(kind, value=None, command=None, **extra):
        packet = {**envelope, **({k: command[k] for k in ('operation_id', 'attempt_id', 'auth_id', 'run_id', 'task_key', 'task_index') if k in command} if command else {}),
                  'type': kind, 'value': value, **extra}
        raw = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode('utf-8')
        limit = 8 * 1024 * 1024 if kind in ('session', 'profile', 'auth_checkpoint') else config['max_result_bytes']
        if len(raw) > limit:
            raise CrawlerError('result_too_large', '工作进程结果超过通信上限')
        with send_lock:
            connection.send_bytes(raw)

    def read_commands():
        try:
            while True:
                packet = json.loads(connection.recv_bytes(64 * 1024))
                if not isinstance(packet, dict):
                    raise ValueError('command')
                messages.put(packet, timeout=2)
        except (EOFError, OSError, ValueError, queue.Full):
            disconnected.set()
            # Control-channel loss is sufficient: Windows can retain the old
            # parent PID after parent exit, and the event loop may be blocked.
            try:
                import psutil
                owned = {p.pid: p.create_time() for p in psutil.Process().children(recursive=True)}
                _terminate_owned_processes(owned, timeout=min(2, config['close_timeout']))
            finally:
                os._exit(4)

    async def next_command():
        while not disconnected.is_set():
            try:
                return messages.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
        raise EOFError

    async def run():
        nonlocal identity, job
        session = None
        try:
            job = _windows_worker_job()
            if identity is None:
                identity = await asyncio.to_thread(generate_identity, config)
                send('profile', identity)
                ack = await next_command()
                if ack.get('type') != 'profile_accepted':
                    raise CrawlerError('ipc_invalid', '会话身份尚未保存')
            active_command = None

            def emit_browser(kind, value):
                if kind == 'auth_checkpoint' and active_command and active_command.get('type') == 'auth':
                    send(kind, value, active_command)

            session = BrowserSession(config, identity, state, emit_browser)
            await session.start()
            send('ready')
            snapshot_sequence = 0
            while True:
                command = await next_command()
                active_command = command
                if command.get('type') == 'shutdown':
                    break
                if any(command.get(k) != envelope[k] for k in ('worker_generation', 'account_alias', 'binding_epoch')):
                    raise CrawlerError('ipc_invalid', '工作进程账号绑定不匹配')
                try:
                    kind = command['type']
                    timeout = command['timeout']
                    if kind == 'resolve':
                        value = await asyncio.wait_for(session.resolve_cities(command['value']['names']), timeout)
                    elif kind == 'collect':
                        value = await asyncio.wait_for(session.collect(command['value']['task'], timeout), timeout)
                    elif kind == 'auth':
                        value = await asyncio.wait_for(session.authenticate(command['value']['account'], timeout), timeout)
                    else:
                        raise CrawlerError('ipc_invalid', '不支持的工作进程命令')
                    snapshot = await session.snapshot()
                    snapshot_sequence += 1
                    send('session', {'identity': identity, 'state': snapshot}, command,
                         snapshot_sequence=snapshot_sequence)
                    send('result', value, command, snapshot_sequence=snapshot_sequence)
                except (CrawlerError, asyncio.TimeoutError) as exc:
                    checkpoint = getattr(session, 'auth_checkpoint', None)
                    if command.get('type') == 'auth' and checkpoint:
                        send('auth_checkpoint', checkpoint, command)
                    if isinstance(exc, asyncio.TimeoutError):
                        exc = CrawlerError('response_timeout', '浏览器操作超时')
                    send('failure', command=command, code=exc.code,
                         message=safe_message(exc), retry_after=exc.retry_after)
                except Exception as exc:
                    send('failure', command=command, code='browser_error', message=type(exc).__name__)
        except EOFError:
            pass
        except Exception as exc:
            try:
                send('startup_failure', code=getattr(exc, 'code', 'browser_startup'), message=safe_message(exc))
            except (OSError, EOFError):
                pass
        finally:
            if session:
                try:
                    await asyncio.wait_for(session.close(), config['close_timeout'])
                except (Exception, asyncio.CancelledError):
                    # Parent owns the final process-tree cleanup after grace.
                    pass
            try:
                send('stopped')
            except (OSError, EOFError):
                pass

    reader = threading.Thread(target=read_commands, daemon=True, name='ctrip-command-reader')
    reader.start()
    try:
        asyncio.run(run())
    finally:
        disconnected.set()
        connection.close()


class WorkerClient:
    """One supervised worker; no browser objects or blocking pipe I/O on parent."""

    def __init__(self, config, identity, state, *, generation, alias=None,
                 on_profile=None, on_session=None, on_checkpoint=None, owner_path=None, target=None):
        self.config = copy.deepcopy(config)
        self.generation = generation
        self.envelope = {'protocol_version': 1, 'worker_generation': generation,
                         'account_alias': alias, 'binding_epoch': generation}
        self.config['envelope'] = self.envelope
        self.config['parent_pid'] = os.getpid()
        self.identity, self.state = identity, state
        self.on_profile, self.on_session = on_profile, on_session
        self.on_checkpoint = on_checkpoint
        self.owner_path = owner_path
        self.target = target or worker_main
        self.context = multiprocessing.get_context('spawn')
        _enable_child_subreaper()
        parent_connection, self.child_connection = self.context.Pipe()
        self.connection = _ParentConnection(parent_connection)
        self.process = self.context.Process(target=self.target,
            args=(self.child_connection, self.config, identity, state), name='ctrip-browser')
        self.incoming = queue.Queue(maxsize=1)
        self.outgoing = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.identities = {}
        self.threads = []
        self.last_sample = 0.0
        self.closed = False
        self.snapshot_sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _deliver(self, value):
        while not self.stop_event.is_set():
            try:
                self.incoming.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def _receive(self):
        try:
            while not self.stop_event.is_set():
                raw = self.connection.recv_bytes(self.config['max_result_bytes'])
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError('message')
                self._deliver(value)
        except (OSError, EOFError, ValueError) as exc:
            self._deliver({'type': 'channel_closed', 'message': type(exc).__name__})

    def _send(self):
        try:
            while not self.stop_event.is_set():
                try:
                    value = self.outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.connection.send_bytes(value)
        except (OSError, EOFError) as exc:
            self._deliver({'type': 'channel_closed', 'message': type(exc).__name__})

    def send(self, kind, value=None, **extra):
        packet = {**self.envelope, 'type': kind, 'value': value, **extra}
        raw = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > 64 * 1024:
            raise CrawlerError('ipc_invalid', '命令超过通信上限')
        try:
            self.outgoing.put_nowait(raw)
        except queue.Full as exc:
            raise CrawlerError('ipc_blocked', '命令发送通道阻塞') from exc

    def _sample(self, enforce_limit=True):
        import psutil
        if time.monotonic() - self.last_sample < 0.5:
            return
        self.last_sample = time.monotonic()
        try:
            parent = psutil.Process(self.process.pid)
            processes = [parent, *parent.children(recursive=True)]
            total = 0
            changed = False
            for process in processes:
                try:
                    created = process.create_time()
                    if self.identities.get(process.pid) != created:
                        self.identities[process.pid] = created
                        changed = True
                    total += process.memory_info().rss
                except psutil.NoSuchProcess:
                    continue
            if self.owner_path and changed:
                atomic_json(self.owner_path, {'owner_pid': os.getpid(), 'processes': self.identities})
            if enforce_limit and total > self.config['memory_limit_mb'] * 1024 * 1024:
                raise CrawlerError('memory_limit', '浏览器进程树超过内存上限')
        except psutil.NoSuchProcess:
            return

    def _next(self, deadline):
        while time.monotonic() < deadline:
            self._sample()
            try:
                packet = self.incoming.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if not self.process.is_alive():
                    raise CrawlerError('worker_crashed', '浏览器工作进程已退出')
                continue
            if time.monotonic() >= deadline:
                break
            if packet.get('type') == 'channel_closed':
                raise CrawlerError('ipc_closed', '浏览器通信连接已关闭')
            if any(packet.get(k) != v for k, v in self.envelope.items()):
                raise CrawlerError('ipc_invalid', '拒绝账号或代次不匹配的消息')
            return packet
        raise CrawlerError('worker_timeout', '工作进程未在截止时间内返回完整结果')

    def start(self, timeout=None):
        self.process.start()
        self.child_connection.close()
        for target in (self._receive, self._send):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        deadline = time.monotonic() + (timeout or self.config['startup_timeout'])
        while True:
            packet = self._next(deadline)
            if packet['type'] == 'profile':
                self.identity = packet['value']
                if not self.on_profile:
                    raise CrawlerError('state_invalid', '缺少会话身份保存处理器')
                self.on_profile(self.identity)
                self.send('profile_accepted')
            elif packet['type'] == 'ready':
                return
            elif packet['type'] == 'startup_failure':
                raise CrawlerError(packet['code'], packet['message'])
            else:
                raise CrawlerError('ipc_invalid', '启动期间收到无效消息')

    def call(self, kind, value, *, timeout, attempt_id=None, auth_id=None):
        operation = uuid.uuid4().hex
        starting_sequence = self.snapshot_sequence
        extra = {'operation_id': operation, 'timeout': timeout}
        if attempt_id:
            extra['attempt_id'] = attempt_id
        if auth_id:
            extra['auth_id'] = auth_id
        if kind == 'collect':
            task = value['task']
            extra.update(run_id=task['run_id'], task_key=task['key'], task_index=task['index'])
        self.send(kind, value, **extra)
        deadline = time.monotonic() + timeout
        while True:
            packet = self._next(deadline)
            if packet.get('operation_id') != operation:
                raise CrawlerError('ipc_invalid', '拒绝迟到的操作结果')
            for key in ('attempt_id', 'auth_id', 'run_id', 'task_key', 'task_index'):
                if key in extra and packet.get(key) != extra[key]:
                    raise CrawlerError('ipc_invalid', '拒绝任务或认证身份不符的结果')
            if packet['type'] == 'auth_checkpoint':
                if kind != 'auth':
                    raise CrawlerError('ipc_invalid', '非认证操作不能更新认证候选状态')
                if self.on_checkpoint:
                    self.on_checkpoint(packet['value'])
            elif packet['type'] == 'session':
                sequence = packet.get('snapshot_sequence')
                if sequence != self.snapshot_sequence + 1:
                    raise CrawlerError('ipc_invalid', '认证快照序号不连续')
                self.state = packet['value']['state']
                if self.on_session:
                    self.on_session(packet['value'])
                self.snapshot_sequence = sequence
            elif packet['type'] == 'result':
                if (self.snapshot_sequence <= starting_sequence
                        or packet.get('snapshot_sequence') != self.snapshot_sequence):
                    raise CrawlerError('ipc_invalid', '结果引用了未持久保存的认证快照')
                return packet['value']
            elif packet['type'] == 'failure':
                raise CrawlerError(packet['code'], packet['message'], retry_after=packet.get('retry_after'))
            else:
                raise CrawlerError('ipc_invalid', '收到未知操作响应')

    def close(self):
        if self.closed:
            return
        self.closed = True
        cleanup_error = None
        if self.process.pid:
            try:
                try:
                    # Do not let rate limiting hide a just-created descendant.
                    self.last_sample = 0.0
                    self._sample(enforce_limit=False)
                except Exception as exc:
                    cleanup_error = exc
                if self.process.is_alive():
                    try:
                        self.send('shutdown')
                    except CrawlerError:
                        pass
                    self.process.join(self.config['close_timeout'])
                # multiprocessing alone owns waitpid/reaping for this process.
                # psutil.wait_procs on it steals the exit status on POSIX and
                # leaves multiprocessing.Process.is_alive() permanently true.
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=min(2, self.config['close_timeout']))
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=2)
                if self.process.is_alive():
                    raise CrawlerError('process_cleanup', '工作进程未能在关闭期限内退出')
                descendants = {pid: created for pid, created in self.identities.items()
                               if int(pid) != self.process.pid}
                _terminate_owned_processes(descendants, timeout=min(2, self.config['close_timeout']))
            except Exception as exc:
                cleanup_error = exc
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=2)
        self.stop_event.set()
        self.connection.close()
        self.child_connection.close()
        for thread in self.threads:
            thread.join(timeout=1)
        if any(t.is_alive() for t in self.threads):
            raise CrawlerError('ipc_cleanup', '通信线程未退出，停止运行以避免泄漏')
        if cleanup_error:
            raise cleanup_error
