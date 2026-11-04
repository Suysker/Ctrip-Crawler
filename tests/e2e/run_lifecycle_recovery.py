"""Bounded Linux lifecycle acceptance of one standalone artifact.

Real CLI processes and Camoufox are used. Website traffic is locally fulfilled
or aborted. A local controller restarts its own killed process; this does not
install or claim to exercise systemd/Task Scheduler on the deployment host.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse


CITIES = {'城市甲': 'AAA', '城市乙': 'BBB'}


def load_artifact(path):
    spec = importlib.util.spec_from_file_location('lifecycle_acceptance_app', path)
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    return app


def event(kind, **fields):
    with Path(os.environ['CTRIP_LIFECYCLE_EVENTS']).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'kind': kind, 'pid': os.getpid(), **fields}) + '\n')


def snapshot(query):
    segment = query['flightSegments'][0]
    flight = {'flightNo': 'TEST100', 'operateFlightNo': 'OPER100',
              'departureCityCode': segment['departureCityCode'],
              'arrivalCityCode': segment['arrivalCityCode'],
              'departureAirportCode': segment['departureCityCode'],
              'arrivalAirportCode': segment['arrivalCityCode'],
              'departureDateTime': segment['departureDate'] + ' 10:00:00',
              'arrivalDateTime': segment['departureDate'] + ' 12:00:00',
              'stopList': [], 'duration': 120}
    item = {'itineraryId': 'fixture-' + segment['departureCityCode'] + segment['departureDate'],
            'flightSegments': [{'transferCount': 0, 'flightList': [flight]}],
            'priceList': [{'adultPrice': 100, 'adultTax': 20, 'currency': 'CNY', 'cabin': 'Y'}]}
    return {'status': 0, 'msg': 'success', 'data': {'needUserLogin': False,
            'context': {'searchId': 'fixture-final', 'finished': True}, 'flightItineraryList': [item]}}


def fixture_worker(connection, config, identity, state):
    app = load_artifact(Path(os.environ['CTRIP_LIFECYCLE_ARTIFACT']))
    control_path = Path(os.environ['CTRIP_LIFECYCLE_CONTROL'])
    markers = Path(os.environ['CTRIP_LIFECYCLE_MARKERS'])

    class FixtureBrowser(app.BrowserSession):
        async def start(self):
            await super().start()
            await self.context.route('**/*', self.fulfill)

        async def fulfill(self, route):
            parsed = urlparse(route.request.url)
            control = app.read_json(control_path)
            if parsed.hostname != 'flights.ctrip.com':
                await route.abort('failed')
                return
            if parsed.path == urlparse(app.CTRIP_HOME).path:
                await route.fulfill(content_type='text/html', body='<!doctype html><title>Local city fixture</title>')
                return
            if parsed.path.startswith('/international/search/api/poi/search'):
                name = parse_qs(parsed.query)['key'][0]
                body = {'Status': 0, 'Key': name, 'Data': [
                    {'Type': 5, 'canSelect': True, 'Code': CITIES[name], 'Name': name}]}
                await route.fulfill(content_type='application/json', body=json.dumps(body))
                return
            if '/online/list/oneway-' in parsed.path:
                origin, destination = parsed.path.rsplit('oneway-', 1)[1].split('-')
                departure = parse_qs(parsed.query)['depdate'][0]
                if control['phase'] == 'term' and origin == 'aaa' and departure == '2099-01-02':
                    app.atomic_json(markers / 'term-ready.json', {'worker_pid': os.getpid()})
                    while not app.read_json(control_path).get('release'):
                        await asyncio.sleep(0.05)
                if control['phase'] == 'kill' and origin == 'bbb' and departure == '2099-01-01':
                    app.atomic_json(markers / 'kill-ready.json', {'worker_pid': os.getpid()})
                    await asyncio.sleep(30)
                if control['phase'] == 'finish' and origin == 'bbb' and departure == '2099-01-01' and control['failures'] < 2:
                    app.atomic_json(control_path, dict(control, failures=control['failures'] + 1))
                    event('navigation_abort')
                    await route.abort('failed')
                    return
                query = {'adultCount': 1, 'childCount': 0, 'infantCount': 0, 'flightWay': 'S',
                         'cabin': 'Y_S_C_F', 'transactionID': 'fixture-transaction', 'flightSegments': [{
                             'departureCityCode': origin.upper(), 'arrivalCityCode': destination.upper(),
                             'departureDate': departure}]}
                html = '<!doctype html><meta charset="utf-8"><script>window.onload=()=>fetch(' \
                       + json.dumps(app.SEARCH_API_PATH) + ',{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(' \
                       + json.dumps(query) + ')});</script>'
                await route.fulfill(content_type='text/html', body=html)
                return
            if parsed.path == app.SEARCH_API_PATH:
                await route.fulfill(content_type='application/json', body=json.dumps(snapshot(route.request.post_data_json)))
                return
            await route.abort('failed')

    app.BrowserSession = FixtureBrowser
    app.worker_main(connection, config, identity, state)


def child_main():
    app = load_artifact(Path(os.environ['CTRIP_LIFECYCLE_ARTIFACT']))
    real_client = app.WorkerClient
    def fixture_client(config, identity, state, **kwargs):
        event('worker_created')
        return real_client(config, identity, state, target=fixture_worker, **kwargs)
    app.WorkerClient = fixture_client
    # Test-only durations keep this representative failure sequence bounded.
    app.DEFAULT_CONFIG.update(proxy=None, accounts=[], login_allowed=False, comft_flight=False,
        crawl_interval=0, retry_delays=[0, 0], disk_reserve_mb=0, attempt_timeout=12,
        navigation_timeout=10, task_budget=60, close_timeout=2,
        auto_recovery_rounds=1, auto_recovery_cooldown=0.2)
    return app.main(json.loads(os.environ['CTRIP_LIFECYCLE_ARGS']))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def controller():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, default=Path(__file__).resolve().parents[2] / 'ctrip_flights_scraper.py')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('Run this POSIX SIGTERM/SIGKILL scenario on Linux or WSL')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('Use an empty output directory; existing evidence is preserved')
    artifact = output / 'standalone/ctrip_flights_scraper.py'
    artifact.parent.mkdir()
    shutil.copyfile(args.artifact.resolve(), artifact)
    app = load_artifact(artifact)
    app._enable_child_subreaper()  # Reap only registered owned orphans in this test controller.
    data, markers = output / 'data', output / 'markers'
    markers.mkdir()
    control_path = output / 'fixture-control.json'
    events_path = output / 'events.jsonl'
    base_argv = ['--anonymous', '--cities', *CITIES, '--begin-date', '2099-01-01', '--days', '2',
                 '--output', str(data), '--run-id', 'lifecycle-fixed']
    env = dict(os.environ, CTRIP_LIFECYCLE_ARTIFACT=str(artifact), CTRIP_LIFECYCLE_CONTROL=str(control_path),
               CTRIP_LIFECYCLE_EVENTS=str(events_path), CTRIP_LIFECYCLE_MARKERS=str(markers),
               CTRIP_LIFECYCLE_ARGS=json.dumps(base_argv), PYTHONFAULTHANDLER='1')
    began = time.monotonic()
    processes, streams = [], []
    report = {'kind': 'controlled-site-real-browser-parent-lifecycle', 'status': 'running',
              'artifact_sha256': digest(artifact), 'started_at': app.utc_now(), 'stages': [],
              'service_installed': False}
    app.atomic_json(output / 'acceptance.json', report)

    def launch(label):
        out, err = output / (label + '.stdout.log'), output / (label + '.stderr.log')
        streams.extend([out.open('wb'), err.open('wb')])
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child'],
                                   stdout=streams[-2], stderr=streams[-1], env=env)
        processes.append(process)
        report['stages'].append({'label': label, 'pid': process.pid, 'stdout': out.name, 'stderr': err.name})
        return process

    def wait_marker(process, name):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if (markers / name).exists():
                return
            if process.poll() is not None:
                raise AssertionError(f'CLI exited {process.returncode} before {name}; inspect {report["stages"][-1]["stderr"]}')
            time.sleep(0.05)
        raise AssertionError(f'CLI did not reach {name} within the bounded wait')

    def owned():
        record = data / '.runs/lifecycle-fixed/owner.json'
        return app.read_json(record).get('processes', {}) if record.exists() else {}

    def reap_finished_owned(timeout=5):
        import psutil
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = []
            for pid, created in owned().items():
                try:
                    process = psutil.Process(int(pid))
                    if process.create_time() != created:
                        continue
                    if process.status() == psutil.STATUS_ZOMBIE:
                        if process.ppid() == os.getpid():
                            os.waitpid(int(pid), os.WNOHANG)
                    else:
                        alive.append(int(pid))
                except (psutil.NoSuchProcess, ChildProcessError):
                    continue
            if not alive:
                return
            time.sleep(0.05)
        raise AssertionError(f'Owned processes did not exit after parent loss: {alive}')

    try:
        app.atomic_json(control_path, {'phase': 'term', 'release': False, 'failures': 0})
        first = launch('initial')
        wait_marker(first, 'term-ready.json')
        duplicate = launch('duplicate')
        assert duplicate.wait(timeout=15) == 4
        first.send_signal(signal.SIGTERM)
        app.atomic_json(control_path, {'phase': 'term', 'release': True, 'failures': 0})
        assert first.wait(timeout=15) == 143
        progress_path = data / '.runs/lifecycle-fixed/progress.json'
        plan_path = progress_path.with_name('run.json')
        progress = app.read_json(progress_path)
        assert progress['status'] == 'interrupted' and progress['cursor'] == 2
        plan_hash = digest(plan_path)
        original_results = {path: digest(path) for path in [*data.glob('*/*/*.csv'), *data.glob('*/*/*.meta.json')]}
        assert len(original_results) == 4

        app.atomic_json(control_path, {'phase': 'kill', 'release': False, 'failures': 0})
        killed = launch('restart-before-kill')
        wait_marker(killed, 'kill-ready.json')
        killed.kill()
        assert killed.wait(timeout=5) == -signal.SIGKILL
        interrupted = app.read_json(progress_path)
        assert interrupted['cursor'] == 2 and interrupted['current']['attempt_count'] == 1
        reserved = interrupted['current']['reserved']
        assert reserved > 0
        reap_finished_owned()

        app.atomic_json(control_path, {'phase': 'finish', 'release': True, 'failures': 0})
        resumed = launch('restart-and-auto-recover')
        assert resumed.wait(timeout=60) == 0
        completed = app.read_json(progress_path)
        assert completed['status'] == 'completed' and completed['cursor'] == 4
        assert completed['stats']['succeeded'] == 4
        assert digest(plan_path) == plan_hash
        assert all(digest(path) == expected for path, expected in original_results.items())
        receipts = [app.read_json(path) for path in data.glob('*/*/*.meta.json')]
        recovered, = [receipt for receipt in receipts if receipt['task_index'] == 2]
        recovery, = recovered['automatic_recoveries']
        assert recovery['attempt_count'] == recovery['old_max_attempts'] == 3
        assert recovery['new_max_attempts'] == 6
        assert recovery['work_used'] >= reserved
        assert app.read_json(control_path)['failures'] == 2
        all_hashes = {path: digest(path) for path in [*data.glob('*/*/*.csv'), *data.glob('*/*/*.meta.json')]}
        workers = events_path.read_text(encoding='utf-8').count('"kind": "worker_created"')
        done = launch('completed-idempotent-start')
        assert done.wait(timeout=15) == 0
        assert events_path.read_text(encoding='utf-8').count('"kind": "worker_created"') == workers
        assert all(digest(path) == expected for path, expected in all_hashes.items())
        assert digest(plan_path) == plan_hash
        reap_finished_owned()
        report.update(status='passed', run_id='lifecycle-fixed', cursor=4, receipts=len(receipts),
                      sigterm_exit=143, sigkill_exit=-signal.SIGKILL, duplicate_exit=4,
                      original_results_unchanged=True, run_json_unchanged=True,
                      completed_start_without_worker=True, automatic_recoveries=recovered['automatic_recoveries'],
                      previous_reserved_seconds=reserved)
    except BaseException as error:
        frames, trace = [], error.__traceback__
        while trace:
            frames.append({'file': trace.tb_frame.f_code.co_filename, 'line': trace.tb_lineno})
            trace = trace.tb_next
        report.update(status='failed', error_type=type(error).__name__, message=str(error), frames=frames)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for stream in streams:
            stream.close()
        remaining = owned()
        if remaining:
            try:
                app._terminate_owned_processes(remaining, timeout=1)
            except Exception as error:
                report['cleanup_error'] = type(error).__name__
                report['status'] = 'failed'
        report.update(finished_at=app.utc_now(), elapsed_seconds=round(time.monotonic() - began, 3))
        app.atomic_json(output / 'acceptance.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    if '--child' in sys.argv:
        raise SystemExit(child_main())
    raise SystemExit(controller())
