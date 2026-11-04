LEGACY_FLIGHT_FIELDS = (
    "sequenceNo", "marketAirlineCode", "marketAlliance", "departureCountryCode",
    "departureProvinceId", "departureCityId", "departureCityCode",
    "departureAirportShortName", "departureTerminal", "arrivalCountryCode",
    "arrivalProvinceId", "arrivalCityId", "arrivalCityCode",
    "arrivalAirportShortName", "arrivalTerminal", "transferDuration",
    "departureDateTime", "arrivalDateTime", "leakedVisaTagSwitch", "highLightPlaneNo",
    "istTransitService", "mainSegment", "operateAirlineCode", "operateAirlineName",
    "operateAlliance", "operateFlightNo", "isLowCostCarrier", "stopList",
)

CSV_COLUMNS = (
    ("run_id", "批次标识"), ("task_key", "任务标识"), ("itineraryId", "行程标识"),
    ("observed_at", "采集时间"), ("dateGetTime", "数据获取日期"), ("flightNo", "航班号"),
    ("operateFlightNo", "实际承运航班号"), ("marketAirlineName", "航空公司"),
    ("departureday", "出发日期"), ("departuretime", "出发时间"), ("arrivalday", "到达日期"),
    ("arrivaltime", "到达时间"), ("duration", "飞行时长"), ("departureCountryName", "出发国家"),
    ("departureCityName", "出发城市"), ("departureAirportName", "出发机场"), ("departureAirportCode", "出发机场三字码"),
    ("arrivalCountryName", "到达国家"), ("arrivalCityName", "到达城市"), ("arrivalAirportName", "到达机场"),
    ("arrivalAirportCode", "到达机场三字码"), ("aircraftName", "飞机型号"), ("aircraftSize", "飞机尺寸"),
    ("aircraftCode", "飞机型号三字码"), ("stopCount", "停留次数"), ("stopInfo", "中转信息"), ("currency", "币种"),
    ("price_status", "价格状态"), ("price_reason", "价格不完整原因"),
    ("economy_origin", "economy_origin"), ("economy_tax", "economy_tax"), ("economy_total", "economy_total"),
    ("economy_full", "economy_full"),
    ("economy_price_status", "经济舱价格状态"), ("economy_comparison_basis", "经济舱比较口径"), ("economy_currency", "经济舱币种"),
    ("bussiness_origin", "bussiness_origin"), ("bussiness_tax", "bussiness_tax"), ("bussiness_total", "bussiness_total"),
    ("bussiness_full", "bussiness_full"),
    ("bussiness_price_status", "商务舱价格状态"), ("bussiness_comparison_basis", "商务舱比较口径"), ("bussiness_currency", "商务舱币种"),
    ("comfort_status", "舒适度状态"), ("departure_delay_time", "出发延误时间"),
    ("departure_bridge_rate", "出发廊桥率"), ("arrival_delay_time", "到达延误时间"),
    ("plane_type", "飞机类型"), ("plane_width", "飞机宽度"), ("plane_age", "飞机机龄"),
    ("Y_has_meal", "经济舱餐食标记（原值）"), ("Y_seat_tilt", "经济舱座椅倾斜度"),
    ("Y_seat_width", "经济舱座椅宽度"), ("Y_seat_pitch", "经济舱座椅间距"),
    ("Y_meal_msg", "经济舱餐食信息"), ("Y_power", "经济舱电源"),
    ("C_has_meal", "商务舱餐食标记（原值）"), ("C_seat_tilt", "商务舱座椅倾斜度"),
    ("C_seat_width", "商务舱座椅宽度"), ("C_seat_pitch", "商务舱座椅间距"),
    ("C_meal_msg", "商务舱餐食信息"), ("C_power", "商务舱电源"),
) + tuple(("legacy_operateFlightNo" if name == "operateFlightNo" else name, name)
          for name in LEGACY_FLIGHT_FIELDS)
CSV_FIELDS = tuple(label for internal, label in CSV_COLUMNS)
CSV_INTERNAL_FIELDS = tuple(internal for internal, label in CSV_COLUMNS)
CSV_INTERNAL_NAMES = {label: internal for internal, label in CSV_COLUMNS}


def _processing_source_value(value):
    """Keep source values, including false/zero and explicit empty structures."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return value


def _processing_money(value, field):
    if value is None or isinstance(value, bool):
        raise CrawlerError("data_invalid", f"Missing or invalid quote field: {field}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CrawlerError("data_invalid", f"Invalid amount in {field}") from exc
    if not amount.is_finite() or amount < 0:
        raise CrawlerError("data_invalid", f"Non-finite or negative amount in {field}")
    return amount


def _processing_decimal_text(amount):
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _processing_currency(value):
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha():
        raise CrawlerError("data_invalid", "Quote currency is missing or invalid")
    code = value.upper()
    return "CNY" if code == "RMB" else code


def _processing_quote(price, currency):
    """Preserve explicit components; absence is partial data, never invented tax."""
    base = _processing_money(price.get("adultPrice"), "adultPrice")
    tax = None
    if price.get("adultTax") is not None and price.get("adultTax") != "":
        tax = _processing_money(price["adultTax"], "adultTax")
    elif price.get("freeOilFeeAndTax") is True:
        tax = Decimal(0)
    # Luggage criteria is an observed explicit total/currency contract.  Its
    # absence is common for domestic quotes.  sortPrice and ancillary package
    # prices do not establish tax, total or currency.
    embedded_currency, embedded_total = None, None
    if price.get("luggageVisaKey") not in (None, ""):
        try:
            baggage = json.loads(price["luggageVisaKey"])
            criteria = baggage["criteria"]
            if not isinstance(criteria, dict):
                raise ValueError("criteria")
            if criteria.get("Currency") not in (None, ""):
                embedded_currency = _processing_currency(criteria["Currency"])
            if criteria.get("TotalPrice") not in (None, ""):
                embedded_total = _processing_money(criteria["TotalPrice"], "criteria.TotalPrice")
        except (TypeError, ValueError, KeyError) as exc:
            raise CrawlerError("data_invalid", "Invalid luggageVisaKey quote criteria") from exc
    total = base + tax if tax is not None else embedded_total
    if embedded_total is not None:
        if embedded_total < base or (tax is not None and embedded_total != base + tax):
            raise CrawlerError("data_invalid", "Quote total conflicts with its explicit components")
    currencies = {_processing_currency(value) for value in
                  (price.get("currency"), price.get("currencyCode"), currency, embedded_currency)
                  if value is not None and value != ""}
    if len(currencies) > 1:
        raise CrawlerError("data_invalid", "Quote currency conflicts with its explicit criteria")
    quote_currency = next(iter(currencies), None)
    missing = []
    if quote_currency is None:
        missing.append("currency_missing")
    if tax is None:
        missing.append("tax_missing")
    if total is None:
        missing.append("total_missing")
    return {"base": base, "tax": tax, "total": total, "currency": quote_currency,
            "complete": not missing, "missing": missing,
            "legacy_full": _processing_source_value(price.get("miseryIndex"))}


def _processing_datetime(value, field):
    if not isinstance(value, str):
        raise CrawlerError("data_invalid", f"Missing datetime field {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrawlerError("data_invalid", f"Invalid datetime field {field}") from exc
    return parsed.date().isoformat(), parsed.time().isoformat(timespec="seconds")


def normalize_response(payload: dict, task: dict, options: dict, observed_at: str) -> dict:
    """Normalize one adapter-confirmed complete search response without I/O."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("flightItineraryList"), list):
        raise CrawlerError("protocol_changed", "Expected data.flightItineraryList list")
    if not options.get("direct_flight", True):
        raise CrawlerError("unsupported_mode", "Only complete direct-flight itineraries are currently supported")
    items = data["flightItineraryList"]
    try:
        observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CrawlerError("data_invalid", "Invalid observation timestamp") from exc
    if observed_time.tzinfo is None:
        raise CrawlerError("data_invalid", "Observation timestamp must include its UTC offset")
    observed_day = observed_time.astimezone(ZoneInfo(options.get("timezone", "Asia/Shanghai"))).date().isoformat()
    comfort_status = "unavailable" if options.get("comft_flight", False) else "not_requested"
    rows = []
    max_rows = options.get('max_rows')
    price_reasons = set()
    identities = set()
    response_currencies = {_processing_currency(value) for value in
                           (data.get("currency"), data.get("currencyCode"))
                           if value is not None and value != ""}
    if len(response_currencies) > 1:
        raise CrawlerError("data_invalid", "Search response has conflicting currency sources")
    default_currency = next(iter(response_currencies), None)
    for item in items:
        if not isinstance(item, dict):
            raise CrawlerError("protocol_changed", "Itinerary must be an object")
        identity = item.get("itineraryId")
        segments = item.get("flightSegments")
        if not isinstance(identity, str) or not identity or not isinstance(segments, list) or not segments:
            raise CrawlerError("protocol_changed", "Itinerary identity or segments are missing")
        if identity in identities:
            raise CrawlerError("data_invalid", "Duplicate itinerary identity in complete response")
        identities.add(identity)
        if len(segments) != 1:
            raise CrawlerError("data_invalid", "Direct itinerary contains multiple flight segments")
        segment = segments[0]
        if not isinstance(segment, dict) or not isinstance(segment.get("transferCount"), int):
            raise CrawlerError("protocol_changed", "Itinerary transferCount is missing")
        if segment["transferCount"] != 0:
            continue
        flights = segment.get("flightList")
        if not isinstance(flights, list) or len(flights) != 1 or not isinstance(flights[0], dict):
            raise CrawlerError("data_invalid", "Direct itinerary must contain exactly one flight record")
        flight = flights[0]
        for field in ("flightNo", "departureAirportCode", "arrivalAirportCode"):
            if not isinstance(flight.get(field), str) or not flight[field]:
                raise CrawlerError("data_invalid", f"Flight identity field is missing: {field}")
        departure_day, departure_time = _processing_datetime(flight.get("departureDateTime"), "departureDateTime")
        arrival_day, arrival_time = _processing_datetime(flight.get("arrivalDateTime"), "arrivalDateTime")
        if task.get("departure_date") and departure_day != task["departure_date"]:
            raise CrawlerError("data_invalid", "Itinerary departure date differs from current task")
        origin = task.get("origin", {})
        destination = task.get("destination", {})
        for side, expected in (("departure", origin), ("arrival", destination)):
            code = flight.get(side + "CityCode")
            if code and expected.get("code") and code.upper() != expected["code"].upper():
                raise CrawlerError("data_invalid", "Itinerary city code differs from current task")
        prices = item.get("priceList")
        if not isinstance(prices, list):
            raise CrawlerError("protocol_changed", "Itinerary priceList must be a list")
        candidates = {"Y": [], "C": []}
        currencies = set()
        unknown_currency = False
        row_reasons = set()
        for price in prices:
            if not isinstance(price, dict) or "cabin" not in price:
                raise CrawlerError("protocol_changed", "Quote cabin is missing")
            cabin = price["cabin"]
            if cabin not in ("Y", "C"):
                continue
            parsed = _processing_quote(price, default_currency)
            candidates[cabin].append(parsed)
            row_reasons.update(parsed["missing"])
            if parsed["currency"] is None:
                unknown_currency = True
            else:
                currencies.add(parsed["currency"])
        if len(currencies) > 1:
            raise CrawlerError("data_invalid", "Cannot compare itinerary quotes in multiple currencies")
        if not any(candidates.values()):
            row_reasons.add("no_target_quote")
        row_price_status = "partial" if row_reasons else "complete"
        price_reasons.update(row_reasons)
        row = dict.fromkeys(CSV_FIELDS, "")
        row.update({name: _processing_source_value(flight.get(name)) for name in LEGACY_FLIGHT_FIELDS})
        row.update({
            "批次标识": task.get("run_id", ""), "任务标识": task.get("key", ""), "行程标识": identity,
            "采集时间": observed_at, "数据获取日期": observed_day, "航班号": flight["flightNo"],
            "实际承运航班号": flight.get("operateFlightNo", ""), "航空公司": flight.get("marketAirlineName", ""),
            "出发日期": departure_day, "出发时间": departure_time, "到达日期": arrival_day, "到达时间": arrival_time,
            "飞行时长": flight.get("duration", ""), "出发国家": flight.get("departureCountryName", ""),
            "出发城市": flight.get("departureCityName") or origin.get("name", ""),
            "出发机场": flight.get("departureAirportName", ""), "出发机场三字码": flight["departureAirportCode"],
            "到达国家": flight.get("arrivalCountryName", ""), "到达城市": flight.get("arrivalCityName") or destination.get("name", ""),
            "到达机场": flight.get("arrivalAirportName", ""), "到达机场三字码": flight["arrivalAirportCode"],
            "飞机型号": flight.get("aircraftName", ""), "飞机尺寸": flight.get("aircraftSize", ""),
            "飞机型号三字码": flight.get("aircraftCode", ""), "停留次数": flight.get("stopCount", ""),
            "币种": next(iter(currencies), "") if not unknown_currency else "",
            "价格状态": row_price_status, "价格不完整原因": ";".join(sorted(row_reasons)),
            "舒适度状态": comfort_status,
        })
        stops = flight.get("stopList") or []
        if not isinstance(stops, list) or any(not isinstance(stop, dict) for stop in stops):
            raise CrawlerError("data_invalid", "Invalid stopList")
        row["中转信息"] = " -> ".join(f"{stop.get('cityName', '')}({stop.get('airportName', '')}, {stop.get('duration', '')}分钟)" for stop in stops) or "无中转"
        for cabin, prefix, label in (("Y", "economy", "经济舱"), ("C", "bussiness", "商务舱")):
            quotes = candidates[cabin]
            if not quotes:
                row[label + "价格状态"] = "not_available"
                continue
            complete = all(quote["complete"] for quote in quotes)
            if complete:
                selected = min(quotes, key=lambda quote: quote["total"])
                basis = "total_fare"
            else:
                selected = min(quotes, key=lambda quote: quote["base"])
                # This is only the minimum source adultPrice number when any
                # candidate omits currency, not a same-currency price claim.
                basis = "base_fare_currency_unknown" if any(quote["currency"] is None for quote in quotes) else "base_fare"
            row[label + "价格状态"] = "complete" if complete else "partial"
            row[label + "比较口径"] = basis
            row[label + "币种"] = selected["currency"] or ""
            row[prefix + "_full"] = selected["legacy_full"]
            for suffix, field in (("origin", "base"), ("tax", "tax"), ("total", "total")):
                value = selected[field]
                row[f"{prefix}_{suffix}"] = _processing_decimal_text(value) if value is not None else ""
        rows.append(row)
        if max_rows is not None and len(rows) > max_rows:
            raise CrawlerError('response_too_large', 'Qualified itinerary rows exceed the configured limit')
    columns = CSV_FIELDS
    if options.get("rename_col", True) is False:
        rows = [{internal: row[label] for internal, label in CSV_COLUMNS} for row in rows]
        columns = CSV_INTERNAL_FIELDS
    return {"rows": rows, "columns": list(columns), "status": "succeeded" if rows else "empty",
            "observed_at": observed_at, "comfort_status": comfort_status,
            "price_status": "not_applicable" if not rows else ("partial" if price_reasons else "complete"),
            "price_reason": sorted(price_reasons)}


def _processing_comfort_value(value):
    return (isinstance(value, (str, int, float, bool))
            and (not isinstance(value, str) or bool(value.strip())))


def _processing_comfort_data(payload):
    if (not isinstance(payload, dict) or type(payload.get("status")) is not int
            or payload["status"] != 0 or payload.get("msg") != "success"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    punctuality, plane, cabins = data.get("punctualityInfo"), data.get("planeInfo"), data.get("cabinInfoList")
    if not isinstance(punctuality, dict) or not isinstance(plane, dict) or not isinstance(cabins, list):
        return None
    values = {}
    for source, mapping in (
        (punctuality, {"departureDelaytime": "出发延误时间", "departureBridge": "出发廊桥率", "arrivalDelaytime": "到达延误时间"}),
        (plane, {"planeTypeName": "飞机类型", "planeWidthCategory": "飞机宽度", "planeAge": "飞机机龄"}),
    ):
        for key, label in mapping.items():
            if _processing_comfort_value(source.get(key)):
                values[label] = source[key]
    for cabin in cabins:
        if not isinstance(cabin, dict) or cabin.get("cabin") not in ("Y", "C"):
            continue
        name = "经济舱" if cabin["cabin"] == "Y" else "商务舱"
        fields = {"hasMeal": "餐食标记（原值）", "mealMsg": "餐食信息", "power": "电源"}
        for key, label in fields.items():
            value = cabin.get(key)
            if _processing_comfort_value(value):
                values[name + label] = value
        for key, label in (("seatTilt", "座椅倾斜度"), ("seatWidth", "座椅宽度"), ("seatPitch", "座椅间距")):
            seat = cabin.get(key)
            if isinstance(seat, dict) and _processing_comfort_value(seat.get("value")):
                value = seat["value"]
                # The source page displays these numeric sentinel values as unknown.
                if isinstance(value, (int, float)) and (value == 0 if key == "seatTilt" else -0.5 <= value < 0.5):
                    continue
                values[name + label] = seat["value"]
    return values


def apply_comfort(rows, responses):
    """Attach known comfort responses by operating flight, preserving quotes."""
    by_flight = {}
    for response in responses:
        if not isinstance(response, dict) or not isinstance(response.get("flight_no"), str):
            continue
        values = _processing_comfort_data(response.get("payload"))
        if values is not None:
            by_flight.setdefault(response["flight_no"], {}).update(values)
    result = []
    statuses = []
    targets, covered = set(), set()
    covered_rows = 0
    missing_fields = {}
    common = {"出发延误时间", "出发廊桥率", "到达延误时间", "飞机类型", "飞机宽度", "飞机机龄"}
    for original in rows:
        row = dict(original)
        english = "flightNo" in row and "航班号" not in row

        def column(label):
            return CSV_INTERNAL_NAMES[label] if english else label

        number = row.get(column("实际承运航班号")) or row.get(column("航班号"))
        targets.add(number)
        values = by_flight.get(number, {})
        required = set(common)
        for prefix, name in (("economy", "经济舱"), ("bussiness", "商务舱")):
            if row.get(prefix + "_origin") not in (None, ""):
                required.update(name + suffix for suffix in ("餐食信息", "座椅倾斜度", "座椅宽度", "座椅间距"))
        status = "complete" if required.issubset(values) else ("partial" if values else "unavailable")
        if number in by_flight:
            covered.add(number)
            covered_rows += 1
            for label in required - values.keys():
                field = CSV_INTERNAL_NAMES[label]
                missing_fields[field] = missing_fields.get(field, 0) + 1
        for label, value in values.items():
            row[column(label)] = value
        row[column("舒适度状态")] = status
        result.append(row)
        statuses.append(status)
    status = "not_applicable" if not rows else "complete" if all(item == "complete" for item in statuses) else (
        "unavailable" if all(item == "unavailable" for item in statuses) else "partial")
    reasons = []
    if covered_rows < len(rows):
        reasons.append("response_coverage_incomplete")
    if missing_fields:
        reasons.append("source_missing")
    return {"rows": result, "comfort_status": status,
            "comfort_reason": ";".join(reasons) or None,
            "comfort_summary": {"result_flights": len(targets), "covered_flights": len(covered),
                                "covered_rows": covered_rows, "complete_rows": statuses.count("complete"),
                                "partial_rows": statuses.count("partial"), "unavailable_rows": statuses.count("unavailable"),
                                "missing_fields": dict(sorted(missing_fields.items()))}}
