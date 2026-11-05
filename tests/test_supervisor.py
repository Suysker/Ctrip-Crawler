import time
import json
import subprocess
import sys
from pathlib import Path
import pytest
from section_loader import load_sections
from worker_helpers import ready_then_hang, ready_then_echo, ready_then_wrong_attempt, ready_then_unsaved_snapshot, ready_then_auth_checkpoint_failure
from worker_helpers import ready_then_stale_snapshot


@pytest.fixture
def app():
    return load_sections('imports', 'config', 'worker')


def ready_with_unmanaged_descendant(connection, config, identity, state):
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    time.sleep(60)


class BlockingBrowserSession:
    def __init__(self, config, *unused):
        self.config = config

    async def start(self):
        return None

    async def collect(self, task, timeout):
        Path(self.config['_blocking_marker']).write_text('started', encoding='utf-8')
        time.sleep(60)

    async def close(self):
        return None


def real_worker_with_blocking_browser(connection, config, identity, state):
    app = load_sections('imports', 'config', 'worker')
    app.BrowserSession = BlockingBrowserSession
    app.worker_main(connection, config, identity, state)


def real_worker_with_job_failure(connection, config, identity, state):
    app = load_sections('imports', 'config', 'worker')

    def fail_job():
        raise app.CrawlerError('process_cleanup', 'job-boundary-failed')

    app._windows_worker_job = fail_job
    app.worker_main(connection, config, identity, state)


def test_worker_returns_only_current_operation(app):
    cfg = dict(app.DEFAULT_CONFIG, close_timeout=0.3)
    with app.WorkerClient(cfg, None, None, generation=7,
                          target=ready_then_echo) as worker:
        worker.start(timeout=5)
        result = worker.call('probe', {}, timeout=3)
        assert result == {'ok': True}
        assert worker.generation == 7


@pytest.mark.parametrize('prime_session', [False, True])
def test_result_requires_a_session_from_the_current_operation(app, prime_session):
    cfg = dict(app.DEFAULT_CONFIG, close_timeout=0.3, _prime_session=prime_session)
    saved = []
    with app.WorkerClient(cfg, None, None, generation=7,
                         target=ready_then_stale_snapshot, on_session=saved.append) as worker:
        worker.start(timeout=5)
        if prime_session:
            assert worker.call('probe', {}, timeout=3) == {'ok': True}
            assert len(saved) == 1
        with pytest.raises(app.CrawlerError) as error:
            worker.call('probe', {}, timeout=3)
        assert error.value.code == 'ipc_invalid'


def test_hung_worker_is_bounded_and_reaped(app):
    cfg = dict(app.DEFAULT_CONFIG, close_timeout=0.2)
    worker = app.WorkerClient(cfg, None, None, generation=1,
                             target=ready_then_hang)
    worker.start(timeout=5)
    started = time.monotonic()
    try:
        with pytest.raises(app.CrawlerError) as error:
            worker.call('probe', {}, timeout=0.3)
        assert error.value.code == 'worker_timeout'
    finally:
        worker.close()
    assert time.monotonic() - started < 5
    assert not worker.process.is_alive()


def test_memory_limit_cannot_skip_descendant_cleanup(app):
    import psutil
    cfg = dict(app.DEFAULT_CONFIG, close_timeout=0.2)
    worker = app.WorkerClient(cfg, None, None, generation=2,
                             target=ready_with_unmanaged_descendant)
    descendants = []
    try:
        worker.start(timeout=5)
        descendants = psutil.Process(worker.process.pid).children(recursive=True)
        assert descendants
        worker.last_sample = 0
        worker._sample()
        worker.config['memory_limit_mb'] = 0
        worker.last_sample = 0
        worker.close()
        assert not worker.process.is_alive()
        assert all(not child.is_running() for child in descendants)
    finally:
        if not worker.closed:
            worker.config['memory_limit_mb'] = 2048
            worker.close()
        for child in descendants:
            try:
                child.kill()
                child.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.skipif(sys.platform != 'linux', reason='Requires a POSIX zombie awaiting its parent')
@pytest.mark.parametrize('already_exited', [False, True])
def test_cleanup_accepts_exit_while_another_parent_has_not_reaped(app, already_exited):
    import psutil

    # The holder owns waitpid and intentionally delays it until cleanup returns.
    holder = subprocess.Popen([sys.executable, '-c', '''
import subprocess
import sys
child = subprocess.Popen([sys.executable, '-c', sys.argv[1]])
print(child.pid, flush=True)
try:
    sys.stdin.read()
finally:
    child.kill()
    child.wait()
''', 'pass' if already_exited else 'import time; time.sleep(30)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        process = psutil.Process(int(holder.stdout.readline()))
        if already_exited:
            deadline = time.monotonic() + 3
            while process.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
                time.sleep(0.01)
        assert (process.status() == psutil.STATUS_ZOMBIE) is already_exited
        app._terminate_owned_processes({process.pid: process.create_time()}, timeout=0.2)
        assert process.status() == psutil.STATUS_ZOMBIE
    finally:
        holder.communicate(timeout=5)


def test_control_eof_exits_even_when_parent_pid_still_exists_and_event_loop_blocks(app, tmp_path):
    marker = tmp_path / 'blocked.txt'
    cfg = dict(app.DEFAULT_CONFIG, close_timeout=0.2, _blocking_marker=str(marker))
    worker = app.WorkerClient(cfg, {}, {}, generation=3, target=real_worker_with_blocking_browser)
    try:
        worker.start(timeout=5)
        worker.send('collect', {'task': {}}, timeout=60, operation_id='blocked')
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        worker.connection.close()
        worker.process.join(timeout=3)
        assert not worker.process.is_alive()
    finally:
        worker.close()


def test_job_boundary_failure_is_reported_as_actionable_startup_failure(app):
    worker = app.WorkerClient(dict(app.DEFAULT_CONFIG, close_timeout=0.2), {}, {}, generation=4,
                             target=real_worker_with_job_failure)
    try:
        with pytest.raises(app.CrawlerError) as error:
            worker.start(timeout=5)
        assert error.value.code == 'process_cleanup'
        assert str(error.value) == 'job-boundary-failed'
    finally:
        worker.close()


def test_matching_operation_cannot_accept_wrong_attempt(app):
    with app.WorkerClient(dict(app.DEFAULT_CONFIG, close_timeout=0.2), {}, {}, generation=8,
                          target=ready_then_wrong_attempt) as worker:
        worker.start(timeout=5)
        with pytest.raises(app.CrawlerError) as error:
            worker.call('collect', {'task': {'key': 'task', 'run_id': 'run', 'index': 0}},
                        timeout=3, attempt_id='expected')
        assert error.value.code == 'ipc_invalid'


def test_result_cannot_reference_an_uncommitted_snapshot(app):
    with app.WorkerClient(dict(app.DEFAULT_CONFIG, close_timeout=0.2), {}, {}, generation=9,
                          target=ready_then_unsaved_snapshot) as worker:
        worker.start(timeout=5)
        with pytest.raises(app.CrawlerError) as error:
            worker.call('probe', {}, timeout=3)
        assert error.value.code == 'ipc_invalid'


def test_command_size_is_rejected_before_sending(app):
    with app.WorkerClient(dict(app.DEFAULT_CONFIG), {}, {}, generation=10) as worker:
        with pytest.raises(app.CrawlerError) as error:
            worker.send('probe', {'oversized': 'a' * 65536})
        assert error.value.code == 'ipc_invalid'


def test_auth_checkpoint_is_not_a_successful_session(app):
    candidates, sessions = [], []
    with app.WorkerClient(dict(app.DEFAULT_CONFIG, close_timeout=0.2), {}, {}, generation=11,
                          target=ready_then_auth_checkpoint_failure,
                          on_checkpoint=candidates.append, on_session=sessions.append) as worker:
        worker.start(timeout=5)
        with pytest.raises(app.CrawlerError) as error:
            worker.call('auth', {'account': {}}, timeout=3, auth_id='auth')
        assert error.value.code == 'auth_timeout'
    assert len(candidates) == 1 and candidates[0]['confirmed'] is False
    assert sessions == []
