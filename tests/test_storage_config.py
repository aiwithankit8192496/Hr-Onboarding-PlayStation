import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from hr_onboarding import hr_login
from hr_onboarding import storage

GOOD = {
    "STORAGE_BACKEND": "s3",
    "S3_BUCKET": "igtl-hr-analytics-ayush-2026",
    "S3_DOCUMENTS_BUCKET": "igtpl-hr-onboarding-docs-2026",
    "S3_RESTRICTED_BUCKET": "igtpl-hr-onboarding-restricted-2026",
    "S3_PREFIX": "onboarding/",
}


class ConfigTests(unittest.TestCase):

    def setUp(self):
        self.addCleanup(setattr, storage, "_store", storage._store)
        storage._store = None
        # Ignore the real .env and boto3: nothing here may reach AWS
        for patcher in (
            mock.patch.object(storage, "load_env_file"),
            mock.patch.dict(sys.modules, {"boto3": mock.MagicMock()}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def check(self, **changes):
        """Run check_config() and get_store() with GOOD plus `changes` (None removes a variable)."""

        env = {**GOOD, **changes}
        env = {name: value for name, value in env.items() if value is not None}

        with mock.patch.dict(os.environ, env, clear=True):
            storage.check_config()
            storage._store = None
            return storage.get_store()

    def assertRefuses(self, variable, **changes):
        for entry_point in (storage.check_config, storage.get_store):
            storage._store = None
            env = {name: value for name, value in {**GOOD, **changes}.items() if value is not None}

            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    entry_point()

            message = str(caught.exception)
            self.assertIn(variable, message)
            self.assertIn("refusing to start", message)
            # Why it is refused is explained, and no bucket name or other value is printed
            for value in env.values():
                if value.strip() and value not in ("s3", "onboarding/"):
                    self.assertNotIn(value, message)

    def test_correct_config_starts(self):
        store = self.check()
        self.assertIsInstance(store, storage.S3Store)
        self.assertEqual(store.bucket, "igtl-hr-analytics-ayush-2026")
        self.assertEqual(store.documents_bucket, "igtpl-hr-onboarding-docs-2026")
        self.assertEqual(store.restricted_bucket, "igtpl-hr-onboarding-restricted-2026")
        self.assertEqual(store.prefix, "onboarding/")

    def test_restricted_bucket_unset(self):
        self.assertRefuses("S3_RESTRICTED_BUCKET", S3_RESTRICTED_BUCKET=None)

    def test_restricted_bucket_empty(self):
        self.assertRefuses("S3_RESTRICTED_BUCKET", S3_RESTRICTED_BUCKET="")
        self.assertRefuses("S3_RESTRICTED_BUCKET", S3_RESTRICTED_BUCKET="   ")

    def test_documents_bucket_unset(self):
        self.assertRefuses("S3_DOCUMENTS_BUCKET", S3_DOCUMENTS_BUCKET=None)

    def test_documents_bucket_empty(self):
        self.assertRefuses("S3_DOCUMENTS_BUCKET", S3_DOCUMENTS_BUCKET="")

    def test_restricted_equals_shared(self):
        self.assertRefuses("S3_RESTRICTED_BUCKET", S3_RESTRICTED_BUCKET=GOOD["S3_BUCKET"])

    def test_documents_equals_shared(self):
        self.assertRefuses("S3_DOCUMENTS_BUCKET", S3_DOCUMENTS_BUCKET=GOOD["S3_BUCKET"])

    def test_restricted_equals_documents(self):
        self.assertRefuses("S3_RESTRICTED_BUCKET", S3_RESTRICTED_BUCKET=GOOD["S3_DOCUMENTS_BUCKET"])

    def test_s3_backend_needs_prefix(self):
        self.assertRefuses("S3_PREFIX", S3_PREFIX=None)
        self.assertRefuses("S3_PREFIX", S3_PREFIX="")

    def test_mongo_s3_backend_may_leave_prefix_empty(self):
        # Records live in MongoDB there, so no student folder is created in the shared bucket
        with mock.patch.dict(os.environ, {**GOOD, "STORAGE_BACKEND": "mongo_s3", "S3_PREFIX": ""}, clear=True):
            storage.check_config()

    def test_missing_main_bucket(self):
        self.assertRefuses("S3_BUCKET", S3_BUCKET=None)

    def test_every_problem_is_reported_at_once(self):
        env = {"STORAGE_BACKEND": "s3"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                storage.check_config()
        for variable in ("S3_BUCKET", "S3_RESTRICTED_BUCKET", "S3_DOCUMENTS_BUCKET", "S3_PREFIX"):
            self.assertIn(variable, str(caught.exception))

    def test_s3store_has_no_silent_fallback(self):
        for restricted, documents in ((None, "docs"), ("restricted", None), ("", "docs"), ("shared", "docs")):
            with self.assertRaises(RuntimeError):
                storage.S3Store("shared", restricted, "ap-south-1", None, documents_bucket=documents)

    def test_local_backend_needs_no_s3_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            storage.check_config()

    def hr_login(self, **changes):
        users = {
            "priya": {"hash": hr_login.hash_password("a-long-enough-password"), "totp": hr_login.generate_totp_secret()},
            "rahul": {"hash": hr_login.hash_password("another-long-pass"), "totp": hr_login.generate_totp_secret()},
        }
        env = {**GOOD, "HR_USERS": json.dumps(users), "SESSION_SECRET": "s" * 40, **changes}
        env = {name: value for name, value in env.items() if value is not None}

        with mock.patch.dict(os.environ, env, clear=True):
            storage.check_config(require_hr_login=True)
        return env

    def refused_login(self, text, **changes):
        with self.assertRaises(RuntimeError) as caught:
            self.hr_login(**changes)
        message = str(caught.exception)
        self.assertIn(text, message)
        self.assertIn("refusing to start", message)
        return message

    def test_hr_portal_starts_with_several_users(self):
        self.hr_login()

    def test_hr_portal_refuses_the_old_single_user_pair_because_it_has_no_authenticator(self):
        message = self.refused_login(
            "without an authenticator secret", HR_USERS=None, HR_USER="solo",
            HR_PASSWORD_HASH=hr_login.hash_password("a-long-enough-password"))
        self.assertNotIn("solo", message)

    def test_hr_portal_refuses_a_user_without_a_second_factor_without_naming_anyone(self):
        good = hr_login.hash_password("a-long-enough-password")
        users = {"priya": {"hash": good, "totp": hr_login.generate_totp_secret()}, "rahul": good, "sam": {"hash": good}}
        message = self.refused_login("2 user(s) without an authenticator secret", HR_USERS=json.dumps(users))
        for name in ("priya", "rahul", "sam"):
            self.assertNotIn(name, message)

    def test_hr_portal_refuses_a_bad_authenticator_secret_without_printing_it(self):
        good = hr_login.hash_password("a-long-enough-password")
        for secret in ("short", "lower0case1not2base3", "A" * 70, 5):
            message = self.refused_login("authenticator secret", HR_USERS=json.dumps({"priya": {"hash": good, "totp": secret}}))
            self.assertNotIn(str(secret) + '"', message)

    def test_hr_portal_refuses_without_any_user(self):
        for changes in ({"HR_USERS": None}, {"HR_USERS": "  "}):
            self.refused_login("HR_USERS is not set", **changes)

    def test_hr_portal_refuses_a_map_that_is_not_usable_without_printing_it(self):
        good_hash = hr_login.hash_password("a-long-enough-password")
        bad = {
            "not json": "{nope",
            "empty map": "{}",
            "a list": "[1]",
            "a plain password": json.dumps({"priya": "my-plain-password-1"}),
            "an unknown field": json.dumps({"priya": {"hash": good_hash, "totp": "A" * 32, "note": "x"}}),
            "a bad name": json.dumps({"priya rao": good_hash}),
            "an e-mail as name": json.dumps({"p@example.com": good_hash}),
        }
        for label, value in bad.items():
            with self.subTest(label):
                message = self.refused_login("HR_USERS", HR_USERS=value)
                self.assertNotIn("my-plain-password-1", message)
                self.assertNotIn("priya rao", message)
                self.assertNotIn("p@example.com", message)

    def test_hr_portal_refuses_a_plain_text_password(self):
        message = self.refused_login("HR_PASSWORD is set", HR_PASSWORD="a-long-enough-password")
        self.assertNotIn("a-long-enough-password", message)

    def test_hr_portal_refuses_a_missing_or_short_session_secret(self):
        for secret in (None, "", "short"):
            message = self.refused_login("SESSION_SECRET", SESSION_SECRET=secret)
            self.assertNotIn("short\n", message + "\n")

    def test_every_login_problem_is_reported_at_once(self):
        with mock.patch.dict(os.environ, GOOD, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                storage.check_config(require_hr_login=True)
        for name in ("HR_USERS", "SESSION_SECRET"):
            self.assertIn(name, str(caught.exception))

    def test_hr_login_is_only_required_when_asked_and_with_a_real_backend(self):
        # The onboarding portal has no HR login, and local development needs none
        with mock.patch.dict(os.environ, GOOD, clear=True):
            storage.check_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            storage.check_config(require_hr_login=True)


if __name__ == "__main__":
    unittest.main()
