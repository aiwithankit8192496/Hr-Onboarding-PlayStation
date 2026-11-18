"""hr_login.py: password hashes, the signed session cookie, and the sign-in throttle."""

import importlib.util
import json
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hr_login

SECRET = "k" * 40


class PasswordTests(unittest.TestCase):

    def test_a_hash_verifies_only_its_own_password(self):
        stored = hr_login.hash_password("correct horse battery")
        self.assertTrue(hr_login.verify_password("correct horse battery", stored))
        self.assertFalse(hr_login.verify_password("correct horse batterY", stored))
        self.assertFalse(hr_login.verify_password("", stored))

    def test_the_hash_does_not_contain_the_password_and_is_salted(self):
        one, two = hr_login.hash_password("correct horse battery"), hr_login.hash_password("correct horse battery")
        self.assertNotIn("correct", one)
        self.assertNotEqual(one, two)
        self.assertTrue(hr_login.valid_hash_format(one))

    def test_malformed_hashes_never_match_and_never_raise(self):
        for stored in ("", None, "plain-password", "scrypt$1$2$3$4", "scrypt$x$8$1$AAAA$BBBB", "md5$1$1$1$AAAAAAAAAAA$BBBBBBBBBBBBBBBBBBBB"):
            self.assertFalse(hr_login.valid_hash_format(stored), str(stored))
            self.assertFalse(hr_login.verify_password("anything", stored), str(stored))


class SessionTests(unittest.TestCase):

    def test_a_fresh_cookie_gives_back_the_user(self):
        self.assertEqual(hr_login.read_session(hr_login.make_session("hr", SECRET), SECRET), "hr")

    def test_it_lasts_eight_hours_and_not_a_second_more(self):
        cookie = hr_login.make_session("hr", SECRET, now=1000)
        self.assertEqual(hr_login.SESSION_SECONDS, 8 * 3600)
        self.assertEqual(hr_login.read_session(cookie, SECRET, now=1000 + 8 * 3600 - 1), "hr")
        self.assertIsNone(hr_login.read_session(cookie, SECRET, now=1000 + 8 * 3600))
        self.assertIsNone(hr_login.read_session(cookie, SECRET, now=1000 + 9 * 3600))

    def test_another_secret_or_a_changed_cookie_is_refused(self):
        cookie = hr_login.make_session("hr", SECRET)
        self.assertIsNone(hr_login.read_session(cookie, "z" * 40))
        payload, signature = cookie.split(".")
        for bad in (payload + "." + signature[:-1], "x" + payload + "." + signature, payload, payload + "." + payload, "", None, "a.b.c"):
            self.assertIsNone(hr_login.read_session(bad, SECRET), str(bad))

    def test_a_cookie_for_one_user_cannot_be_turned_into_another(self):
        cookie = hr_login.make_session("hr", SECRET)
        other = hr_login.make_session("admin", SECRET)
        forged = other.split(".")[0] + "." + cookie.split(".")[1]  # admin's payload, hr's signature
        self.assertIsNone(hr_login.read_session(forged, SECRET))

    def test_the_cookie_flags(self):
        header = hr_login.cookie_header("value")
        for part in ("hr_session=value", "HttpOnly", "Secure", "SameSite=Strict", "Max-Age=28800", "Path=/"):
            self.assertIn(part, header)
        self.assertIn("Max-Age=0", hr_login.cookie_header(""))
        self.assertNotIn("Secure", hr_login.cookie_header("value", secure=False))


class UsersTests(unittest.TestCase):

    def test_a_map_of_users_is_read(self):
        one, two = hr_login.hash_password("first-password-1"), hr_login.hash_password("second-password-2")
        secret = hr_login.generate_totp_secret()
        users = hr_login.parse_users(json.dumps({"priya": one, "rahul.k": {"hash": two, "totp": secret}}))
        self.assertEqual(users, {"priya": {"hash": one, "totp": None}, "rahul.k": {"hash": two, "totp": secret}})

    def test_dump_and_parse_round_trip(self):
        users = {"a": {"hash": hr_login.hash_password("first-password-1"), "totp": hr_login.generate_totp_secret()},
                 "b": {"hash": hr_login.hash_password("second-password-2"), "totp": None}}
        self.assertEqual(hr_login.parse_users(hr_login.dump_users(users)), users)

    def test_bad_maps_are_refused_with_a_message_that_holds_no_value(self):
        good = hr_login.hash_password("first-password-1")
        for text in ("", "nope", "{}", "[]", "5", json.dumps({"": good}), json.dumps({"a b": good}), json.dumps({"x" * 41: good}),
                     json.dumps({"priya": "plain-password"}), json.dumps({"priya": 5}),
                     json.dumps({"priya": {"totp": "A" * 32}}), json.dumps({"priya": {"hash": good, "totp": "bad"}}),
                     json.dumps({"priya": {"hash": good, "extra": 1}})):
            with self.assertRaises(ValueError, msg=text) as caught:
                hr_login.parse_users(text)
            self.assertNotIn("plain-password", str(caught.exception))

    def test_load_users_prefers_the_map_and_falls_back_to_the_pair(self):
        h = hr_login.hash_password("first-password-1")
        self.assertEqual(list(hr_login.load_users({"HR_USERS": json.dumps({"a": h}), "HR_USER": "b", "HR_PASSWORD_HASH": h})), ["a"])
        self.assertEqual(list(hr_login.load_users({"HR_USER": "b", "HR_PASSWORD_HASH": h})), ["b"])
        self.assertEqual(hr_login.load_users({"HR_USER": "b"}), {})
        self.assertEqual(hr_login.load_users({}), {})


class MakeHashScriptTests(unittest.TestCase):
    """scripts/make_hr_password_hash.py builds the HR_USERS value, enrolling an authenticator."""

    def run_script(self, *args, passwords=(), codes=None):
        """Run it; `codes` is a function(secret) -> the code the "app" shows (default: the right one)."""
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            "make_hash", Path(__file__).resolve().parent.parent / "scripts" / "make_hr_password_hash.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = io.StringIO()
        shown = {}
        real_enrol = module.show_enrolment

        def spy(user, secret):
            shown["secret"] = secret
            real_enrol(user, secret)

        def typed(prompt):
            return (codes or (lambda secret: hr_login.totp_code(secret, int(time.time() // 30))))(shown["secret"])

        with mock.patch.object(sys, "argv", ["make", *args]), mock.patch("getpass.getpass", side_effect=list(passwords)), \
                mock.patch.object(module, "show_enrolment", spy), mock.patch("builtins.input", typed), redirect_stdout(out):
            module.main()
        value = [line for line in out.getvalue().splitlines() if line.startswith("HR_USERS =")][0]
        return hr_login.parse_users(value.split("=", 1)[1]), out.getvalue()

    def test_add_a_first_user_enrols_an_authenticator_and_confirms_it(self):
        first, output = self.run_script("add", "priya", passwords=["first-password-1", "first-password-1"])
        self.assertTrue(hr_login.verify_password("first-password-1", first["priya"]["hash"]))
        secret = first["priya"]["totp"]
        self.assertTrue(hr_login.TOTP_SECRET.fullmatch(secret))
        # the address for the app, with the secret, the issuer and the user name
        self.assertIn(hr_login.otpauth_uri("priya", secret), output)
        self.assertIn("otpauth://totp/HR%20Review%20Portal%3Apriya?secret=" + secret, output)
        self.assertNotIn("first-password-1", output)

    def test_a_wrong_confirmation_code_makes_nothing(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_script("add", "priya", passwords=["first-password-1"] * 2, codes=lambda secret: "000000")
        self.assertIn("does not match", str(caught.exception))

    def test_add_a_second_user_keeps_the_first_users_secret_and_password(self):
        first, _ = self.run_script("add", "priya", passwords=["first-password-1"] * 2)
        both, output = self.run_script("add", "rahul", "--map", hr_login.dump_users(first), passwords=["second-password-2"] * 2)
        self.assertEqual(sorted(both), ["priya", "rahul"])
        self.assertEqual(both["priya"], first["priya"])
        self.assertNotEqual(both["rahul"]["totp"], first["priya"]["totp"])
        self.assertTrue(hr_login.verify_password("second-password-2", both["rahul"]["hash"]))
        self.assertNotIn(first["priya"]["totp"], output.split("HR_USERS")[0])  # not shown again for the unchanged user

    def test_changing_a_password_keeps_the_secret_and_reset_makes_a_new_one(self):
        users, _ = self.run_script("add", "priya", passwords=["first-password-1"] * 2)
        changed, output = self.run_script("add", "priya", "--map", hr_login.dump_users(users), passwords=["third-password-3"] * 2)
        self.assertEqual(changed["priya"]["totp"], users["priya"]["totp"])
        self.assertNotIn("otpauth://", output)  # nothing to scan again
        self.assertTrue(hr_login.verify_password("third-password-3", changed["priya"]["hash"]))
        self.assertFalse(hr_login.verify_password("first-password-1", changed["priya"]["hash"]))
        reset, output = self.run_script("add", "priya", "--map", hr_login.dump_users(changed), "--reset-totp", passwords=["third-password-3"] * 2)
        self.assertNotEqual(reset["priya"]["totp"], users["priya"]["totp"])
        self.assertIn("otpauth://", output)

    def test_an_old_user_without_a_secret_gets_one(self):
        old = hr_login.dump_users({"priya": {"hash": hr_login.hash_password("first-password-1"), "totp": None}})
        users, _ = self.run_script("add", "priya", "--map", old, passwords=["first-password-1"] * 2)
        self.assertTrue(users["priya"]["totp"])

    def test_no_mfa_is_for_a_laptop_and_says_so(self):
        users, output = self.run_script("add", "priya", "--no-mfa", passwords=["first-password-1"] * 2)
        self.assertIsNone(users["priya"]["totp"])
        self.assertIn("NO authenticator", output)
        self.assertNotIn("otpauth://", output)

    def test_removing_a_user_and_refusing_the_last_one(self):
        users, _ = self.run_script("add", "priya", passwords=["first-password-1"] * 2)
        users, _ = self.run_script("add", "rahul", "--map", hr_login.dump_users(users), passwords=["second-password-2"] * 2)
        left, _ = self.run_script("remove", "rahul", "--map", hr_login.dump_users(users))
        self.assertEqual(list(left), ["priya"])
        with self.assertRaises(SystemExit):
            self.run_script("remove", "priya", "--map", hr_login.dump_users(left))

    def test_it_refuses_a_short_password_and_a_mismatch(self):
        with self.assertRaises(SystemExit):
            self.run_script("add", "priya", passwords=["short", "short"])
        with self.assertRaises(SystemExit):
            self.run_script("add", "priya", passwords=["first-password-1", "different-password"])

    def test_a_new_session_secret_can_be_asked_for(self):
        _, output = self.run_script("add", "priya", "--new-secret", passwords=["first-password-1"] * 2)
        secret = [line for line in output.splitlines() if line.startswith("SESSION_SECRET =")][0].split("=", 1)[1].strip()
        self.assertGreaterEqual(len(secret), hr_login.MIN_SECRET_LENGTH)


class TotpTests(unittest.TestCase):
    """RFC 6238 and the sign-in rules around it."""

    # RFC 6238 appendix B, SHA-1, secret "12345678901234567890"; the RFC lists 8 digits, we use the last 6
    RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    RFC_VECTORS = ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
                   (2000000000, "279037"), (20000000000, "353130"))

    def test_it_matches_the_published_test_vectors(self):
        for seconds, expected in self.RFC_VECTORS:
            self.assertEqual(hr_login.totp_code(self.RFC_SECRET, seconds // 30), expected, seconds)

    def test_check_accepts_this_step_and_one_either_side_only(self):
        now = 1111111109
        step = now // 30
        for offset, ok in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
            code = hr_login.totp_code(self.RFC_SECRET, step + offset)
            self.assertEqual(hr_login.check_totp(self.RFC_SECRET, code, now=now), step + offset if ok else None, offset)

    def test_the_window_is_ninety_seconds_wide_around_the_code_span(self):
        # the code for the span [1111111080, 1111111110) is accepted from one step before to one step after it
        code = hr_login.totp_code(self.RFC_SECRET, 1111111109 // 30)
        self.assertIsNone(hr_login.check_totp(self.RFC_SECRET, code, now=1111111049))
        self.assertIsNotNone(hr_login.check_totp(self.RFC_SECRET, code, now=1111111050))
        self.assertIsNotNone(hr_login.check_totp(self.RFC_SECRET, code, now=1111111139))
        self.assertIsNone(hr_login.check_totp(self.RFC_SECRET, code, now=1111111140))

    def test_spaces_are_ignored_and_junk_is_refused(self):
        code = hr_login.totp_code(self.RFC_SECRET, 1111111109 // 30)
        self.assertIsNotNone(hr_login.check_totp(self.RFC_SECRET, code[:3] + " " + code[3:], now=1111111109))
        for bad in ("", None, "12345", "1234567", "abcdef", "0000000", "-12345", 123456):
            self.assertIsNone(hr_login.check_totp(self.RFC_SECRET, bad, now=1111111109), str(bad))

    def test_a_new_secret_is_random_base32_of_160_bits(self):
        import base64

        secrets_made = {hr_login.generate_totp_secret() for _ in range(50)}
        self.assertEqual(len(secrets_made), 50)
        for secret in secrets_made:
            self.assertTrue(hr_login.TOTP_SECRET.fullmatch(secret))
            self.assertEqual(len(base64.b32decode(secret + "=" * (-len(secret) % 8))), 20)

    def test_the_otpauth_address(self):
        uri = hr_login.otpauth_uri("priya", self.RFC_SECRET)
        self.assertEqual(
            uri,
            "otpauth://totp/HR%20Review%20Portal%3Apriya?secret=" + self.RFC_SECRET
            + "&issuer=HR%20Review%20Portal&algorithm=SHA1&digits=6&period=30")

    def test_the_in_memory_throttle_accepts_a_step_once_and_never_an_older_one(self):
        throttle = hr_login.Throttle()
        self.assertTrue(throttle.use_step("priya", 100))
        self.assertFalse(throttle.use_step("priya", 100))
        self.assertFalse(throttle.use_step("priya", 99))
        self.assertTrue(throttle.use_step("priya", 101))
        self.assertTrue(throttle.use_step("rahul", 100))  # per user

    def test_it_uses_no_network_and_nothing_outside_the_standard_library(self):
        source = (Path(__file__).resolve().parent.parent / "hr_login.py").read_text(encoding="utf-8")
        for module in re.findall(r"^(?:import|from) (\w+)", source, re.M):
            self.assertIn(module, sys.stdlib_module_names, module)
        for forbidden in ("socket", "urllib.request", "http.client", "requests", "pyotp"):
            self.assertNotIn(forbidden, source)


class ScryptSettingsTests(unittest.TestCase):
    """The test run lowers scrypt for speed (conftest.py); production keeps and enforces the strong numbers."""

    def test_the_production_numbers_in_the_source_are_unchanged(self):
        source = (Path(__file__).resolve().parent.parent / "hr_login.py").read_text(encoding="utf-8")
        self.assertIn("SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1", source)
        self.assertIn("MIN_SCRYPT_N = 2 ** 14", source)

    def test_no_application_file_reads_a_test_setting(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("hr_login.py", "hr_info.py", "storage.py", "react_onboard.py", "assets.py", "security_headers.py"):
            text = (root / name).read_text(encoding="utf-8")
            for word in ("import pytest", "TEST_SCRYPT", "FAST_SCRYPT", "environ.get(\"SCRYPT", "environ.get(\"HR_SCRYPT"):
                self.assertNotIn(word, text, name)

    def test_a_hash_weaker_than_the_production_minimum_is_not_accepted(self):
        from unittest import mock

        weak = hr_login.hash_password("some-password-12")  # made with the lowered test cost
        with mock.patch.object(hr_login, "MIN_SCRYPT_N", 2 ** 14):
            self.assertFalse(hr_login.valid_hash_format(weak))
            self.assertFalse(hr_login.verify_password("some-password-12", weak))
            with self.assertRaises(ValueError):
                hr_login.parse_users(json.dumps({"priya": weak}))
        self.assertTrue(hr_login.valid_hash_format(weak))  # with the test setting it is fine

    def test_a_hash_at_the_production_cost_is_accepted(self):
        from unittest import mock

        with mock.patch.object(hr_login, "SCRYPT_N", 2 ** 14), mock.patch.object(hr_login, "MIN_SCRYPT_N", 2 ** 14):
            strong = hr_login.hash_password("some-password-12")
            self.assertTrue(hr_login.valid_hash_format(strong))
            self.assertTrue(hr_login.verify_password("some-password-12", strong))
            self.assertFalse(hr_login.verify_password("some-password-13", strong))


class ThrottleTests(unittest.TestCase):

    def test_five_failures_block_and_the_window_lets_go(self):
        throttle = hr_login.Throttle(limit=5, window=300)
        for second in range(5):
            self.assertFalse(throttle.blocked("hr", now=second))
            throttle.fail("hr", now=second)
        self.assertTrue(throttle.blocked("hr", now=10))
        self.assertTrue(throttle.blocked("hr", now=299))
        self.assertFalse(throttle.blocked("hr", now=305))  # the failures are older than 300 s
        self.assertFalse(throttle.blocked("someone-else", now=10))

    def test_a_good_sign_in_clears_the_count(self):
        throttle = hr_login.Throttle(limit=2, window=300)
        throttle.fail("hr", now=1)
        throttle.clear("hr")
        throttle.fail("hr", now=2)
        self.assertFalse(throttle.blocked("hr", now=3))


if __name__ == "__main__":
    unittest.main()
