"""Body-read failures retain evidence for the exact response, across IPC."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from playwright.async_api import Error

from section_loader import load_sections
from test_browser_contract import body, task


@pytest.mark.parametrize('failure', [None, 'NS_ERROR_NET_PARTIAL_TRANSFER'])
def test_body_failure_is_correlated_with_its_request_and_survives_parent(failure):
    app = load_sections('imports', 'processing', 'browser')

    class Page:
        def __init__(self):
            self.handlers = {}

        def on(self, event, handler):
            self.handlers[event] = handler

        def remove_listener(self, *args):
            pass

        async def close(self):
            pass

        async def goto(self, *args, **kwargs):
            url = 'https://flights.ctrip.com' + app.SEARCH_API_PATH
            request = SimpleNamespace(url=url, method='POST', post_data_json=body(), failure=None)

            async def read_body():
                # An unrelated failure must not be attributed to this response.
                unrelated = SimpleNamespace(url=url, method='POST', failure='NS_ERROR_NET_RESET')
                self.handlers['requestfailed'](unrelated)
                request.failure = failure
                if failure:
                    self.handlers['requestfailed'](request)
                raise Error('Response.body: Protocol error (Network.getResponseBody): Request "123" is not found')

            self.handlers['response'](SimpleNamespace(url=url, request=request,
                status=200, headers={}, body=read_body))

    async def run():
        session = app.BrowserSession({}, {}, {}, None)
        async def new_page():
            return Page()
        session.context = SimpleNamespace(new_page=new_page)
        with pytest.raises(Error) as caught:
            await session.collect(task(), 30)
        child = app.make_diagnostic(caught.value)
        parent = app.make_diagnostic(app.CrawlerError('browser_error', child['message'], diagnostic=child))
        network = parent['network']
        assert network['response_body_failed'] is True
        assert network['response_request_failed'] is bool(failure)
        assert network.get('response_request_error_code') == failure
        assert network['http_status'] == 200
        assert network['response_kind'] == 'search'
        assert network['failed_request_count'] == (2 if failure else 1)
        assert '123' not in json.dumps(network)

    asyncio.run(run())
