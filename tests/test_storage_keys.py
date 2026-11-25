import sys
import unittest
from pathlib import Path
from unittest import mock

from hr_onboarding import storage


class TableKeyTests(unittest.TestCase):

    def test_shared_tables_are_top_level_by_default(self):
        self.assertEqual(storage.table_key("employees", "IGTPL001"), "employees/IGTPL001.parquet")
        self.assertEqual(
            storage.table_key("employee_education", "IGTPL001"), "employee_education/IGTPL001.parquet"
        )

    def test_no_hr_prefix_or_stray_slashes(self):
        for table in ("employees", "employee_education"):
            key = storage.table_key(table, "IGTPL001")
            self.assertFalse(key.startswith(("hr/", "/")))
            self.assertNotIn("//", key)

    def test_prefix_is_configurable_and_normalized(self):
        for prefix in ("hr", "hr/", "/hr/", "//hr//"):
            self.assertEqual(storage.table_key("employees", "IGTPL001", prefix), "hr/employees/IGTPL001.parquet")
        self.assertEqual(storage.table_key("employees", "IGTPL001", "a//b"), "a/b/employees/IGTPL001.parquet")
        self.assertEqual(storage.table_key("employees", "IGTPL001", "  "), "employees/IGTPL001.parquet")

    def test_restricted_tables_ignore_the_shared_prefix(self):
        for table in storage.RESTRICTED_TABLES:
            for prefix in ("", "x/"):
                self.assertEqual(storage.table_key(table, "IGTPL001", prefix), f"hr/{table}/IGTPL001.parquet")


class S3StoreKeyTests(unittest.TestCase):

    def make_store(self, **options):
        with mock.patch.dict(sys.modules, {"boto3": mock.MagicMock()}):
            store = storage.S3Store("shared", "restricted", "ap-south-1", None, documents_bucket="docs", **options)
        store.client = mock.MagicMock()
        return store

    def test_write_rows_keys_and_buckets(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow is not installed")

        store = self.make_store()
        store.write_rows("employees", [], "IGTPL001", [{"employee_id": "IGTPL001"}])
        store.write_rows("employee_education", [], "IGTPL001", [])
        store.write_rows("employee_bank_details", [], "IGTPL001", [{"employee_id": "IGTPL001"}])

        calls = {(c.kwargs["Bucket"], c.kwargs["Key"]) for c in store.client.put_object.call_args_list}
        self.assertEqual(calls, {
            ("shared", "employees/IGTPL001.parquet"),
            ("shared", "employee_education/IGTPL001.parquet"),
            ("restricted", "hr/employee_bank_details/IGTPL001.parquet"),
        })

    def test_table_prefix_option(self):
        self.assertEqual(self.make_store(table_prefix="hr")._table_key("employees", "IGTPL001"),
                         "hr/employees/IGTPL001.parquet")


if __name__ == "__main__":
    unittest.main()
