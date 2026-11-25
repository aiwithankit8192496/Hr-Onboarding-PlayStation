"""
Where onboarding data lives. STORAGE_BACKEND picks one:

local (default)
    submissions/<id>.json, forms/<id>.json, uploads/<id>/<file>, hr_tables/<table>.csv

dynamodb_s3   (the AWS deployment: AWS Lambda, DynamoDB and S3)
    DynamoDB  DDB_APPLICATIONS_TABLE  one item per employee (key employee_id): the whole record, so its
                                      status, flags and document_folder are top-level attributes
              DDB_FORMS_TABLE         the form as submitted (key employee_id)
              DDB_COUNTERS_TABLE      the atomic employee-ID counter (key name), UpdateItem ADD
              A new ID is claimed when the application is SUBMITTED (a Draft item made with a conditional
              put, attribute_not_exists), so nothing is ever overwritten and an abandoned form uses no ID.
    S3        the same documents and HR tables as below. The browser uploads each file with a pre-signed
              POST (one exact key, 5 minutes, size and type limited) to <S3_PREFIX>pending/<random id>/<name>;
              on submit the files are checked, the ID is claimed and each file is copied to
              <S3_PREFIX>employees/<ID>/documents/. A lifecycle rule expires pending/ after a day. HR reads
              documents through a pre-signed GET that lasts 5 minutes.
    Settings  SSM_PARAMETER_PREFIX + SSM_PARAMETER_NAMES: secrets (GEMINI_API_KEY; HR_USERS,
              SESSION_SECRET) read once from SSM Parameter Store SecureStrings at start-up.

mongo_s3   (DEPRECATED: the earlier AWS design; kept only so scripts/migrate_mongo_to_dynamodb.py can
            read it. New deployments use dynamodb_s3.)
    MongoDB   database MONGODB_DB, collection applications: one document per student (_id = employee
              ID) holding the whole record; collection forms: the form as submitted; collection
              counters: the atomic employee-ID counter, safe across any number of containers.
    S3        <S3_PREFIX>employees/<id>/documents/<file>   every upload, in that student's own folder
              <SHARED_TABLE_PREFIX><table>/<id>.parquet    the HR tables, one small file per employee
                                                           per table (same schema as samples/hr_tables);
                                                           the bank and sensitive tables always use hr/<table>/

s3         (no MongoDB: the records are JSON files in the student's S3 folder instead)
    <S3_PREFIX>employees/<id>/submission.json, form.json, documents/<file>, plus the same tables.

S3 settings: S3_BUCKET (the shared bucket: only the employees and employee_education tables);
S3_DOCUMENTS_BUCKET (the uploaded ID documents) and S3_RESTRICTED_BUCKET (the bank and sensitive
tables), both required, each its own private bucket, different from S3_BUCKET and from each other:
check_config() refuses to start otherwise; S3_PREFIX (e.g. "onboarding/"; required with
STORAGE_BACKEND=s3, so student folders never land inside the shared table folders);
SHARED_TABLE_PREFIX (folder in front of the employees / employee_education table folders, default
"" = top level of the bucket, as the live Athena tables expect; use "hr/" for the old layout);
AWS_REGION; S3_KMS_KEY_ID (SSE-KMS with that key, AES256 otherwise). MongoDB settings: MONGODB_URI,
MONGODB_DB. AWS credentials come from the usual chain (on AWS: the ECS task role).
"""

import csv
import io
import json
import logging
import os
import re
import threading
import time
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # the project folder (.env and the local data folders live here)

log = logging.getLogger("storage")


def load_env_file(path):
    """Put KEY=VALUE lines of a .env file into os.environ (real environment variables win)."""

    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ==========================================================
# 1. LOCAL FOLDERS
# ==========================================================

class LocalStore:

    def __init__(self):
        self.submissions = BASE_DIR / "submissions"
        self.forms = BASE_DIR / "forms"
        self.uploads = BASE_DIR / "uploads"
        self.tables = BASE_DIR / "hr_tables"
        for folder in (self.submissions, self.forms, self.uploads, self.tables):
            folder.mkdir(exist_ok=True)

    def list_ids(self):
        return [path.stem for path in self.submissions.glob("*.json")]

    def list_records(self):
        return [record for record in map(lambda i: self.get_json("submission", i), self.list_ids()) if record]

    def folder(self, employee_id):
        return f"uploads/{employee_id}/"

    def claim_id(self, make_id, numbers):
        # One server process and no await between this and the save, so no two get the same number
        return make_id(max(numbers(self.list_ids()), default=0) + 1)

    def get_json(self, kind, employee_id):
        folder = self.submissions if kind == "submission" else self.forms
        try:
            return json.loads((folder / f"{employee_id}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put_json(self, kind, employee_id, data):
        folder = self.submissions if kind == "submission" else self.forms
        indent = 4 if kind == "submission" else None
        (folder / f"{employee_id}.json").write_text(
            json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8"
        )

    def has_form(self, employee_id):
        return (self.forms / f"{employee_id}.json").is_file()

    def put_document(self, employee_id, filename, content):
        folder = self.uploads / employee_id
        folder.mkdir(exist_ok=True)
        (folder / filename).write_bytes(content)

    def get_document(self, employee_id, filename):
        try:
            return (self.uploads / employee_id / filename).read_bytes()
        except OSError:
            return None

    def list_documents(self, employee_id):
        folder = self.uploads / employee_id
        return [path.name for path in folder.iterdir()] if folder.is_dir() else []

    def delete_document(self, employee_id, filename):
        (self.uploads / employee_id / filename).unlink(missing_ok=True)

    # hr_tables/<table>.csv: all employees in one file, like samples/hr_tables
    def read_rows(self, table, columns, employee_id):
        path = self.tables / f"{table}.csv"
        if not path.is_file():
            return []
        with open(path, newline="", encoding="utf-8") as file:
            return [row for row in csv.DictReader(file) if row["employee_id"] == employee_id]

    def write_rows(self, table, columns, employee_id, rows):
        path = self.tables / f"{table}.csv"
        kept = []
        if path.is_file():
            with open(path, newline="", encoding="utf-8") as file:
                kept = [row for row in csv.DictReader(file) if row["employee_id"] != employee_id]

        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for row in kept + rows:
                writer.writerow({
                    column: "" if row.get(column) is None else row[column] for column in columns
                })


# ==========================================================
# 2. AMAZON S3
# ==========================================================

UPLOAD_URL_SECONDS = 300    # a pre-signed upload works for 5 minutes
DOWNLOAD_URL_SECONDS = 300  # so does a pre-signed download

# put_object argument -> the header a browser POST must carry for it
ENCRYPTION_HEADERS = {"ServerSideEncryption": "x-amz-server-side-encryption", "SSEKMSKeyId": "x-amz-server-side-encryption-aws-kms-key-id"}

# Document file name -> the only content type it may be uploaded as
DOCUMENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}\.(jpg|pdf)")

# One application's uploads wait together under pending/<upload id>/ (a random uuid, 32 hex digits)
UPLOAD_ID = re.compile(r"[0-9a-f]{32}")


def new_upload_id():
    import uuid

    return uuid.uuid4().hex


def document_content_type(filename):
    """image/jpeg or application/pdf for a well-formed document name, None for anything else."""

    match = DOCUMENT_NAME.fullmatch(filename or "")
    return None if match is None else {"jpg": "image/jpeg", "pdf": "application/pdf"}[match.group(1)]


class S3Store:

    def __init__(self, bucket, restricted_bucket, region, kms_key_id, prefix="", documents_bucket=None,
                 table_prefix=""):
        import boto3

        self.bucket = bucket
        # No fallback to `bucket`: the bank and sensitive tables and the ID documents must never
        # land in the shared bucket because a setting was forgotten
        problems = bucket_problems(bucket, documents_bucket, restricted_bucket)
        if problems:
            raise RuntimeError(config_error(problems))

        self.restricted_bucket = restricted_bucket
        self.documents_bucket = documents_bucket
        self.prefix = prefix
        self.table_prefix = normalize_prefix(table_prefix)
        # Regional endpoint and virtual-hosted addressing: a pre-signed link is then
        # https://<bucket>.s3.<region>.amazonaws.com/..., the one address the pages' Content-Security-Policy
        # and the bucket's CORS rule name (the global s3.amazonaws.com address redirects and breaks both)
        from botocore.config import Config

        self.client = boto3.client(
            "s3", region_name=region or None,
            endpoint_url=f"https://s3.{region}.amazonaws.com" if region else None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        self.encryption = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}
            if kms_key_id else {"ServerSideEncryption": "AES256"}
        )

    def _put(self, key, body, content_type, bucket=None, **extra):
        self.client.put_object(
            Bucket=bucket or self.bucket, Key=key, Body=body, ContentType=content_type,
            **self.encryption, **extra,
        )

    def _get(self, key, bucket=None):
        try:
            return self.client.get_object(Bucket=bucket or self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None

    def _list(self, prefix, bucket=None, **options):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket or self.bucket, Prefix=prefix, **options):
            yield page

    # Each student's own folder: <prefix>employees/<employee_id>/
    def folder(self, employee_id):
        return f"s3://{self.documents_bucket}/{self.prefix}employees/{employee_id}/"

    def _key(self, employee_id, name):
        return f"{self.prefix}employees/{employee_id}/{name}"

    def list_ids(self):
        base = f"{self.prefix}employees/"
        return [
            item["Prefix"][len(base):].strip("/")
            for page in self._list(base, Delimiter="/")
            for item in page.get("CommonPrefixes", [])
        ]

    def list_records(self):
        return [record for record in map(lambda i: self.get_json("submission", i), self.list_ids()) if record]

    def claim_id(self, make_id, numbers):
        """Reserve the next ID with a create-only marker, so two servers never share one."""

        number = max(numbers(self.list_ids()), default=0) + 1

        while True:
            employee_id = make_id(number)
            try:
                self._put(self._key(employee_id, ".claimed"), b"", "text/plain", IfNoneMatch="*")
                return employee_id
            except self.client.exceptions.ClientError as error:
                if error.response["Error"]["Code"] not in ("PreconditionFailed", "ConditionalRequestConflict"):
                    raise
                number += 1

    def _json_name(self, kind):
        return "submission.json" if kind == "submission" else "form.json"

    def get_json(self, kind, employee_id):
        raw = self._get(self._key(employee_id, self._json_name(kind)))
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    def put_json(self, kind, employee_id, data):
        self._put(
            self._key(employee_id, self._json_name(kind)),
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

    def has_form(self, employee_id):
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(employee_id, "form.json"))
            return True
        except self.client.exceptions.ClientError:
            return False

    def put_document(self, employee_id, filename, content):
        content_type = "application/pdf" if filename.endswith(".pdf") else "image/jpeg"
        self._put(
            self._key(employee_id, f"documents/{filename}"), content, content_type,
            bucket=self.documents_bucket,
        )

    def get_document(self, employee_id, filename):
        return self._get(self._key(employee_id, f"documents/{filename}"), self.documents_bucket)

    def list_documents(self, employee_id):
        prefix = self._key(employee_id, "documents/")
        return [
            item["Key"][len(prefix):]
            for page in self._list(prefix, self.documents_bucket) for item in page.get("Contents", [])
        ]

    def delete_document(self, employee_id, filename):
        self.client.delete_object(
            Bucket=self.documents_bucket, Key=self._key(employee_id, f"documents/{filename}")
        )

    # Direct browser uploads and HR downloads: the file bytes never pass through the server
    direct_upload = False

    def pending_key(self, upload_id, filename):
        """Where a file waits before it has an owner: <prefix>pending/<upload id>/<filename>."""

        if not UPLOAD_ID.fullmatch(upload_id or ""):
            raise ValueError("Invalid upload id.")
        return f"{self.prefix}pending/{upload_id}/{filename}"

    def presign_upload(self, upload_id, filename, max_bytes):
        """Pre-signed POST for exactly one key (<prefix>pending/<upload id>/<filename>).

        The file waits under pending/ because no employee ID exists yet: the ID is claimed only when
        the application is submitted, and the submit copies the file into the employee's own folder
        (promote_upload). Valid for UPLOAD_URL_SECONDS; the file must be 1..max_bytes, of the one content
        type the file name allows, and be encrypted the way the buckets are (SSE-S3 unless
        S3_KMS_KEY_ID is set). A lifecycle rule expires pending/ after a day.
        Returns {"url": ..., "fields": {...}}; the browser sends the fields, then the file last.
        """

        content_type = document_content_type(filename)
        if content_type is None:
            raise ValueError("Unsupported document name.")

        fields = {"Content-Type": content_type}
        conditions = [{"Content-Type": content_type}]

        for argument, value in self.encryption.items():
            header = ENCRYPTION_HEADERS[argument]
            fields[header] = value
            conditions.append({header: value})

        conditions.append(["content-length-range", 1, max_bytes])

        return self.client.generate_presigned_post(
            Bucket=self.documents_bucket,
            Key=self.pending_key(upload_id, filename),
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=UPLOAD_URL_SECONDS,
        )

    def promote_upload(self, upload_id, employee_id, filename):
        """Copy a checked pending file into the employee's own folder. No delete: the pending copy
        is removed by the lifecycle rule."""

        self.client.copy_object(
            Bucket=self.documents_bucket,
            Key=self._key(employee_id, f"documents/{filename}"),
            CopySource={"Bucket": self.documents_bucket, "Key": self.pending_key(upload_id, filename)},
            MetadataDirective="COPY",
            **self.encryption,
        )

    def check_upload(self, upload_id, filename, signature, max_bytes):
        """None when the pending file exists with a valid size, type and first bytes, else a reason:
        "missing", "empty", "too_large" or "wrong_type". Reads the object's header and 8 bytes of it."""

        key = self.pending_key(upload_id, filename)

        try:
            head = self.client.head_object(Bucket=self.documents_bucket, Key=key)
        except self.client.exceptions.ClientError:
            return "missing"

        if head["ContentLength"] <= 0:
            return "empty"
        if head["ContentLength"] > max_bytes:
            return "too_large"
        if head.get("ContentType") != document_content_type(filename):
            return "wrong_type"

        try:
            first = self.client.get_object(
                Bucket=self.documents_bucket, Key=key, Range=f"bytes=0-{len(signature) - 1}"
            )["Body"].read()
        except self.client.exceptions.ClientError:
            return "missing"

        return None if first.startswith(signature) else "wrong_type"

    def presign_download(self, employee_id, filename):
        """Pre-signed GET for one document, valid for DOWNLOAD_URL_SECONDS. Call it only for a logged-in HR user."""

        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.documents_bucket,
                "Key": self._key(employee_id, f"documents/{filename}"),
                "ResponseContentType": document_content_type(filename) or "application/octet-stream",
                "ResponseContentDisposition": "inline",
            },
            ExpiresIn=DOWNLOAD_URL_SECONDS,
        )

    # <table>/<employee_id>.parquet: Athena reads every file under the table's prefix
    def _table_bucket(self, table):
        return self.restricted_bucket if table in RESTRICTED_TABLES else self.bucket

    def _table_key(self, table, employee_id):
        return table_key(table, employee_id, self.table_prefix)

    def read_rows(self, table, columns, employee_id):
        import pyarrow.parquet as pq

        raw = self._get(self._table_key(table, employee_id), self._table_bucket(table))
        return pq.read_table(io.BytesIO(raw)).to_pylist() if raw else []

    def write_rows(self, table, columns, employee_id, rows):
        import pyarrow as pa
        import pyarrow.parquet as pq

        types = {"string": pa.string(), "date": pa.date32(), "int": pa.int32()}
        schema = pa.schema([(name, types[kind]) for name, kind in TABLE_SCHEMAS[table]])
        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), buffer, compression="snappy")
        self._put(
            self._table_key(table, employee_id), buffer.getvalue(), "application/octet-stream",
            bucket=self._table_bucket(table),
        )


# ==========================================================
# 2b. MONGODB (records) + S3 (documents and tables)
# ==========================================================

DOT = "．"  # MongoDB field names should not hold ".": the form has a key with one


def encode_keys(value):
    """Copy of `value` with "." in every dict key swapped for a look-alike (for MongoDB)."""

    if isinstance(value, dict):
        return {key.replace(".", DOT): encode_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_keys(item) for item in value]
    return value


def decode_keys(value):

    if isinstance(value, dict):
        return {key.replace(DOT, "."): decode_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_keys(item) for item in value]
    return value


class MongoS3Store(S3Store):
    """DEPRECATED: use DynamoS3Store (STORAGE_BACKEND=dynamodb_s3). Kept so
    scripts/migrate_mongo_to_dynamodb.py can read the old MongoDB records.

    Records and forms in MongoDB; documents and HR tables in S3 (inherited)."""

    COLLECTIONS = {"submission": "applications", "form": "forms"}

    def __init__(self, mongo_uri, database, *s3_arguments, **s3_options):
        from pymongo import MongoClient

        super().__init__(*s3_arguments, **s3_options)
        # One client per process: pymongo pools connections and is safe across threads
        self.mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000, tz_aware=False)
        self.db = self.mongo[database]
        self.db["applications"].create_index("application_status")

    def list_ids(self):
        return [document["_id"] for document in self.db["applications"].find({}, {"_id": 1})]

    def list_records(self):
        return [self._record(document) for document in self.db["applications"].find({})]

    @staticmethod
    def _record(document):
        document.pop("_id", None)
        return decode_keys(document)

    def claim_id(self, make_id, numbers):
        """Next ID from an atomic counter, so containers running side by side never share one."""

        counters = self.db["counters"]
        # Never below the highest ID already stored (first run, or records moved in by sync_tables.py)
        counters.update_one({"_id": "employee_id"}, {"$max": {"seq": max(numbers(self.list_ids()), default=0)}}, upsert=True)
        seq = counters.find_one_and_update(
            {"_id": "employee_id"}, {"$inc": {"seq": 1}}, return_document=True
        )["seq"]
        return make_id(seq)

    def get_json(self, kind, employee_id):
        document = self.db[self.COLLECTIONS[kind]].find_one({"_id": employee_id})
        return self._record(document) if document else None

    def put_json(self, kind, employee_id, data):
        self.db[self.COLLECTIONS[kind]].replace_one(
            {"_id": employee_id}, {"_id": employee_id, **encode_keys(data)}, upsert=True
        )

    def has_form(self, employee_id):
        return self.db["forms"].count_documents({"_id": employee_id}, limit=1) > 0


# ==========================================================
# 2c. DYNAMODB (records) + S3 (documents and tables)
# ==========================================================

DRAFT = "Draft"            # an ID that was claimed and is waiting for its documents and form
DRAFT_LIFETIME_DAYS = 7    # DynamoDB's TTL (attribute expires_at) removes an abandoned Draft after this


class RecordNotFound(RuntimeError):
    """A record was saved for an employee ID that was never claimed."""


def to_item(value):
    """JSON-style data -> DynamoDB attribute values (numbers must be Decimal, never float)."""

    from boto3.dynamodb.types import TypeSerializer

    return TypeSerializer().serialize(json.loads(json.dumps(value), parse_float=Decimal))


def from_item(attribute):
    """A DynamoDB attribute value -> the plain data it was made from."""

    from boto3.dynamodb.types import TypeDeserializer

    def plain(value):
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (list, set)):
            return [plain(item) for item in value]
        return value

    return plain(TypeDeserializer().deserialize(attribute))


class DynamoS3Store(S3Store):
    """Records and forms in DynamoDB; documents and HR tables in S3 (inherited).

    Uses the low-level client, which is safe across threads (a boto3 "resource" is not).
    """

    direct_upload = True

    def __init__(self, tables, *s3_arguments, **s3_options):
        import boto3

        super().__init__(*s3_arguments, **s3_options)
        self.tables = {"submission": tables["applications"], "form": tables["forms"], "counter": tables["counters"]}
        self.ddb = boto3.client("dynamodb", region_name=s3_arguments[2] or None)

    def _failed_condition(self, error):
        return error.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def _get_item(self, table, employee_id, **options):
        response = self.ddb.get_item(
            TableName=self.tables[table], Key={"employee_id": {"S": employee_id}}, ConsistentRead=True, **options
        )
        return from_item({"M": response["Item"]}) if "Item" in response else None

    def _scan(self, **options):
        for page in self.ddb.get_paginator("scan").paginate(
            TableName=self.tables["submission"], ConsistentRead=True, **options
        ):
            for item in page["Items"]:
                yield from_item({"M": item})

    def list_ids(self):
        return [
            item["employee_id"]
            for item in self._scan(ProjectionExpression="employee_id, application_status")
            if item.get("application_status") != DRAFT
        ]

    def list_records(self):
        return [item for item in self._scan() if item.get("application_status") != DRAFT]

    def claim_id(self, make_id, numbers):
        """Claim the next ID, called when an application is submitted (not when files are uploaded).

        The counter's UpdateItem ADD is atomic, so two callers never get the same number. The claim is
        a Draft item made with a conditional put (attribute_not_exists): if that ID already has an item
        (records moved in with the migration script, say) the next number is tried, and nothing is
        overwritten. The Draft is hidden from HR and replaced by the real record; if the submit then
        fails, DynamoDB's TTL removes the Draft after DRAFT_LIFETIME_DAYS (the number is not reused).
        """

        while True:
            number = int(self.ddb.update_item(
                TableName=self.tables["counter"],
                Key={"name": {"S": "employee_id"}},
                UpdateExpression="ADD seq :one",
                ExpressionAttributeValues={":one": {"N": "1"}},
                ReturnValues="UPDATED_NEW",
            )["Attributes"]["seq"]["N"])

            employee_id = make_id(number)

            try:
                self.ddb.put_item(
                    TableName=self.tables["submission"],
                    Item={
                        "employee_id": {"S": employee_id},
                        "application_status": {"S": DRAFT},
                        "expires_at": {"N": str(int(time.time()) + DRAFT_LIFETIME_DAYS * 86400)},
                    },
                    ConditionExpression="attribute_not_exists(employee_id)",
                )
                return employee_id
            except self.ddb.exceptions.ConditionalCheckFailedException:
                continue


    def get_json(self, kind, employee_id):
        item = self._get_item(kind, employee_id)

        if kind == "submission" and item and item.get("application_status") == DRAFT:
            return None

        if kind == "form" and item:
            item.pop("employee_id", None)  # the key, not part of the form as submitted

        return item

    def put_json(self, kind, employee_id, data):
        """Save the record (it must have been claimed: replaces its Draft or earlier version) or the form."""

        options = {"ConditionExpression": "attribute_exists(employee_id)"} if kind == "submission" else {}

        try:
            self.ddb.put_item(
                TableName=self.tables[kind], Item=to_item({**data, "employee_id": employee_id})["M"], **options
            )
        except self.ddb.exceptions.ConditionalCheckFailedException:
            raise RecordNotFound("No claimed employee ID for this record.") from None

    def import_json(self, kind, employee_id, data):
        """Add a record or form that already exists elsewhere (scripts/sync_tables.py --from-local).

        A conditional put: an item that is already here is left exactly as it is. Returns True when added.
        """

        try:
            self.ddb.put_item(
                TableName=self.tables[kind], Item=to_item({**data, "employee_id": employee_id})["M"],
                ConditionExpression="attribute_not_exists(employee_id)",
            )
            return True
        except self.ddb.exceptions.ConditionalCheckFailedException:
            return False

    def has_form(self, employee_id):
        response = self.ddb.get_item(
            TableName=self.tables["form"], Key={"employee_id": {"S": employee_id}},
            ProjectionExpression="employee_id", ConsistentRead=True,
        )
        return "Item" in response


# ==========================================================
# 3. TABLE SCHEMAS (match samples/hr_tables/athena_tables.sql)
# ==========================================================

# Kept in separate, restricted storage (see samples/hr_tables/README.md)
RESTRICTED_TABLES = {"employee_bank_details", "employee_sensitive_details"}

# The restricted tables keep their own folder; only the other tables follow SHARED_TABLE_PREFIX
RESTRICTED_TABLE_PREFIX = "hr/"


def normalize_prefix(prefix):
    """"" or a folder ending in one "/", never starting with "/" or holding "//"."""

    prefix = re.sub(r"/+", "/", (prefix or "").strip()).strip("/")
    return f"{prefix}/" if prefix else ""


def table_key(table, employee_id, shared_prefix=""):
    """S3 key of one employee's file in a table: employees/IGTPL001.parquet (by default)."""

    prefix = RESTRICTED_TABLE_PREFIX if table in RESTRICTED_TABLES else normalize_prefix(shared_prefix)
    return f"{prefix}{table}/{employee_id}.parquet"

TABLE_SCHEMAS = {
    "employees": [
        ("employee_id", "string"), ("full_name", "string"), ("work_email", "string"),
        ("work_phone", "string"), ("department", "string"), ("designation", "string"),
        ("manager_id", "string"), ("employment_status", "string"), ("work_location", "string"),
        ("work_type", "string"), ("date_of_birth", "date"), ("gender", "string"),
    ],
    "employee_education": [
        ("education_id", "string"), ("employee_id", "string"), ("highest_qualification", "string"),
        ("specialization", "string"), ("institution", "string"), ("graduation_year", "int"),
        ("certifications", "string"),
    ],
    "employee_bank_details": [
        ("employee_id", "string"), ("bank_name", "string"), ("account_number_placeholder", "string"),
        ("ifsc_placeholder", "string"), ("account_type", "string"), ("verification_status", "string"),
        ("effective_from", "date"),
    ],
    "employee_sensitive_details": [
        ("employee_id", "string"), ("personal_email", "string"), ("personal_phone_placeholder", "string"),
        ("current_address", "string"), ("pan_placeholder", "string"), ("aadhaar_placeholder", "string"),
        ("emergency_contact_name", "string"), ("emergency_contact_phone_placeholder", "string"),
        ("sensitive_data_note", "string"),
    ],
}


# ==========================================================
# 4. CHOOSING THE BACKEND
# ==========================================================

_store = None
_lock = threading.Lock()


def setting(name):
    return os.environ.get(name, "").strip()


def bucket_problems(bucket, documents_bucket, restricted_bucket):
    """Why these three buckets are unsafe (names the variables, never their values)."""

    problems = []

    if not (restricted_bucket or "").strip():
        problems.append(
            "S3_RESTRICTED_BUCKET is not set: the bank and sensitive tables (PAN, Aadhaar, account "
            "numbers) need their own private bucket, or they could be written to the shared bucket."
        )
    elif restricted_bucket.strip() == (bucket or "").strip():
        problems.append(
            "S3_RESTRICTED_BUCKET is the same as S3_BUCKET: the bank and sensitive tables would be "
            "written to the shared bucket, which others can read and query."
        )

    if not (documents_bucket or "").strip():
        problems.append(
            "S3_DOCUMENTS_BUCKET is not set: the uploaded ID documents (Aadhaar, PAN) need their own "
            "private bucket, or they could be written to the shared bucket."
        )
    elif documents_bucket.strip() == (bucket or "").strip():
        problems.append(
            "S3_DOCUMENTS_BUCKET is the same as S3_BUCKET: the uploaded ID documents would be "
            "written to the shared bucket, which others can read and query."
        )

    if (
        (restricted_bucket or "").strip() and (documents_bucket or "").strip()
        and restricted_bucket.strip() == documents_bucket.strip()
    ):
        problems.append(
            "S3_RESTRICTED_BUCKET is the same as S3_DOCUMENTS_BUCKET: the sensitive tables and the "
            "ID documents must be in separate buckets, so one policy mistake cannot expose both."
        )

    return problems


def config_error(problems):
    return "Unsafe storage configuration, refusing to start:\n  - " + "\n  - ".join(problems)


def hr_login_problems():
    """Why the HR sign-in settings are unsafe (names the variables, never a value)."""

    from hr_onboarding import hr_login

    problems = []

    try:
        users = hr_login.load_users({name: setting(name) for name in ("HR_USERS", "HR_USER", "HR_PASSWORD_HASH")})

        if not users:
            problems.append(
                "HR_USERS is not set: without at least one HR user the HR portal would show "
                "every employee's PAN, Aadhaar and bank details to anyone who can reach it."
            )

        without = sum(1 for user in users.values() if not user["totp"])
        if without:
            problems.append(
                f"HR_USERS has {without} user(s) without an authenticator secret (totp): a password alone is "
                "not enough for the HR portal. Enrol them with scripts/make_hr_password_hash.py."
            )
    except ValueError as error:
        problems.append(f"{error}: make it with scripts/make_hr_password_hash.py.")

    if setting("HR_PASSWORD"):
        problems.append(
            "HR_PASSWORD is set: the password must not be kept as plain text. Remove it and use HR_USERS."
        )

    if len(setting("SESSION_SECRET")) < hr_login.MIN_SECRET_LENGTH:
        problems.append(
            f"SESSION_SECRET is missing or shorter than {hr_login.MIN_SECRET_LENGTH} characters: it signs the HR sign-in cookie."
        )

    return problems

# Environment variable -> default table name (the names the setup guide creates)
DDB_TABLE_DEFAULTS = {
    "DDB_APPLICATIONS_TABLE": "hr-onboarding-applications",
    "DDB_FORMS_TABLE": "hr-onboarding-forms",
    "DDB_COUNTERS_TABLE": "hr-onboarding-counters",
}

_ssm_loaded = False


def load_ssm_settings():
    """Read secrets from SSM Parameter Store once, at start-up, into os.environ.

    SSM_PARAMETER_PREFIX (e.g. /hr-onboarding/portal/) and SSM_PARAMETER_NAMES (e.g. GEMINI_API_KEY)
    name the SecureString parameters; each becomes the setting of the same name. A real environment
    variable wins. Cached for the life of the process (a Lambda container), so a warm request makes
    no SSM call. Does nothing when SSM_PARAMETER_PREFIX is unset (local runs).
    """

    global _ssm_loaded

    prefix = setting("SSM_PARAMETER_PREFIX")
    names = [name.strip() for name in setting("SSM_PARAMETER_NAMES").split(",") if name.strip()]

    if _ssm_loaded or not prefix or not names:
        return

    import boto3

    prefix = prefix if prefix.endswith("/") else prefix + "/"
    wanted = [name for name in names if not os.environ.get(name)]

    if wanted:
        response = boto3.client("ssm", region_name=os.environ.get("AWS_REGION") or None).get_parameters(
            Names=[prefix + name for name in wanted], WithDecryption=True
        )

        for parameter in response["Parameters"]:
            os.environ.setdefault(parameter["Name"][len(prefix):], parameter["Value"])

        missing = [name for name in wanted if not os.environ.get(name)]
        if missing:
            # Names only, never values
            raise RuntimeError("SSM parameters not found or not readable: " + ", ".join(missing))

    _ssm_loaded = True


def check_config(require_hr_login=False):
    """Fail loudly, before serving anything, if the S3 settings could expose sensitive data.

    Reads .env like get_store(), connects to nothing, and prints no values. The local backend has
    no S3 settings, so it passes. The HR portal passes require_hr_login=True: with a real backend
    it must not start without a login, because it shows every employee's PAN and Aadhaar.
    """

    load_env_file(BASE_DIR / ".env")
    backend = setting("STORAGE_BACKEND").lower() or "local"

    if backend not in ("s3", "mongo_s3", "dynamodb_s3"):
        return

    # Secrets kept in SSM Parameter Store become settings before anything below looks at them
    load_ssm_settings()

    problems = []

    if not setting("S3_BUCKET"):
        problems.append(f"S3_BUCKET is not set: STORAGE_BACKEND={backend} needs the main bucket.")

    problems += bucket_problems(setting("S3_BUCKET"), setting("S3_DOCUMENTS_BUCKET"), setting("S3_RESTRICTED_BUCKET"))

    if backend == "s3" and not setting("S3_PREFIX"):
        problems.append(
            "S3_PREFIX is empty with STORAGE_BACKEND=s3: the student folders (employees/<id>/) would "
            "land inside the shared Athena table folders. Set it, e.g. onboarding/."
        )

    if backend == "dynamodb_s3":
        if not setting("S3_PREFIX"):
            problems.append(
                "S3_PREFIX is empty with STORAGE_BACKEND=dynamodb_s3: the documents must go under one "
                "folder (onboarding/) so the function's role can be limited to it."
            )

        # The tables have defaults, so only a value that is set but unusable is a problem
        names = [setting(name) or default for name, default in DDB_TABLE_DEFAULTS.items()]
        if len(set(names)) != len(names):
            problems.append("DDB_APPLICATIONS_TABLE, DDB_FORMS_TABLE and DDB_COUNTERS_TABLE must be three different tables.")

        if setting("SSM_PARAMETER_NAMES") and not setting("SSM_PARAMETER_PREFIX"):
            problems.append("SSM_PARAMETER_NAMES is set but SSM_PARAMETER_PREFIX is not.")

    if require_hr_login:
        problems += hr_login_problems()

    if problems:
        raise RuntimeError(config_error(problems))


def get_store():

    global _store

    with _lock:
        if _store is None:
            check_config()
            backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"

            if backend in ("s3", "mongo_s3", "dynamodb_s3"):
                s3_arguments = (
                    setting("S3_BUCKET"),
                    setting("S3_RESTRICTED_BUCKET"),
                    os.environ.get("AWS_REGION"),
                    os.environ.get("S3_KMS_KEY_ID"),
                )
                s3_options = {
                    "prefix": setting("S3_PREFIX"),
                    "documents_bucket": setting("S3_DOCUMENTS_BUCKET"),
                    "table_prefix": os.environ.get("SHARED_TABLE_PREFIX", ""),
                }

                if backend == "s3":
                    _store = S3Store(*s3_arguments, **s3_options)
                elif backend == "dynamodb_s3":
                    tables = {
                        "applications": setting("DDB_APPLICATIONS_TABLE") or DDB_TABLE_DEFAULTS["DDB_APPLICATIONS_TABLE"],
                        "forms": setting("DDB_FORMS_TABLE") or DDB_TABLE_DEFAULTS["DDB_FORMS_TABLE"],
                        "counters": setting("DDB_COUNTERS_TABLE") or DDB_TABLE_DEFAULTS["DDB_COUNTERS_TABLE"],
                    }
                    _store = DynamoS3Store(tables, *s3_arguments, **s3_options)
                else:
                    uri = os.environ.get("MONGODB_URI")
                    if not uri:
                        raise RuntimeError("STORAGE_BACKEND=mongo_s3 needs MONGODB_URI.")
                    _store = MongoS3Store(
                        uri, os.environ.get("MONGODB_DB", "hr_onboarding"), *s3_arguments, **s3_options
                    )
            else:
                _store = LocalStore()

    return _store


def save_record(record):
    """Save the record, then bring the HR tables up to date with it.

    The record is the source of truth: if a table write fails it is logged and not raised, so
    the employee's submission is never lost; sync_tables.py rebuilds the tables from the records.
    """

    from hr_onboarding import hr_tables

    store = get_store()
    store.put_json("submission", record["employee_id"], record)

    try:
        hr_tables.sync_tables(store, record)
    except Exception as e:
        log.error(
            "HR table sync failed for employee_id=%s: %s",
            record["employee_id"],
            type(e).__name__,
        )