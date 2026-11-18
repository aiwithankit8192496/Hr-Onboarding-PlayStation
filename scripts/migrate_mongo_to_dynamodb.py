"""
One-time copy of the old MongoDB records into the DynamoDB tables (DEPRECATED design -> dynamodb_s3).

Copies the collections `applications` and `forms`, and sets the employee-ID counter to the highest
IGTPL number found, so the next new application continues from there. Documents and HR tables are
already in S3 and are not touched.

It is a DRY RUN unless --apply is given: it reads MongoDB and DynamoDB, writes nothing, and reports
what it would do. It is safe to run again: an item that already exists in DynamoDB is never
overwritten (conditional put), and the counter only ever moves up.

    pip install pymongo                        # only this script needs it
    set MONGODB_URI=...                        # the old connection string
    python scripts/migrate_mongo_to_dynamodb.py            # dry run
    python scripts/migrate_mongo_to_dynamodb.py --apply    # really copy

Settings (environment): MONGODB_URI, MONGODB_DB (default hr_onboarding), AWS_REGION (default ap-south-1),
DDB_APPLICATIONS_TABLE, DDB_FORMS_TABLE, DDB_COUNTERS_TABLE (defaults are the names in the setup guide).
Only employee IDs are printed, never a value from a record.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage  # noqa: E402

ID_NUMBER = re.compile(r"IGTPL(\d+)")


def table_names():
    return {
        key: os.environ.get(variable) or default
        for key, (variable, default) in {
            "applications": ("DDB_APPLICATIONS_TABLE", storage.DDB_TABLE_DEFAULTS["DDB_APPLICATIONS_TABLE"]),
            "forms": ("DDB_FORMS_TABLE", storage.DDB_TABLE_DEFAULTS["DDB_FORMS_TABLE"]),
            "counters": ("DDB_COUNTERS_TABLE", storage.DDB_TABLE_DEFAULTS["DDB_COUNTERS_TABLE"]),
        }.items()
    }


def copy_collection(ddb, collection, table, apply, label):
    """Copy every document; returns (created, already_there, highest IGTPL number)."""

    created = already = 0
    highest = 0

    for document in collection.find({}):
        employee_id = document["_id"]
        match = ID_NUMBER.fullmatch(employee_id)
        highest = max(highest, int(match.group(1)) if match else 0)

        data = storage.decode_keys({key: value for key, value in document.items() if key != "_id"})
        item = storage.to_item({**data, "employee_id": employee_id})["M"]

        if not apply:
            exists = "Item" in ddb.get_item(TableName=table, Key={"employee_id": {"S": employee_id}})
            created, already = (created, already + 1) if exists else (created + 1, already)
            print(f"  {label} {employee_id}: {'already in DynamoDB' if exists else 'would be copied'}")
            continue

        try:
            ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(employee_id)")
            created += 1
            print(f"  {label} {employee_id}: copied")
        except ddb.exceptions.ConditionalCheckFailedException:
            already += 1
            print(f"  {label} {employee_id}: already in DynamoDB, left as it is")

    return created, already, highest


def set_counter(ddb, table, number, apply):
    """Raise the counter to `number`; never lowers it."""

    current = ddb.get_item(TableName=table, Key={"name": {"S": "employee_id"}}).get("Item", {})
    current = int(current.get("seq", {}).get("N", 0))

    if current >= number:
        print(f"  counter: stays at {current} (highest migrated ID is {number})")
        return

    if not apply:
        print(f"  counter: would be set to {number} (now {current})")
        return

    try:
        ddb.update_item(
            TableName=table, Key={"name": {"S": "employee_id"}},
            UpdateExpression="SET seq = :n", ConditionExpression="attribute_not_exists(seq) OR seq < :n",
            ExpressionAttributeValues={":n": {"N": str(number)}},
        )
        print(f"  counter: set to {number}")
    except ddb.exceptions.ConditionalCheckFailedException:
        print("  counter: someone raised it meanwhile, left as it is")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="really write to DynamoDB (default: dry run)")
    args = parser.parse_args()

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI is not set.")

    import boto3
    from pymongo import MongoClient

    tables = table_names()
    ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION") or "ap-south-1")
    mongo = MongoClient(uri, serverSelectionTimeoutMS=10000)[os.environ.get("MONGODB_DB", "hr_onboarding")]

    print("APPLYING" if args.apply else "DRY RUN (nothing is written; use --apply to copy)")

    print("applications:")
    made, already, highest = copy_collection(ddb, mongo["applications"], tables["applications"], args.apply, "application")
    print(f"  {made} {'copied' if args.apply else 'to copy'}, {already} already there")

    print("forms:")
    made, already, _ = copy_collection(ddb, mongo["forms"], tables["forms"], args.apply, "form")
    print(f"  {made} {'copied' if args.apply else 'to copy'}, {already} already there")

    print("counter:")
    set_counter(ddb, tables["counters"], highest, args.apply)


if __name__ == "__main__":
    main()
