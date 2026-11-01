"""Behavioral contracts for itinerary and quote normalization."""
import copy
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_processing():
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "processing_contract.py"
    header = '''from __future__ import annotations
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo
import copy
import json
class CrawlerError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
'''
    path.write_text(header + (ROOT / "source_sections/processing.py.inc").read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("processing_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return directory, module


def quote(base="100.10", tax="20.20", cabin="Y", currency="CNY"):
    return {"adultPrice": base, "adultTax": tax, "sortPrice": str(float(base) + float(tax)),
            "cabin": cabin, "currency": currency, "miseryIndex": 1.7}


def itinerary(identity="one", number="MU1234", airport="SHA", prices=None, transfer=0):
    flight = {"flightNo": number, "operateFlightNo": "FM4567", "marketAirlineName": "测试航空",
              "departureCityName": "上海", "departureCityCode": "SHA", "departureAirportCode": airport,
              "departureAirportName": "上海机场", "arrivalCityName": "中国香港", "arrivalCityCode": "HKG",
              "arrivalAirportCode": "HKG", "arrivalAirportName": "香港机场",
              "departureDateTime": "2027-01-10 12:00", "arrivalDateTime": "2027-01-10 14:00",
              "duration": 120, "stopList": []}
    return {"itineraryId": identity, "flightSegments": [{"transferCount": transfer, "flightList": [flight]}],
            "priceList": [quote()] if prices is None else prices}


class ProcessingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory, cls.module = load_processing()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def normalize(self, items, observed_at="2027-01-01T12:00:00+00:00", **options):
        task = {"run_id": "run", "index": 0, "key": "route", "origin": {"code": "SHA", "name": "上海"},
                "destination": {"code": "HKG", "name": "香港"}, "departure_date": "2027-01-10"}
        return self.module.normalize_response({"data": {"flightItineraryList": items}}, task,
                                               {"direct_flight": True, **options}, observed_at)

    def test_same_flight_number_different_itineraries_never_cross_join(self):
        result = self.normalize([itinerary("a", airport="SHA"), itinerary("b", airport="PVG")])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual({r["行程标识"] for r in result["rows"]}, {"a", "b"})
        self.assertEqual({r["出发机场三字码"] for r in result["rows"]}, {"SHA", "PVG"})
        self.assertTrue(all(r["实际承运航班号"] == "FM4567" for r in result["rows"]))

    def test_minimum_total_keeps_base_tax_from_same_quote_with_exact_decimal(self):
        result = self.normalize([itinerary(prices=[quote("100.10", "20.20"), quote("110.10", "1.20"), quote("200", "30", "C")])])
        row = result["rows"][0]
        self.assertEqual((row["economy_origin"], row["economy_tax"], row["economy_total"]), ("110.1", "1.2", "111.3"))
        self.assertEqual(row["bussiness_total"], "230")
        self.assertEqual(row["economy_full"], 1.7)

    def test_export_retains_the_legacy_example_column_union_and_new_identity(self):
        legacy = set()
        for path in (ROOT / "data_examples").rglob("*.csv"):
            if '.ipynb_checkpoints' not in path.parts:
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    legacy.update(next(csv.reader(handle)))
        result = self.normalize([itinerary()])
        self.assertTrue(legacy.issubset(result["columns"]), legacy - set(result["columns"]))
        self.assertTrue({'批次标识', '任务标识', '行程标识', '价格状态', '舒适度状态'}.issubset(result['columns']))

    def test_legacy_source_fields_preserve_false_zero_json_and_missing_values(self):
        item = itinerary()
        flight = item['flightSegments'][0]['flightList'][0]
        flight.update(sequenceNo=0, departureTerminal='T2', arrivalTerminal=None,
                      departureCityId='0002', marketAirlineCode='MU', marketAlliance='联盟',
                      isLowCostCarrier=False, mainSegment={'index': 0},
                      departureDateTime='2027-01-10 12:00:07', stopList=[])
        row = self.normalize([item])['rows'][0]
        self.assertEqual(row['sequenceNo'], 0)
        self.assertEqual(row['departureTerminal'], 'T2')
        self.assertEqual(row['arrivalTerminal'], '')
        self.assertEqual(row['departureCountryCode'], '')
        self.assertEqual(row['departureCityId'], '0002')
        self.assertIs(row['isLowCostCarrier'], False)
        self.assertEqual(json.loads(row['mainSegment']), {'index': 0})
        self.assertEqual(row['stopList'], '[]')
        self.assertEqual(row['operateFlightNo'], row['实际承运航班号'])
        self.assertEqual(row['departureDateTime'], '2027-01-10 12:00:07')
        self.assertEqual(row['出发时间'], '12:00:07')
        del flight['stopList']
        self.assertEqual(self.normalize([item])['rows'][0]['stopList'], '')

    def test_legacy_full_is_the_selected_quotes_raw_misery_index(self):
        first = dict(quote('100', '50'), miseryIndex=999)
        best = dict(quote('110', '10'), miseryIndex=0)
        business = dict(quote('200', '20', cabin='C'), miseryIndex=456.7)
        row = self.normalize([itinerary(prices=[first, best, business])])['rows'][0]
        self.assertEqual(row['economy_full'], 0)
        self.assertEqual(row['bussiness_full'], 456.7)
        self.assertEqual(row['economy_total'], '120')
        del best['miseryIndex']
        row = self.normalize([itinerary(prices=[first, best])])['rows'][0]
        self.assertEqual(row['economy_full'], '')
        self.assertEqual(row['bussiness_full'], '')
        partial = {'adultPrice': 50, 'cabin': 'Y', 'miseryIndex': 8.25}
        row = self.normalize([itinerary(prices=[first, partial])])['rows'][0]
        self.assertEqual(row['economy_full'], 8.25)
        self.assertEqual(row['经济舱比较口径'], 'base_fare_currency_unknown')

    def test_legacy_alias_has_unique_english_columns_and_same_source_value(self):
        result = self.normalize([itinerary()], rename_col=False)
        self.assertEqual(len(result['columns']), len(set(result['columns'])))
        row = result['rows'][0]
        self.assertEqual(row['operateFlightNo'], 'FM4567')
        self.assertEqual(row['legacy_operateFlightNo'], row['operateFlightNo'])
        self.assertEqual(row['departureDateTime'], '2027-01-10 12:00')

    def test_explicit_empty_is_distinct_from_missing_data_structure(self):
        self.assertEqual(self.normalize([])["status"], "empty")
        with self.assertRaises(self.module.CrawlerError) as caught:
            self.module.normalize_response({"data": {}}, {}, {}, "2027-01-01T00:00:00Z")
        self.assertEqual(caught.exception.code, "protocol_changed")

    def test_row_limit_rejects_the_whole_result_instead_of_truncating(self):
        with self.assertRaises(self.module.CrawlerError) as caught:
            self.normalize([itinerary('first'), itinerary('second')], max_rows=1)
        self.assertEqual(caught.exception.code, 'response_too_large')

    def test_row_limit_counts_only_qualified_direct_itineraries_and_allows_the_boundary(self):
        result=self.normalize([itinerary('transfer-one', transfer=1), itinerary('direct'),
                               itinerary('transfer-two', transfer=1)], max_rows=1)
        self.assertEqual([row['行程标识'] for row in result['rows']], ['direct'])

    def test_direct_filter_does_not_mutate_response_and_rejects_unsupported_multi_leg_mode(self):
        items = [itinerary(transfer=1)]
        original = copy.deepcopy(items)
        self.assertEqual(self.normalize(items)["status"], "empty")
        self.assertEqual(items, original)
        with self.assertRaises(self.module.CrawlerError):
            self.normalize(items, direct_flight=False)

    def test_known_different_currencies_are_not_silently_compared(self):
        with self.assertRaises(self.module.CrawlerError) as caught:
            self.normalize([itinerary(prices=[quote(), quote(currency="USD")])])
        self.assertEqual(caught.exception.code, "data_invalid")

    def test_zero_fare_is_valid_but_negative_or_non_finite_money_is_rejected(self):
        self.assertEqual(self.normalize([itinerary(prices=[quote("0", "0")])])["rows"][0]["economy_total"], "0")
        for value in ("NaN", "Infinity", "-1"):
            with self.subTest(value=value), self.assertRaises(self.module.CrawlerError):
                self.normalize([itinerary(prices=[quote(value, "0")])])

    def test_conflicting_itinerary_identity_is_not_deduplicated_away(self):
        with self.assertRaises(self.module.CrawlerError):
            self.normalize([itinerary("same", airport="SHA"), itinerary("same", airport="PVG")])

    def test_every_output_row_has_fixed_fields_and_observation_time(self):
        row = self.normalize([itinerary()])["rows"][0]
        self.assertEqual(set(row), set(self.module.CSV_FIELDS))
        self.assertEqual(row["采集时间"], "2027-01-01T12:00:00+00:00")

    def test_observation_day_uses_output_timezone_across_utc_midnight(self):
        observed = "2027-01-01T20:30:00+00:00"
        local = self.normalize([itinerary()], observed_at=observed)["rows"][0]
        utc = self.normalize([itinerary()], observed_at=observed, timezone="UTC")["rows"][0]
        self.assertEqual(local["数据获取日期"], "2027-01-02")
        self.assertEqual(utc["数据获取日期"], "2027-01-01")
        self.assertEqual(local["采集时间"], observed)

    def test_currency_and_total_are_taken_from_observed_luggage_criteria_contract(self):
        price = {"adultPrice": 1965, "adultTax": 1221, "cabin": "Y", "freeOilFeeAndTax": False,
                 "luggageVisaKey": json.dumps({"criteria": {"Currency": "RMB", "TotalPrice": 3186.0,
                                                              "TripType": "OW", "FlightInfoList": []}})}
        row = self.normalize([itinerary(prices=[price])])["rows"][0]
        self.assertEqual(row["币种"], "CNY")
        self.assertEqual((row["economy_origin"], row["economy_tax"], row["economy_total"]),
                         ("1965", "1221", "3186"))

    def test_embedded_quote_contract_cannot_conflict_with_explicit_price_fields(self):
        base = {"adultPrice": 1965, "adultTax": 1221, "cabin": "Y", "freeOilFeeAndTax": False}
        conflicting_total = dict(base, luggageVisaKey=json.dumps({"criteria": {"Currency": "RMB", "TotalPrice": 3100}}))
        conflicting_currency = dict(base, currency="USD",
            luggageVisaKey=json.dumps({"criteria": {"Currency": "RMB", "TotalPrice": 3186}}))
        malformed = dict(base, currency="CNY", luggageVisaKey='{not-json')
        for price in (conflicting_total, conflicting_currency, malformed):
            with self.subTest(price=price):
                with self.assertRaises(self.module.CrawlerError) as caught:
                    self.normalize([itinerary(prices=[price])])
                self.assertEqual(caught.exception.code, "data_invalid")

    def test_english_column_option_preserves_same_business_values(self):
        chinese = self.normalize([itinerary()])
        english = self.normalize([itinerary()], rename_col=False)
        self.assertEqual(english["rows"][0]["flightNo"], chinese["rows"][0]["航班号"])
        self.assertEqual(english["rows"][0]["operateFlightNo"], "FM4567")
        self.assertEqual(english["rows"][0]["economy_total"], chinese["rows"][0]["economy_total"])
        self.assertEqual(set(english["rows"][0]), set(english["columns"]))
        self.assertNotIn("航班号", english["columns"])

    def test_domestic_source_base_fare_is_partial_without_invented_tax_total_or_currency(self):
        price = {"adultPrice": 700, "sortPrice": 700, "cabin": "Y", "freeOilFeeAndTax": False}
        original = copy.deepcopy(price)
        result = self.normalize([itinerary(prices=[price])])
        row = result["rows"][0]
        self.assertEqual(price, original)
        self.assertEqual((row["economy_origin"], row["economy_tax"], row["economy_total"]), ("700", "", ""))
        self.assertEqual((row["币种"], row["经济舱币种"]), ("", ""))
        self.assertEqual(row["经济舱价格状态"], "partial")
        self.assertEqual(row["经济舱比较口径"], "base_fare_currency_unknown")
        self.assertEqual(result["price_status"], "partial")
        self.assertEqual(result["price_reason"], ["currency_missing", "tax_missing", "total_missing"])

    def test_partial_candidates_make_comparison_base_based_even_if_selected_quote_is_complete(self):
        complete = quote("100", "300")
        missing = {"adultPrice": 150, "cabin": "Y", "currency": "CNY", "freeOilFeeAndTax": False}
        result = self.normalize([itinerary(prices=[complete, missing])])
        row = result["rows"][0]
        self.assertEqual((row["economy_origin"], row["economy_tax"], row["economy_total"]), ("100", "300", "400"))
        self.assertEqual(row["经济舱价格状态"], "partial")
        self.assertEqual(row["经济舱比较口径"], "base_fare")
        self.assertEqual(result["price_reason"], ["tax_missing", "total_missing"])

    def test_partial_base_fare_candidate_can_be_selected_without_borrowing_other_quote_tax(self):
        complete = quote("120", "10")
        missing = {"adultPrice": 100, "cabin": "Y", "currency": "CNY"}
        row = self.normalize([itinerary(prices=[complete, missing])])["rows"][0]
        self.assertEqual((row["economy_origin"], row["economy_tax"], row["economy_total"]), ("100", "", ""))
        self.assertEqual(row["经济舱比较口径"], "base_fare")

    def test_unknown_unselected_currency_stays_visible_and_is_not_replaced_by_selected_currency(self):
        complete = quote("100", "20")
        unknown = {"adultPrice": 150, "adultTax": 20, "cabin": "Y"}
        result = self.normalize([itinerary(prices=[complete, unknown])])
        row = result["rows"][0]
        self.assertEqual(row["经济舱币种"], "CNY")
        self.assertEqual(row["币种"], "")
        self.assertEqual(row["经济舱价格状态"], "partial")
        self.assertEqual(row["经济舱比较口径"], "base_fare_currency_unknown")
        self.assertEqual(result["price_reason"], ["currency_missing"])

    def test_explicit_total_with_missing_tax_preserves_total_without_fabricating_tax(self):
        price = {"adultPrice": 100, "cabin": "Y", "luggageVisaKey": json.dumps({
            "criteria": {"Currency": "RMB", "TotalPrice": 150}})}
        result = self.normalize([itinerary(prices=[price])])
        row = result["rows"][0]
        self.assertEqual((row["economy_origin"], row["economy_tax"], row["economy_total"]), ("100", "", "150"))
        self.assertEqual(row["经济舱比较口径"], "base_fare")
        self.assertEqual(result["price_reason"], ["tax_missing"])

    def test_complete_candidates_keep_total_based_minimum_and_absent_cabin_is_not_partial(self):
        result = self.normalize([itinerary(prices=[quote("100", "30"), quote("110", "1")])])
        row = result["rows"][0]
        self.assertEqual(row["economy_total"], "111")
        self.assertEqual(row["经济舱价格状态"], "complete")
        self.assertEqual(row["经济舱比较口径"], "total_fare")
        self.assertEqual(row["商务舱价格状态"], "not_available")
        self.assertEqual(row["商务舱比较口径"], "")
        self.assertEqual(result["price_status"], "complete")
        self.assertEqual(result["price_reason"], [])

    def test_empty_and_no_target_cabin_quote_have_distinct_price_status(self):
        empty = self.normalize([])
        self.assertEqual((empty["price_status"], empty["price_reason"]), ("not_applicable", []))
        no_quotes = self.normalize([itinerary(prices=[])])
        self.assertEqual(no_quotes["status"], "succeeded")
        self.assertEqual(no_quotes["price_status"], "partial")
        self.assertEqual(no_quotes["price_reason"], ["no_target_quote"])
        self.assertEqual(no_quotes["rows"][0]["economy_origin"], "")

    def test_partial_english_columns_keep_same_price_status_and_reason(self):
        price = {"adultPrice": 100, "cabin": "Y"}
        result = self.normalize([itinerary(prices=[price])], rename_col=False)
        row = result["rows"][0]
        self.assertEqual(row["economy_price_status"], "partial")
        self.assertEqual(row["economy_comparison_basis"], "base_fare_currency_unknown")
        self.assertEqual(row["price_reason"], "currency_missing;tax_missing;total_missing")
        self.assertEqual(row["price_status"], result["price_status"])

    def test_conflicting_response_currency_sources_are_not_discarded(self):
        payload = {"data": {"flightItineraryList": [itinerary()], "currency": "CNY", "currencyCode": "USD"}}
        with self.assertRaises(self.module.CrawlerError) as caught:
            self.module.normalize_response(payload, {}, {"direct_flight": True}, "2027-01-01T00:00:00Z")
        self.assertEqual(caught.exception.code, "data_invalid")

    def test_response_currency_aliases_agree_without_becoming_conflict(self):
        payload = {"data": {"flightItineraryList": [itinerary()], "currency": "RMB", "currencyCode": "CNY"}}
        result = self.module.normalize_response(payload, {}, {"direct_flight": True}, "2027-01-01T00:00:00Z")
        self.assertEqual(result["price_status"], "complete")
        self.assertEqual(result["rows"][0]["币种"], "CNY")

    def test_undeclared_configuration_currency_cannot_fill_missing_response_currency(self):
        price = {"adultPrice": 100, "adultTax": 20, "cabin": "Y"}
        result = self.normalize([itinerary(prices=[price])], currency="CNY")
        self.assertEqual(result["price_status"], "partial")
        self.assertEqual(result["price_reason"], ["currency_missing"])
        self.assertEqual(result["rows"][0]["币种"], "")
        self.assertEqual(result["rows"][0]["economy_total"], "120")

    def comfort_response(self, flight_no="FM4567"):
        return {"flight_no": flight_no, "payload": {"status": 0, "msg": "success", "data": {
            "punctualityInfo": {"departureDelaytime": 5, "departureBridge": "80%", "arrivalDelaytime": 3},
            "planeInfo": {"planeTypeName": "A320", "planeWidthCategory": "窄体", "planeAge": 4},
            "cabinInfoList": [{"cabin": "Y", "hasMeal": False, "seatTilt": {"value": "100°"},
                               "seatWidth": {"value": "45cm"}, "seatPitch": {"value": "76cm"},
                               "mealMsg": "无餐食", "power": "USB"}]
        }}}

    def test_comfort_uses_operating_flight_and_preserves_core_quotes_without_mutating(self):
        rows = self.normalize([itinerary()], comft_flight=True)["rows"]
        original = copy.deepcopy(rows)
        result = self.module.apply_comfort(rows, [self.comfort_response()])
        self.assertEqual(rows, original)
        self.assertEqual(result["comfort_status"], "complete")
        row = result["rows"][0]
        self.assertEqual(row["舒适度状态"], "complete")
        self.assertEqual(row["飞机机龄"], 4)
        self.assertIs(row["经济舱餐食标记（原值）"], False)
        self.assertEqual(row["economy_total"], original[0]["economy_total"])

    def test_comfort_missing_flight_or_field_is_explicit_partial(self):
        rows = self.normalize([itinerary("one"), itinerary("two")], comft_flight=True)["rows"]
        rows[1]["实际承运航班号"] = "FM9999"
        result = self.module.apply_comfort(rows, [self.comfort_response()])
        self.assertEqual(result["comfort_status"], "partial")
        self.assertEqual([row["舒适度状态"] for row in result["rows"]], ["complete", "unavailable"])
        response = self.comfort_response()
        del response["payload"]["data"]["planeInfo"]["planeAge"]
        partial = self.module.apply_comfort(rows[:1], [response])
        self.assertEqual(partial["comfort_status"], "partial")

    def test_comfort_checks_cabin_fields_even_when_fare_tax_is_unknown(self):
        rows = self.normalize([itinerary(prices=[{"adultPrice": 100, "cabin": "Y"}])], comft_flight=True)["rows"]
        response = self.comfort_response()
        response["payload"]["data"]["cabinInfoList"] = []
        result = self.module.apply_comfort(rows, [response])
        self.assertEqual(result["comfort_status"], "partial")
        self.assertEqual(result["rows"][0]["economy_origin"], "100")
        self.assertEqual(result["rows"][0]["economy_total"], "")

    def test_comfort_zero_seat_measurements_are_unknown_as_on_the_source_page(self):
        rows = self.normalize([itinerary()], comft_flight=True)["rows"]
        for field, label in (("seatTilt", "经济舱座椅倾斜度"), ("seatWidth", "经济舱座椅宽度"),
                             ("seatPitch", "经济舱座椅间距")):
            with self.subTest(field=field):
                response = self.comfort_response()
                response["payload"]["data"]["cabinInfoList"][0][field]["value"] = 0.0
                result = self.module.apply_comfort(rows, [response])
                self.assertEqual(result["rows"][0][label], "")
                self.assertEqual(result["comfort_status"], "partial")

    def test_meal_source_flag_is_not_interpreted_as_a_no_meal_claim(self):
        rows = self.normalize([itinerary()], comft_flight=True)["rows"]
        response = self.comfort_response()
        response["payload"]["data"]["cabinInfoList"][0].update(hasMeal=False, mealMsg="有餐食")
        row = self.module.apply_comfort(rows, [response])["rows"][0]
        self.assertIs(row["经济舱餐食标记（原值）"], False)
        self.assertEqual(row["经济舱餐食信息"], "有餐食")

    def test_comfort_accepts_english_rows_and_does_not_fallback_from_operating_identity(self):
        rows = self.normalize([itinerary()], rename_col=False, comft_flight=True)["rows"]
        result = self.module.apply_comfort(rows, [self.comfort_response()])
        self.assertEqual(result["rows"][0]["plane_age"], 4)
        self.assertEqual(result["rows"][0]["comfort_status"], "complete")
        missing = self.module.apply_comfort(rows, [self.comfort_response("MU1234")])
        self.assertEqual(missing["comfort_status"], "unavailable")
        self.assertEqual(self.module.apply_comfort([], [])["comfort_status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
