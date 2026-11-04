# Browser dependencies are imported lazily so --help works without installation.
CAMOUFOX_BUILD = "official/152.0.4-beta.29"
CTRIP_HOME = "https://flights.ctrip.com/online/channel/domestic"
SEARCH_API_PATH = "/international/search/api/search/batchSearch"


def browser_environment_check():
    """Check installed assets without downloading or starting a collection."""
    import subprocess
    from camoufox.fpgen_model import is_pinned
    from camoufox.multiversion import find_installed_version
    from camoufox.pkgman import launch_path
    installed = find_installed_version(CAMOUFOX_BUILD)
    if installed is None:
        raise CrawlerError("browser_missing", "Install the pinned Camoufox browser: python -m camoufox fetch " + CAMOUFOX_BUILD)
    if not is_pinned():
        raise CrawlerError("browser_missing", "Install the pinned fingerprint model explicitly: python -c \"from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()\"")
    try:
        checked = subprocess.run([launch_path(installed), "--version"], capture_output=True,
                                 timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CrawlerError("browser_unavailable", "The installed Camoufox executable cannot start; check its platform dependencies") from exc
    if checked.returncode:
        raise CrawlerError("browser_unavailable", "The installed Camoufox executable failed its startup check")
    return {"browser_build": CAMOUFOX_BUILD, "installed": True}


def generate_identity(config):
    """Resolve one identity once; the caller persists it before browser launch."""
    from camoufox.fingerprints import get_random_preset
    from camoufox.addons import DefaultAddons
    from camoufox.multiversion import find_installed_version
    from camoufox.pkgman import launch_path
    from camoufox.utils import launch_options

    installed = find_installed_version(CAMOUFOX_BUILD)
    if not installed:
        raise CrawlerError("browser_missing", "Install the pinned Camoufox browser: python -m camoufox fetch " + CAMOUFOX_BUILD)
    operating_system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    if operating_system is None:
        raise CrawlerError("platform_unsupported", "Camoufox requires a supported desktop operating system")
    preset = get_random_preset(os=operating_system, ff_version=CAMOUFOX_BUILD.split("/")[-1].split(".")[0])
    if not preset:
        raise CrawlerError("identity_unavailable", "The pinned package has no fingerprint preset for this platform")
    options = launch_options(executable_path=launch_path(installed), fingerprint_preset=copy.deepcopy(preset),
                             os=operating_system, headless=bool(config.get("headless", True)),
                             locale="zh-CN", env={}, pin_cpu_cores=False,
                             exclude_addons=list(DefaultAddons))
    # env={} prevents credentials and unrelated host variables entering session.json.
    options["env"] = {key: value for key, value in options["env"].items()
                      if key.startswith("CAMOU_") or key == "FONTCONFIG_FILE"}
    identity = {"schema": 1, "platform": platform.system(), "browser_build": CAMOUFOX_BUILD,
                "identity_id": uuid.uuid4().hex, "preset": preset, "launch": options}
    # Refuse an identity that cannot be persisted without custom encoders.
    json.dumps(identity, ensure_ascii=False, allow_nan=False)
    return identity


def identity_launch_options(identity, config):
    if (not isinstance(identity, dict) or identity.get("schema") != 1
            or identity.get("platform") != platform.system()
            or identity.get("browser_build") != CAMOUFOX_BUILD):
        raise CrawlerError("identity_incompatible", "Saved browser identity requires explicit recreation on this platform/build")
    options = copy.deepcopy(identity["launch"])
    options["env"] = {**os.environ, **options.get("env", {})}
    if platform.system() == "Linux":
        binary_directory = str(Path(options["executable_path"]).parent)
        options["env"]["LD_LIBRARY_PATH"] = binary_directory + (
            ":" + options["env"]["LD_LIBRARY_PATH"] if options["env"].get("LD_LIBRARY_PATH") else "")
        runtime = options["env"].get("XDG_RUNTIME_DIR")
        if runtime and config.get("headless", True):
            try:
                if Path(runtime).stat().st_uid != os.getuid():
                    options["env"].pop("XDG_RUNTIME_DIR", None)
            except OSError:
                options["env"].pop("XDG_RUNTIME_DIR", None)
    options["headless"] = bool(config.get("headless", True))
    proxy = config.get("proxy")
    if proxy:
        options["proxy"] = {"server": proxy} if isinstance(proxy, str) else copy.deepcopy(proxy)
    return options


def search_request_matches(payload, task):
    """Only the current single-adult, one-way query is eligible for capture."""
    if not isinstance(payload, dict):
        return False
    segments = payload.get("flightSegments")
    if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict):
        return False
    segment = segments[0]
    if (segment.get("departureCityCode") != task["origin"]["code"]
            or segment.get("arrivalCityCode") != task["destination"]["code"]
            or segment.get("departureDate") != task["departure_date"]):
        return False
    return (type(payload.get("adultCount")) is int and payload["adultCount"] == 1
            and type(payload.get("childCount")) is int and payload["childCount"] == 0
            and type(payload.get("infantCount")) is int and payload["infantCount"] == 0
            and payload.get("flightWay") == "S" and payload.get("cabin") == "Y_S_C_F")


def resolve_city_payload(payload, requested_name):
    if (not isinstance(payload, dict) or payload.get("Status") != 0
            or payload.get("Key") != requested_name or not isinstance(payload.get("Data"), list)):
        raise CrawlerError("protocol_changed", "City suggestions do not match the requested city")
    candidates = {}
    for entry in payload["Data"]:
        if not isinstance(entry, dict) or entry.get("Type") != 5 or entry.get("canSelect") is not True:
            continue
        if (not isinstance(entry.get("Code"), str) or not re.fullmatch(r"[A-Z]{3}", entry["Code"])
                or not isinstance(entry.get("Name"), str) or not entry["Name"]):
            raise CrawlerError("protocol_changed", "City suggestion has an invalid identity")
        candidates[entry["Code"]] = entry
    exact = [entry for entry in candidates.values()
             if requested_name.casefold() in (entry["Name"].casefold(), str(entry.get("EName", "")).casefold(), entry["Code"].casefold())]
    choices = exact if exact else list(candidates.values())
    if len(choices) != 1:
        raise CrawlerError("city_ambiguous", "City name is absent or ambiguous; use its precise city name")
    return {"code": choices[0]["Code"], "name": choices[0]["Name"]}


def member_authenticated(payload):
    if not isinstance(payload, dict):
        return False
    status = payload.get("ResponseStatus")
    result = payload.get("result")
    return (isinstance(status, dict) and status.get("Ack") == "Success"
            and "Errors" in status and isinstance(status["Errors"], list) and not status["Errors"]
            and isinstance(result, dict) and type(result.get("resultCode")) is int
            and result["resultCode"] == 0)


def is_member_summary_request(url, method):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == "m.ctrip.com"
            and parsed.path == "/restapi/soa2/15201/getMemberSummaryInfo" and method == "POST")


async def accept_login_agreement(page, timeout_ms):
    checkbox = page.locator("#checkboxAgreementInput")
    if not await checkbox.is_checked(timeout=timeout_ms):
        await page.locator('label[for="checkboxAgreementInput"]').click(timeout=timeout_ms)
    if not await checkbox.is_checked(timeout=timeout_ms):
        raise CrawlerError("auth_failed", "The login agreement was not selected")


def comfort_target_aliases(rows):
    aliases = {}
    for row in rows:
        marketing = row.get("航班号") or row.get("flightNo")
        operating = row.get("实际承运航班号") or row.get("operateFlightNo") or marketing
        for number in (marketing, operating):
            if number:
                aliases.setdefault(number, set()).add(operating)
    return {number: next(iter(targets)) for number, targets in aliases.items() if len(targets) == 1}


def comfort_request_key(payload, task):
    if (not isinstance(payload, dict)
            or payload.get("departureCityCode") != task["origin"]["code"]
            or payload.get("arrivalCityCode") != task["destination"]["code"]
            or payload.get("departureDate") != task["departure_date"]):
        return None
    numbers = payload.get("flightNoList")
    if not isinstance(numbers, list) or len(numbers) != 1 or not isinstance(numbers[0], str):
        return None
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()
    return numbers[0], digest


def comfort_request_identity(payload, task, aliases):
    key = comfort_request_key(payload, task)
    operating = aliases.get(key[0]) if key is not None else None
    if not operating:
        return None
    return operating, key[1]


class SearchChain:
    """Track linked snapshots and pending notices until explicit completion."""
    def __init__(self, task):
        self.task = task
        self.search_id = None
        self.transaction_id = None
        self.itineraries = None

    def matches_request(self, path, method, request_payload):
        if method != "POST" or not search_request_matches(request_payload, self.task):
            return False
        if self.search_id is None:
            if path != SEARCH_API_PATH:
                return False
        elif path != "/international/search/api/search/pull/" + self.search_id:
            return False
        transaction_id = request_payload.get("transactionID")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise CrawlerError("protocol_changed", "Search request has no transaction identity")
        return self.search_id is None or transaction_id == self.transaction_id

    def accept(self, path, method, request_payload, http_status, payload):
        if not self.matches_request(path, method, request_payload):
            return None
        if http_status == 429:
            raise CrawlerError("rate_limited", "The search service requested a cooling period")
        if http_status >= 500:
            raise CrawlerError("network_error", "The search service is temporarily unavailable")
        if http_status != 200:
            raise CrawlerError("site_blocked", "The search response did not have a successful HTTP status")
        if not isinstance(payload, dict) or payload.get("status") != 0 or not isinstance(payload.get("data"), dict):
            raise CrawlerError("protocol_changed", "The search service did not return the expected business response")
        data = payload["data"]
        if data.get("needUserLogin") is True:
            raise CrawlerError("needs_auth", "The current search requires authentication")
        context = data.get("context")
        if isinstance(context, dict) and context.get("showAuthCode") is True:
            raise CrawlerError("challenge", "The current search requires human verification")
        if (not isinstance(context, dict) or type(context.get("finished")) is not bool
                or not isinstance(context.get("searchId"), str) or not context["searchId"]):
            raise CrawlerError("protocol_changed", "Search completion or itinerary structure is missing")
        if "flightItineraryList" in data:
            itineraries = data["flightItineraryList"]
            if not isinstance(itineraries, list):
                raise CrawlerError("protocol_changed", "Search itinerary structure is invalid")
        elif context["finished"] and self.itineraries is None:
            raise CrawlerError("protocol_changed", "Search response has no known itinerary snapshot")
        else:
            itineraries = None
        # A pending response may advance the poll chain before a snapshot arrives.
        self.transaction_id = request_payload["transactionID"]
        self.search_id = context["searchId"]
        # The site's SEARCH_FLIGHTS reducer retains a nonempty list when a
        # later pull carries no itineraries, while still applying isFinished.
        # Nonempty responses replace the snapshot; they are never appended.
        if itineraries is not None and (itineraries or self.itineraries is None):
            self.itineraries = itineraries
        if not context["finished"]:
            return None
        if itineraries is not None and (itineraries or not self.itineraries):
            return payload
        completed = copy.deepcopy(payload)
        completed["data"]["flightItineraryList"] = copy.deepcopy(self.itineraries)
        self.itineraries = None
        return completed


class BrowserSession:
    def __init__(self, config, identity, state, emit):
        self.config = config
        self.identity = identity
        self.state = state or {}
        self.emit = emit
        self.manager = None
        self.browser = None
        self.context = None
        self.challenge_page = None
        self.challenge_handle = None
        self.auth_checkpoint = None
        self.diagnostic_stage = 'startup.browser'

    async def start(self):
        self.diagnostic_stage = 'startup.browser'
        from camoufox.async_api import AsyncCamoufox
        self.manager = AsyncCamoufox(from_options=identity_launch_options(self.identity, self.config))
        try:
            self.browser = await self.manager.__aenter__()
            options = {"locale": "zh-CN", "timezone_id": "Asia/Shanghai", "no_viewport": True,
                       "ignore_https_errors": True}
            if self.state.get("storage_state"):
                options["storage_state"] = self.state["storage_state"]
            self.context = await self.browser.new_context(**options)
        except BaseException:
            await self.close()
            raise

    async def snapshot(self):
        return {"storage_state": await self.context.storage_state(indexed_db=True)}

    async def _read_json(self, response):
        if response.status == 429:
            retry_after = response.headers.get("retry-after")
            delay = None
            if retry_after:
                try:
                    delay = max(0, float(retry_after))
                except ValueError:
                    from email.utils import parsedate_to_datetime
                    try:
                        delay = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        raise CrawlerError("protocol_changed", "The service supplied an invalid Retry-After header")
            raise CrawlerError("rate_limited", "The service requested a cooling period", retry_after=delay)
        if response.status >= 500:
            raise CrawlerError("network_error", "The target service is temporarily unavailable")
        if response.status != 200:
            raise CrawlerError("site_blocked", "The target interface did not return a successful HTTP status")
        body = await response.body()
        if len(body) > self.config.get("max_response_bytes", 32 * 1024 * 1024):
            raise CrawlerError("response_too_large", "A target response exceeds the configured size limit")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CrawlerError("protocol_changed", "A target interface returned a non-JSON response") from exc

    async def resolve_cities(self, names):
        self.diagnostic_stage = 'resolve.page'
        page = await self.context.new_page()
        try:
            self.diagnostic_stage = 'resolve.navigation'
            await page.goto(CTRIP_HOME, wait_until="domcontentloaded",
                            timeout=self.config.get("navigation_timeout", 30) * 1000)
            result = []
            for name in names:
                self.diagnostic_stage = 'resolve.city_lookup'
                endpoint = "/international/search/api/poi/search?" + urlencode({"key": name, "filterAirport": "false"})
                async with page.expect_response(lambda response: response.request.method == "GET"
                        and response.url == "https://flights.ctrip.com" + endpoint,
                        timeout=self.config.get("action_timeout", 10) * 1000) as response_info:
                    await page.evaluate("endpoint => fetch(endpoint, {credentials: 'same-origin'}).then(() => null)", endpoint)
                payload = await self._read_json(await response_info.value)
                self.diagnostic_stage = 'resolve.normalize'
                result.append(resolve_city_payload(payload, name))
            return result
        finally:
            await page.close()

    async def _collect_comfort(self, page, task, rows, queue, deadline, errors, traffic):
        from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout
        aliases = comfort_target_aliases(rows)
        responses, seen, triggered = [], set(), set()
        targets = set(aliases.values())
        capacity = self.config.get("max_candidates", 64)

        def truncate():
            traffic["truncated"] = True
            errors.add("response_too_large")

        def timeout_ms():
            return max(1, min(self.config.get("action_timeout", 10), deadline - time.monotonic()) * 1000)

        def flights(kind):
            return {aliases[number] for number, key in traffic[kind] if number in aliases}

        def resolved():
            return flights("received") | flights("failed")

        async def check_limit():
            for response in traffic["limits"].values():
                if comfort_request_identity(response.request.post_data_json, task, aliases) is not None:
                    await self._read_json(response)

        async def read(response):
            try:
                if response.request.method != "POST":
                    return
                identity = comfort_request_identity(response.request.post_data_json, task, aliases)
                if identity is None or response in seen:
                    return
                if len(seen) >= capacity:
                    truncate()
                    return
                seen.add(response)
                traffic["http_status"] = response.status
                payload = await asyncio.wait_for(self._read_json(response), max(0.01, deadline - time.monotonic()))
                responses.append({"flight_no": identity[0], "payload": payload})
            except CrawlerError as exc:
                if response.status == 429:
                    raise
                errors.add(exc.code)
            except (ValueError, TypeError):
                errors.add("protocol_changed")
            except (asyncio.TimeoutError, BrowserError):
                errors.add("network_error")

        async def drain():
            while not queue.empty() and time.monotonic() < deadline:
                await read(queue.get_nowait())

        async def wait_for_flights(numbers, until):
            while time.monotonic() < until and not traffic["truncated"]:
                traffic["changed"].clear()
                await drain()
                if not numbers - resolved():
                    return
                try:
                    await asyncio.wait_for(traffic["changed"].wait(), until - time.monotonic())
                except asyncio.TimeoutError:
                    return

        await check_limit()
        await drain()
        if targets - resolved() - flights("sent") and time.monotonic() < deadline:
            locators = page.locator(".flight-item .plane-No")
            try:
                await locators.first.wait_for(state="attached", timeout=timeout_ms())
                index = 0
                hover_failures = {}
                while time.monotonic() < deadline and not traffic["truncated"]:
                    await drain()
                    needed = targets - resolved() - flights("sent") - triggered
                    if not needed:
                        break
                    count = await locators.count()
                    if index >= count:
                        # Wheel over the list so its normal scroll container loads the next cards.
                        await locators.last.scroll_into_view_if_needed(timeout=timeout_ms())
                        box = await locators.last.bounding_box(timeout=timeout_ms())
                        if box is None:
                            errors.add("page_details_not_loaded")
                            break
                        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        await page.mouse.wheel(0, 700)
                        try:
                            await page.wait_for_function("count => document.querySelectorAll('.flight-item .plane-No').length > count",
                                                         arg=count, timeout=max(1, min(1.5, deadline - time.monotonic()) * 1000))
                        except BrowserTimeout:
                            errors.add("page_details_not_loaded")
                            break
                        continue
                    locator = locators.nth(index)
                    # The aircraft label is a child element; the flight number is direct text.
                    number = await locator.evaluate("el => Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim()", timeout=timeout_ms())
                    operating = aliases.get(number)
                    if operating not in needed:
                        index += 1
                        continue
                    if len(triggered) >= capacity or (operating not in hover_failures and len(hover_failures) >= capacity):
                        truncate()
                        break
                    try:
                        await page.mouse.move(0, 0)
                        await locator.hover(timeout=timeout_ms())
                    except BrowserTimeout:
                        errors.add("page_details_timeout")
                        hover_failures[operating] = hover_failures.get(operating, 0) + 1
                        if hover_failures[operating] >= 2:
                            index += 1
                        continue
                    triggered.add(operating)
                    index += 1
                    await wait_for_flights({operating}, min(deadline, time.monotonic() + 1.5))
            except BrowserTimeout:
                errors.add("page_details_timeout")
            except BrowserError:
                errors.add("network_error")
        pending = (flights("sent") | triggered) - resolved()
        while pending and time.monotonic() < deadline and not traffic["truncated"]:
            await wait_for_flights(pending, deadline)
            pending = (flights("sent") | triggered) - resolved()
        await check_limit()
        await drain()
        if flights("failed"):
            errors.add("network_error")
        if targets - resolved() and time.monotonic() >= deadline:
            errors.add("comfort_budget_exhausted")
        result = apply_comfort(rows, responses)
        result["comfort_summary"].update(
            requested_flights=len(flights("sent")), responded_flights=len(flights("received")),
            requests_sent=sum(count for (number, key), count in traffic["sent"].items() if number in aliases),
            responses_received=sum(count for (number, key), count in traffic["received"].items() if number in aliases),
            truncated=traffic["truncated"])
        if errors and result["comfort_status"] in ("partial", "unavailable"):
            result["comfort_reason"] = ";".join(sorted(errors | {result["comfort_reason"]}))
        return result

    async def collect(self, task, deadline_seconds):
        from playwright.async_api import Error as BrowserError
        self.diagnostic_stage = 'collect.page'
        page = await self.context.new_page()
        queue = asyncio.Queue(maxsize=self.config.get("max_candidates", 64))
        comfort_queue = asyncio.Queue(maxsize=self.config.get("max_candidates", 64))
        comfort_errors = set()
        traffic = {"sent": {}, "received": {}, "failed": {}, "limits": {}, "http_status": None,
                   "changed": asyncio.Event(), "truncated": False}
        capacity = self.config.get("max_candidates", 64)
        network = {"search_responses_received": 0, "failed_request_count": 0, "request_failed": False}
        statuses = {"navigation": None, "search": None}
        navigation_url = None
        overflow = False
        chain = SearchChain(task)
        final_payload = None
        deadline = time.monotonic() + deadline_seconds
        retain_challenge = False

        def request_kind(request):
            parsed = urlparse(request.url)
            if parsed.hostname != "flights.ctrip.com":
                return None
            if request.url == navigation_url and request.method == "GET":
                return "navigation"
            if parsed.path == SEARCH_API_PATH or parsed.path.startswith("/international/search/api/search/pull/"):
                return "search"
            if parsed.path == "/international/search/api/flight/comfort/getFlightComfort":
                return "comfort"
            return None

        def comfort_key(request):
            if request.method != "POST" or request_kind(request) != "comfort":
                return None
            try:
                return comfort_request_key(request.post_data_json, task)
            except (BrowserError, ValueError, TypeError, KeyError):
                return None

        def track(kind, key):
            values = traffic[kind]
            if key not in values and len(values) >= capacity:
                traffic["truncated"] = True
                comfort_errors.add("response_too_large")
                traffic["changed"].set()
                return False
            values[key] = values.get(key, 0) + 1
            traffic["changed"].set()
            return True

        def receive_request(request):
            if self.config.get("comft_flight"):
                key = comfort_key(request)
                if key is not None:
                    track("sent", key)

        def receive_failed(request):
            if request_kind(request) is None:
                return
            network["request_failed"] = True
            network["failed_request_count"] += 1
            if self.config.get("comft_flight"):
                key = comfort_key(request)
                if key is not None:
                    track("failed", key)

        def annotate(error):
            kind = "navigation" if self.diagnostic_stage == "collect.navigation" else (
                "comfort" if self.diagnostic_stage == "collect.comfort" else "search")
            status = traffic["http_status"] if kind == "comfort" else statuses[kind]
            diagnostic = dict(getattr(error, "diagnostic", {}) or {})
            diagnostic.update(stage=self.diagnostic_stage, network={**network, "response_kind": kind,
                              "method": "GET" if kind == "navigation" else "POST", "http_status": status})
            error.diagnostic = diagnostic
            return error

        def receive(response):
            nonlocal overflow
            parsed = urlparse(response.url)
            if parsed.hostname != "flights.ctrip.com":
                return
            path = parsed.path
            if path == SEARCH_API_PATH or path.startswith("/international/search/api/search/pull/"):
                network["search_responses_received"] += 1
                try:
                    queue.put_nowait(response)
                except asyncio.QueueFull:
                    overflow = True
            elif self.config.get("comft_flight") and path == "/international/search/api/flight/comfort/getFlightComfort":
                key = comfort_key(response.request)
                if key is None or not track("received", key):
                    return
                if response.status == 429:
                    traffic["limits"][key] = response
                try:
                    comfort_queue.put_nowait(response)
                except asyncio.QueueFull:
                    comfort_errors.add("response_too_large")

        page.on("request", receive_request)
        page.on("requestfailed", receive_failed)
        page.on("response", receive)
        try:
            url = ("https://flights.ctrip.com/online/list/oneway-" + task["origin"]["code"].lower()
                   + "-" + task["destination"]["code"].lower() + "?" + urlencode({
                       "depdate": task["departure_date"], "cabin": "y_s_c_f", "adult": 1, "child": 0, "infant": 0}))
            navigation_url = url
            self.diagnostic_stage = 'collect.navigation'
            navigation = await page.goto(url, wait_until="commit", timeout=max(1, min(
                self.config.get("navigation_timeout", 30), deadline - time.monotonic()) * 1000))
            if navigation is not None:
                statuses["navigation"] = navigation.status
                if navigation.status >= 400:
                    await self._read_json(navigation)
            while final_payload is None:
                self.diagnostic_stage = 'collect.wait_response'
                if overflow:
                    raise CrawlerError("response_too_large", "The target response queue exceeded its capacity")
                remaining = deadline - time.monotonic() - 10
                if remaining <= 0:
                    raise CrawlerError("response_timeout", "The search did not reach its explicit completion state")
                response = await asyncio.wait_for(queue.get(), remaining)
                path = urlparse(response.url).path
                try:
                    request_payload = response.request.post_data_json
                except (BrowserError, ValueError, TypeError, KeyError):
                    request_payload = None
                if not chain.matches_request(path, response.request.method, request_payload):
                    continue
                statuses["search"] = response.status
                self.diagnostic_stage = 'collect.decode_response'
                payload = await self._read_json(response)
                self.diagnostic_stage = 'collect.protocol'
                data = payload.get('data') if isinstance(payload, dict) else None
                context = data.get('context') if isinstance(data, dict) else None
                context = context if isinstance(context, dict) else {}
                items = data.get('flightItineraryList') if isinstance(data, dict) else None
                transaction = request_payload.get('transactionID')
                search_id = context.get('searchId')
                summary = {
                    'http_status': response.status,
                    'business_status': payload.get('status') if isinstance(payload, dict) else None,
                    'finished': context.get('finished') if type(context.get('finished')) is bool else None,
                    'finished_type': type(context['finished']).__name__ if 'finished' in context else 'missing',
                    'list_present': isinstance(data, dict) and 'flightItineraryList' in data,
                    'list_type': type(items).__name__ if isinstance(data, dict) and 'flightItineraryList' in data else 'missing',
                    'list_count': len(items) if isinstance(items, list) else None,
                    'known_snapshot': chain.itineraries is not None,
                    'request_matches': True, 'transaction_present': bool(transaction),
                    'transaction_matches': transaction == chain.transaction_id if chain.transaction_id is not None else None,
                    'search_id_present': bool(search_id),
                    'search_id_matches': search_id == chain.search_id if chain.search_id is not None else None,
                }
                try:
                    final_payload = chain.accept(path, response.request.method, request_payload, response.status, payload)
                except CrawlerError as exc:
                    exc.diagnostic.update(stage=self.diagnostic_stage, protocol=summary)
                    raise
            observed_at = datetime.now(timezone.utc).isoformat()
            self.diagnostic_stage = 'collect.normalize'
            observation = normalize_response(final_payload, task, self.config, observed_at)
            if self.config.get("comft_flight"):
                self.diagnostic_stage = 'collect.comfort'
                comfort_deadline = min(deadline - 10, time.monotonic() + self.config.get("comfort_timeout", 15))
                observation.update(await self._collect_comfort(page, task, observation["rows"],
                                                              comfort_queue, comfort_deadline, comfort_errors, traffic))
            return observation
        except (CrawlerError, BrowserError) as exc:
            annotate(exc)
            if isinstance(exc, CrawlerError) and exc.code == "challenge" and self.config.get("challenge_policy") == "wait_then_switch":
                self.challenge_page = page
                self.challenge_handle = uuid.uuid4().hex
                self.challenge_expires_at = time.monotonic() + 10
                exc.challenge_handle = self.challenge_handle
                retain_challenge = True
            raise
        except asyncio.TimeoutError as exc:
            raise annotate(CrawlerError("response_timeout", "No complete response arrived before the attempt deadline")) from exc
        finally:
            page.remove_listener("request", receive_request)
            page.remove_listener("requestfailed", receive_failed)
            page.remove_listener("response", receive)
            if not retain_challenge:
                await page.close()

    async def authenticate(self, account, timeout, challenge_handle=None):
        from playwright.async_api import TimeoutError as BrowserTimeout
        self.diagnostic_stage = 'auth.prepare'
        self.auth_checkpoint = None
        if not account:
            raise CrawlerError("needs_auth", "Authentication requires a configured account")
        page = None
        transferred_challenge = False
        if self.challenge_page:
            if (challenge_handle in (None, self.challenge_handle)
                    and time.monotonic() <= self.challenge_expires_at):
                page = self.challenge_page
                transferred_challenge = True
            else:
                await self.challenge_page.close()
            self.challenge_page = None
            self.challenge_handle = None
        page = page or await self.context.new_page()
        deadline = time.monotonic() + timeout
        authenticated = asyncio.Event()
        member_seen = asyncio.Event()
        member_failure = None
        jobs = set()
        checkpoint_at = 0.0

        def check_member_failure():
            if member_failure is not None:
                raise member_failure

        def action_timeout():
            check_member_failure()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CrawlerError("auth_timeout", "The authentication operation reached its deadline")
            return min(self.config.get("action_timeout", 10), remaining) * 1000

        async def check_member(response):
            nonlocal member_failure
            if not is_member_summary_request(response.url, response.request.method):
                return
            try:
                if response.status in (200, 429) and member_authenticated(await self._read_json(response)):
                    authenticated.set()
            except CrawlerError as exc:
                if response.status == 429:
                    member_failure = exc
            except ValueError:
                return
            finally:
                member_seen.set()

        def receive_member(response):
            if is_member_summary_request(response.url, response.request.method):
                job = asyncio.create_task(check_member(response))
                jobs.add(job)
                job.add_done_callback(jobs.discard)

        self.context.on("response", receive_member)
        try:
            if not transferred_challenge:
                self.diagnostic_stage = 'auth.session_check'
                await page.goto(CTRIP_HOME, wait_until="domcontentloaded", timeout=min(
                    self.config.get("navigation_timeout", 30), max(1, deadline - time.monotonic())) * 1000)
                try:
                    await asyncio.wait_for(member_seen.wait(), min(5, max(0.01, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    pass
                check_member_failure()
                if authenticated.is_set():
                    return {"authenticated": True}
                if self.config.get("headless", True) and not account.get("password"):
                    raise CrawlerError("needs_auth", "The saved session is not valid; headless login requires a password")
                login_url = "https://passport.ctrip.com/user/login?" + urlencode({"BackUrl": CTRIP_HOME})
                self.diagnostic_stage = 'auth.login'
                await page.goto(login_url, wait_until="domcontentloaded", timeout=min(
                    self.config.get("navigation_timeout", 30), max(1, deadline - time.monotonic())) * 1000)
                if account.get("username") and account.get("password"):
                    panel = page.locator('[data-testid="accountPanel"]')
                    await panel.wait_for(state="visible", timeout=action_timeout())
                    await panel.locator('[data-testid="accountNameInput"]').fill(account["username"], timeout=action_timeout())
                    await panel.locator('[data-testid="passwordInput"]').fill(account["password"], timeout=action_timeout())
                    await accept_login_agreement(page, action_timeout())
                    await panel.locator('[data-testid="loginButton"]').click(timeout=action_timeout())
            self.diagnostic_stage = 'auth.wait_verification'
            while time.monotonic() < deadline:
                check_member_failure()
                if authenticated.is_set():
                    return {"authenticated": True}
                challenge_visible = await page.locator('#sc_mobilevalidate').is_visible()
                if time.monotonic() >= checkpoint_at:
                    self.auth_checkpoint = {"state": await self.snapshot(), "confirmed": False,
                                            "challenge_visible": challenge_visible}
                    if self.emit:
                        self.emit('auth_checkpoint', self.auth_checkpoint)
                    checkpoint_at = time.monotonic() + 5
                if challenge_visible:
                    if self.config.get("headless", True) or self.config.get("challenge_policy") == "switch":
                        raise CrawlerError("challenge", "Login requires manual verification")
                await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))
            raise CrawlerError("auth_timeout", "Login was not confirmed before the authentication deadline")
        except BrowserTimeout as exc:
            raise CrawlerError("auth_timeout", "A login page operation did not complete before its deadline") from exc
        finally:
            self.context.remove_listener("response", receive_member)
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            try:
                self.auth_checkpoint = {"state": await self.snapshot(), "confirmed": authenticated.is_set()}
            except Exception as exc:
                if authenticated.is_set():
                    raise CrawlerError("session_snapshot_failed", "Authenticated state could not be captured") from exc
            finally:
                await page.close()

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = None
            if self.manager:
                await self.manager.__aexit__(None, None, None)
            self.manager = None
            self.browser = None
