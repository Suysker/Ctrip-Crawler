"""Bounded incident-recovery E2E using an isolated standalone artifact.

Scenario: complete task 0; pause task 1 on an invalid completion notice; one
resume receives two real navigation errors and exhausts its third attempt;
repair the local fixture and use --retry-current 1:3 to finish the same run.
Both successful searches contain initial and intermediate pending notices
without itineraries before their final full snapshot. All page requests are
fulfilled or aborted locally. No Ctrip service is contacted and no soak runs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.parse import parse_qs, urlparse


CITIES = {'城市甲': 'AAA', '城市乙': 'BBB'}


def load_artifact(path):
    spec = importlib.util.spec_from_file_location('incident_acceptance_app', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def event(kind, **fields):
    record = {'at': datetime.now(timezone.utc).isoformat(), 'kind': kind, **fields}
    with Path(os.environ['CTRIP_INCIDENT_EVENTS']).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def complete_snapshot(query):
    segment = query['flightSegments'][0]
    flight = {'flightNo': 'TEST100', 'operateFlightNo': 'OPER100', 'marketAirlineName': 'Fixture Airline',
              'departureCityCode': segment['departureCityCode'], 'arrivalCityCode': segment['arrivalCityCode'],
              'departureAirportCode': segment['departureCityCode'], 'arrivalAirportCode': segment['arrivalCityCode'],
              'departureDateTime': segment['departureDate'] + ' 10:00:00',
              'arrivalDateTime': segment['departureDate'] + ' 12:00:00', 'duration': 120, 'stopList': []}
    itinerary = {'itineraryId': 'fixture-' + segment['departureCityCode'],
                 'flightSegments': [{'transferCount': 0, 'flightList': [flight]}],
                 'priceList': [{'adultPrice': 100, 'adultTax': 20, 'currency': 'CNY', 'cabin': 'Y'}]}
    return {'status': 0, 'msg': 'success', 'data': {'needUserLogin': False,
            'context': {'searchId': 'fixture-final', 'finished': True}, 'flightItineraryList': [itinerary]}}


def fixture_worker(connection, config, identity, state):
    app = load_artifact(Path(os.environ['CTRIP_INCIDENT_ARTIFACT']))

    class FixtureBrowser(app.BrowserSession):
        async def start(self):
            await super().start()
            await self.context.route('**/*', self.fulfill)

        async def fulfill(self, route):
            request = route.request
            parsed = urlparse(request.url)
            phase = json.loads(Path(os.environ['CTRIP_INCIDENT_CONTROL']).read_text(encoding='utf-8'))['phase']
            if parsed.hostname != 'flights.ctrip.com':
                event('local_abort', phase=phase, resource='other-origin')
                await route.abort('failed')
                return
            if parsed.path.startswith('/international/search/api/poi/search'):
                name = parse_qs(parsed.query)['key'][0]
                payload = {'Status': 0, 'Key': name, 'Data': [
                    {'Type': 5, 'canSelect': True, 'Code': CITIES[name], 'Name': name}]}
                event('local_fulfill', phase=phase, resource='city')
                await route.fulfill(content_type='application/json', body=json.dumps(payload))
            elif '/online/list/oneway-' in parsed.path:
                origin, destination = parsed.path.rsplit('oneway-', 1)[1].split('-')
                if phase == 'browser_error' and origin.upper() == 'BBB':
                    event('navigation_abort', phase=phase, origin=origin.upper())
                    await route.abort('failed')
                    return
                departure = parse_qs(parsed.query)['depdate'][0]
                query = {'adultCount': 1, 'childCount': 0, 'infantCount': 0, 'flightWay': 'S',
                         'cabin': 'Y_S_C_F', 'transactionID': 'fixture-current-query',
                         'flightSegments': [{'departureCityCode': origin.upper(),
                                             'arrivalCityCode': destination.upper(), 'departureDate': departure}]}
                html = '''<!doctype html><meta charset="utf-8"><title>Local incident fixture</title>
<script>
const query = QUERY;
window.onload = async () => {
  let path = '/international/search/api/search/batchSearch';
  while (true) {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(query)});
    const payload = await response.json();
    if (payload.data.context.finished) { document.body.textContent = 'complete'; break; }
    path = '/international/search/api/search/pull/' + payload.data.context.searchId;
  }
};
</script><body>loading</body>'''.replace('QUERY', json.dumps(query))
                event('local_fulfill', phase=phase, resource='list', origin=origin.upper())
                await route.fulfill(content_type='text/html', body=html)
            elif parsed.path == app.SEARCH_API_PATH or '/search/pull/' in parsed.path:
                query = request.post_data_json
                origin = query['flightSegments'][0]['departureCityCode']
                if phase == 'protocol' and origin == 'BBB':
                    payload = {'status': 0, 'msg': 'success', 'data': {'needUserLogin': False,
                               'context': {'searchId': 'fixture-invalid-completion', 'finished': True}}}
                elif parsed.path.endswith('/fixture-middle'):
                    payload = complete_snapshot(query)
                else:
                    search_id = 'fixture-initial' if parsed.path == app.SEARCH_API_PATH else 'fixture-middle'
                    payload = {'status': 0, 'msg': 'success', 'data': {'needUserLogin': False,
                               'context': {'searchId': search_id, 'finished': False}}}
                event('local_fulfill', phase=phase, resource='search', origin=origin,
                      finished=payload['data']['context']['finished'],
                      list_present='flightItineraryList' in payload['data'])
                await route.fulfill(content_type='application/json', body=json.dumps(payload))
            else:
                event('local_fulfill', phase=phase, resource='home-or-static')
                await route.fulfill(content_type='text/html', body='<title>Local controlled home</title>')

    app.BrowserSession = FixtureBrowser
    app.worker_main(connection, config, identity, state)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, default=Path(__file__).resolve().parents[2] / 'ctrip_flights_scraper.py')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('Use an empty output directory; existing evidence is preserved')
    artifact = output / 'standalone' / 'ctrip_flights_scraper.py'
    artifact.parent.mkdir()
    shutil.copyfile(args.artifact.resolve(), artifact)
    os.environ.update(CTRIP_INCIDENT_ARTIFACT=str(artifact),
                      CTRIP_INCIDENT_CONTROL=str(output / 'fixture-control.json'),
                      CTRIP_INCIDENT_EVENTS=str(output / 'events.jsonl'))
    app = load_artifact(artifact)
    real_client = app.WorkerClient

    def fixture_client(config, identity, state, **kwargs):
        event('worker_created', phase=json.loads(Path(os.environ['CTRIP_INCIDENT_CONTROL']).read_text())['phase'])
        return real_client(config, identity, state, target=fixture_worker, **kwargs)

    app.WorkerClient = fixture_client
    app.DEFAULT_CONFIG.update(crawl_interval=0.0, retry_delays=[0, 0], disk_reserve_mb=0,
                              comft_flight=False, auto_recovery_rounds=0)
    data = output / 'data'
    began = time.monotonic()
    report = {'kind': 'controlled-site-incident-recovery-real-browser', 'status': 'running',
              'artifact_sha256': digest(artifact), 'started_at': app.utc_now(), 'stages': []}
    app.atomic_json(output / 'acceptance.json', report)

    def phase(value):
        app.atomic_json(output / 'fixture-control.json', {'phase': value})

    def invoke(label, argv):
        event('cli_invocation', label=label)
        code = app.main(['--output', str(data), *argv])
        report['stages'].append({'label': label, 'exit_code': code})
        app.atomic_json(output / 'acceptance.json', report)
        return code

    try:
        phase('protocol')
        assert invoke('first-task-complete-second-protocol-pause', [
            '--anonymous', '--cities', *CITIES, '--begin-date', '2099-01-01', '--days', '1']) == 3
        progress_path, = data.glob('.runs/*/progress.json')
        run_id = progress_path.parent.name
        plan_path = progress_path.with_name('run.json')
        original_plan = plan_path.read_bytes()
        progress = app.read_json(progress_path)
        assert progress['cursor'] == 1 and progress['stats']['succeeded'] == 1
        assert progress['current']['attempt_count'] == 1 and progress['last_error']['code'] == 'protocol_changed'
        original_csv, = data.glob('*/*/*.csv')
        original_receipt = original_csv.with_suffix('.meta.json')
        original_hashes = {original_csv: digest(original_csv), original_receipt: digest(original_receipt)}

        phase('browser_error')
        assert invoke('one-resume-two-browser-errors-exhausted', ['--resume', run_id]) == 3
        exhausted = app.read_json(progress_path)
        assert exhausted['cursor'] == 1 and exhausted['stats']['succeeded'] == 1
        assert exhausted['current']['attempt_count'] == exhausted['current']['max_attempts'] == 3
        assert exhausted['pause_reason'] == 'task_exhausted' and exhausted['last_error']['code'] == 'browser_error'
        assert exhausted['last_error']['diagnostic']['stage'] == 'collect.navigation'
        assert exhausted['last_error']['diagnostic']['exception_type'] == 'Error'
        assert exhausted['current'].get('extra_work_budget', 0) == 0
        assert not exhausted['current'].get('manual_recoveries')
        assert sum(item['kind'] == 'navigation_abort' for item in read_events(output / 'events.jsonl')) == 2
        workers = sum(item['kind'] == 'worker_created' for item in read_events(output / 'events.jsonl'))
        assert invoke('ordinary-resume-cannot-reset-exhausted-budget', ['--resume', run_id]) == 3
        assert sum(item['kind'] == 'worker_created' for item in read_events(output / 'events.jsonl')) == workers
        assert app.read_json(progress_path)['current'] == exhausted['current']

        phase('repaired')
        assert invoke('explicit-manual-retry-completes-same-run', [
            '--resume', run_id, '--retry-current', '1:3', '--recovery-reason', 'Controlled local fixture repaired']) == 0
        completed = app.read_json(progress_path)
        assert completed['status'] == 'completed' and completed['cursor'] == 2
        assert completed['stats']['succeeded'] == 2 and completed['stats']['empty'] == 0
        assert plan_path.read_bytes() == original_plan
        assert all(digest(path) == expected for path, expected in original_hashes.items())
        receipts = list(data.glob('*/*/*.meta.json'))
        assert len(receipts) == len(list(data.glob('*/*/*.csv'))) == 2
        manual_receipt, = [app.read_json(path) for path in receipts if path != original_receipt]
        recovery, = manual_receipt['manual_recoveries']
        assert recovery['token'] == '1:3' and recovery['attempt_count'] == 3
        assert recovery['old_max_attempts'] == 3 and recovery['new_max_attempts'] == 6
        assert recovery['old_task_budget'] == 300 and recovery['new_task_budget'] == 600
        assert recovery['work_used'] >= exhausted['current']['work_used']
        assert app.read_json(original_receipt).get('manual_recoveries') is None
        hashes = {path: digest(path) for path in [*data.glob('*/*/*.csv'), *receipts]}
        workers = sum(item['kind'] == 'worker_created' for item in read_events(output / 'events.jsonl'))
        assert invoke('completed-resume-does-not-republish-or-launch-browser', ['--resume', run_id]) == 0
        assert sum(item['kind'] == 'worker_created' for item in read_events(output / 'events.jsonl')) == workers
        assert all(digest(path) == expected for path, expected in hashes.items())
        assert plan_path.read_bytes() == original_plan
        searches = [item for item in read_events(output / 'events.jsonl') if item.get('resource') == 'search']
        assert sum(not item['finished'] and not item['list_present'] for item in searches) == 4
        report.update(status='passed', run_id=run_id, cursor=completed['cursor'], stats=completed['stats'],
                      first_csv_unchanged=True, run_json_unchanged=True, receipt_count=2,
                      navigation_errors=2, manual_recoveries=manual_receipt['manual_recoveries'],
                      successful_search_pending_notices=4,
                      output_sha256={str(path.relative_to(output)): expected for path, expected in hashes.items()})
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__, error_code=getattr(exc, 'code', None))
        raise
    finally:
        report.update(finished_at=app.utc_now(), elapsed_seconds=round(time.monotonic() - began, 3))
        app.atomic_json(output / 'acceptance.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
