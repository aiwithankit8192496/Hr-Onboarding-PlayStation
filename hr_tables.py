"""
Turn an onboarding record into rows of the HR tables in Data/sources_hr
(employees, employee_education, employee_bank_details, employee_sensitive_details) and
keep them in step with the record: called on every save, including HR's decisions and flags.

Not written, because the portal does not collect them: employee_compensation (what the
employee drew before is not their CTC here), employment_details, leave_records, attendance.
Columns the portal does not own (work_email, department, manager_id ...) are left as HR set
them: a resave only overwrites the columns built below.
"""

import datetime
import re

from storage import TABLE_SCHEMAS


def iso_date(text):
    """DD/MM/YYYY (the form's format) -> date, or None."""

    try:
        return datetime.datetime.strptime(str(text).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def employment_status(application_status):
    return {"Approved": "Approved - Joining", "Rejected": "Rejected"}.get(application_status, "Onboarding")


def verification_status(checks):
    statuses = {check.get("status") for check in checks}
    if "mismatch" in statuses:
        return "Mismatch"
    return "Verified" if statuses == {"match"} else "Pending"


def build_rows(record):
    """{table: (rows, replace)}; `replace` means the rows are the employee's whole set."""

    employee_id = record["employee_id"]
    personal = record.get("personal_information", {})
    professional = record.get("professional_details", {})
    emergency = personal.get("emergency_contact", {})
    submitted = (record.get("submitted_at") or "")[:10]

    tables = {
        "employees": ([{
            "employee_id": employee_id,
            "full_name": personal.get("name", ""),
            "designation": record.get("policies", {}).get("designation") or professional.get("last_designation", ""),
            "employment_status": employment_status(record.get("application_status")),
            "date_of_birth": iso_date(personal.get("date_of_birth")),
            "gender": personal.get("gender", ""),
        }], False),
        "employee_sensitive_details": ([{
            "employee_id": employee_id,
            "personal_email": personal.get("email", ""),
            "personal_phone_placeholder": personal.get("mobile", ""),
            "current_address": record.get("address", {}).get("current_address", ""),
            "pan_placeholder": personal.get("pan_number", ""),
            "aadhaar_placeholder": personal.get("aadhaar_number", ""),
            "emergency_contact_name": emergency.get("name", ""),
            "emergency_contact_phone_placeholder": emergency.get("phone", ""),
            "sensitive_data_note": "Collected through the onboarding portal",
        }], False),
        # One row per qualification (the 10th, the degree, each certification ...)
        "employee_education": ([
            {
                "education_id": f"EDU-{employee_id}-{number}",
                "employee_id": employee_id,
                "highest_qualification": entry.get("Qualification", ""),
                "specialization": entry.get("Major Subject", ""),
                "institution": entry.get("University/Institute", ""),
                "graduation_year": re.sub(r"\D", "", str(entry.get("Year of Passing", ""))) or None,
                "certifications": entry.get("Major Subject", "") if entry.get("Qualification") == "Certification" else "",
            }
            for number, entry in enumerate(record.get("education_details", []), start=1)
        ], True),
    }

    bank = record.get("bank_details")
    if bank:
        tables["employee_bank_details"] = ([{
            "employee_id": employee_id,
            "bank_name": bank.get("bank_name", ""),
            "account_number_placeholder": bank.get("account_number", ""),
            "ifsc_placeholder": bank.get("ifsc", ""),
            "verification_status": verification_status(bank.get("verification", [])),
            "effective_from": submitted,
        }], False)

    return tables


def coerce(kind, value):

    if kind == "string":
        return "" if value is None else str(value)

    if value in (None, ""):
        return None

    if kind == "date":
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.date.fromisoformat(str(value))
        except ValueError:
            return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_tables(store, record):
    """Write the record's rows into every table it has data for."""

    for table, (rows, replace) in build_rows(record).items():
        schema = TABLE_SCHEMAS[table]
        columns = [name for name, _ in schema]
        existing = {} if replace or not rows else next(
            iter(store.read_rows(table, columns, record["employee_id"])), {}
        )

        final = [
            {name: coerce(kind, {**existing, **row}.get(name)) for name, kind in schema}
            for row in rows
        ]
        store.write_rows(table, columns, record["employee_id"], final)
