"""Controlled-site soak of the distributed script, real Camoufox and real IPC.

Only website traffic is fulfilled by a deterministic local fixture. This is not
a claim about continuous access to Ctrip. CTRIP_ACCEPTANCE_APP must point to an
isolated copy of the single distribution file; no source sections are loaded.
"""
from __future__ import annotations
import argparse
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse


APP_PATH = Path(os.environ['CTRIP_ACCEPTANCE_APP']).resolve()
spec = importlib.util.spec_from_file_location('acceptance_app', APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)

FIXTURE_CITIES = {'城市甲': 'AAA', '城市乙': 'BBB'}


def snapshot(query, finished):
    segment = query['flightSegments'][0]
    rows = []
    for index in range(32 if finished else 2):
        flight = {
            'flightNo': f'TEST{index // 2:03}', 'operateFlightNo': f'OPER{index:03}',
            'marketAirlineName': 'Fixture Airline',
            'departureCityCode': segment['departureCityCode'],
            'arrivalCityCode': segment['arrivalCityCode'],
            'departureCityName': segment['departureCityCode'],
            'arrivalCityName': segment['arrivalCityCode'],
            'departureAirportCode': 'AAX' if index % 2 else 'AAY',
            'arrivalAirportCode': 'BBX',
            'departureDateTime': segment['departureDate'] + ' 10:00:00',
            'arrivalDateTime': segment['departureDate'] + ' 12:00:00',
            'stopList': [], 'duration': 120,
        }
        prices = [{'adultPrice': base, 'adultTax': 20, 'currency': 'CNY', 'cabin': cabin}
                  for cabin in ('Y', 'C') for base in (100 + index, 110 + index)]
        rows.append({'itineraryId': f'route-{index}',
                     'flightSegments': [{'transferCount': 0, 'flightList': [flight]}],
                     'priceList': prices})
    return {'status': 0, 'msg': 'success', 'data': {
        'needUserLogin': False, 'context': {'finished': finished, 'searchId': 'fixture-final' if finished else 'fixture-pending',
                                          'showAuthCode': False}, 'flightItineraryList': rows}}


class FixtureBrowser(app.BrowserSession):
    async def start(self):
        await super().start()
        await self.context.route('**/*', self.fulfill)

    async def fulfill(self, route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.hostname != 'flights.ctrip.com':
            await route.abort()
            return
        if parsed.path.startswith('/international/search/api/poi/search'):
            name = parse_qs(parsed.query)['key'][0]
            payload = {'Status': 0, 'Key': name, 'Data': [
                {'Type': 5, 'canSelect': True, 'Code': FIXTURE_CITIES[name], 'Name': name}]}
            await route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
        elif parsed.path == app.SEARCH_API_PATH or '/search/pull/' in parsed.path:
            query = request.post_data_json
            await route.fulfill(status=200, content_type='application/json',
                                body=json.dumps(snapshot(query, '/search/pull/' in parsed.path)))
        elif '/online/list/oneway-' in parsed.path:
            origin, destination = parsed.path.rsplit('oneway-', 1)[1].split('-')
            departure = parse_qs(parsed.query)['depdate'][0]
            query = {'adultCount': 1, 'childCount': 0, 'infantCount': 0,
                     'cabin': 'Y_S_C_F', 'flightWay': 'S', 'transactionID': 'fixture-transaction',
                     'flightSegments': [{'departureCityCode': origin.upper(),
                                        'arrivalCityCode': destination.upper(), 'departureDate': departure}]}
            html = '''<!doctype html><meta charset="utf-8"><title>Controlled flight fixture</title>
<script>
const query = QUERY;
async function run() {
  let path = '/international/search/api/search/batchSearch';
  while (true) {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(query)});
    const payload = await response.json();
    if (payload.data.context.finished) { document.body.textContent = 'complete'; break; }
    path = '/international/search/api/search/pull/' + payload.data.context.searchId;
  }
}
window.onload = run;
</script><body>loading</body>'''.replace('QUERY', json.dumps(query))
            await route.fulfill(status=200, content_type='text/html', body=html)
        else:
            await route.fulfill(status=200, content_type='text/html', body='<title>Controlled home</title>')


def fixture_worker(connection, config, identity, state):
    app.BrowserSession = FixtureBrowser
    app.worker_main(connection, config, identity, state)


def fixture_client(config, identity, state, **kwargs):
    return app.WorkerClient(config, identity, state, target=fixture_worker, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--tasks', type=int, default=10000)
    parser.add_argument('--hours', type=float, default=24)
    args = parser.parse_args()
    if args.tasks < 2 or args.tasks % 2 or args.hours < 0:
        parser.error('tasks must be an even positive number >=2, hours nonnegative')
    args.output.mkdir(parents=True, exist_ok=True)
    artifact = args.output / 'acceptance.json'
    if artifact.exists():
        parser.error('use a new output directory; existing acceptance is preserved')
    config = dict(app.DEFAULT_CONFIG, login_allowed=False, accounts=[],
                  output=str(args.output / 'data'), crawl_citys=list(FIXTURE_CITIES),
                  begin_date='2090-01-01', crawl_days=args.tasks // 2,
                  headless=True, crawl_interval=args.hours * 3600 / (args.tasks - 1),
                  disk_reserve_mb=32, recycle_tasks=100)
    config = app.validate_config(config)
    app.check_environment()
    app.browser_environment_check()
    metrics = args.output / 'metrics.jsonl'
    stop = threading.Event()
    began = time.monotonic()
    report = {'kind': 'controlled-site-real-browser', 'started_at': app.utc_now(),
              'requested_tasks': args.tasks, 'requested_hours': args.hours,
              'artifact_sha256': app.hashlib.sha256(APP_PATH.read_bytes()).hexdigest(),
              'memory_hard_budget_bytes': config['memory_limit_mb'] * 1024 * 1024,
              'status': 'running'}
    app.atomic_json(artifact, report)
    runner = app.Runner(config, worker_factory=fixture_client)

    def sample():
        import psutil
        parent = psutil.Process()
        with metrics.open('a', encoding='utf-8') as handle:
            while not stop.is_set():
                processes = [parent, *parent.children(recursive=True)]
                rss = threads = count = 0
                details = []
                for process in processes:
                    try:
                        process_rss = process.memory_info().rss
                        process_threads = process.num_threads()
                        resources = process.num_handles() if os.name == 'nt' else process.num_fds()
                        details.append({'pid': process.pid, 'created_at': process.create_time(),
                                        'name': process.name(), 'rss_bytes': process_rss,
                                        'threads': process_threads, 'handles_or_fds': resources})
                        rss += process_rss
                        threads += process_threads
                        count += 1
                    except psutil.NoSuchProcess:
                        continue
                handle.write(json.dumps({'seconds': round(time.monotonic() - began, 3),
                                         'rss_bytes': rss, 'threads': threads, 'processes': count,
                                         'parent_pid': parent.pid, 'details': details,
                                         'worker_generation': runner.generation,
                                         'cursor': runner.control.get('cursor', 0)}) + '\n')
                handle.flush()
                stop.wait(5)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        code = runner.run()
        report.update(status='passed' if code == 0 else 'failed', exit_code=code,
                      cursor=runner.control.get('cursor'), stats=runner.control.get('stats'),
                      run_id=runner.control.get('run_id'))
        receipts = list((args.output / 'data').glob('*/*/*.meta.json'))
        assert len(receipts) == args.tasks
        assert all(app.read_json(path)['row_count'] == 32 for path in receipts)
        before = {path: path.read_bytes() for path in receipts}
        assert app.Runner(config, worker_factory=lambda *a, **k: (_ for _ in ()).throw(AssertionError('resume launched browser'))).run(
            resume=runner.control['run_id']) == 0
        assert all(path.read_bytes() == raw for path, raw in before.items())
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__, error_code=getattr(exc, 'code', None))
        raise
    finally:
        stop.set()
        sampler.join(timeout=6)
        report.update(finished_at=app.utc_now(), elapsed_seconds=time.monotonic() - began)
        if report['status'] == 'passed' and report['elapsed_seconds'] < args.hours * 3600:
            report['status'] = 'duration_not_met'
        app.atomic_json(artifact, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    app.multiprocessing.freeze_support()
    raise SystemExit(main())
