"""
Rebuild the HR tables from the saved records.

    python sync_tables.py                 rebuild the tables of every record in the configured storage
    python sync_tables.py --from-local     first copy submissions/, forms/ and uploads/ from this
                                           machine into the configured storage (e.g. S3), then rebuild

Use --from-local once when moving to AWS (STORAGE_BACKEND=s3, S3_BUCKET=...). Safe to repeat.
"""

import sys

import hr_tables
import storage


def copy_local_into(target):
    source = storage.LocalStore()

    for employee_id in sorted(source.list_ids()):
        for filename in source.list_documents(employee_id):
            target.put_document(employee_id, filename, source.get_document(employee_id, filename))

        form = source.get_json("form", employee_id)
        if form is not None:
            target.put_json("form", employee_id, form)

        target.put_json("submission", employee_id, source.get_json("submission", employee_id))
        print(f"copied {employee_id}")


def main():
    target = storage.get_store()

    if "--from-local" in sys.argv:
        if isinstance(target, storage.LocalStore):
            sys.exit("--from-local needs STORAGE_BACKEND=s3 (the target is the local folders).")
        copy_local_into(target)

    for employee_id in sorted(target.list_ids()):
        record = target.get_json("submission", employee_id)
        if record:
            hr_tables.sync_tables(target, record)
            print(f"tables updated for {employee_id}")


if __name__ == "__main__":
    main()
