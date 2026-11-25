"""Tests for the DynamoDB + S3 backend (STORAGE_BACKEND=dynamodb_s3), against moto's fake AWS.

Nothing here reaches real AWS: moto replaces boto3's network layer, and the credentials are fake.
"""

import base64
import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


import boto3
from moto import mock_aws

from hr_onboarding import hr_login
from hr_onboarding import jsonlog
from hr_onboarding import storage

REGION = "ap-south-1"
SHARED, DOCS, RESTRICTED = "igtl-hr-analytics-ayush-2026", "igtpl-hr-onboarding-docs-2026", "igtpl-hr-onboarding-restricted-2026"
TABLES = {"applications": "hr-onboarding-applications", "forms": "hr-onboarding-forms", "counters": "hr-onboarding-counters"}

# Windows needs these for TLS to initialise even though moto never opens a connection
SYSTEM = {name: value for name, value in os.environ.items() if name.upper() in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")}

ENV = {
    **SYSTEM,
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION,
    "STORAGE_BACKEND": "dynamodb_s3", "S3_BUCKET": SHARED, "S3_DOCUMENTS_BUCKET": DOCS,
    "S3_RESTRICTED_BUCKET": RESTRICTED, "S3_PREFIX": "onboarding/", "AWS_REGION": REGION,
}

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
PDF = b"%PDF-1.4 " + b"0" * 100
UPLOAD_ID = "0123456789abcdef0123456789abcdef"

HR_PASSWORD = "a-long-enough-password"
HR_SECRET = "s" * 40
OTHER_PASSWORD = "another-long-password"
TOTP = {"priya": hr_login.generate_totp_secret(), "rahul": hr_login.generate_totp_secret()}
HR_USERS = {
    "priya": {"hash": hr_login.hash_password(HR_PASSWORD), "totp": TOTP["priya"]},
    "rahul": {"hash": hr_login.hash_password(OTHER_PASSWORD), "totp": TOTP["rahul"]},
}


def make_id(number):
    return f"IGTPL{number:03d}"


class AwsTestCase(unittest.TestCase):

    def setUp(self):
        for patcher in (mock_aws(), mock.patch.dict(os.environ, ENV, clear=True), mock.patch.object(storage, "load_env_file")):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(setattr, storage, "_store", None)
        storage._store = None

        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (SHARED, DOCS, RESTRICTED):
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})

        ddb = boto3.client("dynamodb", region_name=REGION)
        for name, key in ((TABLES["applications"], "employee_id"), (TABLES["forms"], "employee_id"), (TABLES["counters"], "name")):
            ddb.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
            )

        self.s3, self.ddb = s3, ddb
        self.store = storage.get_store()

    def counter_item(self):
        return self.ddb.get_item(TableName=TABLES["counters"], Key={"name": {"S": "employee_id"}}).get("Item")

    def docs_keys(self):
        return [item["Key"] for item in self.s3.list_objects_v2(Bucket=DOCS).get("Contents", [])]


class StoreTests(AwsTestCase):

    def test_backend_is_chosen_by_the_setting(self):
        self.assertIsInstance(self.store, storage.DynamoS3Store)
        self.assertTrue(self.store.direct_upload)

    def test_ids_count_up_from_the_counter(self):
        ids = [self.store.claim_id(make_id, None) for _ in range(3)]
        self.assertEqual(ids, ["IGTPL001", "IGTPL002", "IGTPL003"])

    def test_two_concurrent_calls_never_get_the_same_id(self):
        results, barrier = [], threading.Barrier(8)

        def claim():
            barrier.wait()  # all eight start together
            for _ in range(5):
                results.append(self.store.claim_id(make_id, None))

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 40)
        self.assertEqual(len(set(results)), 40)
        self.assertEqual(sorted(results)[0], "IGTPL001")
        self.assertEqual(sorted(results)[-1], "IGTPL040")

    def test_counter_uses_update_item_add_with_updated_new(self):
        calls = []
        real = self.store.ddb.update_item

        def spy(**arguments):
            calls.append(arguments)
            return real(**arguments)

        with mock.patch.object(self.store.ddb, "update_item", spy):
            self.store.claim_id(make_id, None)

        self.assertEqual(calls[0]["UpdateExpression"], "ADD seq :one")
        self.assertEqual(calls[0]["ReturnValues"], "UPDATED_NEW")
        self.assertEqual(calls[0]["TableName"], TABLES["counters"])

    def test_a_new_record_is_a_conditional_put_that_never_overwrites(self):
        # IGTPL001 already exists (as after a migration) while the counter is still at 0
        self.ddb.put_item(
            TableName=TABLES["applications"],
            Item={"employee_id": {"S": "IGTPL001"}, "application_status": {"S": "Approved"}, "marker": {"S": "keep me"}},
        )

        employee_id = self.store.claim_id(make_id, None)

        self.assertEqual(employee_id, "IGTPL002")  # skipped the taken one
        kept = self.ddb.get_item(TableName=TABLES["applications"], Key={"employee_id": {"S": "IGTPL001"}})["Item"]
        self.assertEqual(kept["marker"]["S"], "keep me")

    def test_a_record_cannot_be_saved_for_an_id_nobody_claimed(self):
        with self.assertRaises(storage.RecordNotFound):
            self.store.put_json("submission", "IGTPL099", {"application_status": "New"})

        self.assertNotIn(
            "Item", self.ddb.get_item(TableName=TABLES["applications"], Key={"employee_id": {"S": "IGTPL099"}})
        )

    def test_a_claimed_id_is_hidden_until_the_record_is_saved(self):
        employee_id = self.store.claim_id(make_id, None)

        self.assertIsNone(self.store.get_json("submission", employee_id))
        self.assertEqual(self.store.list_records(), [])
        self.assertEqual(self.store.list_ids(), [])

        self.store.put_json("submission", employee_id, {"application_status": "New", "flags": []})

        self.assertEqual(self.store.get_json("submission", employee_id)["application_status"], "New")
        self.assertEqual(self.store.list_ids(), [employee_id])

    def test_the_claim_expires_by_ttl_attribute_but_a_saved_record_never_does(self):
        employee_id = self.store.claim_id(make_id, None)
        item = self.ddb.get_item(TableName=TABLES["applications"], Key={"employee_id": {"S": employee_id}})["Item"]
        self.assertIn("expires_at", item)

        self.store.put_json("submission", employee_id, {"application_status": "New"})
        item = self.ddb.get_item(TableName=TABLES["applications"], Key={"employee_id": {"S": employee_id}})["Item"]
        self.assertNotIn("expires_at", item)

    def test_record_round_trip_keeps_numbers_text_and_dotted_keys(self):
        employee_id = self.store.claim_id(make_id, None)
        record = {
            "employee_id": employee_id, "application_status": "New", "document_folder": "s3://x/y/",
            "flags": [{"status": "open", "n": 2}],
            "professional_details": {"total_experience": 3, "ratio": 1.5, "empty": ""},
            "family_details": [{"Name": "A", "Age/Gender": "30/M"}],
            "form.dotted": "kept",
        }
        self.store.put_json("submission", employee_id, record)
        self.assertEqual(self.store.get_json("submission", employee_id), record)

    def test_import_is_create_only(self):
        # sync_tables.py --from-local: a record that is already there is kept, never overwritten
        self.assertTrue(self.store.import_json("submission", "IGTPL050", {"application_status": "New", "n": 1}))
        self.assertFalse(self.store.import_json("submission", "IGTPL050", {"application_status": "Rejected", "n": 2}))
        self.assertEqual(self.store.get_json("submission", "IGTPL050")["n"], 1)

    def test_form_round_trip_and_has_form(self):
        employee_id = self.store.claim_id(make_id, None)
        self.assertFalse(self.store.has_form(employee_id))
        form = {"name": "A", "family": [{"Name": "B"}]}
        self.store.put_json("form", employee_id, form)
        self.assertTrue(self.store.has_form(employee_id))
        self.assertEqual(self.store.get_json("form", employee_id), form)


class UploadTests(AwsTestCase):

    def policy(self, post):
        return json.loads(base64.b64decode(post["fields"]["policy"]))

    def test_presigned_post_conditions(self):
        post = self.store.presign_upload(UPLOAD_ID, "aadhaar_front.jpg", 5 * 1024 * 1024)
        policy = self.policy(post)
        conditions = policy["conditions"]

        self.assertIn({"bucket": DOCS}, conditions)
        # pending/<uuid>/: no employee ID exists yet
        self.assertIn({"key": f"onboarding/pending/{UPLOAD_ID}/aadhaar_front.jpg"}, conditions)
        self.assertIn({"Content-Type": "image/jpeg"}, conditions)
        self.assertIn({"x-amz-server-side-encryption": "AES256"}, conditions)  # SSE-S3, matching the bucket default
        self.assertIn(["content-length-range", 1, 5 * 1024 * 1024], conditions)
        self.assertNotIn("x-amz-server-side-encryption-aws-kms-key-id", json.dumps(conditions))
        # one exact key: no starts-with on the key
        self.assertFalse([c for c in conditions if isinstance(c, list) and c[0] == "starts-with" and c[1] == "$key"])

        self.assertEqual(post["fields"]["x-amz-server-side-encryption"], "AES256")
        self.assertEqual(post["fields"]["Content-Type"], "image/jpeg")
        # the regional, virtual-hosted address: the one the Content-Security-Policy and the CORS rule name
        self.assertEqual(post["url"], f"https://{DOCS}.s3.{REGION}.amazonaws.com/")

    def test_presigned_post_expires_in_five_minutes(self):
        import datetime

        post = self.store.presign_upload(UPLOAD_ID, "resume.pdf", 1000)
        expires = datetime.datetime.strptime(self.policy(post)["expiration"], "%Y-%m-%dT%H:%M:%SZ")
        seconds = (expires - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).total_seconds()
        self.assertTrue(240 < seconds <= 300, seconds)

    def test_pdf_gets_the_pdf_content_type(self):
        post = self.store.presign_upload(UPLOAD_ID, "payslip_1.pdf", 1000)
        self.assertIn({"Content-Type": "application/pdf"}, self.policy(post)["conditions"])

    def test_kms_setting_enforces_kms_instead(self):
        with mock.patch.dict(os.environ, {"S3_KMS_KEY_ID": "alias/hr-onboarding"}):
            storage._store = None
            conditions = self.policy(storage.get_store().presign_upload(UPLOAD_ID, "pan.jpg", 1000))["conditions"]
        self.assertIn({"x-amz-server-side-encryption": "aws:kms"}, conditions)

    def test_only_known_document_names_are_signed(self):
        for name in ("../x.jpg", "a.png", "a.exe", "A.jpg", "", "x.jpg/../../y.jpg", "a b.pdf"):
            with self.assertRaises(ValueError, msg=name):
                self.store.presign_upload(UPLOAD_ID, name, 1000)

    def test_only_a_uuid_is_accepted_as_an_upload_id(self):
        for bad in ("", "../x", "IGTPL001", UPLOAD_ID + "0", UPLOAD_ID.upper(), None):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.store.presign_upload(bad, "pan.jpg", 1000)
        self.assertTrue(storage.UPLOAD_ID.fullmatch(storage.new_upload_id()))

    def test_document_content_types(self):
        self.assertEqual(storage.document_content_type("pan.jpg"), "image/jpeg")
        self.assertEqual(storage.document_content_type("resume.pdf"), "application/pdf")
        self.assertIsNone(storage.document_content_type("pan.png"))
        self.assertIsNone(storage.document_content_type(None))

    def put(self, name, body, content_type):
        self.s3.put_object(
            Bucket=DOCS, Key=f"onboarding/pending/{UPLOAD_ID}/{name}", Body=body,
            ContentType=content_type, ServerSideEncryption="AES256",
        )

    def check(self, name, signature, limit=1000):
        return self.store.check_upload(UPLOAD_ID, name, signature, limit)

    def test_check_upload_accepts_a_good_file(self):
        self.put("pan.jpg", JPEG, "image/jpeg")
        self.assertIsNone(self.check("pan.jpg", b"\xff\xd8\xff"))

    def test_check_upload_rejects_a_missing_file(self):
        self.assertEqual(self.check("pan.jpg", b"\xff\xd8\xff"), "missing")

    def test_check_upload_rejects_an_empty_file(self):
        self.put("pan.jpg", b"", "image/jpeg")
        self.assertEqual(self.check("pan.jpg", b"\xff\xd8\xff"), "empty")

    def test_check_upload_rejects_a_file_over_the_limit(self):
        self.put("pan.jpg", JPEG, "image/jpeg")
        self.assertEqual(self.check("pan.jpg", b"\xff\xd8\xff", limit=50), "too_large")

    def test_check_upload_rejects_the_wrong_content_type(self):
        self.put("pan.jpg", JPEG, "application/pdf")
        self.assertEqual(self.check("pan.jpg", b"\xff\xd8\xff"), "wrong_type")

    def test_check_upload_rejects_a_renamed_file(self):
        self.put("pan.jpg", PDF, "image/jpeg")  # a PDF uploaded under a .jpg name
        self.assertEqual(self.check("pan.jpg", b"\xff\xd8\xff"), "wrong_type")

    def test_promote_copies_into_the_employee_folder_and_deletes_nothing(self):
        self.put("pan.jpg", JPEG, "image/jpeg")
        self.store.promote_upload(UPLOAD_ID, "IGTPL007", "pan.jpg")

        copied = self.s3.get_object(Bucket=DOCS, Key="onboarding/employees/IGTPL007/documents/pan.jpg")
        self.assertEqual(copied["Body"].read(), JPEG)
        self.assertEqual(copied["ContentType"], "image/jpeg")
        self.assertEqual(copied["ServerSideEncryption"], "AES256")
        # the pending copy is left for the lifecycle rule: nothing is deleted by the app
        self.assertIn(f"onboarding/pending/{UPLOAD_ID}/pan.jpg", self.docs_keys())

    def test_presigned_download_lasts_five_minutes_and_names_one_key(self):
        url = self.store.presign_download("IGTPL001", "pan.jpg")
        self.assertIn("X-Amz-Expires=300", url)
        self.assertIn("onboarding/employees/IGTPL001/documents/pan.jpg", url)
        self.assertTrue(url.startswith(f"https://{DOCS}.s3.{REGION}.amazonaws.com/"), url)

    def test_documents_never_land_in_the_shared_or_restricted_bucket(self):
        self.put("pan.jpg", JPEG, "image/jpeg")
        for bucket in (SHARED, RESTRICTED):
            self.assertEqual(self.s3.list_objects_v2(Bucket=bucket).get("KeyCount"), 0)


class ConfigTests(AwsTestCase):

    def refuses(self, text, **changes):
        env = {**ENV, **changes}
        env = {name: value for name, value in env.items() if value is not None}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                storage.check_config()
        message = str(caught.exception)
        self.assertIn(text, message)
        self.assertIn("refusing to start", message)
        for bucket in (SHARED, DOCS, RESTRICTED):
            self.assertNotIn(bucket, message)  # why it is refused is explained; no bucket name is printed

    def test_good_config_starts(self):
        storage.check_config()

    def test_needs_a_prefix(self):
        self.refuses("S3_PREFIX", S3_PREFIX="")

    def test_restricted_bucket_must_not_be_the_shared_bucket(self):
        self.refuses("S3_RESTRICTED_BUCKET", S3_RESTRICTED_BUCKET=SHARED)

    def test_documents_bucket_must_not_be_the_shared_bucket(self):
        self.refuses("S3_DOCUMENTS_BUCKET", S3_DOCUMENTS_BUCKET=SHARED)

    def test_the_three_tables_must_differ(self):
        self.refuses("three different tables", DDB_APPLICATIONS_TABLE="same", DDB_FORMS_TABLE="same")

    def test_ssm_names_need_a_prefix(self):
        self.refuses("SSM_PARAMETER_PREFIX", SSM_PARAMETER_NAMES="GEMINI_API_KEY")

    def test_hr_portal_still_needs_a_login(self):
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                storage.check_config(require_hr_login=True)
        self.assertIn("HR_USERS", str(caught.exception))

    def test_table_names_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"DDB_APPLICATIONS_TABLE": "mine-a", "DDB_FORMS_TABLE": "mine-f", "DDB_COUNTERS_TABLE": "mine-c"}):
            for name in ("mine-a", "mine-f", "mine-c"):
                self.ddb.create_table(
                    TableName=name, BillingMode="PAY_PER_REQUEST",
                    KeySchema=[{"AttributeName": "name" if name == "mine-c" else "employee_id", "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": "name" if name == "mine-c" else "employee_id", "AttributeType": "S"}],
                )
            storage._store = None
            store = storage.get_store()
        self.assertEqual(store.tables, {"submission": "mine-a", "form": "mine-f", "counter": "mine-c"})

    def test_secrets_are_read_from_ssm_once_and_cached(self):
        ssm = boto3.client("ssm", region_name=REGION)
        ssm.put_parameter(Name="/hr-onboarding/portal/GEMINI_API_KEY", Value="sekret-value-123", Type="SecureString")

        env = {**ENV, "SSM_PARAMETER_PREFIX": "/hr-onboarding/portal", "SSM_PARAMETER_NAMES": "GEMINI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(storage, "_ssm_loaded", False):
            storage.check_config()
            self.assertEqual(os.environ["GEMINI_API_KEY"], "sekret-value-123")

            with mock.patch("boto3.client") as client:  # a warm start makes no second call
                storage.load_ssm_settings()
            client.assert_not_called()

    def test_a_missing_ssm_parameter_is_named_without_values(self):
        env = {**ENV, "SSM_PARAMETER_PREFIX": "/hr-onboarding/portal/", "SSM_PARAMETER_NAMES": "GEMINI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(storage, "_ssm_loaded", False):
            with self.assertRaises(RuntimeError) as caught:
                storage.check_config()
        self.assertIn("GEMINI_API_KEY", str(caught.exception))


class LogTests(unittest.TestCase):

    def format(self, **extra):
        try:
            raise ValueError("Aadhaar 123412341234 for Asha Rao")
        except ValueError:
            record = logging.getLogger("t").makeRecord("t", logging.ERROR, __file__, 1, "Extraction failed", (), sys.exc_info(), extra=extra)
        return json.loads(jsonlog.JsonFormatter().format(record))

    def test_has_levelname_and_employee_id(self):
        entry = self.format(employee_id="IGTPL007")
        self.assertEqual(entry["levelname"], "ERROR")
        self.assertEqual(entry["employee_id"], "IGTPL007")

    def test_an_exception_is_logged_as_type_and_place_never_its_text(self):
        line = json.dumps(self.format())
        self.assertIn("ValueError", line)
        self.assertNotIn("Aadhaar", line)
        self.assertNotIn("Asha", line)
        self.assertNotIn("123412341234", line)

    def test_debug_is_off_unless_asked(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            jsonlog.setup()
            self.assertEqual(logging.getLogger().level, logging.INFO)
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            jsonlog.setup()
            self.assertEqual(logging.getLogger().level, logging.DEBUG)
        jsonlog.setup()


class PortalTests(AwsTestCase):
    """The upload and submit endpoints, end to end, against the fake AWS."""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient

        sys.modules.pop("hr_onboarding.employee_portal", None)
        self.portal = importlib.import_module("hr_onboarding.employee_portal")
        self.addCleanup(sys.modules.pop, "hr_onboarding.employee_portal", None)
        self.client = TestClient(self.portal.app)

        # The policies step has its own tests elsewhere; here it is answered and signed
        for patcher in (
            mock.patch.object(self.portal.policies, "validate_policies", return_value=None),
            mock.patch.object(self.portal.policies, "build_policy_record", return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def keys(self):
        return ["aadhaar_front", "aadhaar_back", "pan", "photo", "education_certificate"]

    def form_data(self):
        data = {key: "x" for key in self.portal.REQUIRED_FIELDS.values()}
        data.update(
            dob="01/01/1995", age=str(self.portal.calculate_age("01/01/1995")), pan_number="ABCDE1234F",
            aadhaar_number="123412341234",
            mobile="9876543210", emergency_phone="9876543210", ref1_contact="9876543210",
            ref2_contact="9876543210", total_experience="3", email="a@b.co", languages="English",
            period_from="01/01/2020", period_to="Present", gender="Male", declaration=True,
            family=[{"Name": "A", "Age/Gender": "30/M", "Relation": "Father", "Occupation": "x"}],
            education=[{"Qualification": "10th", "University/Institute": "X", "Year of Passing": "2010",
                        "% Marks": "80", "Major Subject": "Science"}],
            passport_number="",
            reason_for_leaving="",
            policies={"designation": "Engineer", "signature": "A B", "statutory_answers": {}},
        )
        return data

    def upload_urls(self, keys=None, **extra):
        reply = self.client.post("/api/upload-urls", json={"keys": keys or self.keys(), **extra})
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    def upload(self, session, keys=None):
        for key in keys or self.keys():
            name = session["uploads"][key]["filename"]
            self.s3.put_object(
                Bucket=DOCS, Key=f"onboarding/pending/{session['upload_id']}/{name}",
                Body=PDF if name.endswith(".pdf") else JPEG,
                ContentType="application/pdf" if name.endswith(".pdf") else "image/jpeg", ServerSideEncryption="AES256",
            )

    def submit(self, session, uploaded=None, **extra):
        fields = {
            "data": json.dumps(self.form_data()), "direct": "1",
            "uploaded": json.dumps(uploaded or self.keys()), "upload_id": session["upload_id"], **extra,
        }
        return self.client.post("/api/submit", data=fields)

    # ---- upload-urls: no ID, files wait under pending/<uuid>/

    def test_upload_urls_claims_no_id_and_signs_one_exact_pending_key_per_file(self):
        body = self.upload_urls()

        self.assertNotIn("employee_id", body)
        self.assertTrue(storage.UPLOAD_ID.fullmatch(body["upload_id"]))
        self.assertEqual(set(body["uploads"]), set(self.keys()))
        policy = json.loads(base64.b64decode(body["uploads"]["pan"]["fields"]["policy"]))
        self.assertIn({"key": f"onboarding/pending/{body['upload_id']}/pan.jpg"}, policy["conditions"])
        self.assertIsNone(self.counter_item())  # no number was used
        self.assertEqual(self.store.list_ids(), [])

    def test_an_abandoned_upload_claims_no_id(self):
        session = self.upload_urls()
        self.upload(session)  # the files arrive, the employee never submits

        self.assertIsNone(self.counter_item())
        self.assertEqual(self.store.list_ids(), [])
        # the next real application is still IGTPL001
        second = self.upload_urls()
        self.upload(second)
        self.assertEqual(self.submit(second).json()["employee_id"], "IGTPL001")

    def test_asking_again_with_the_same_upload_id_keeps_the_same_place(self):
        first = self.upload_urls(["pan"])
        again = self.upload_urls(["pan"], upload_id=first["upload_id"])
        self.assertEqual(again["upload_id"], first["upload_id"])

    def test_upload_urls_refuses_bad_ids_and_names(self):
        for bad in ("../x", "IGTPL001", "ABC", 5):
            self.assertEqual(self.client.post("/api/upload-urls", json={"keys": ["pan"], "upload_id": bad}).status_code, 400, bad)
        for keys in (["../x"], [], ["pan", "pan"], "pan"):
            self.assertEqual(self.client.post("/api/upload-urls", json={"keys": keys}).status_code, 400, keys)

    # ---- submit: validate, claim, copy, save

    def test_submit_accepts_a_complete_direct_application(self):
        session = self.upload_urls()
        self.upload(session)

        reply = self.submit(session)

        self.assertEqual(reply.status_code, 200, reply.text)
        record = reply.json()
        self.assertEqual(record["employee_id"], "IGTPL001")
        self.assertEqual(record["documents"]["pan"], "pan.jpg")
        self.assertEqual(self.store.get_json("submission", "IGTPL001")["application_status"], "New")
        self.assertEqual(self.store.list_ids(), ["IGTPL001"])

        # every file was copied into the employee's own folder
        keys = self.docs_keys()
        for name in ("aadhaar_front.jpg", "aadhaar_back.jpg", "pan.jpg", "photo.jpg", "education_certificate.pdf"):
            self.assertIn(f"onboarding/employees/IGTPL001/documents/{name}", keys)

        # The HR tables follow the record: the shared bucket gets only its own two tables
        shared = [item["Key"] for item in self.s3.list_objects_v2(Bucket=SHARED).get("Contents", [])]
        self.assertIn("employees/IGTPL001.parquet", shared)
        self.assertFalse([key for key in shared if "bank" in key or "sensitive" in key])
        restricted = [item["Key"] for item in self.s3.list_objects_v2(Bucket=RESTRICTED).get("Contents", [])]
        self.assertIn("hr/employee_sensitive_details/IGTPL001.parquet", restricted)

    def test_a_retry_after_a_missing_file_works_and_claimed_no_id_in_between(self):
        session = self.upload_urls()
        self.upload(session, keys=self.keys()[:-1])  # the certificate never arrives

        reply = self.submit(session)

        self.assertEqual(reply.status_code, 400)
        self.assertIn("was not received", reply.json()["error"])
        self.assertIsNone(self.counter_item())  # validation failed before any ID was claimed
        self.assertEqual(self.store.list_ids(), [])
        self.assertEqual(self.docs_keys().count(f"onboarding/employees/IGTPL001/documents/pan.jpg"), 0)

        # the page asks for fresh links with the same upload id, sends the missing file, submits again
        again = self.upload_urls(upload_id=session["upload_id"])
        self.upload(again, keys=["education_certificate"])
        retry = self.submit(again)

        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["employee_id"], "IGTPL001")  # the first ID ever claimed

    def test_a_form_error_also_claims_no_id(self):
        session = self.upload_urls()
        self.upload(session)
        data = self.form_data()
        data["pan_number"] = "bad"

        reply = self.client.post("/api/submit", data={
            "data": json.dumps(data), "direct": "1", "uploaded": json.dumps(self.keys()), "upload_id": session["upload_id"],
        })

        self.assertEqual(reply.status_code, 400)
        self.assertIsNone(self.counter_item())

    def test_a_failed_copy_shows_nothing_to_hr_and_a_retry_works(self):
        session = self.upload_urls()
        self.upload(session)

        with mock.patch.object(self.store, "promote_upload", side_effect=RuntimeError("boom")):
            failed = self.submit(session)

        self.assertEqual(failed.status_code, 500)
        self.assertIn("press Submit again", failed.json()["error"])
        self.assertEqual(self.store.list_ids(), [])           # HR sees nothing
        self.assertEqual(self.store.list_records(), [])

        retry = self.submit(session)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(self.store.list_ids(), [retry.json()["employee_id"]])  # the failed attempt's number is simply skipped

    def test_submit_refuses_a_malformed_upload_id(self):
        session = self.upload_urls()
        self.upload(session)
        reply = self.client.post("/api/submit", data={
            "data": json.dumps(self.form_data()), "direct": "1", "uploaded": json.dumps(self.keys()), "upload_id": "../../x",
        })
        self.assertEqual(reply.status_code, 400)
        self.assertEqual(self.store.list_ids(), [])

    def test_a_file_swapped_for_the_wrong_type_is_refused(self):
        session = self.upload_urls()
        self.upload(session)
        self.s3.put_object(  # a PDF uploaded under the Aadhaar .jpg name
            Bucket=DOCS, Key=f"onboarding/pending/{session['upload_id']}/aadhaar_front.jpg",
            Body=PDF, ContentType="image/jpeg", ServerSideEncryption="AES256",
        )
        reply = self.submit(session)
        self.assertEqual(reply.status_code, 400)
        self.assertIn("JPG/JPEG", reply.json()["error"])
        self.assertIsNone(self.counter_item())

    def test_two_submissions_get_two_ids(self):
        ids = []
        for _ in range(2):
            session = self.upload_urls()
            self.upload(session)
            ids.append(self.submit(session).json()["employee_id"])
        self.assertEqual(ids, ["IGTPL001", "IGTPL002"])

    def test_the_page_script_knows_direct_upload_is_on(self):
        # the compiled script is a file of its own; the deployment's value is put in when it is served
        self.assertIn("const DIRECT_UPLOAD = true;", self.client.get("/assets/index.app.js").text)


class HrPortalTests(AwsTestCase):
    """HR sign-in, and downloads: a signed-in HR user is sent to a 5-minute link; nobody else gets anything."""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient

        login = {"HR_USERS": json.dumps(HR_USERS), "SESSION_SECRET": HR_SECRET}
        patcher = mock.patch.dict(os.environ, login)
        patcher.start()
        self.addCleanup(patcher.stop)

        sys.modules.pop("hr_onboarding.hr_portal", None)
        self.hr = importlib.import_module("hr_onboarding.hr_portal")
        self.addCleanup(sys.modules.pop, "hr_onboarding.hr_portal", None)
        # https, so the Secure cookie is kept by the test client like a browser would
        self.client = TestClient(self.hr.app, base_url="https://testserver", follow_redirects=False)

        employee_id = self.store.claim_id(make_id, None)
        self.store.put_json("submission", employee_id, {
            "application_status": "New", "personal_information": {"name": "A"}, "access_token": "tok"})
        self.store.put_json("form", employee_id, {"name": "A"})  # a flag needs the form, so the employee can reopen it
        self.s3.put_object(
            Bucket=DOCS, Key=f"onboarding/employees/{employee_id}/documents/pan.jpg", Body=JPEG,
            ContentType="image/jpeg", ServerSideEncryption="AES256",
        )

    def next_code(self, user):
        """A valid one-time code for this user that has not been used in this test (a code works once):
        this step, then the next, then the previous (all inside the accepted window)."""

        used = self.__dict__.setdefault("code_offsets", {})
        offset = (0, 1, -1)[used.get(user, 0) % 3]
        used[user] = used.get(user, 0) + 1
        return hr_login.totp_code(TOTP[user], int(time.time() // hr_login.TOTP_PERIOD) + offset)

    def sign_in(self, password=HR_PASSWORD, user="priya", code="auto"):
        if code == "auto":
            code = self.next_code(user) if user in TOTP else ""
        return self.client.post("/login", json={"user": user, "password": password, "code": code})

    def test_needs_the_login(self):
        self.assertEqual(self.client.get("/api/employees/IGTPL001/documents/pan").status_code, 401)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_a_page_request_without_a_login_goes_to_the_form(self):
        reply = self.client.get("/")
        self.assertEqual(reply.status_code, 302)
        self.assertEqual(reply.headers["location"], "/login")

    def test_health_and_the_form_are_open(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="password"', page.text)
        self.assertNotIn("IGTPL", page.text)

    def test_a_good_sign_in_sets_one_strict_cookie(self):
        reply = self.sign_in()
        self.assertEqual(reply.status_code, 200)
        cookie = reply.headers["set-cookie"]
        for part in ("HttpOnly", "Secure", "SameSite=Strict", "Max-Age=28800", "Path=/"):
            self.assertIn(part, cookie)
        self.assertTrue(cookie.startswith("hr_session="))
        self.assertNotIn(HR_PASSWORD, cookie)

    def test_the_cookie_opens_the_portal(self):
        self.sign_in()
        self.assertEqual(self.client.get("/api/employees").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_wrong_password_and_wrong_user_are_refused_with_the_same_message(self):
        wrong_password, wrong_user = self.sign_in(password="not-the-one-123"), self.sign_in(user="nobody")
        self.assertEqual((wrong_password.status_code, wrong_user.status_code), (401, 401))
        self.assertEqual(wrong_password.json(), wrong_user.json())
        self.assertNotIn("set-cookie", wrong_password.headers)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_an_expired_cookie_is_refused(self):
        old = hr_login.make_session("priya", HR_SECRET, now=1_000_000, lifetime=hr_login.SESSION_SECONDS)
        self.client.cookies.set("hr_session", old)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_a_cookie_signed_with_another_secret_or_changed_is_refused(self):
        forged = hr_login.make_session("priya", "x" * 40)
        self.client.cookies.set("hr_session", forged)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

        good = hr_login.make_session("priya", HR_SECRET)
        payload, signature = good.split(".")
        self.client.cookies.clear()
        self.client.cookies.set("hr_session", payload + "." + signature[:-2] + "AA")
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_sign_out_clears_the_cookie(self):
        self.sign_in()
        reply = self.client.post("/logout")
        self.assertIn("Max-Age=0", reply.headers["set-cookie"])
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_the_old_basic_header_no_longer_works(self):
        token = base64.b64encode(f"priya:{HR_PASSWORD}".encode()).decode()
        reply = self.client.get("/api/employees", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(reply.status_code, 401)

    def test_signed_in_hr_is_sent_to_a_five_minute_link(self):
        self.sign_in()
        reply = self.client.get("/api/employees/IGTPL001/documents/pan")
        self.assertEqual(reply.status_code, 302)
        self.assertIn("X-Amz-Expires=300", reply.headers["location"])
        self.assertIn("onboarding/employees/IGTPL001/documents/pan.jpg", reply.headers["location"])
        self.assertEqual(reply.headers["cache-control"], "no-store")

    def test_a_document_that_does_not_exist_is_a_404_not_a_link(self):
        self.sign_in()
        self.assertEqual(self.client.get("/api/employees/IGTPL001/documents/resume").status_code, 404)

    # ---- one-time code (TOTP)

    def test_a_wrong_or_missing_code_is_refused_like_a_wrong_password(self):
        wrong_password = self.sign_in(password="wrong-wrong-1234")
        for code in ("000000", "", "12345"):
            reply = self.sign_in(code=code)
            self.assertEqual(reply.status_code, 401, code)
            self.assertEqual(reply.json(), wrong_password.json(), code)  # nothing tells which part was wrong
            self.assertNotIn("set-cookie", reply.headers)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_the_code_of_another_user_does_not_work(self):
        reply = self.sign_in(user="priya", code=hr_login.totp_code(TOTP["rahul"], int(time.time() // 30)))
        self.assertEqual(reply.status_code, 401)

    def test_codes_one_step_either_side_are_accepted_and_two_steps_are_not(self):
        step = int(time.time() // hr_login.TOTP_PERIOD)
        for offset, status in ((-2, 401), (2, 401), (-1, 200)):
            self.client.cookies.clear()
            code = hr_login.totp_code(TOTP["priya"], step + offset)
            self.assertEqual(self.sign_in(code=code).status_code, status, offset)

    def test_a_code_works_only_once(self):
        step = int(time.time() // 30)
        code = hr_login.totp_code(TOTP["priya"], step)
        self.assertEqual(self.sign_in(code=code).status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.sign_in(code=code).status_code, 401)  # the same code again, even with the right password
        item = self.ddb.get_item(TableName=TABLES["counters"], Key={"name": {"S": "login#priya"}})["Item"]
        self.assertEqual(int(item["last_step"]["N"]), step)
        self.assertIn("expires_at", item)  # TTL removes it later

    def test_a_replayed_older_code_is_refused_after_a_newer_one(self):
        step = int(time.time() // 30)
        self.assertEqual(self.sign_in(code=hr_login.totp_code(TOTP["priya"], step + 1)).status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.sign_in(code=hr_login.totp_code(TOTP["priya"], step)).status_code, 401)

    def test_wrong_codes_count_toward_the_lock_out_even_with_the_right_password(self):
        for _ in range(5):
            self.assertEqual(self.sign_in(code="000000").status_code, 401)
        item = self.ddb.get_item(TableName=TABLES["counters"], Key={"name": {"S": "login#priya"}})["Item"]
        self.assertEqual(item["attempts"]["N"], "5")
        self.assertEqual(self.sign_in().status_code, 429)  # now even the right password and a right code wait

    def test_a_good_code_with_a_wrong_password_is_refused(self):
        self.assertEqual(self.sign_in(password="wrong-wrong-1234").status_code, 401)

    def test_the_second_factor_is_never_logged(self):
        code = self.next_code("rahul")
        lines = self.logged(lambda: self.sign_in(user="rahul", password=OTHER_PASSWORD, code=code))
        text = json.dumps(lines)
        self.assertNotIn(code, text)
        self.assertNotIn(TOTP["rahul"], text)
        self.assertEqual([line for line in lines if line["message"] == "HR sign-in"][0]["hr_user"], "rahul")

    def test_the_login_form_has_a_code_box_and_sends_it(self):
        self.assertIn('id="code"', self.client.get("/login").text)
        self.assertIn("code: document.getElementById", self.client.get("/assets/login.script1.js").text)

    def test_the_counter_still_touches_only_login_keys_with_the_code_step(self):
        calls = []
        real = self.store.ddb.update_item

        def spy(**kw):
            calls.append(kw["Key"]["name"]["S"])
            return real(**kw)

        with mock.patch.object(self.store.ddb, "update_item", spy):
            self.sign_in()
        self.assertTrue(calls and all(key == "login#priya" for key in calls), calls)

    def test_if_the_step_cannot_be_recorded_the_sign_in_fails_closed(self):
        with mock.patch.object(hr_login.DynamoThrottle, "use_step", side_effect=RuntimeError("down")):
            reply = self.sign_in()
        self.assertEqual(reply.status_code, 503)
        self.assertNotIn("set-cookie", reply.headers)

    # ---- several HR users

    def test_each_user_signs_in_with_their_own_password_and_gets_their_own_cookie(self):
        self.assertEqual(self.sign_in(user="priya", password=HR_PASSWORD).status_code, 200)
        self.assertEqual(self.sign_in(user="rahul", password=OTHER_PASSWORD).status_code, 200)

    def test_one_users_password_does_not_open_another_user(self):
        self.assertEqual(self.sign_in(user="rahul", password=HR_PASSWORD).status_code, 401)
        self.assertEqual(self.sign_in(user="priya", password=OTHER_PASSWORD).status_code, 401)

    def test_a_cookie_names_its_user_and_cannot_be_changed_to_another(self):
        self.sign_in(user="rahul", password=OTHER_PASSWORD)
        cookie = self.client.cookies.get("hr_session")
        self.assertEqual(hr_login.read_session(cookie, HR_SECRET), "rahul")
        payload, signature = hr_login.make_session("priya", HR_SECRET).split(".")
        forged = payload + "." + cookie.split(".")[1]  # priya's payload, rahul's signature
        self.client.cookies.clear()
        self.client.cookies.set("hr_session", forged)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_a_cookie_of_a_user_removed_from_the_map_stops_working(self):
        cookie = hr_login.make_session("exleaver", HR_SECRET)  # validly signed, but not in HR_USERS
        self.client.cookies.set("hr_session", cookie)
        self.assertEqual(self.client.get("/api/employees").status_code, 401)

    def test_the_old_single_user_settings_still_work(self):
        users = hr_login.load_users({"HR_USER": "solo", "HR_PASSWORD_HASH": hr_login.hash_password(HR_PASSWORD)})
        self.assertEqual(list(users), ["solo"])
        self.assertIsNone(users["solo"]["totp"])
        self.assertEqual(hr_login.load_users({}), {})

    # ---- hr_user on the log lines

    def logged(self, action):
        """The JSON log lines written while `action` runs."""
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(jsonlog.JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.INFO)
        try:
            action()
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    def test_a_sign_in_logs_the_user_name_and_never_the_password(self):
        lines = self.logged(lambda: self.sign_in(user="rahul", password=OTHER_PASSWORD))
        entry = [line for line in lines if line["message"] == "HR sign-in"][0]
        self.assertEqual(entry["hr_user"], "rahul")
        self.assertNotIn(OTHER_PASSWORD, json.dumps(lines))

    def test_a_failed_sign_in_logs_no_name_at_all(self):
        # someone may type a password or an e-mail address into the name box
        lines = self.logged(lambda: self.sign_in(user="someone@example.com", password="whatever-1234567"))
        text = json.dumps(lines)
        self.assertIn("HR sign-in failed", text)
        self.assertNotIn("someone", text)
        self.assertNotIn("whatever", text)

    def test_view_approve_flag_and_document_open_each_log_the_user_and_the_employee(self):
        self.sign_in(user="rahul", password=OTHER_PASSWORD)

        def actions():
            self.client.get("/api/employees")
            self.client.get("/api/employees/IGTPL001")
            self.client.get("/api/employees/IGTPL001/documents/pan")
            self.client.post("/api/employees/IGTPL001/status", json={"status": "Approved"})
            self.client.post("/api/employees/IGTPL001/flags", json={"flags": [{"key": "name", "label": "Name", "note": "typo"}]})
            self.client.post("/logout")

        lines = self.logged(actions)
        by_message = {line["message"]: line for line in lines if line["logger"] == "hr"}
        for message in ("HR viewed application", "Document opened", "HR set the application to Approved", "HR sign-out"):
            self.assertEqual(by_message[message]["hr_user"], "rahul", message)
        for message in ("HR viewed application", "Document opened", "HR set the application to Approved"):
            self.assertEqual(by_message[message]["employee_id"], "IGTPL001", message)
        self.assertEqual(by_message["HR viewed the list: 1 application(s)"]["hr_user"], "rahul")
        flagged = [line for line in lines if line["message"].startswith("HR flagged")]
        self.assertTrue(flagged, [line["message"] for line in lines])
        self.assertEqual(flagged[0]["hr_user"], "rahul")

    def test_rejecting_is_logged_too(self):
        self.sign_in()
        lines = self.logged(lambda: self.client.post("/api/employees/IGTPL001/status", json={"status": "Rejected"}))
        self.assertIn("HR set the application to Rejected", [line["message"] for line in lines])
        self.assertEqual([line for line in lines if "Rejected" in line["message"]][0]["hr_user"], "priya")

    # ---- the lock-out is kept in DynamoDB

    def test_five_wrong_attempts_are_counted_in_the_counters_table(self):
        for _ in range(5):
            self.assertEqual(self.sign_in(password="wrong-wrong-1234").status_code, 401)
        item = self.ddb.get_item(TableName=TABLES["counters"], Key={"name": {"S": "login#priya"}})["Item"]
        self.assertEqual(item["attempts"]["N"], "5")
        import time
        self.assertTrue(250 < int(item["expires_at"]["N"]) - time.time() <= 300)  # TTL attribute, 5 minutes
        self.assertEqual(self.sign_in().status_code, 429)

    def test_the_lock_holds_in_another_lambda_copy(self):
        for _ in range(5):
            self.sign_in(password="wrong-wrong-1234")

        # a second copy of the function: the module loaded again, a new client, nothing in memory
        from starlette.testclient import TestClient

        sys.modules.pop("hr_onboarding.hr_portal", None)
        second = importlib.import_module("hr_onboarding.hr_portal")
        other = TestClient(second.app, base_url="https://testserver", follow_redirects=False)
        reply = other.post("/login", json={"user": "priya", "password": HR_PASSWORD, "code": self.next_code("priya")})
        self.assertEqual(reply.status_code, 429)

    def test_the_lock_is_per_user_and_unknown_names_share_one_item(self):
        for _ in range(5):
            self.sign_in(user="priya", password="wrong-wrong-1234")
        self.assertEqual(self.sign_in(user="rahul", password=OTHER_PASSWORD).status_code, 200)

        for name in ("a", "b", "c"):
            self.sign_in(user=name, password="x")
        keys = [item["name"]["S"] for item in self.ddb.scan(TableName=TABLES["counters"])["Items"]]
        self.assertIn("login#_unknown", keys)
        self.assertEqual([key for key in keys if key.startswith("login#") and key not in ("login#priya", "login#rahul", "login#_unknown")], [])

    def test_an_expired_window_counts_as_zero_even_before_ttl_deletes_it(self):
        self.ddb.put_item(TableName=TABLES["counters"], Item={
            "name": {"S": "login#priya"}, "attempts": {"N": "9"}, "expires_at": {"N": "1000"}})  # long ago
        self.assertEqual(self.sign_in().status_code, 200)

    def test_a_good_sign_in_resets_the_count_without_deleting_the_item(self):
        for _ in range(3):
            self.sign_in(password="wrong-wrong-1234")
        self.sign_in()
        item = self.ddb.get_item(TableName=TABLES["counters"], Key={"name": {"S": "login#priya"}})["Item"]
        self.assertEqual(item["attempts"]["N"], "0")

    def test_the_counter_uses_only_get_and_update_on_login_keys(self):
        calls = []
        real_update, real_get = self.store.ddb.update_item, self.store.ddb.get_item

        def update(**kw):
            calls.append(("update", kw["Key"]["name"]["S"]))
            return real_update(**kw)

        def get(**kw):
            calls.append(("get", kw["Key"].get("name", {}).get("S")))
            return real_get(**kw)

        with mock.patch.object(self.store.ddb, "update_item", update), mock.patch.object(self.store.ddb, "get_item", get):
            self.sign_in(password="wrong-wrong-1234")
            self.sign_in()
        self.assertTrue(calls)
        self.assertTrue(all(key.startswith("login#") for _, key in calls), calls)

    def test_if_the_counter_is_unreachable_the_sign_in_fails_closed(self):
        with mock.patch.object(self.store.ddb, "get_item", side_effect=RuntimeError("down")):
            reply = self.sign_in()
        self.assertEqual(reply.status_code, 503)
        self.assertNotIn("set-cookie", reply.headers)

    def test_the_list_never_shows_a_claimed_but_unsaved_id(self):
        self.sign_in()
        self.store.claim_id(make_id, None)
        reply = self.client.get("/api/employees")
        self.assertEqual([row["employee_id"] for row in reply.json()], ["IGTPL001"])


class MigrationTests(AwsTestCase):

    class FakeCollection:
        def __init__(self, documents):
            self.documents = documents

        def find(self, query):
            return list(self.documents)

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "migrate", Path(__file__).resolve().parent.parent / "scripts" / "migrate_mongo_to_dynamodb.py"
        )
        self.migrate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migrate)
        self.documents = [
            {"_id": "IGTPL001", "application_status": "Approved", "form．dotted": "x", "n": 1},
            {"_id": "IGTPL007", "application_status": "New", "n": 2},
            {"_id": "EMP-ABCDEF12", "application_status": "New"},
        ]

    def run_migration(self, apply):
        with mock.patch("builtins.print"):
            _, _, highest = self.migrate.copy_collection(
                self.ddb, self.FakeCollection(self.documents), TABLES["applications"], apply, "application"
            )
            self.migrate.set_counter(self.ddb, TABLES["counters"], highest, apply)
        return highest

    def count(self):
        return self.ddb.scan(TableName=TABLES["applications"], Select="COUNT")["Count"]

    def test_a_dry_run_writes_nothing(self):
        self.assertEqual(self.run_migration(apply=False), 7)
        self.assertEqual(self.count(), 0)
        self.assertIsNone(self.counter_item())

    def test_apply_copies_and_sets_the_counter_to_the_highest_id(self):
        self.run_migration(apply=True)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.store.get_json("submission", "IGTPL001")["form.dotted"], "x")
        self.assertEqual(self.store.claim_id(make_id, None), "IGTPL008")  # continues after 007

    def test_running_it_twice_changes_nothing_and_never_overwrites(self):
        self.run_migration(apply=True)
        self.store.put_json("submission", "IGTPL001", {"application_status": "Rejected"})  # HR acted since
        self.run_migration(apply=True)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.store.get_json("submission", "IGTPL001")["application_status"], "Rejected")

    def test_the_counter_is_never_lowered(self):
        for _ in range(10):
            self.store.claim_id(make_id, None)
        self.run_migration(apply=True)
        self.assertEqual(self.store.claim_id(make_id, None), "IGTPL011")


if __name__ == "__main__":
    unittest.main()
