"""Exercise native hover traffic on dense, local Camoufox pages."""
import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from section_loader import load_sections
from test_browser_contract import body, task
import test_processing as processing_fixtures


requires_camoufox = pytest.mark.skipif(
    os.getenv("CTRIP_BROWSER_TESTS") != "1", reason="Requires the installed Camoufox runtime"
)


async def collect_fixture(count, *, debounce_ms=0, response_delay=0, blocked_card=False,
                          comfort_timeout=15, max_candidates=64, body_reads=None, rate_limited=False):
    app = load_sections("imports", "config", "processing", "browser")
    config = dict(app.DEFAULT_CONFIG, headless=True, comft_flight=True,
                  comfort_timeout=comfort_timeout, max_candidates=max_candidates)
    session = app.BrowserSession(config, app.generate_identity(config), {}, None)
    items, cards = [], []
    for index in range(count):
        marketing, operating = f"MU{1000 + index}", f"FM{2000 + index}"
        item = processing_fixtures.itinerary(identity=f"itinerary-{index}", number=marketing)
        item["flightSegments"][0]["flightList"][0]["operateFlightNo"] = operating
        items.append(item)
        style = ' style="pointer-events:none"' if blocked_card and index == 0 else ""
        cards.append(f'<div class="flight-item"><span class="plane-No" data-no="{operating}"{style}>'
                     f'{marketing}<span> aircraft</span></span></div>')
    search = {"status": 0, "msg": "success", "data": {
        "context": {"searchId": "done", "finished": True}, "flightItineraryList": items,
    }}
    detail = processing_fixtures.ProcessingContractTests().comfort_response()["payload"]
    query = {"departureCityCode": "SHA", "arrivalCityCode": "HKG", "departureDate": "2027-01-10",
             "cabinList": ["Y"]}
    html = """<!doctype html><meta charset="utf-8">
    <style>body{display:grid;grid-template-columns:repeat(5,145px);gap:4px}
    .plane-No{display:inline-block;padding:2px}</style>
    """ + "".join(cards) + f"""<script>
    function post(path, body) {{
        return fetch(path, {{method:'POST', headers:{{'Content-Type':'application/json'}},
                           body:JSON.stringify(body)}});
    }}
    for (const node of document.querySelectorAll('.plane-No')) {{
        let timer;
        node.addEventListener('mouseenter', () => {{
            timer = setTimeout(() => post('/international/search/api/flight/comfort/getFlightComfort',
                {{...{json.dumps(query)}, flightNoList:[node.dataset.no]}}), {debounce_ms});
        }});
        node.addEventListener('mouseleave', () => clearTimeout(timer));
    }}
    post('/international/search/api/search/batchSearch', {json.dumps(body())});
    </script>"""
    requested = []
    try:
        await session.start()
        if body_reads is not None:
            read_json = session._read_json

            async def observe_body(response):
                if response.url.endswith("/getFlightComfort"):
                    flight = response.request.post_data_json["flightNoList"][0]
                    body = response.body

                    async def delayed_body():
                        if flight == "FM2000":
                            await asyncio.sleep(1.4)
                        result = await body()
                        body_reads.append(flight)
                        return result

                    response.body = delayed_body
                return await read_json(response)

            session._read_json = observe_body
        await session.context.route("**/*", lambda route: route.abort())
        await session.context.route("https://flights.ctrip.com/online/list/**",
                                    lambda route: route.fulfill(content_type="text/html", body=html))
        await session.context.route("**/batchSearch", lambda route: route.fulfill(json=search))

        async def respond(route):
            requested.append(route.request.post_data_json["flightNoList"][0])
            if response_delay:
                await asyncio.sleep(response_delay)
            if rate_limited:
                await route.fulfill(status=429, headers={"Retry-After": "3600"}, json={})
            else:
                await route.fulfill(json=detail)

        await session.context.route("**/getFlightComfort", respond)
        began = time.monotonic()
        result = await asyncio.wait_for(session.collect(task(), 30), comfort_timeout + 5)
        return result, requested, time.monotonic() - began
    finally:
        await session.close()


@pytest.mark.parametrize("debounce_ms,response_delay,capacity", [(0, 0, 64), (45, 0.35, 8)])
@requires_camoufox
def test_dense_native_hover_covers_more_flights_than_the_queue_capacity(
        debounce_ms, response_delay, capacity, record_property):
    result, requested, elapsed = asyncio.run(collect_fixture(
        70, debounce_ms=debounce_ms, response_delay=response_delay, max_candidates=capacity,
    ))
    summary = result["comfort_summary"]
    record_property("elapsed_seconds", round(elapsed, 3))
    record_property("comfort_summary", json.dumps(summary, ensure_ascii=False))
    assert summary["result_flights"] == 70
    assert summary["requested_flights"] == summary["covered_flights"] == 70, summary
    assert result["comfort_status"] == "complete"
    assert len(set(requested)) == len(requested) == 70
    assert summary["truncated"] is False
    assert elapsed < 15


@requires_camoufox
def test_unhoverable_card_leaves_time_for_later_targets(record_property):
    result, requested, elapsed = asyncio.run(collect_fixture(
        6, blocked_card=True, comfort_timeout=3,
    ))
    summary = result["comfort_summary"]
    record_property("elapsed_seconds", round(elapsed, 3))
    record_property("comfort_summary", json.dumps(summary, ensure_ascii=False))
    assert result["status"] == "succeeded"
    assert result["comfort_status"] == "partial"
    assert "page_hover_timeout" in result["comfort_reason"]
    assert summary["covered_flights"] == 5, summary
    assert set(requested) == {f"FM{2000 + index}" for index in range(1, 6)}
    assert elapsed < 4.5


@requires_camoufox
def test_normal_debounce_can_start_a_request_before_pointer_leaves():
    result, requested, _ = asyncio.run(collect_fixture(
        4, debounce_ms=300, comfort_timeout=3,
    ))
    assert result["comfort_status"] == "complete", result["comfort_summary"]
    assert len(set(requested)) == len(requested) == 4


@requires_camoufox
def test_slow_body_still_contributes_and_does_not_block_later_bodies():
    body_reads = []
    result, _, _ = asyncio.run(collect_fixture(
        6, comfort_timeout=3, body_reads=body_reads,
    ))
    assert result["comfort_status"] == "complete", result["comfort_summary"]
    assert len(body_reads) == 6 and body_reads[-1] == "FM2000"


@requires_camoufox
def test_comfort_rate_limit_keeps_its_http_status_in_diagnostic():
    with pytest.raises(Exception) as caught:
        asyncio.run(collect_fixture(1, rate_limited=True, comfort_timeout=3))
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 3600
    assert caught.value.diagnostic["network"]["http_status"] == 429


def test_rate_limit_observed_while_draining_body_readers_still_pauses():
    app = load_sections("imports", "config", "processing", "browser")
    session = app.BrowserSession(app.DEFAULT_CONFIG, {}, {}, None)
    payload = {"data": {"flightItineraryList": [processing_fixtures.itinerary()]}}
    rows = app.normalize_response(payload, task(), {}, "2027-01-01T00:00:00Z")["rows"]
    flight = "FM4567"

    async def collect_until_limit():
        traffic = {"sent": {flight: 1}, "received": {flight: 1}, "failed": {}, "limits": {},
                   "pending": {}, "reading": 0, "http_status": None, "changed": asyncio.Event(),
                   "truncated": False}

        async def read_body(response):
            try:
                await asyncio.Event().wait()
            finally:
                # The network can deliver a rate-limit response while the
                # expired body task is being cancelled and drained.
                traffic["limits"][flight] = "3600"

        session._read_json = read_body
        queue = asyncio.Queue(1)
        queue.put_nowait((flight, SimpleNamespace(status=200)))
        try:
            await session._collect_comfort(None, task(), rows, queue, time.monotonic() + 0.2, set(), traffic)
        finally:
            assert traffic["limits"] == {flight: "3600"}

    with pytest.raises(app.CrawlerError) as caught:
        asyncio.run(collect_until_limit())
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 3600


def collect_at_reader_deadline(read_body):
    app = load_sections("imports", "config", "processing", "browser")
    session = app.BrowserSession(app.DEFAULT_CONFIG, {}, {}, None)
    clock = [0]
    jobs = []

    def create_task(coro):
        job = asyncio.create_task(coro)
        jobs.append(job)
        return job

    async def wait_for(coro, timeout):
        if coro.cr_code is not asyncio.Event.wait.__code__:
            return await coro
        coro.close()
        # Let the real reader task finish, then expire the waiting controller
        # before the reader's done callback gets an event-loop turn.
        await asyncio.sleep(0)
        assert jobs[0].done() and not jobs[0].cancelled()
        clock[0] = 10
        raise asyncio.TimeoutError

    app.time = SimpleNamespace(monotonic=lambda: clock[0])
    app.asyncio = SimpleNamespace(create_task=create_task, wait_for=wait_for, gather=asyncio.gather,
                                  TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError)

    async def collect_until_deadline():
        flight = "FM4567"
        traffic = {"sent": {flight: 1}, "received": {flight: 1}, "failed": {}, "limits": {},
                   "pending": {}, "reading": 0, "http_status": None, "changed": asyncio.Event(),
                   "truncated": False}
        queue = asyncio.Queue(1)
        queue.put_nowait((flight, SimpleNamespace(status=200)))
        session._read_json = read_body
        rows = [{"flightNo": "MU1234", "operateFlightNo": flight}]
        return await session._collect_comfort(None, task(), rows, queue, 10, set(), traffic)

    return asyncio.run(collect_until_deadline())


def test_completed_reader_exception_is_preserved_at_deadline():
    async def read_body(response):
        raise RuntimeError("unexpected decoder failure")

    with pytest.raises(RuntimeError, match="unexpected decoder failure"):
        collect_at_reader_deadline(read_body)


def test_completed_body_at_deadline_reports_missing_fields_without_read_timeout():
    async def read_body(response):
        payload = processing_fixtures.ProcessingContractTests().comfort_response()["payload"]
        del payload["data"]["planeInfo"]["planeAge"]
        return payload

    result = collect_at_reader_deadline(read_body)
    assert result["comfort_status"] == "partial"
    assert result["comfort_summary"]["covered_flights"] == 1
    assert result["comfort_reason"] == "source_missing"


@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
def test_nonfinite_retry_after_is_rejected(value):
    app = load_sections("imports", "config", "processing", "browser")
    with pytest.raises(app.CrawlerError) as caught:
        app._rate_limit_error(value)
    assert caught.value.code == "protocol_changed"
