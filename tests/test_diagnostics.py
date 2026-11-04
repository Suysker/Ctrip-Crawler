"""Safe, useful errors survive the actual worker failure transport."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from section_loader import load_sections


def test_diagnostic_preserves_failure_location_without_secrets_or_local_values():
    app = load_sections('imports')
    password = 'p@ss/secret?value'
    config = {'accounts': [{'username': 'sensitive-user@example.test',
                            'password': password}],
              'proxy': {'server': 'http://proxy-user:proxy-pass@proxy.example.test:8080',
                        'username': 'proxy-user', 'password': 'proxy-pass'}}
    private_local = 'must-not-be-captured-from-locals'
    try:
        raise ValueError(
            'conversion failed for sensitive-user@example.test ' + password
            + ' via https://proxy-user:proxy-pass@proxy.example.test:8080/pull/raw-token?token=query-secret'
            + ' wss://browser.example.test/session/websocket-token?key=socket-secret'
            + ' at "C:\\Users\\private-name\\AppData\\Local\\file.py"'
            + ' and /home/private-name/cache/file.py')
    except ValueError as error:
        diagnostic = app.make_diagnostic(error, stage='collect.normalize', config=config)
    text = json.dumps(diagnostic, ensure_ascii=False)
    assert diagnostic['exception_type'] == 'ValueError'
    assert diagnostic['stage'] == 'collect.normalize'
    assert 'conversion failed' in diagnostic['message']
    assert diagnostic['frames'][-1]['function'] == 'test_diagnostic_preserves_failure_location_without_secrets_or_local_values'
    assert diagnostic['frames'][-1]['line'] > 0
    for secret in ('sensitive-user@example.test', password, 'proxy-user', 'proxy-pass',
                   'raw-token', 'query-secret', 'websocket-token', 'socket-secret', 'private-name', private_local):
        assert secret not in text
    assert all('/' not in frame['file'] and '\\' not in frame['file'] for frame in diagnostic['frames'])


def test_diagnostic_is_bounded_and_retains_safe_protocol_summary_only():
    app = load_sections('imports')
    error = app.CrawlerError('protocol_changed', 'invalid protocol ' + '长' * 50000,
        diagnostic={'stage': 'collect.protocol',
                    'frames': [{'file': '模块' * 60 + '.py', 'line': 5, 'function': '函数' * 60} for _ in range(8)],
                    'protocol': {
            'http_status': 200, 'business_status': 0, 'finished': False,
            'list_present': False, 'known_snapshot': False, 'request_matches': True,
            'raw_response': {'cookie': 'unrelated-cookie-value'},
            'search_id': 'unrelated-query-token'}})
    diagnostic = app.make_diagnostic(error)
    assert diagnostic['protocol']['http_status'] == 200
    assert diagnostic['protocol']['known_snapshot'] is False
    assert diagnostic['stage'] == 'collect.protocol'
    assert len(json.dumps(diagnostic, ensure_ascii=False).encode('utf-8')) <= 8192
    assert 'unrelated-cookie-value' not in json.dumps(diagnostic)
    assert 'unrelated-query-token' not in json.dumps(diagnostic)


def test_parent_resanitization_keeps_the_original_exception_cause():
    app = load_sections('imports')
    try:
        try:
            raise ValueError('invalid quote amount')
        except ValueError as cause:
            raise app.CrawlerError('data_invalid', 'quote conversion failed') from cause
    except app.CrawlerError as error:
        child = app.make_diagnostic(error, stage='collect.normalize')
    forwarded = app.CrawlerError('data_invalid', child['message'], diagnostic=child)
    parent = app.make_diagnostic(forwarded, stage='parent.collect')
    assert parent['exception_type'] == child['exception_type']
    assert parent['stage'] == 'collect.normalize'
    assert parent['causes'] == [{'exception_type': 'ValueError', 'message': 'invalid quote amount'}]


def test_command_credentials_and_embedded_payloads_are_not_replayed_in_diagnostics():
    app = load_sections('imports')
    command = {'value': {'account': {'username': 'command-user', 'password': 'command-password'}}}
    error = RuntimeError('authentication failed command-user command-password; '
                         'Authorization: Bearer unconfigured-header-token\n'
                         'payload={"flightItineraryList":[{"raw":"full-response-content"}]}')
    diagnostic = app.make_diagnostic(error, command=command)
    text = json.dumps(diagnostic)
    assert 'authentication failed' in text
    for secret in ('command-user', 'command-password', 'unconfigured-header-token', 'full-response-content'):
        assert secret not in text


@pytest.mark.parametrize('status', [None, 503])
def test_network_summary_survives_parent_sanitization_without_raw_requests(status):
    app = load_sections('imports')
    network = {'response_kind': 'search', 'http_status': status, 'method': 'POST',
               'request_failed': True, 'failed_request_count': 2,
               'search_responses_received': 1, 'browser_error_code': 'NS_ERROR_PROXY_FORBIDDEN'}
    error = app.CrawlerError('response_timeout', 'Search did not finish', diagnostic={'network': dict(
        network, url='https://private.invalid/?token=private-token', headers={'cookie': 'private-cookie'})})
    child = app.make_diagnostic(error, stage='collect.wait_response')
    parent = app.make_diagnostic(app.CrawlerError('response_timeout', child['message'], diagnostic=child))
    assert child['network'] == network
    assert parent['network'] == network
    assert 'private-token' not in json.dumps(parent)
    assert 'private-cookie' not in json.dumps(parent)


def test_network_summary_rejects_invalid_enums_types_and_negative_counts():
    app = load_sections('imports')
    error = app.CrawlerError('network_error', 'Network failed', diagnostic={'network': {
        'response_kind': 'https://private.invalid/', 'method': 'private-method',
        'http_status': True, 'request_failed': 'private-failure-payload',
        'failed_request_count': -1, 'search_responses_received': False,
        'browser_error_code': 'https://private.invalid/?token=private-token'}})
    diagnostic = app.make_diagnostic(error)
    assert not diagnostic.get('network')
    assert 'private' not in json.dumps(diagnostic)


class DiagnosticSession:
    def __init__(self, config, *unused):
        self.config = config
        self.diagnostic_stage = 'fixture.collect.normalize'

    async def start(self):
        pass

    async def collect(self, task, timeout):
        kind = self.config['_diagnostic_kind']
        if kind == 'proxy_denied':
            from playwright.async_api import Error
            error = Error('Page.goto: NS_ERROR_PROXY_FORBIDDEN')
            error.diagnostic = {'network': {'response_kind': 'navigation', 'request_failed': True,
                                           'failed_request_count': 1, 'search_responses_received': 0}}
            raise error
        if kind == 'known_browser':
            from playwright.async_api import Error
            raise Error('browser transport lost for current-user')
        if kind == 'browser_timeout':
            from playwright.async_api import TimeoutError
            raise TimeoutError('browser action deadline for current-user')
        raise ValueError('unexpected conversion for current-user current-password')

    async def close(self):
        pass


def diagnostic_worker(connection, config, identity, state):
    app = load_sections('imports', 'config', 'worker')
    app.BrowserSession = DiagnosticSession
    app.worker_main(connection, config, identity, state)


@pytest.mark.parametrize('kind,code,error_type,excerpt', [
    ('unknown', 'worker_error', 'ValueError', 'conversion'),
    ('known_browser', 'browser_error', 'Error', 'browser'),
    ('browser_timeout', 'response_timeout', 'TimeoutError', 'browser'),
    ('proxy_denied', 'proxy_access_denied', 'Error', 'NS_ERROR_PROXY_FORBIDDEN'),
])
def test_real_worker_preserves_safe_diagnostics_and_does_not_retry_programming_errors(kind, code, error_type, excerpt):
    if kind != 'unknown':
        pytest.importorskip('playwright.async_api')
    app = load_sections('imports', 'config', 'worker')
    config = dict(app.DEFAULT_CONFIG, close_timeout=0.2, _diagnostic_kind=kind,
                  account={'username': 'current-user', 'password': 'current-password'})
    with app.WorkerClient(config, {}, {}, generation=3, target=diagnostic_worker) as worker:
        worker.start(timeout=5)
        with pytest.raises(app.CrawlerError) as caught:
            worker.call('collect', {'task': {'key': 'task', 'run_id': 'run', 'index': 0}},
                        timeout=3, attempt_id='attempt')
    assert caught.value.code == code
    diagnostic = caught.value.diagnostic
    assert diagnostic['exception_type'] == error_type
    assert diagnostic['stage'] == 'fixture.collect.normalize'
    assert diagnostic['frames']
    assert 'current-user' not in json.dumps(diagnostic)
    assert 'current-password' not in str(caught.value)
    assert excerpt in diagnostic['message']
    if kind == 'proxy_denied':
        assert diagnostic['network']['browser_error_code'] == 'NS_ERROR_PROXY_FORBIDDEN'
        assert diagnostic['network']['response_kind'] == 'navigation'
        assert diagnostic['network']['failed_request_count'] == 1


@pytest.mark.parametrize('symbol,expected', [
    ('NS_ERROR_PROXY_UNAUTHORIZED', 'proxy_access_denied'),
    ('NS_ERROR_PROXY_FORBIDDEN', 'proxy_access_denied'),
    ('NS_ERROR_PROXY_AUTHENTICATION_FAILED', 'proxy_access_denied'),
    ('NS_ERROR_PROXY_NETWORK_AUTHENTICATION_REQUIRED', 'proxy_access_denied'),
    ('NS_ERROR_PROXY_CONNECTION_REFUSED', 'proxy_unavailable'),
    ('NS_ERROR_UNKNOWN_PROXY_HOST', 'proxy_unavailable'),
    ('NS_ERROR_PROXY_REQUEST_TIMEOUT', 'proxy_unavailable'),
    ('NS_ERROR_PROXY_GATEWAY_TIMEOUT', 'proxy_unavailable'),
    ('NS_ERROR_PROXY_BAD_GATEWAY', 'proxy_unavailable'),
    ('NS_ERROR_PROXY_TOO_MANY_REQUESTS', 'proxy_unavailable'),
    ('NS_ERROR_CONNECTION_REFUSED', 'network_error'),
    ('NS_ERROR_NET_TIMEOUT', 'network_error'),
    ('NS_ERROR_NET_RESET', 'network_error'),
    ('NS_ERROR_NET_INTERRUPT', 'network_error'),
    ('NS_ERROR_UNKNOWN_HOST', 'network_error'),
    ('NS_ERROR_DEFINITIVE_UNKNOWN_HOST', 'network_error'),
    ('NS_ERROR_OFFLINE', 'network_error'),
])
def test_firefox_network_codes_keep_their_failure_layer_including_startup(symbol, expected):
    browser = pytest.importorskip('playwright.async_api')
    app = load_sections('imports', 'config', 'worker')
    error = browser.Error('Page.goto: ' + symbol + '\nCall log:\n - navigating to "https://example.invalid/"')
    assert app._worker_failure_code(error) == expected
    assert app._worker_failure_code(error, startup=True) == expected


@pytest.mark.parametrize('message', [
    'proxy forbidden: HTTP 403',
    'Page.goto: navigation failed at https://example.invalid/?error=NS_ERROR_PROXY_FORBIDDEN',
    'Page.goto: NS_ERROR_PROXY_FORBIDDEN_SUFFIX',
    'Page.goto: SSL_ERROR_UNKNOWN_ISSUER',
    'Page.goto: NS_BINDING_ABORTED',
    'locator expected text "NS_ERROR_PROXY_FORBIDDEN"',
])
def test_browser_messages_are_not_classified_by_keywords_or_url_tokens(message):
    browser = pytest.importorskip('playwright.async_api')
    app = load_sections('imports', 'config', 'worker')
    assert app._worker_failure_code(browser.Error(message)) == 'browser_error'
    assert app._worker_failure_code(browser.Error(message), startup=True) == 'browser_startup'
    assert app._worker_failure_code(ValueError('Page.goto: NS_ERROR_PROXY_FORBIDDEN')) == 'worker_error'


def test_protocol_failure_includes_structure_without_response_or_chain_tokens():
    app = load_sections('imports', 'config', 'processing', 'browser')
    task = {'origin': {'code': 'SHA'}, 'destination': {'code': 'HKG'}, 'departure_date': '2099-01-01'}
    request = {'adultCount': 1, 'childCount': 0, 'infantCount': 0, 'flightWay': 'S',
               'cabin': 'Y_S_C_F', 'transactionID': 'private-transaction',
               'flightSegments': [{'departureCityCode': 'SHA', 'arrivalCityCode': 'HKG',
                                   'departureDate': '2099-01-01'}]}
    payload = {'status': 0, 'data': {'context': {'finished': 'invalid', 'searchId': 'private-search-token'},
                                   'private_body': 'not-a-diagnostic'}}

    class Page:
        def on(self, unused, receive):
            self.receive = receive

        async def goto(self, *unused, **kwargs):
            async def body():
                return json.dumps(payload).encode()
            self.receive(SimpleNamespace(url='https://flights.ctrip.com' + app.SEARCH_API_PATH,
                status=200, request=SimpleNamespace(method='POST', post_data_json=request), body=body))

        def remove_listener(self, *unused):
            pass

        async def close(self):
            pass

    async def collect():
        session = app.BrowserSession(app.DEFAULT_CONFIG, {}, {}, None)
        async def new_page():
            return Page()
        session.context = SimpleNamespace(new_page=new_page)
        with pytest.raises(app.CrawlerError) as caught:
            await session.collect(task, 15)
        return caught.value

    error = asyncio.run(collect())
    diagnostic = app.make_diagnostic(error)
    summary = diagnostic['protocol']
    assert summary['http_status'] == 200 and summary['business_status'] == 0
    assert summary['list_present'] is False
    assert summary['known_snapshot'] is False
    assert summary['request_matches'] is True
    assert diagnostic['stage'] == 'collect.protocol'
    assert all(secret not in json.dumps(diagnostic) for secret in
               ('private-transaction', 'private-search-token', 'not-a-diagnostic'))
