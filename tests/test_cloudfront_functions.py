"""The two CloudFront viewer-request Functions, run in a real JavaScript engine (V8), not translated.

The night check closes both portals from 00:00 to 06:59 India time (UTC+05:30). The HR Function also
only lets the office addresses in. Needs `mini-racer` (requirements-dev.txt).
"""

import datetime
import json
import unittest
from pathlib import Path

from py_mini_racer import MiniRacer

FOLDER = Path(__file__).resolve().parent.parent / "cloudfront"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def millis(year, month, day, hour, minute, second=0):
    return int(datetime.datetime(year, month, day, hour, minute, second, tzinfo=IST).timestamp() * 1000)


def engine(name, now_ms=None, office=None):
    """The Function's code in a fresh engine; Date.now() returns `now_ms` when given."""

    code = (FOLDER / name).read_text(encoding="utf-8")
    context = MiniRacer()

    if now_ms is not None:
        context.eval(f"Date.now = function () {{ return {now_ms}; }};")

    context.eval(code)

    if office is not None:
        context.eval(f"OFFICE_IPS = {json.dumps(office)};")

    return context


def call(context, ip="203.0.113.10"):
    event = {"request": {"uri": "/", "headers": {}}, "viewer": {"ip": ip}}
    return json.loads(context.eval(f"JSON.stringify(handler({json.dumps(event)}))"))


class NightCheckTests(unittest.TestCase):
    """The same boundaries for both Functions."""

    FILES = ("portal-viewer-request.js", "hr-viewer-request.js")

    def closed(self, name, hour, minute, second=0):
        return engine(name).eval(f"isClosed({millis(2026, 11, 17, hour, minute, second)})")

    def test_boundaries_in_india_time(self):
        for name in self.FILES:
            with self.subTest(name):
                self.assertFalse(self.closed(name, 23, 59), "23:59 is open")
                self.assertTrue(self.closed(name, 0, 0), "00:00 is closed")
                self.assertTrue(self.closed(name, 0, 0, 1))
                self.assertTrue(self.closed(name, 6, 59), "06:59 is closed")
                self.assertTrue(self.closed(name, 6, 59, 59), "06:59:59 is closed")
                self.assertFalse(self.closed(name, 7, 0), "07:00 is open")

    def test_the_whole_day(self):
        for name in self.FILES:
            context = engine(name)
            for hour in range(24):
                with self.subTest(name=name, hour=hour):
                    self.assertEqual(context.eval(f"isClosed({millis(2026, 11, 17, hour, 30)})"), hour < 7)

    def test_it_is_india_time_not_the_servers_time(self):
        # 18:30 UTC is midnight in India: closed. 01:29 UTC is 06:59 in India: closed. 01:30 UTC is 07:00: open.
        utc = datetime.timezone.utc
        for name in self.FILES:
            context = engine(name)
            for (h, m, closed) in ((18, 29, False), (18, 30, True), (19, 0, True)):
                moment = int(datetime.datetime(2026, 11, 16, h, m, tzinfo=utc).timestamp() * 1000)
                self.assertEqual(context.eval(f"isClosed({moment})"), closed, f"{h}:{m} UTC")
            for (h, m, closed) in ((1, 29, True), (1, 30, False)):
                moment = int(datetime.datetime(2026, 11, 17, h, m, tzinfo=utc).timestamp() * 1000)
                self.assertEqual(context.eval(f"isClosed({moment})"), closed, f"{h}:{m} UTC")

    def test_every_day_including_a_month_and_year_end(self):
        for name in self.FILES:
            context = engine(name)
            for (y, mo, d) in ((2026, 12, 31), (2027, 1, 1), (2028, 2, 29), (2026, 3, 1)):
                self.assertTrue(context.eval(f"isClosed({millis(y, mo, d, 0, 0)})"))
                self.assertFalse(context.eval(f"isClosed({millis(y, mo, d, 7, 0)})"))

    def test_the_closed_reply_is_a_503_page_that_is_never_cached(self):
        for name in self.FILES:
            with self.subTest(name):
                reply = call(engine(name, millis(2026, 11, 17, 3, 0)))
                self.assertEqual(reply["statusCode"], 503)
                self.assertEqual(reply["headers"]["cache-control"]["value"], "no-store")
                self.assertIn("text/html", reply["headers"]["content-type"]["value"])
                self.assertIn("The portal is available 7 am", reply["body"])
                self.assertIn("12 am IST", reply["body"])
                self.assertIn("<html", reply["body"])

    def test_when_open_the_request_goes_on(self):
        for name in self.FILES:
            with self.subTest(name):
                reply = call(engine(name, millis(2026, 11, 17, 7, 0)))
                self.assertEqual(reply["uri"], "/")
                self.assertNotIn("statusCode", reply)

    def test_the_functions_stay_inside_the_cloudfront_size_limit(self):
        for name in self.FILES:
            self.assertLess((FOLDER / name).stat().st_size, 10 * 1024, name)


class HrOfficeRuleTests(unittest.TestCase):

    NAME = "hr-viewer-request.js"
    DAY = millis(2026, 11, 17, 12, 0)
    NIGHT = millis(2026, 11, 17, 2, 0)

    def test_the_office_address_passes_by_day(self):
        reply = call(engine(self.NAME, self.DAY, ["203.0.113.10"]), ip="203.0.113.10")
        self.assertNotIn("statusCode", reply)

    def test_another_address_gets_a_403(self):
        reply = call(engine(self.NAME, self.DAY, ["203.0.113.10"]), ip="198.51.100.7")
        self.assertEqual(reply["statusCode"], 403)
        self.assertEqual(reply["body"], "Forbidden")

    def test_a_range_matches_exactly_its_addresses(self):
        context = engine(self.NAME, self.DAY, ["198.51.100.0/24"])
        self.assertNotIn("statusCode", call(context, ip="198.51.100.0"))
        self.assertNotIn("statusCode", call(context, ip="198.51.100.255"))
        self.assertEqual(call(context, ip="198.51.101.0")["statusCode"], 403)
        self.assertEqual(call(context, ip="198.51.99.255")["statusCode"], 403)

    def test_ipv6_and_garbage_are_refused(self):
        context = engine(self.NAME, self.DAY, ["203.0.113.10"])
        for ip in ("2001:db8::1", "", "not an ip", "999.1.1.1"):
            self.assertEqual(call(context, ip=ip)["statusCode"], 403, ip)

    def test_an_outsider_at_night_still_just_gets_403(self):
        # nothing is revealed to a stranger, not even that the portal is closed
        reply = call(engine(self.NAME, self.NIGHT, ["203.0.113.10"]), ip="198.51.100.7")
        self.assertEqual(reply["statusCode"], 403)

    def test_the_office_at_night_gets_the_closed_page(self):
        reply = call(engine(self.NAME, self.NIGHT, ["203.0.113.10"]), ip="203.0.113.10")
        self.assertEqual(reply["statusCode"], 503)

    def test_the_example_list_lets_nobody_in(self):
        # the file as shipped holds documentation addresses only
        reply = call(engine(self.NAME, self.DAY), ip="192.0.2.1")
        self.assertEqual(reply["statusCode"], 403)

    def test_it_no_longer_touches_the_login_header(self):
        self.assertNotIn("authorization", (FOLDER / self.NAME).read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
