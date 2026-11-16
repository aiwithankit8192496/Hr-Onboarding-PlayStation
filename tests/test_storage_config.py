import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage

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


if __name__ == "__main__":
    unittest.main()
