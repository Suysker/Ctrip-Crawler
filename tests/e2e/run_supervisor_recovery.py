"""Validate the Supervisor template with a real manager and local browser fixtures."""
from __future__ import annotations

import argparse
import configparser
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

import psutil

from run_lifecycle_recovery import CITIES, child_main, digest, event, load_artifact


PROGRAM = 'ctrip-crawler'
RUN_ID = 'supervisor-fixed'


def controller():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, default=root / 'ctrip_flights_scraper.py')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--supervisord', default='supervisord')
    parser.add_argument('--supervisorctl', default='supervisorctl')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Run this Supervisor scenario on Linux or WSL')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('Use an empty output directory; existing evidence is preserved')

    artifact = output / 'standalone/ctrip_flights_scraper.py'
    artifact.parent.mkdir()
    shutil.copyfile(args.artifact.resolve(), artifact)
    app = load_artifact(artifact)
    app._enable_child_subreaper()
    data, markers = output / 'data', output / 'markers'
    markers.mkdir()
    control_path = output / 'fixture-control.json'
    events_path = output / 'events.jsonl'
    progress_path = data / '.runs' / RUN_ID / 'progress.json'
    plan_path = progress_path.with_name('run.json')
    supervisor_log = output / 'supervisord.log'
    conf_path = output / 'supervisord.conf'
    report_path = output / 'acceptance.json'
    argv = ['--anonymous', '--cities', *CITIES, '--begin-date', '2099-01-01', '--days', '2',
            '--output', str(data), '--run-id', RUN_ID]
    env = dict(os.environ, CTRIP_LIFECYCLE_ARTIFACT=str(artifact), CTRIP_LIFECYCLE_CONTROL=str(control_path),
               CTRIP_LIFECYCLE_EVENTS=str(events_path), CTRIP_LIFECYCLE_MARKERS=str(markers),
               CTRIP_LIFECYCLE_ARGS=json.dumps(argv), PYTHONFAULTHANDLER='1')
    version = subprocess.check_output([args.supervisord, '--version'], text=True, timeout=5).strip()
    report = {'status': 'running', 'run_id': RUN_ID, 'artifact_sha256': digest(artifact), 'supervisor': version}
    began = time.monotonic()
    app.atomic_json(report_path, report)

    def control(phase, *, release=False, exit_code=None):
        app.atomic_json(control_path, {'phase': phase, 'release': release, 'failures': 0, 'exit_code': exit_code})

    def manager_log():
        return supervisor_log.read_text(encoding='utf-8') if supervisor_log.exists() else ''

    def ctl(*command, check=True, timeout=10):
        result = subprocess.run([args.supervisorctl, '-c', str(conf_path), *command],
                                text=True, capture_output=True, timeout=timeout)
        if check:
            assert result.returncode == 0, f'{command}: {result.stdout.strip()} {result.stderr.strip()}'
        return result.stdout.strip()

    def running_pid():
        value = ctl('pid', PROGRAM, check=False)
        return int(value) if value.isdecimal() else 0

    def wait_for(predicate, label, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                raise AssertionError(f'Supervisor exited {daemon.returncode} while waiting for {label}')
            if value := predicate():
                return value
            time.sleep(0.1)
        raise AssertionError(f'Timed out waiting for {label}')

    def wait_marker(name, previous_pid=None):
        path = markers / name

        def marker_pid():
            if path.exists():
                pid = app.read_json(path)['worker_pid']
                if pid != previous_pid:
                    return pid
            return None

        return wait_for(marker_pid, name)

    def result_hashes():
        return {path: digest(path) for path in [*data.glob('*/*/*.csv'), *data.glob('*/*/*.meta.json')]}

    def managed_descendants():
        identities = {}
        for process in psutil.Process().children(recursive=True):
            if process.pid != daemon.pid:
                try:
                    identities[process.pid] = process.create_time()
                except psutil.NoSuchProcess:
                    continue
        return identities

    with tempfile.TemporaryDirectory(prefix='ctrip-supervisor-') as runtime:
        socket = Path(runtime) / 'control.sock'
        config = configparser.ConfigParser(interpolation=None)
        config.read(root / 'deploy/ctrip-crawler.supervisor.conf', encoding='utf-8')
        config['unix_http_server']['file'] = str(socket)
        config['supervisorctl']['serverurl'] = f'unix://{socket}'
        config['supervisord']['pidfile'] = str(Path(runtime) / 'supervisord.pid')
        config['supervisord']['logfile'] = str(supervisor_log)
        config['supervisord']['childlogdir'] = str(output)
        program = config[f'program:{PROGRAM}']
        program['command'] = shlex.join([sys.executable, str(Path(__file__).resolve()), '--child'])
        program['directory'] = str(output)
        for stream in ('stdout', 'stderr'):
            program[f'{stream}_logfile'] = str(output / f'crawler.{stream}.log')
        with conf_path.open('w', encoding='utf-8') as handle:
            config.write(handle)

        control('probe', exit_code=2)
        with (output / 'supervisord.console.log').open('wb') as stream:
            daemon = subprocess.Popen([args.supervisord, '-c', str(conf_path)], env=env,
                                      stdout=stream, stderr=subprocess.STDOUT)
        try:
            wait_for(lambda: 'exit status 2; expected' in manager_log(), 'immediate expected exit 2')
            assert 'EXITED' in ctl('status', PROGRAM, check=False)
            assert manager_log().count(f"spawned: '{PROGRAM}'") == 1
            control('probe', exit_code=3)
            # A short-lived program may already be EXITED by the time start replies.
            ctl('start', PROGRAM, check=False)
            wait_for(lambda: 'exit status 3; expected' in manager_log(), 'immediate expected exit 3')
            assert 'EXITED' in ctl('status', PROGRAM, check=False)
            assert manager_log().count(f"spawned: '{PROGRAM}'") == 2

            control('term')
            ctl('start', PROGRAM)
            wait_marker('term-ready.json')
            first = running_pid()
            assert first > 0
            before_term = result_hashes()
            assert len(before_term) == 2
            plan_hash = digest(plan_path)
            os.kill(first, signal.SIGTERM)
            control('kill', release=True)
            worker_before_kill = wait_marker('kill-ready.json')
            second = running_pid()
            assert second > 0 and second != first
            assert 'exit status 143; not expected' in manager_log()
            original = result_hashes()
            assert len(original) == 4
            assert all(original.get(path) == expected for path, expected in before_term.items())
            assert digest(plan_path) == plan_hash
            progress = app.read_json(progress_path)
            assert progress['cursor'] == 2 and progress['current']['reserved'] > 0

            os.kill(second, signal.SIGKILL)
            wait_marker('kill-ready.json', worker_before_kill)
            third = running_pid()
            assert third > 0 and third not in (first, second)
            assert 'terminated by SIGKILL; not expected' in manager_log()
            assert app.read_json(progress_path)['current']['attempt_count'] == 2
            assert 'code=process_interrupted' in (data / 'crawler.log').read_text(encoding='utf-8')

            ctl('stop', PROGRAM, timeout=35)
            assert 'STOPPED' in ctl('status', PROGRAM, check=False)
            assert running_pid() == 0
            assert manager_log().count(f"spawned: '{PROGRAM}'") == 5
            interrupted = app.read_json(progress_path)
            assert interrupted['status'] == 'interrupted' and interrupted['cursor'] == 2
            assert interrupted['pause_reason'] == 'StopRequested'

            control('finish', release=True)
            ctl('start', PROGRAM)
            wait_for(lambda: 'exit status 0; expected' in manager_log(), 'completed expected exit 0', timeout=60)
            assert 'EXITED' in ctl('status', PROGRAM, check=False)
            assert manager_log().count(f"spawned: '{PROGRAM}'") == 6
            completed = app.read_json(progress_path)
            assert completed['status'] == 'completed' and completed['cursor'] == 4
            assert digest(plan_path) == plan_hash
            final_results = result_hashes()
            assert all(final_results.get(path) == expected for path, expected in original.items())
            assert len(final_results) == 8
            report.update(status='passed', managed_pids=[first, second, third], external_sigterm_exit=143,
                          sigkill_restarted=True, managed_stop_without_restart=True,
                          expected_probe_exits=[2, 3], expected_completed_exit=0, cursor=4,
                          original_plan_and_results_unchanged=True)
        except BaseException as error:
            report.update(status='failed', error=repr(error), traceback=traceback.format_exc())
        finally:
            remaining = managed_descendants()
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                    report.update(status='failed', cleanup_error='Supervisor did not exit within 35 seconds')
            remaining.update(managed_descendants())
            try:
                app._terminate_owned_processes(remaining, timeout=1)
            except Exception as error:
                report.update(status='failed', descendant_cleanup_error=repr(error))
            report.update(supervisord_exit=daemon.returncode, elapsed_seconds=round(time.monotonic() - began, 3))
            if daemon.returncode != 0:
                report['status'] = 'failed'
            app.atomic_json(report_path, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    if '--child' in sys.argv:
        control = json.loads(Path(os.environ['CTRIP_LIFECYCLE_CONTROL']).read_text(encoding='utf-8'))
        if control['phase'] == 'probe':
            event('policy_probe', exit_code=control['exit_code'])
            raise SystemExit(control['exit_code'])
        raise SystemExit(child_main())
    raise SystemExit(controller())
