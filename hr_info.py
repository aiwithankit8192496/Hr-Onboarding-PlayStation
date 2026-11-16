"""
HR Review Portal: lets HR review every onboarding submission from react_onboard.py in
seven steps (identity, address and family, education, experience, bank, references and
policies, decision). In each step the submitted details sit next to a viewer for the
documents they came from (zoom, rotate, full screen), with a "Matches" tick and a flag for
every field and document, and the "Need to reconsider" results beside the items they concern.

Reads and writes each employee's record and documents through storage.py (local folders,
or one S3 folder per employee with STORAGE_BACKEND=s3). Set HR_USER and HR_PASSWORD to
require a login; leave them unset only on a local machine.

Run:
    python hr_info.py
Then open http://localhost:8001
"""

import base64
import datetime
import hmac
import json
import os
import re
import secrets
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import storage
import theme


# ==========================================================
# 1. APPLICATION CONFIGURATION
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent

# Local folders or S3, chosen by STORAGE_BACKEND (see storage.py); .env is read first
storage.load_env_file(BASE_DIR / ".env")
storage.check_config()  # refuse to start on an unsafe S3 setup (see storage.py)

HOST = os.environ.get("HOST", "127.0.0.1")  # 0.0.0.0 inside a container
PORT = int(os.environ.get("PORT", "8001"))

# Document key -> (label shown to HR, file type); keys match react_onboard.DOCUMENTS.
# "aadhaar" is kept for older submissions that uploaded a single Aadhaar image.
DOCUMENTS = {
    "aadhaar_front": ("Aadhaar Card (Front)", "jpg"),
    "aadhaar_back": ("Aadhaar Card (Back)", "jpg"),
    "aadhaar": ("Aadhaar Card", "jpg"),
    "pan": ("PAN Card (Front)", "jpg"),
    "photo": ("Profile Photograph", "jpg"),
    "education_certificate": ("Highest Education Certificate", "pdf"),
    "bank_proof": ("Bank Proof (Cheque / Passbook)", "jpg"),
    **{f"payslip_{n}": (f"Payslip {n}", "pdf") for n in range(1, 7)},
    "relieving_letter": ("Relieving Letter", "pdf"),
    "resume": ("Resume", "pdf"),
}

MEDIA_TYPES = {"jpg": "image/jpeg", "pdf": "application/pdf"}

# New: just submitted, or sent back by the employee after HR flagged something.
# Changes Requested: HR flagged items and is waiting for the employee.
# Pending / Approved / Rejected: HR's decision after reviewing.
STATUSES = ("New", "Changes Requested", "Pending", "Approved", "Rejected")
HR_DECISIONS = ("Pending", "Approved", "Rejected")

# Applications from before these statuses were added were saved as "Pending HR Review"
LEGACY_NEW = "Pending HR Review"

# Where employees open the onboarding form (react_onboard.py); the flagged-application
# link HR shares is built from it. Set ONBOARDING_URL to the public address on AWS.
ONBOARDING_URL = os.environ.get("ONBOARDING_URL", "http://127.0.0.1:8000").rstrip("/")

# New IDs are IGTPL001, IGTPL002, ...; EMP-XXXXXXXX is the older random format
EMPLOYEE_ID_PATTERN = re.compile(r"IGTPL\d{3,}|EMP-[0-9A-F]{8}")

# Keys of the fields and documents HR ticks as matching (name, bank.ifsc, payslip_1, ...)
VERIFIED_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.]{1,60}")


# ==========================================================
# 2. SUBMISSION STORAGE
# ==========================================================

def load_record(employee_id):

    # Only a well-formed ID reaches storage (blocks path tricks)
    if not EMPLOYEE_ID_PATTERN.fullmatch(employee_id or ""):
        return None

    record = storage.get_store().get_json("submission", employee_id)

    if record is None:
        return None

    if record.get("application_status") in (None, LEGACY_NEW):
        record["application_status"] = STATUSES[0]

    return record


# Saves the record and keeps the HR tables in step with it (storage.py)
save_record = storage.save_record


def list_documents(employee_id):

    if not EMPLOYEE_ID_PATTERN.fullmatch(employee_id or ""):
        return []

    stored = set(storage.get_store().list_documents(employee_id))

    return [
        {
            "key": key,
            "label": label,
            "type": file_type,
            "url": f"/api/employees/{employee_id}/documents/{key}",
        }
        for key, (label, file_type) in DOCUMENTS.items()
        if f"{key}.{file_type}" in stored
    ]


def summarize(record):
    """The few fields HR needs in the employee list."""

    personal = record.get("personal_information", {})
    professional = record.get("professional_details", {})

    return {
        "employee_id": record.get("employee_id", ""),
        "name": personal.get("name", ""),
        "email": personal.get("email", ""),
        "last_designation": professional.get("last_designation", ""),
        "application_status": record.get("application_status", STATUSES[0]),
        "submitted_at": record.get("submitted_at", ""),
        # Sent back by the employee after HR flagged something: shown at the top as New
        "resubmitted_at": record.get("resubmitted_at", ""),
        "open_flags": sum(1 for flag in record.get("flags", []) if flag.get("status") == "open"),
    }


# ==========================================================
# 3. API ENDPOINTS
# ==========================================================

def not_found():
    return JSONResponse({"error": "Employee not found."}, status_code=404)


async def index(request: Request):
    return HTMLResponse(theme.inject(PAGE))


async def employees(request: Request):

    # One query for every record (MongoDB), not one lookup per employee
    records = storage.get_store().list_records()
    for record in records:
        if record.get("application_status") in (None, LEGACY_NEW):
            record["application_status"] = STATUSES[0]

    # Newest activity first: a resubmitted application counts from when it came back
    summaries = sorted(
        (summarize(record) for record in records),
        key=lambda summary: summary["resubmitted_at"] or summary["submitted_at"],
        reverse=True,
    )

    return JSONResponse(summaries)


async def employee_detail(request: Request):

    employee_id = request.path_params["employee_id"]
    record = load_record(employee_id)

    if record is None:
        return not_found()

    return JSONResponse(detail_payload(record))


def detail_payload(record):
    """Everything the detail page needs: the record, its documents and the employee's link."""

    employee_id = record["employee_id"]
    token = record.get("access_token")

    return {
        "record": record,
        "documents": list_documents(employee_id),
        "application_link": f"{ONBOARDING_URL}/?application={employee_id}&token={token}" if token else "",
        # Only applications saved with their form can be reopened by the employee
        "can_flag": bool(token) and storage.get_store().has_form(employee_id),
    }


async def employee_document(request: Request):

    employee_id = request.path_params["employee_id"]
    document = request.path_params["document"]
    content = None

    if document in DOCUMENTS and EMPLOYEE_ID_PATTERN.fullmatch(employee_id):
        file_type = DOCUMENTS[document][1]
        content = storage.get_store().get_document(employee_id, f"{document}.{file_type}")

    if content is None:
        return JSONResponse({"error": "Document not found."}, status_code=404)

    return Response(content, media_type=MEDIA_TYPES[file_type])


async def update_status(request: Request):

    employee_id = request.path_params["employee_id"]
    record = load_record(employee_id)

    if record is None:
        return not_found()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = None

    status = body.get("status") if isinstance(body, dict) else None

    if status not in HR_DECISIONS:
        return JSONResponse({"error": "Invalid status."}, status_code=400)

    if status == "Approved" and open_flags(record):
        return JSONResponse(
            {"error": "This application has open flags. Wait for the employee to send the changes back first."},
            status_code=409,
        )

    record["application_status"] = status
    record["reviewed_at"] = datetime.datetime.now().isoformat()
    save_record(record)

    return JSONResponse(detail_payload(record))


async def save_verification(request: Request):
    """Remember which fields and documents HR has ticked as matching (the review can be continued later).

    A resubmitted application is rebuilt without these ticks, so HR checks the new answers again.
    """

    employee_id = request.path_params["employee_id"]
    record = load_record(employee_id)

    if record is None:
        return not_found()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = None

    keys = body.get("verified") if isinstance(body, dict) else None

    if (
        not isinstance(keys, list) or len(keys) > 300
        or not all(isinstance(key, str) and VERIFIED_KEY_PATTERN.fullmatch(key) for key in keys)
    ):
        return JSONResponse({"error": "Invalid list of verified items."}, status_code=400)

    record["hr_verification"] = {
        "verified": sorted(set(keys)),
        "updated_at": datetime.datetime.now().isoformat(),
    }

    # The ticks are not part of the HR tables, so only the record itself is rewritten
    storage.get_store().put_json("submission", employee_id, record)

    return JSONResponse({"ok": True})


def open_flags(record):
    return [flag for flag in record.get("flags", []) if flag.get("status") == "open"]


async def request_changes(request: Request):
    """HR flags what is wrong: the application goes back to the employee as Changes Requested."""

    employee_id = request.path_params["employee_id"]
    record = load_record(employee_id)

    if record is None:
        return not_found()

    if not detail_payload(record)["can_flag"]:
        return JSONResponse(
            {"error": "This application was submitted before flags were added, so the employee cannot reopen it."},
            status_code=400,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = None

    raised = body.get("flags") if isinstance(body, dict) else None
    now = datetime.datetime.now().isoformat()
    new_flags = []

    for item in raised if isinstance(raised, list) else []:
        key, label, note = (str(item.get(name) or "").strip() for name in ("key", "label", "note"))
        if not key:
            continue
        if not note:
            return JSONResponse({"error": f"Please write what is wrong with {label or key}."}, status_code=400)
        new_flags.append({
            "id": secrets.token_hex(4), "key": key, "label": label or key, "note": note,
            "raised_at": now, "status": "open",
        })

    if not new_flags:
        return JSONResponse({"error": "Flag at least one field or document first."}, status_code=400)

    # A newer flag on the same item replaces the one still open
    flagged_keys = {flag["key"] for flag in new_flags}
    record["flags"] = [
        flag for flag in record.get("flags", [])
        if not (flag.get("status") == "open" and flag["key"] in flagged_keys)
    ] + new_flags
    record["application_status"] = "Changes Requested"
    record["reviewed_at"] = now
    save_record(record)

    return JSONResponse(detail_payload(record))


# ==========================================================
# 4. REACT FRONTEND
# ==========================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HR Review Portal</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🗂️</text></svg>">
<!-- __THEME_HEAD__ -->
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js" crossorigin></script>
<script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js" crossorigin></script>
<script src="https://unpkg.com/@babel/standalone@7.26.4/babel.min.js"></script>
<style>
:root {
  --navy: #142d59;
  --navy-2: #234b83;
  --ink: #1d2939;
  --muted: #5b6b82;
  --line: #d5d9e1;
  --field-line: #bcc2cd;
  --bg: #e6e9ef;
  --card: #f5f6f8;
  --primary: #1260d5;
  --primary-dark: #0d48a5;
  --banner: #e4efff;
  --banner-ink: #183b75;
  --error-bg: #fdecec;
  --error: #b42318;
  --ok-bg: #e8f6ee;
  --ok: #1e7a46;
  --warn-bg: #fff4e0;
  --warn: #9a5b00;
  --hover: #eceef2;
  --active-line: #aebbd4;
  --active-bg: #e9eef8;
  --active-ring: #c5d3ee;
  --info-bg: #dbeafe;
  --info-ink: #1d4ed8;
  --info-bg-2: #eff6ff;
  --info-ink-2: #1e40af;
  --info-line: #bfdbfe;
  --flag-bg: #fff7ed;
  --flag-bg-2: #ffedd5;
  --flag-line: #fdba74;
  --flag-ink: #9a3412;
  --flag-ink-2: #c2410c;
  --flag-tab-bg: #ffe8d4;
  --flag-tab-line: #f5a35e;
  --flag-tab-ink: #a5400a;
  --ok-soft: #e9f1ec;
  --ok-line: #9bc7ae;
  --ok-tab-bg: #d9eadf;
  --ok-tab-line: #7cc79b;
  --ok-tab-ink: #14633a;
  --update-bg: #e8f1ff;
  --update-line: #9ebdf0;
  --update-ink: #123a7a;
  --update-hover: #dbe9ff;
  --warn-line: #f0c987;
  --warn-hover: #ffe9c2;
  --soft: #e3e6ec;
  --card-soft: #eceef2;
  --track: #d9dde5;
  --rail: #b4bbc8;
  --tabs-bg: #d3d8e2;
  --tabs-line: #a9b2c4;
  --heading: #173568;
  --heading-strong: #10223f;
  --ink-soft: #3d4b61;
}
/* Dark theme: set on <html> by the script from theme.py (Default follows the device) */
:root[data-theme="dark"] {
  --navy: #0e1d3c;
  --navy-2: #1a3560;
  --ink: #e4e9f4;
  --muted: #9aa6bf;
  --line: #27324d;
  --field-line: #3a4766;
  --bg: #0b1120;
  --card: #141c30;
  --primary: #3b7ae4;
  --primary-dark: #5b93ee;
  --banner: #1b2b4a;
  --banner-ink: #bcd3ff;
  --error-bg: #3a1618;
  --error: #dc4040;
  --ok-bg: #10291d;
  --ok: #22a05f;
  --warn-bg: #33270d;
  --warn: #c98512;
  --hover: #1b2542;
  --active-line: #3a5a94;
  --active-bg: #1b2b4a;
  --active-ring: #2c4a7c;
  --info-bg: #1e3a5f;
  --info-ink: #93c5fd;
  --info-bg-2: #172a4a;
  --info-ink-2: #93c5fd;
  --info-line: #2c4a7c;
  --flag-bg: #33200f;
  --flag-bg-2: #4a2a12;
  --flag-line: #a85a1f;
  --flag-ink: #fdba74;
  --flag-ink-2: #fb923c;
  --flag-tab-bg: #4a2a12;
  --flag-tab-line: #a85a1f;
  --flag-tab-ink: #fdba74;
  --ok-soft: #10291d;
  --ok-line: #1f6b45;
  --ok-tab-bg: #10291d;
  --ok-tab-line: #1f6b45;
  --ok-tab-ink: #6ee7b7;
  --update-bg: #172a4a;
  --update-line: #2c4a7c;
  --update-ink: #bcd3ff;
  --update-hover: #1f3560;
  --warn-line: #7a5a14;
  --warn-hover: #45340f;
  --soft: #1b2440;
  --card-soft: #19223a;
  --track: #26324f;
  --rail: #3a4766;
  --tabs-bg: #1b2744;
  --tabs-line: #38496f;
  --heading: #a9c4f5;
  --heading-strong: #e4e9f4;
  --ink-soft: #b6c0d6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 15px;
  line-height: 1.45;
}
.page { max-width: 1280px; margin: 0 auto; padding: 24px 16px 64px; }
.hero {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  background: linear-gradient(100deg, var(--navy), var(--navy-2));
  color: #fff;
  padding: 25px;
  border-radius: 12px;
  margin-bottom: 20px;
}
.hero h1 { margin: 0 0 5px; font-size: 28px; }
.hero p { margin: 0; color: #dce8ff; }
.layout { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 20px; align-items: start; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 20px; }
.sidebar { padding: 14px; position: sticky; top: 16px; }
.filters { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
input, select {
  font: inherit;
  color: var(--ink);
  background: var(--card);
  border: 1px solid var(--field-line);
  border-radius: 8px;
  padding: 8px 11px;
  width: 100%;
}
input:focus, select:focus { outline: 2px solid var(--primary); outline-offset: -1px; }
.count { color: var(--muted); font-size: 13px; margin: 4px 2px 8px; }
.employee-list { list-style: none; margin: 0; padding: 0; max-height: calc(100vh - 260px); overflow-y: auto; }
.employee-list button {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 10px;
  color: inherit;
}
.employee-list button:hover { background: var(--hover); }
.employee-list button.active { background: var(--banner); border-color: var(--active-line); }
.employee-name { font-weight: 600; }
.employee-meta { color: var(--muted); font-size: 13px; display: flex; justify-content: space-between; gap: 8px; margin-top: 2px; }
.badge { display: inline-block; font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 999px; white-space: nowrap; }
.badge.new { background: var(--info-bg); color: var(--info-ink); }
.badge.flagged { background: var(--flag-bg-2); color: var(--flag-ink-2); }
.tag { display: inline-block; font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 999px; background: #1d4ed8; color: #fff; margin-left: 6px; vertical-align: middle; }
.flag-control { margin-top: 4px; }
.flag-btn { background: none; border: 1px dashed var(--field-line); color: var(--muted); border-radius: 999px; font-size: 12px; font-weight: 600; padding: 1px 10px; }
.flag-btn:hover { color: var(--flag-ink-2); border-color: var(--flag-line); background: var(--flag-bg); }
.flag-edit { display: grid; gap: 6px; }
.flag-edit textarea { font: inherit; width: 100%; min-height: 60px; border: 1px solid var(--flag-line); background: var(--flag-bg); border-radius: 8px; padding: 8px 10px; }
.flag-edit button { justify-self: start; background: none; border: none; color: var(--error); font-size: 12.5px; font-weight: 600; padding: 0; }
.flag-chip { font-size: 12.5px; padding: 5px 9px; border-radius: 8px; margin-bottom: 4px; }
.flag-chip.open { background: var(--flag-bg); color: var(--flag-ink); border: 1px solid var(--flag-line); }
.flag-chip.resolved { background: var(--info-bg-2); color: var(--info-ink-2); border: 1px solid var(--info-line); }
.flag-panel { margin-top: 16px; padding: 14px 16px; border: 1px solid var(--flag-line); background: var(--flag-bg); border-radius: 10px; }
.flag-panel h3 { margin: 0 0 8px; font-size: 15px; color: var(--flag-ink); }
.flag-panel ul { margin: 0 0 12px; padding-left: 18px; display: grid; gap: 4px; }
.link-row { display: flex; gap: 8px; align-items: center; margin-top: 14px; flex-wrap: wrap; }
.link-row input { flex: 1; min-width: 240px; font-size: 12.5px; }
.badge.pending { background: var(--warn-bg); color: var(--warn); }
.badge.approved { background: var(--ok-bg); color: var(--ok); }
.badge.rejected { background: var(--error-bg); color: var(--error); }
.profile { display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
.avatar { width: 110px; height: 130px; object-fit: cover; border-radius: 10px; border: 1px solid var(--line); background: var(--soft); }
.avatar.empty { display: flex; align-items: center; justify-content: center; font-size: 42px; }
.profile h2 { margin: 0 0 4px; font-size: 24px; }
.profile-meta { color: var(--muted); font-size: 14px; display: flex; flex-direction: column; gap: 2px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
button { font: inherit; cursor: pointer; }
.btn { border-radius: 8px; font-weight: 600; padding: 8px 16px; border: 1px solid transparent; }
.btn:disabled { opacity: 0.6; cursor: progress; }
.btn.approve { background: var(--ok); color: #fff; }
.btn.reject { background: var(--card); color: var(--error); border-color: #f1b8b3; }
.btn.neutral { background: var(--card); color: var(--primary); border-color: var(--active-line); }
.btn.light { background: rgba(255, 255, 255, 0.14); color: #fff; border-color: rgba(255, 255, 255, 0.35); }
.section-title { color: var(--heading); font-size: 19px; font-weight: 700; margin: 26px 0 12px; }
.sub { font-size: 15px; font-weight: 700; margin: 20px 0 10px; color: var(--heading); }
.info-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px 20px; }
.info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.info.wide { grid-column: 1 / -1; }
.info-label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); font-weight: 600; }
.info-value { white-space: pre-wrap; overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 520px; }
th { text-align: left; font-size: 13px; color: var(--muted); font-weight: 600; padding: 8px; border-bottom: 1px solid var(--line); }
td { padding: 8px; border-bottom: 1px solid var(--line); }
tr.check-row.match td { background: var(--ok-bg); }
tr.check-row.match td:first-child { box-shadow: inset 4px 0 0 var(--ok); }
tr.check-row.mismatch td { background: var(--error-bg); }
tr.check-row.mismatch td:first-child { box-shadow: inset 4px 0 0 var(--error); }
tr.check-row.missing td { background: var(--warn-bg); }
tr.check-row.missing td:first-child { box-shadow: inset 4px 0 0 var(--warn); }
.check-badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-weight: 600; font-size: 13px; white-space: nowrap; }
.check-badge.match { background: var(--ok); color: #fff; }
.check-badge.mismatch { background: var(--error); color: #fff; }
.check-badge.missing { background: var(--warn); color: #fff; }
tr:last-child td { border-bottom: none; }
.doc-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.doc-card { border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
.doc-card h3 { margin: 0 0 10px; font-size: 15px; }
.doc-card img { display: block; width: 100%; height: 170px; object-fit: contain; background: var(--soft); border-radius: 6px; cursor: zoom-in; }
.doc-pdf { display: flex; align-items: center; justify-content: center; height: 170px; background: var(--error-bg); color: #b42318; border-radius: 6px; font-weight: 600; text-decoration: none; }
.doc-links { display: flex; gap: 14px; margin-top: 10px; font-size: 14px; }
a { color: var(--primary); font-weight: 600; text-decoration: none; }
a:hover { text-decoration: underline; }
.caption { color: var(--muted); font-size: 14px; margin: 0; }
.alert { padding: 12px 14px; border-radius: 8px; margin-bottom: 14px; }
.alert.error { background: var(--error-bg); color: var(--error); }
.alert.success { background: var(--ok-bg); color: var(--ok); }
.empty-state { text-align: center; color: var(--muted); padding: 60px 20px; }
.empty-state div { font-size: 40px; }
.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(10, 18, 32, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  z-index: 10;
  cursor: zoom-out;
}
.lightbox img { max-width: 100%; max-height: 100%; border-radius: 8px; background: #fff; }
pre {
  background: #0f1b2d;
  color: #dce8ff;
  padding: 14px;
  border-radius: 8px;
  overflow: auto;
  font-size: 13px;
  max-height: 420px;
}
details { margin-top: 20px; }
summary { cursor: pointer; color: var(--primary); font-weight: 600; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { position: static; }
  .employee-list { max-height: 300px; }
  .info-grid, .doc-grid { grid-template-columns: 1fr; }
  .hero { flex-direction: column; align-items: flex-start; }
  .hero h1 { font-size: 22px; }
}

/* ---------- guided review ---------- */
.page { max-width: 1680px; }
.layout.collapsed { grid-template-columns: minmax(0, 1fr); }
.layout.collapsed .sidebar { display: none; }
.list-toggle { margin-bottom: 12px; }
.alert.update { background: var(--update-bg); color: var(--update-ink); border: 1px solid var(--update-line); }
.update-banner { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; background: var(--update-bg); border: 1px solid var(--update-line); border-left: 6px solid var(--primary); color: var(--update-ink); border-radius: 10px; padding: 11px 14px; margin-bottom: 16px; }
.update-banner button { background: var(--card); border: 1px solid var(--update-line); color: var(--update-ink); border-radius: 999px; font-weight: 700; padding: 4px 12px; }
.update-banner button:hover { background: var(--update-hover); }
.profile-main { flex: 1; min-width: 220px; }
.profile-progress { min-width: 280px; max-width: 360px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.progress-bar { width: 100%; height: 10px; background: var(--track); border-radius: 999px; overflow: hidden; }
.progress-bar span { display: block; height: 100%; background: var(--ok); border-radius: 999px; transition: width 0.25s; }
.ai-chip { background: var(--warn-bg); color: var(--warn); border: 1px solid var(--warn-line); border-radius: 8px; font-weight: 600; font-size: 13px; padding: 6px 10px; text-align: left; }
.ai-chip:hover { background: var(--warn-hover); }

.stepper { display: flex; gap: 8px; overflow-x: auto; padding: 14px 2px 12px; position: sticky; top: 0; z-index: 6; background: var(--bg); }
.step-tab { display: flex; align-items: center; gap: 9px; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px 14px 8px 10px; color: var(--ink); white-space: nowrap; flex: 1 0 auto; text-align: left; }
.step-tab:hover { border-color: var(--active-line); }
.step-tab.active { border-color: var(--primary); box-shadow: 0 0 0 2px var(--active-ring); background: var(--active-bg); }
.step-num { width: 26px; height: 26px; border-radius: 50%; background: var(--track); color: var(--muted); display: inline-flex; align-items: center; justify-content: center; font-weight: 700; font-size: 13px; flex: none; }
.step-tab.active .step-num { background: var(--primary); color: #fff; }
.step-tab.complete .step-num { background: var(--ok); color: #fff; }
.step-tab.has-flag .step-num { background: #ea580c; color: #fff; }
.step-text { display: flex; flex-direction: column; line-height: 1.2; }
.step-title { font-weight: 700; font-size: 14px; }
.step-count { font-size: 12px; color: var(--muted); }

.step-card { padding: 20px 22px; }
.step-heading { margin: 0 0 4px; font-size: 21px; color: var(--heading); }
.step-help { color: var(--muted); margin: 0 0 16px; }
.step-nav { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--line); }
.btn.primary { background: var(--primary); color: #fff; }
.btn.primary:hover { background: var(--primary-dark); }
.btn.send { background: #ea580c; color: #fff; }

.review-split { display: grid; grid-template-columns: minmax(340px, 5fr) minmax(0, 7fr); gap: 22px; align-items: start; }
.review-split.no-docs { grid-template-columns: minmax(0, 1fr); }
.review-fields { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.review-fields .sub { margin: 14px 0 0; }
.review-fields > .btn { align-self: flex-start; margin-top: 6px; }
.review-viewer { position: sticky; top: 84px; min-width: 0; }

.review-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(150px, 270px); gap: 8px 14px; align-items: start; border: 1px solid var(--line); border-left: 5px solid var(--rail); border-radius: 10px; padding: 10px 12px; background: var(--card); }
.review-row.verified { border-left-color: var(--ok); background: var(--ok-soft); }
.review-row.flagged { border-left-color: #ea580c; background: var(--flag-bg); }
.review-row.skipped { opacity: 0.55; border-style: dashed; }
.review-row .review-controls { min-width: 150px; }
.review-row.wide { grid-template-columns: minmax(0, 1fr); }
.review-ai { grid-column: 1 / -1; }
.review-row.wide .review-controls { min-width: 0; }
.review-value { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.info-value.big { font-size: 17px; font-weight: 600; color: var(--heading-strong); }
.hint { color: var(--muted); font-size: 13px; }
.review-controls { display: flex; flex-direction: column; gap: 6px; }
.mark-buttons { display: flex; gap: 6px; flex-wrap: wrap; }
.mark { background: var(--card); border: 1px solid var(--field-line); color: var(--muted); border-radius: 8px; font-weight: 600; font-size: 13px; padding: 5px 11px; }
.mark.ok:hover { color: var(--ok); border-color: var(--ok-line); background: var(--ok-bg); }
.mark.bad:hover { color: var(--flag-ink-2); border-color: var(--flag-line); background: var(--flag-bg); }
.mark.ok.on { background: var(--ok); border-color: var(--ok); color: #fff; }
.mark.bad.on { background: #ea580c; border-color: #ea580c; color: #fff; }
.flag-edit textarea { font: inherit; width: 100%; min-height: 64px; border: 1px solid var(--flag-line); background: var(--card); border-radius: 8px; padding: 8px 10px; }

.ai-box { margin-top: 8px; border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; background: var(--card-soft); }
.ai-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin-bottom: 6px; }
.ai-row { display: flex; gap: 10px; align-items: flex-start; padding: 6px 8px; border-radius: 6px; margin-bottom: 4px; font-size: 13.5px; }
.ai-row.match { background: var(--ok-bg); }
.ai-row.mismatch { background: var(--error-bg); }
.ai-row.missing { background: var(--warn-bg); }
.ai-detail { color: var(--ink-soft); font-size: 13px; overflow-wrap: anywhere; }

.viewer { border: 1px solid var(--line); border-radius: 12px; background: var(--card); overflow: hidden; }
.viewer.full { position: fixed; inset: 12px; z-index: 30; display: flex; flex-direction: column; box-shadow: 0 0 0 100vmax rgba(10, 18, 32, 0.75); }
.viewer-title { display: flex; justify-content: space-between; align-items: center; gap: 10px; background: linear-gradient(100deg, var(--navy), var(--navy-2)); color: #fff; font-size: 17px; font-weight: 700; padding: 11px 14px; }
.viewer-title.verified { background: linear-gradient(100deg, #14633a, #1e7a46); }
.viewer-title.flagged { background: linear-gradient(100deg, #b4400c, #ea580c); }
.viewer-title-state { font-size: 13px; font-weight: 700; background: rgba(255, 255, 255, 0.22); border-radius: 999px; padding: 2px 10px; }
.viewer-title-state:empty { display: none; }
.viewer-tabs { display: flex; gap: 6px; overflow-x: auto; padding: 8px 8px 8px; background: var(--tabs-bg); border-bottom: 2px solid var(--navy-2); }
.viewer-tab { background: var(--card); border: 1px solid var(--tabs-line); border-radius: 8px; padding: 6px 13px; font-size: 13.5px; font-weight: 700; color: var(--heading); white-space: nowrap; }
.viewer-tab:hover { background: var(--hover); }
.viewer-tab.active { background: var(--navy-2); border-color: var(--navy-2); color: #fff; box-shadow: 0 2px 6px rgba(20, 45, 89, 0.35); }
.viewer-tab.verified:not(.active) { background: var(--ok-tab-bg); border-color: var(--ok-tab-line); color: var(--ok-tab-ink); }
.viewer-tab.flagged:not(.active) { background: var(--flag-tab-bg); border-color: var(--flag-tab-line); color: var(--flag-tab-ink); }
.viewer-toolbar { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.viewer-toolbar button { background: var(--card); border: 1px solid var(--field-line); border-radius: 6px; padding: 3px 10px; font-weight: 600; color: var(--ink); }
.viewer-toolbar button:hover { background: var(--hover); }
.viewer-toolbar a { font-size: 14px; }
.toolbar-spacer { flex: 1; }
.zoom-label { min-width: 44px; text-align: center; font-size: 13px; color: var(--muted); }
.viewer-body { background: var(--soft); }
.viewer-scroll { height: min(68vh, 760px); overflow: auto; cursor: grab; touch-action: pan-x pan-y; padding: 3px; }
.viewer-scroll.dragging { cursor: grabbing; user-select: none; }
.viewer-scroll { overflow: hidden; position: relative; padding: 0; cursor: grab; touch-action: none; user-select: none; background: #1f2937; }
.viewer-scroll img { user-select: none; will-change: transform; }
.viewer.full .viewer-body { flex: 1; min-height: 0; }
.viewer.full .viewer-scroll, .viewer.full .viewer-frame { height: 100%; }
.viewer-frame { display: block; width: 100%; height: min(68vh, 760px); border: 0; background: #fff; }
.viewer-hint { font-size: 12.5px; color: var(--muted); padding: 6px 10px 0; }
.doc-review { margin: 8px 10px 10px; padding: 10px 12px; border: 1px solid var(--line); border-left: 5px solid var(--rail); border-radius: 10px; display: flex; flex-direction: column; gap: 6px; }
.doc-review.verified { border-left-color: var(--ok); background: var(--ok-soft); }
.doc-review.flagged { border-left-color: #ea580c; background: var(--flag-bg); }
.viewer.full .doc-review { max-height: 30vh; overflow: auto; }

.decision-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.card.inner { box-shadow: none; padding: 14px 16px; background: var(--card-soft); }
.progress-line { display: flex; justify-content: space-between; width: 100%; background: none; border: none; border-bottom: 1px solid var(--line); padding: 8px 2px; color: var(--ink); font-size: 14.5px; text-align: left; }
.progress-line:hover { background: var(--hover); }
.flag-list { margin: 0; padding-left: 18px; display: grid; gap: 6px; }
.decision .actions { margin-top: 18px; }
.step-card .caption { margin-top: 0; }

@media (max-width: 1100px) {
  .review-split { grid-template-columns: minmax(0, 1fr); }
  .review-viewer { position: static; }
  .decision-grid { grid-template-columns: minmax(0, 1fr); }
  .review-row { grid-template-columns: minmax(0, 1fr); }
  .profile-progress { max-width: none; width: 100%; }
}
.hero-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
/* __THEME_CSS__ */
</style>
</head>
<body>
<div id="root"></div>

<script type="text/babel" data-presets="react">
const { useState, useEffect, useMemo, useRef } = React;

// __THEME_SCRIPT__

const STATUSES = ["New", "Changes Requested", "Pending", "Approved", "Rejected"];

const STATUS_CLASS = {
  "New": "new",
  "Changes Requested": "flagged",
  "Pending": "pending",
  "Approved": "approved",
  "Rejected": "rejected",
};

// An application the employee sent back after HR flagged something, not yet looked at again
const isUpdated = (employee) => Boolean(employee.resubmitted_at) && employee.application_status === "New";
const UPDATED_FILTER = "Updated applications";

async function request(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Could not reach the server. Is hr_info.py still running?");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return isNaN(date) ? iso : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// ---------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------

function StatusBadge({ status }) {
  return <span className={`badge ${STATUS_CLASS[status] || "pending"}`}>{status}</span>;
}

function DataTable({ rows, rowStatus }) {
  if (!rows || !rows.length) return <p className="caption">Not provided.</p>;
  const columns = Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className={rowStatus ? `check-row ${rowStatus[index] || ""}` : undefined}>
              {columns.map((column) => <td key={column}>{row[column] || "—"}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Sidebar({ employees, selectedId, onSelect, loading }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("All");

  const visible = useMemo(() => {
    const text = query.trim().toLowerCase();
    return employees.filter((employee) =>
      (status === "All" || (status === UPDATED_FILTER ? isUpdated(employee) : employee.application_status === status)) &&
      (!text || [employee.name, employee.employee_id, employee.email]
        .some((value) => (value || "").toLowerCase().includes(text)))
    );
  }, [employees, query, status]);

  return (
    <aside className="card sidebar">
      <div className="filters">
        <input
          type="search"
          placeholder="Search name, ID or email"
          aria-label="Search employees"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select aria-label="Filter by status" value={status} onChange={(event) => setStatus(event.target.value)}>
          {["All", UPDATED_FILTER, ...STATUSES].map((option) => <option key={option}>{option}</option>)}
        </select>
      </div>

      <div className="count">
        {loading ? "Loading..." : `${visible.length} of ${employees.length} submissions`}
      </div>

      <ul className="employee-list">
        {visible.map((employee) => (
          <li key={employee.employee_id}>
            <button
              type="button"
              className={employee.employee_id === selectedId ? "active" : ""}
              onClick={() => onSelect(employee.employee_id)}
            >
              <div className="employee-name">
                {employee.name || "Unnamed"}
                {isUpdated(employee) && <span className="tag">Updated application</span>}
              </div>
              <div className="employee-meta">
                <span>{employee.employee_id}</span>
                <StatusBadge status={employee.application_status} />
              </div>
              <div className="employee-meta">
                <span>{employee.last_designation}</span>
                <span>{formatDateTime(employee.resubmitted_at || employee.submitted_at)}</span>
              </div>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}

// ---------------------------------------------------------------
// Guided review: one step at a time, the details next to the document
// ---------------------------------------------------------------

const CHECK_TEXT = { match: "✓ Matches", mismatch: "✕ Does not match", missing: "– Not found" };

// HR's marks while reviewing: "verified" ticks are saved on the record, flag notes are drafts
// until HR sends them back to the employee.
const ReviewContext = React.createContext({
  canFlag: false, verified: new Set(), drafts: {}, flagByKey: {},
  toggleVerified() {}, setDraft() {}, removeDraft() {},
});

// The AI checks (bank_details.verification) that belong to a field or a document
function aiChecksFor(key, checks) {
  const pick = (pattern) => checks.filter((item) => pattern.test(item.check));
  if (key === "bank.account_holder") return pick(/^Account holder/i);
  if (key === "bank.current_address") return pick(/^Bank address/i);
  if (key.startsWith("payslip_")) return pick(/^Payslip/i);
  if (key === "relieving_letter") return pick(/^Relieving letter/i);
  if (key === "organisation") return pick(/^Relieving letter organisation/i);
  if (key === "period_to") return pick(/^Last working/i);
  return [];
}

// A ready-made flag note for what the AI could not match
function aiSuggestion(checks) {
  return checks.filter((item) => item.status !== "match").map((item) => {
    if (item.status === "mismatch") return `${item.check}: the document shows "${item.document}" but "${item.expected}" was expected.`;
    if (item.document && !item.expected) return `${item.check}: the document shows "${item.document}" but nothing was entered in the form.`;
    return `${item.check}: could not be found on the document.`;
  }).join(" ");
}

function AiChecks({ checks, bare }) {
  if (!checks.length) return null;
  return (
    <div className="ai-box">
      {!bare && (
        <div className="ai-title">
          {checks.some((item) => item.status !== "match") ? "⚠ Need to reconsider" : "✓ Checked, no concerns"}
        </div>
      )}
      {checks.map((item, index) => (
        <div key={index} className={`ai-row ${item.status}`}>
          <span className={`check-badge ${item.status}`}>{CHECK_TEXT[item.status] || item.status}</span>
          <div>
            <strong>{item.check}</strong>
            <div className="ai-detail">On the document: {item.document || "—"}</div>
            <div className="ai-detail">Expected: {item.expected || "—"} ({item.source})</div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ✓ Matches / ⚑ Flag for one field or document
function ReviewControls({ itemKey, label, suggestion }) {
  const { canFlag, verified, drafts, flagByKey, toggleVerified, setDraft, removeDraft } = React.useContext(ReviewContext);
  const draft = drafts[itemKey];
  const previous = flagByKey[itemKey];
  return (
    <div className="review-controls">
      {previous && (
        <div className={`flag-chip ${previous.status}`}>
          ⚑ {previous.status === "open" ? "Flagged, waiting for the employee" : "Employee updated this, please re-check"}: {previous.note}
        </div>
      )}
      <div className="mark-buttons">
        <button type="button" className={"mark ok" + (verified.has(itemKey) ? " on" : "")} onClick={() => toggleVerified(itemKey)}>
          ✓ Matches
        </button>
        {canFlag && (
          <button
            type="button"
            className={"mark bad" + (draft ? " on" : "")}
            onClick={() => (draft ? removeDraft(itemKey) : setDraft(itemKey, label, suggestion || ""))}
          >⚑ Flag</button>
        )}
      </div>
      {draft && (
        <div className="flag-edit">
          <textarea
            autoFocus={!draft.note}
            aria-label={`What is wrong with ${label}`}
            placeholder="What is wrong? The employee will see this note."
            value={draft.note}
            onChange={(event) => setDraft(itemKey, label, event.target.value)}
          />
        </div>
      )}
    </div>
  );
}

function ReviewRow({ item, checks }) {
  const { verified, drafts } = React.useContext(ReviewContext);
  const state = drafts[item.key] ? "flagged" : verified.has(item.key) ? "verified" : "";
  const related = aiChecksFor(item.key, checks);
  const skipped = item.optional && item.empty;
  return (
    <div className={`review-row ${state}${item.wide ? " wide" : ""}${skipped ? " skipped" : ""}`}>
      <div className="review-value">
        <span className="info-label">{item.label}{skipped && " (optional, not provided)"}</span>
        <div className="info-value big">{item.node}</div>
        {item.hint && <span className="hint">{item.hint}</span>}
      </div>
      {!skipped && <ReviewControls itemKey={item.key} label={item.label} suggestion={aiSuggestion(related)} />}
      {related.length > 0 && <div className="review-ai"><AiChecks checks={related} /></div>}
    </div>
  );
}

// ---------------------------------------------------------------
// Document viewer: tabs, zoom, rotate, drag to pan, full screen
// ---------------------------------------------------------------

// A picture you move with the mouse: the wheel zooms in and out at the pointer, dragging moves it,
// a double click fits it again. It is placed with a transform, so nothing reflows while it moves.
function ImageView({ url, label, view, rotation, onView }) {
  const box = useRef(null);
  const latest = useRef({});
  const drag = useRef(null);
  const [natural, setNatural] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [dragging, setDragging] = useState(false);

  useEffect(() => { setNatural(null); }, [url]);

  useEffect(() => {
    const element = box.current;
    if (!element) return undefined;
    const measure = () => setSize({ w: element.clientWidth, h: element.clientHeight });
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, []);

  const sideways = rotation % 180 !== 0;
  const naturalWidth = natural ? natural.w : 1;
  const naturalHeight = natural ? natural.h : 1;
  const fit = natural && size.w
    ? Math.min(size.w / (sideways ? naturalHeight : naturalWidth), size.h / (sideways ? naturalWidth : naturalHeight))
    : 1;
  const scale = fit * view.mul;
  latest.current = { view, size, onView };

  useEffect(() => {
    const element = box.current;
    if (!element) return undefined;
    const onWheel = (event) => {
      event.preventDefault();
      const { view: current, size: box_, onView: change } = latest.current;
      const rect = element.getBoundingClientRect();
      const px = event.clientX - rect.left - box_.w / 2;
      const py = event.clientY - rect.top - box_.h / 2;
      const mul = Math.min(10, Math.max(0.5, current.mul * (event.deltaY < 0 ? 1.2 : 1 / 1.2)));
      const ratio = mul / current.mul;
      const next = { mul, ox: px - (px - current.ox) * ratio, oy: py - (py - current.oy) * ratio };
      latest.current.view = next;
      change(next);
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);

  const onDown = (event) => {
    box.current.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, y: event.clientY, ox: view.ox, oy: view.oy };
    setDragging(true);
  };
  const onMove = (event) => {
    if (!drag.current) return;
    onView({ ...view, ox: drag.current.ox + event.clientX - drag.current.x, oy: drag.current.oy + event.clientY - drag.current.y });
  };
  const onUp = () => { drag.current = null; setDragging(false); };

  return (
    <div
      className={"viewer-scroll" + (dragging ? " dragging" : "")}
      ref={box}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onDoubleClick={() => onView({ mul: 1, ox: 0, oy: 0 })}
    >
      <img
        src={url}
        alt={label}
        draggable={false}
        onLoad={(event) => setNatural({ w: event.target.naturalWidth, h: event.target.naturalHeight })}
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          width: natural ? naturalWidth : "auto",
          height: natural ? naturalHeight : "auto",
          maxWidth: "none",
          opacity: natural ? 1 : 0,
          transformOrigin: "50% 50%",
          transform: `translate(${size.w / 2 + view.ox - naturalWidth / 2}px, ${size.h / 2 + view.oy - naturalHeight / 2}px) scale(${scale}) rotate(${rotation}deg)`,
        }}
      />
    </div>
  );
}

function DocViewer({ docs, activeKey, onActive, employeeId, statusOf, children }) {
  // mul: zoom on top of "fit the panel"; ox, oy: how far the picture was moved
  const [view, setView] = useState({ mul: 1, ox: 0, oy: 0 });
  const [rotation, setRotation] = useState(0);
  const [full, setFull] = useState(false);
  const active = docs.find((item) => item.key === activeKey) || docs[0];

  useEffect(() => { setView({ mul: 1, ox: 0, oy: 0 }); setRotation(0); }, [active && active.key]);
  useEffect(() => {
    if (!full) return undefined;
    const close = (event) => { if (event.key === "Escape") setFull(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [full]);

  if (!active) return null;
  const isImage = active.type !== "pdf";

  // The + and − buttons zoom about the middle of the panel
  const zoomBy = (factor) => setView((current) => {
    const mul = Math.min(10, Math.max(0.5, current.mul * factor));
    const ratio = mul / current.mul;
    return { mul, ox: current.ox * ratio, oy: current.oy * ratio };
  });

  return (
    <div className={"viewer" + (full ? " full" : "")}>
      <div className={"viewer-title " + (statusOf(active.key) || "")}>
        <span>{active.type === "pdf" ? "📄" : "🖼️"} {active.label}{active.reference ? " (for comparison)" : ""}</span>
        <span className="viewer-title-state">
          {statusOf(active.key) === "verified" && "✓ Matches"}
          {statusOf(active.key) === "flagged" && "⚑ Flagged"}
        </span>
      </div>
      <div className="viewer-tabs" role="tablist">
        {docs.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={item.key === active.key}
            className={"viewer-tab " + (statusOf(item.key) || "") + (item.key === active.key ? " active" : "")}
            onClick={() => onActive(item.key)}
          >
            {statusOf(item.key) === "verified" && "✓ "}{statusOf(item.key) === "flagged" && "⚑ "}{item.label}
          </button>
        ))}
      </div>

      <div className="viewer-toolbar">
        {isImage && (
          <>
            <button type="button" onClick={() => zoomBy(1 / 1.25)} aria-label="Zoom out">−</button>
            <span className="zoom-label">{Math.round(view.mul * 100)}%</span>
            <button type="button" onClick={() => zoomBy(1.25)} aria-label="Zoom in">+</button>
            <button type="button" onClick={() => setView({ mul: 1, ox: 0, oy: 0 })}>Fit</button>
            <button type="button" onClick={() => { setRotation((value) => (value + 90) % 360); setView({ mul: 1, ox: 0, oy: 0 }); }}>⟳ Rotate</button>
          </>
        )}
        <span className="toolbar-spacer" />
        <button type="button" onClick={() => setFull((value) => !value)}>{full ? "✕ Close" : "⤢ Full screen"}</button>
        <a href={active.url} target="_blank" rel="noopener">Open ↗</a>
        <a href={active.url} download={`${employeeId}-${active.key}.${active.type}`}>Download</a>
      </div>

      <div className="viewer-body">
        {isImage
          ? <ImageView url={active.url} label={active.label} view={view} rotation={rotation} onView={setView} />
          : <iframe className="viewer-frame" src={`${active.url}#view=FitH`} title={active.label} />}
      </div>

      {isImage && <div className="viewer-hint">Mouse wheel zooms in and out. Drag to move the picture. Double-click to fit it again.</div>}
      {children}
    </div>
  );
}

// ---------------------------------------------------------------
// Employee detail: the seven-step review
// ---------------------------------------------------------------

function EmployeeDetail({ detail, onStatusChange, onRequestChanges, onSaveVerification, updating, message }) {
  const { record, documents } = detail;

  const [stepIndex, setStepIndex] = useState(0);
  const [activeDocs, setActiveDocs] = useState({});
  const [drafts, setDrafts] = useState({});
  const [verified, setVerified] = useState(() => new Set((record.hr_verification && record.hr_verification.verified) || []));
  const [copied, setCopied] = useState(false);

  const flagByKey = {};
  (record.flags || []).forEach((flag) => { flagByKey[flag.key] = flag; });
  const openFlags = (record.flags || []).filter((flag) => flag.status === "open");

  const setDraft = (key, label, note) => {
    setDrafts((current) => ({ ...current, [key]: { label, note } }));
    setVerified((current) => { const next = new Set(current); next.delete(key); return next; });
  };
  const removeDraft = (key) => setDrafts((current) => {
    const { [key]: removed, ...rest } = current;
    return rest;
  });
  const toggleVerified = (key) => {
    setVerified((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
    removeDraft(key);
  };
  const reviewContext = { canFlag: detail.can_flag, verified, drafts, flagByKey, toggleVerified, setDraft, removeDraft };

  // Ticks are saved on the record shortly after HR makes them, so a review can be continued later
  const firstRun = useRef(true);
  const unsaved = useRef(false);
  const latest = useRef(verified);
  latest.current = verified;
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return undefined; }
    unsaved.current = true;
    const timer = setTimeout(() => {
      unsaved.current = false;
      onSaveVerification(record.employee_id, [...verified]);
    }, 700);
    return () => clearTimeout(timer);
  }, [verified]);
  useEffect(() => () => {
    if (unsaved.current) onSaveVerification(record.employee_id, [...latest.current]);
  }, []);

  const personal = record.personal_information || {};
  const emergency = personal.emergency_contact || {};
  const address = record.address || {};
  const professional = record.professional_details || {};
  const employment = professional.employment_history || {};
  const period = employment.period || {};
  const designation = employment.designation || {};
  const salary = employment.gross_salary_drawn || {};
  const references = record.references || {};
  const bankDetails = record.bank_details;
  const signed = record.policies;
  const statutory = (signed && signed.statutory_declaration) || {};
  const aiChecks = (bankDetails && bankDetails.verification) || [];
  const photo = documents.find((item) => item.key === "photo");

  const isBlank = (value) => value === undefined || value === null || value === "";
  const field = (key, label, value, extra) => ({ key, label, node: isBlank(value) ? "—" : String(value), empty: isBlank(value), ...extra });
  const docsOf = (keys) => documents.filter((item) => keys.includes(item.key));
  const docItems = (list) => list.map((item) => ({ key: item.key, label: item.label, kind: "doc", doc: true }));

  const identityDocs = docsOf(["pan", "aadhaar_front", "aadhaar", "photo"]);
  const addressDocs = docsOf(["aadhaar_back"]);
  const educationDocs = docsOf(["education_certificate"]);
  const experienceDocs = docsOf(["relieving_letter", "payslip_1", "payslip_2", "payslip_3", "payslip_4", "payslip_5", "payslip_6", "resume"]);
  const bankDocs = docsOf(["bank_proof"]);
  const reference = (keys) => docsOf(keys).map((item) => ({ ...item, reference: true }));

  const steps = [
    {
      id: "identity", title: "Identity", icon: "🪪",
      help: "Compare each detail with the PAN card, the Aadhaar and the photograph. The name, mobile number and email also come from the resume, which is in the last tab. Tick ✓ when it matches, or flag it.",
      docs: identityDocs, refDocs: reference(["aadhaar_back", "resume"]),
      order: ["aadhaar_front", "aadhaar", "aadhaar_back", "pan", "photo", "resume"],
      otherTitle: "Other details (compare with the resume)",
      items: [
        field("name", "Full Name", personal.name),
        field("dob", "Date of Birth", personal.date_of_birth, { hint: personal.age ? `Age ${personal.age}` : "" }),
        field("gender", "Gender", personal.gender),
        field("aadhaar_number", "Aadhaar Number", personal.aadhaar_number),
        field("father_name", "Father's Name", personal.father_name),
        field("pan_number", "PAN Number", personal.pan_number),
        ...docItems(identityDocs),
        field("mobile", "Mobile Number", personal.mobile, { noDoc: true }),
        field("email", "Personal Email", personal.email, { noDoc: true }),
        field("passport_number", "Passport Number", personal.passport_number, { noDoc: true, optional: true }),
        field("languages", "Languages Known", personal.languages, { noDoc: true }),
        field("hobbies", "Hobbies and Interests", personal.hobbies, { noDoc: true, optional: true }),
      ],
    },
    {
      id: "address", title: "Address and family", icon: "📍",
      help: "The permanent address should match the Aadhaar back. Check the emergency contact and family members too.",
      docs: addressDocs, refDocs: reference(["aadhaar_front"]),
      items: [
        field("permanent_address", "Permanent Address", address.permanent_address, { wide: true }),
        field("current_address", "Current Address", address.current_address, { wide: true }),
        ...docItems(addressDocs),
        field("emergency_name", "Emergency Contact: Name", emergency.name, { noDoc: true }),
        field("emergency_relation", "Emergency Contact: Relation", emergency.relation, { noDoc: true }),
        field("emergency_phone", "Emergency Contact: Phone", emergency.phone, { noDoc: true }),
        { key: "family", label: "Family Member Details", wide: true, noDoc: true, node: <DataTable rows={record.family_details} /> },
      ],
    },
    {
      id: "education", title: "Education", icon: "🎓",
      help: "Compare the qualifications table with the highest education certificate.",
      docs: educationDocs, refDocs: [],
      items: [
        { key: "education", label: "Education Details", wide: true, node: <DataTable rows={record.education_details} /> },
        ...docItems(educationDocs),
      ],
    },
    {
      id: "experience", title: "Experience", icon: "💼",
      help: "Check the last employer against the relieving letter, the payslips and the resume. The PAN card and Aadhaar are beside them for the name.",
      docs: experienceDocs, refDocs: reference(["pan", "aadhaar_front"]),
      items: [
        field("total_experience", "Total Experience (Years)", professional.total_experience),
        field("organisation", "Organisation", employment.organisation),
        field("period_from", "Period From", period.from),
        field("period_to", "Period To", period.to),
        field("joining_designation", "Designation at Joining", designation.at_joining),
        field("last_designation", "Last Position Held", designation.last_position_held),
        field("joining_salary", "Gross Salary at Joining", salary.at_joining),
        field("last_salary", "Gross Salary Last Drawn", salary.last_drawn),
        field("job_responsibility", "Job Responsibility", employment.job_responsibility, { wide: true }),
        field("reason_for_leaving", "Reason for Leaving", employment.reason_for_leaving, { wide: true, optional: true }),
        ...docItems(experienceDocs),
      ],
    },
    {
      id: "bank", title: "Bank details", icon: "🏦",
      help: "The bank proof has already been compared with the PAN and Aadhaar. Items marked \"Need to reconsider\" are the ones to look at first.",
      docs: bankDocs, refDocs: reference(["pan", "aadhaar_front"]),
      items: bankDetails ? [
        field("bank.account_holder", "Account Holder", bankDetails.account_holder),
        field("bank.account_number", "Account Number", bankDetails.account_number),
        field("bank.ifsc", "IFSC Code", bankDetails.ifsc),
        field("bank.bank_name", "Bank Name", bankDetails.bank_name),
        field("bank.branch_address", "Branch Address", bankDetails.branch_address, { wide: true }),
        field("bank.current_address", "Current Address (as per bank)", bankDetails.current_address, { wide: true }),
        ...docItems(bankDocs),
      ] : [],
    },
    {
      id: "references", title: "References and policies", icon: "📝",
      help: "No document to compare here. Read through the references and the signed policies.",
      docs: [], refDocs: [],
      items: [
        ...["senior_1", "senior_2"].flatMap((key, index) => {
          const senior = references[key] || {};
          return [
            field(`ref${index + 1}_name`, `Reference ${index + 1}: Name`, senior.name, { noDoc: true }),
            field(`ref${index + 1}_designation`, `Reference ${index + 1}: Designation`, senior.designation, { noDoc: true }),
            field(`ref${index + 1}_contact`, `Reference ${index + 1}: Contact`, senior.contact, { noDoc: true }),
          ];
        }),
        signed ? {
          key: "policies", label: "Company Policies", wide: true, noDoc: true,
          node: (
            <div>
              <div>Designation offered: <strong>{signed.designation || "—"}</strong> · E-signature: <strong>{signed.signature || "—"}</strong> · Signed on {signed.signed_on || "—"}</div>
              <div style={{ margin: "8px 0" }}>{Object.values(signed.accepted || {}).map((policy) => <div key={policy.title}>✓ {policy.title}</div>)}</div>
              <DataTable rows={Object.entries(statutory.answers || {}).map(([point, answer]) => ({ Point: point, Answer: answer }))} />
              {statutory.details && <p><strong>Details for points answered Yes:</strong> {statutory.details}</p>}
            </div>
          ),
        } : { key: "policies", label: "Company Policies", noDoc: true, optional: true, empty: true, node: "Submitted before the policies step was added." },
      ],
    },
  ];

  const decisionIndex = steps.length;
  const countable = (item) => !(item.optional && item.empty);
  const stateOf = (key) => (drafts[key] ? "flagged" : verified.has(key) ? "verified" : "");
  const stepStats = steps.map((step) => {
    const items = step.items.filter(countable);
    return {
      total: items.length,
      done: items.filter((item) => verified.has(item.key)).length,
      flagged: items.filter((item) => drafts[item.key]).length,
    };
  });
  const totals = stepStats.reduce((sum, item) => ({ total: sum.total + item.total, done: sum.done + item.done, flagged: sum.flagged + item.flagged }), { total: 0, done: 0, flagged: 0 });
  const allItems = steps.flatMap((step, index) => step.items.filter(countable).map((item) => ({ ...item, step: index })));
  const notChecked = allItems.filter((item) => !stateOf(item.key));
  const draftList = Object.entries(drafts);
  const aiBad = aiChecks.filter((item) => item.status !== "match");

  useEffect(() => {
    const onKey = (event) => {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test((event.target.tagName || "")) || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.key === "ArrowRight") setStepIndex((value) => Math.min(decisionIndex, value + 1));
      if (event.key === "ArrowLeft") setStepIndex((value) => Math.max(0, value - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decisionIndex]);

  const sendFlags = async () => {
    const sent = await onRequestChanges(draftList.map(([key, { label, note }]) => ({ key, label, note })));
    if (sent) setDrafts({});
  };
  const copyLink = () => {
    navigator.clipboard?.writeText(detail.application_link);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  const downloadJson = () => {
    const blob = new Blob([JSON.stringify(record, null, 4)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${record.employee_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };
  const approve = () => {
    if (notChecked.length && !window.confirm(`${notChecked.length} item${notChecked.length === 1 ? " is" : "s are"} not ticked as matching yet. Approve anyway?`)) return;
    onStatusChange("Approved");
  };
  const reject = () => {
    if (window.confirm("Reject this application?")) onStatusChange("Rejected");
  };

  const percent = totals.total ? Math.round((totals.done / totals.total) * 100) : 0;

  const renderStep = (step, index) => {
    const mainItems = step.items.filter((item) => !item.noDoc && !item.doc);
    const otherItems = step.items.filter((item) => item.noDoc);
    let viewerDocs = [...step.docs, ...step.refDocs];
    if (step.order) {
      const rank = (item) => (step.order.includes(item.key) ? step.order.indexOf(item.key) : step.order.length);
      viewerDocs = [...viewerDocs].sort((a, b) => rank(a) - rank(b));
    }
    const activeKey = activeDocs[step.id] || (viewerDocs[0] && viewerDocs[0].key);
    const activeDoc = viewerDocs.find((item) => item.key === activeKey);
    const viewerStatus = (key) => stateOf(key);
    const stepDone = stepStats[index].total > 0 && stepStats[index].done === stepStats[index].total;

    const markAll = () => {
      const keys = step.items.filter(countable).map((item) => item.key).filter((key) => !drafts[key]);
      setVerified((current) => new Set([...current, ...keys]));
    };

    return (
      <div className={"review-split" + (viewerDocs.length ? "" : " no-docs")}>
        <div className="review-fields">
          <p className="step-help">{step.help}</p>
          {step.id === "bank" && !bankDetails && <p className="caption">This submission was made before the bank details step was added.</p>}
          {mainItems.map((item) => <ReviewRow key={item.key} item={item} checks={aiChecks} />)}
          {otherItems.length > 0 && (
            <>
              {mainItems.length > 0 && <div className="sub">{step.otherTitle || "Other details (no document to compare)"}</div>}
              {otherItems.map((item) => <ReviewRow key={item.key} item={item} checks={aiChecks} />)}
            </>
          )}
          {step.items.some(countable) && (
            <button type="button" className="btn neutral" disabled={stepDone} onClick={markAll}>
              {stepDone ? "✓ Everything in this step is ticked" : "✓ Everything in this step matches"}
            </button>
          )}
        </div>

        {viewerDocs.length > 0 && (
          <div className="review-viewer">
            <DocViewer
              docs={viewerDocs}
              activeKey={activeKey}
              onActive={(key) => setActiveDocs((current) => ({ ...current, [step.id]: key }))}
              employeeId={record.employee_id}
              statusOf={viewerStatus}
            >
              {activeDoc && !activeDoc.reference && (
                <div className={`doc-review ${stateOf(activeDoc.key)}`}>
                  <span className="info-label">Is this document clear and correct?</span>
                  <ReviewControls
                    itemKey={activeDoc.key}
                    label={activeDoc.label}
                    suggestion={aiSuggestion(aiChecksFor(activeDoc.key, aiChecks))}
                  />
                  <AiChecks checks={aiChecksFor(activeDoc.key, aiChecks)} />
                </div>
              )}
              {activeDoc && activeDoc.reference && (
                <p className="caption" style={{ marginTop: 8 }}>Shown for comparison only. This document is ticked or flagged in the step it belongs to.</p>
              )}
            </DocViewer>
          </div>
        )}
      </div>
    );
  };

  const renderDecision = () => (
    <div className="decision">
      <p className="step-help">Everything you ticked or flagged is below. Send the flags back, approve, or reject.</p>

      <div className="decision-grid">
        <div className="card inner">
          <div className="sub" style={{ marginTop: 0 }}>Progress</div>
          {steps.map((step, index) => (
            <button key={step.id} type="button" className="progress-line" onClick={() => setStepIndex(index)}>
              <span>{step.icon} {step.title}</span>
              <span>{stepStats[index].total ? `${stepStats[index].done}/${stepStats[index].total}` : "—"}{stepStats[index].flagged ? ` · ⚑ ${stepStats[index].flagged}` : ""}</span>
            </button>
          ))}
          {notChecked.length > 0 && (
            <p className="caption" style={{ marginTop: 10 }}>
              {notChecked.length} item{notChecked.length === 1 ? "" : "s"} not ticked yet:{" "}
              {notChecked.slice(0, 6).map((item, position) => (
                <span key={item.key}>
                  {position > 0 && ", "}
                  <a href="#review" onClick={(event) => { event.preventDefault(); setStepIndex(item.step); }}>{item.label}</a>
                </span>
              ))}
              {notChecked.length > 6 && ` and ${notChecked.length - 6} more`}
            </p>
          )}
        </div>

        <div className="card inner">
          <div className="sub" style={{ marginTop: 0 }}>⚠ Need to reconsider</div>
          {aiChecks.length === 0 && <p className="caption">No automatic checks are stored for this submission.</p>}
          {aiChecks.length > 0 && aiBad.length === 0 && <p className="caption">Nothing to reconsider: all {aiChecks.length} checks matched.</p>}
          {aiBad.length > 0 && <AiChecks checks={aiBad} bare />}

          <div className="sub">⚑ Flags to send ({draftList.length})</div>
          {draftList.length === 0 && <p className="caption">No flags written. Flag a field or document in any step.</p>}
          {draftList.length > 0 && (
            <ul className="flag-list">
              {draftList.map(([key, { label, note }]) => (
                <li key={key}><strong>{label}</strong>: {note.trim() || <em>write what is wrong</em>}</li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="actions">
        {draftList.length > 0 && (
          <button
            type="button"
            className="btn send"
            disabled={updating || draftList.some(([, { note }]) => !note.trim())}
            onClick={sendFlags}
          >⚑ Send back to employee ({draftList.length})</button>
        )}
        <button
          type="button"
          className="btn approve"
          disabled={updating || record.application_status === "Approved" || openFlags.length > 0 || draftList.length > 0}
          title={openFlags.length ? "Waiting for the employee to fix the flagged items" : draftList.length ? "Send the flags first" : ""}
          onClick={approve}
        >✓ Approve</button>
        <button type="button" className="btn reject" disabled={updating || record.application_status === "Rejected"} onClick={reject}>✕ Reject</button>
        {record.application_status !== "Pending" && (
          <button type="button" className="btn neutral" disabled={updating} onClick={() => onStatusChange("Pending")}>⏸ Mark as Pending</button>
        )}
        {draftList.length > 0 && <button type="button" className="btn neutral" onClick={() => setDrafts({})}>Discard flags</button>}
        <button type="button" className="btn neutral" onClick={downloadJson}>⬇ Download Record</button>
      </div>

      {detail.application_link && (
        <div className="link-row">
          <span className="info-label">Employee link</span>
          <input readOnly value={detail.application_link} onFocus={(event) => event.target.select()} aria-label="Employee link" />
          <button type="button" className="btn neutral" onClick={copyLink}>{copied ? "Copied" : "Copy link"}</button>
        </div>
      )}

      {(record.flags || []).length > 0 && (
        <>
          <div className="sub">⚑ Flags raised so far</div>
          <DataTable rows={[...record.flags].reverse().map((flag) => ({
            Item: flag.label,
            Note: flag.note,
            Raised: formatDateTime(flag.raised_at),
            Status: flag.status === "open" ? "Waiting for employee" : `Updated ${formatDateTime(flag.resolved_at)}`,
          }))} />
        </>
      )}

      <details>
        <summary>View raw submission JSON</summary>
        <pre>{JSON.stringify(record, null, 4)}</pre>
      </details>
    </div>
  );

  return (
    <ReviewContext.Provider value={reviewContext}>
      <section>
        <div className="card">
          {message && <div className={`alert ${message.type}`} role="status">{message.text}</div>}

          {record.resubmitted_at && record.application_status === "New" && (
            <div className="alert update" role="status">
              🔔 <strong>Updated application.</strong> {personal.name || "The employee"} sent changes on {formatDateTime(record.resubmitted_at)}.
              Re-check the items marked "Employee updated this, please re-check", then approve or flag again.
            </div>
          )}

          <div className="profile">
            {photo
              ? <img className="avatar" src={photo.url} alt="Employee photograph" />
              : <div className="avatar empty">👤</div>}
            <div className="profile-main">
              <h2>{personal.name || "Unnamed"}</h2>
              <div className="profile-meta">
                <span>
                  <strong>{record.employee_id}</strong> · <StatusBadge status={record.application_status} />
                  {record.resubmitted_at && record.application_status === "New" && <span className="tag">Updated application</span>}
                </span>
                <span>Submitted: {formatDateTime(record.submitted_at)}</span>
                {record.resubmitted_at && <span>Sent back by employee: {formatDateTime(record.resubmitted_at)}</span>}
                {record.reviewed_at && <span>Last reviewed: {formatDateTime(record.reviewed_at)}</span>}
              </div>
            </div>
            <div className="profile-progress">
              <div className="progress-top"><strong>{percent}%</strong> reviewed ({totals.done} of {totals.total} ticked{totals.flagged ? `, ${totals.flagged} flagged` : ""})</div>
              <div className="progress-bar"><span style={{ width: `${percent}%` }} /></div>
              {aiBad.length > 0 && (
                <button type="button" className="ai-chip" onClick={() => setStepIndex(aiBad.some((item) => /^(Account|Bank)/i.test(item.check)) ? 4 : 3)}>
                  ⚠ {aiBad.length} item{aiBad.length === 1 ? "" : "s"} need{aiBad.length === 1 ? "s" : ""} to be reconsidered. Look at {aiBad.length === 1 ? "it" : "them"} first
                </button>
              )}
              {openFlags.length > 0 && <span className="flag-chip open">⚑ {openFlags.length} flag{openFlags.length === 1 ? "" : "s"} waiting for the employee</span>}
            </div>
          </div>

          {!detail.can_flag && (
            <p className="caption" style={{ marginTop: 12 }}>
              This application was submitted before flags were added, so fields cannot be flagged for the employee.
            </p>
          )}
        </div>

        <nav className="stepper" aria-label="Review steps">
          {[...steps.map((step, index) => ({ id: step.id, title: step.title, icon: step.icon, stat: stepStats[index] })),
            { id: "decision", title: "Decision", icon: "✅", stat: null }].map((tab, index) => {
            const complete = tab.stat && tab.stat.total > 0 && tab.stat.done === tab.stat.total;
            return (
              <button
                key={tab.id}
                type="button"
                className={"step-tab" + (index === stepIndex ? " active" : "") + (complete ? " complete" : "") + (tab.stat && tab.stat.flagged ? " has-flag" : "")}
                aria-current={index === stepIndex ? "step" : undefined}
                onClick={() => setStepIndex(index)}
              >
                <span className="step-num">{complete ? "✓" : tab.stat && tab.stat.flagged ? "⚑" : index + 1}</span>
                <span className="step-text">
                  <span className="step-title">{tab.title}</span>
                  <span className="step-count">{tab.stat ? (tab.stat.total ? `${tab.stat.done}/${tab.stat.total}` : "nothing to check") : `${notChecked.length} left`}</span>
                </span>
              </button>
            );
          })}
        </nav>

        <div className="card step-card">
          <h2 className="step-heading">{stepIndex === decisionIndex ? "✅ Decision" : `${steps[stepIndex].icon} Step ${stepIndex + 1}: ${steps[stepIndex].title}`}</h2>
          {stepIndex === decisionIndex ? renderDecision() : renderStep(steps[stepIndex], stepIndex)}

          <div className="step-nav">
            <button type="button" className="btn neutral" disabled={stepIndex === 0} onClick={() => setStepIndex(stepIndex - 1)}>← Back</button>
            <span className="caption">Tip: the ← and → keys also change step.</span>
            {stepIndex < decisionIndex && (
              <button type="button" className="btn primary" onClick={() => setStepIndex(stepIndex + 1)}>
                {stepIndex === decisionIndex - 1 ? "Go to decision →" : `Next: ${(steps[stepIndex + 1] || {}).title} →`}
              </button>
            )}
          </div>
        </div>
      </section>
    </ReviewContext.Provider>
  );
}

// ---------------------------------------------------------------
// Main app
// ---------------------------------------------------------------

function App() {
  const [employees, setEmployees] = useState([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState(null);
  const [selectedId, setSelectedId] = useState(() => location.hash.slice(1) || null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(null);
  const [updating, setUpdating] = useState(false);
  const [message, setMessage] = useState(null);
  const [listHidden, setListHidden] = useState(false);

  async function loadEmployees(silent) {
    if (!silent) {
      setLoading(true);
      setListError(null);
    }
    try {
      const data = await request("/api/employees");
      setEmployees(data);
      setSelectedId((current) => current || (data[0] && data[0].employee_id) || null);
    } catch (error) {
      if (!silent) setListError(error.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => { loadEmployees(); }, []);

  // New updates from employees show up without pressing Refresh
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) loadEmployees(true); }, 30000);
    return () => clearInterval(timer);
  }, []);

  const updatedApplications = employees.filter(isUpdated);
  useEffect(() => {
    document.title = updatedApplications.length ? `(${updatedApplications.length}) Updated application · HR Review Portal` : "HR Review Portal";
  }, [updatedApplications.length]);

  useEffect(() => {
    if (!selectedId) return;
    history.replaceState(null, "", `#${selectedId}`);
    setDetail(null);
    setDetailError(null);
    setMessage(null);
    request(`/api/employees/${encodeURIComponent(selectedId)}`)
      .then(setDetail)
      .catch((error) => setDetailError(error.message));
  }, [selectedId]);

  async function changeStatus(status) {
    setUpdating(true);
    setMessage(null);
    try {
      const data = await request(`/api/employees/${encodeURIComponent(selectedId)}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      setDetail(data);
      setEmployees((current) => current.map((employee) =>
        employee.employee_id === selectedId ? { ...employee, application_status: status } : employee
      ));
      setMessage({ type: "success", text: `Status updated to "${status}".` });
    } catch (error) {
      setMessage({ type: "error", text: error.message });
    } finally {
      setUpdating(false);
    }
  }

  // Remember which items HR has ticked as matching, so a review can be continued later
  async function saveVerification(employeeId, keys) {
    try {
      await request(`/api/employees/${encodeURIComponent(employeeId)}/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verified: keys }),
      });
    } catch (error) {
      setMessage({ type: "error", text: `Your ticks could not be saved: ${error.message}` });
    }
  }

  // Send HR's flags to the employee; true when they were saved
  async function requestChanges(flags) {
    setUpdating(true);
    setMessage(null);
    try {
      const data = await request(`/api/employees/${encodeURIComponent(selectedId)}/flags`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flags }),
      });
      setDetail(data);
      setEmployees((current) => current.map((employee) =>
        employee.employee_id === selectedId
          ? { ...employee, application_status: "Changes Requested", open_flags: flags.length }
          : employee
      ));
      setMessage({
        type: "success",
        text: "Sent to the employee. Share the employee link below so they can fix the flagged items.",
      });
      return true;
    } catch (error) {
      setMessage({ type: "error", text: error.message });
      return false;
    } finally {
      setUpdating(false);
    }
  }

  return (
    <div className="page">
      <header className="hero">
        <div>
          <h1>🗂️ HR Review Portal</h1>
          <p>Review each submission step by step, with the details next to the document they come from.</p>
        </div>
        <div className="hero-actions">
          <ThemeToggle onDark />
          <button type="button" className="btn light" onClick={() => loadEmployees()} disabled={loading}>
            ⟳ Refresh
          </button>
        </div>
      </header>

      {listError && <div className="alert error" role="alert">{listError}</div>}

      {updatedApplications.length > 0 && (
        <div className="update-banner" role="status">
          <strong>🔔 Updated application{updatedApplications.length === 1 ? "" : "s"} ({updatedApplications.length})</strong>
          <span>The employee sent changes back after you flagged something:</span>
          {updatedApplications.map((employee) => (
            <button key={employee.employee_id} type="button" onClick={() => setSelectedId(employee.employee_id)}>
              {employee.name || employee.employee_id} · {employee.employee_id}
            </button>
          ))}
        </div>
      )}

      <div className={"layout" + (listHidden ? " collapsed" : "")}>
        <Sidebar
          employees={employees}
          selectedId={selectedId}
          onSelect={setSelectedId}
          loading={loading}
        />

        <main>
          <button type="button" className="btn neutral list-toggle" onClick={() => setListHidden((value) => !value)}>
            {listHidden ? "☰ Show employee list" : "⇤ Hide list for more room"}
          </button>

          {detailError && <div className="alert error" role="alert">{detailError}</div>}

          {!loading && !employees.length && !listError && (
            <div className="card empty-state">
              <div>📭</div>
              <p>No onboarding submissions yet.</p>
            </div>
          )}

          {selectedId && !detail && !detailError && employees.length > 0 && (
            <div className="card empty-state"><p>Loading employee details...</p></div>
          )}

          {detail && (
            <EmployeeDetail
              detail={detail}
              onStatusChange={changeStatus}
              onRequestChanges={requestChanges}
              onSaveVerification={saveVerification}
              updating={updating}
              message={message}
            />
          )}
        </main>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
</script>
</body>
</html>
"""


# ==========================================================
# 5. SERVER
# ==========================================================

class BasicAuth:
    """HTTP Basic login for the whole portal when HR_USER and HR_PASSWORD are set.

    /health stays open for the load balancer's health check. Behind HTTPS only: Basic
    sends the password with every request.
    """

    def __init__(self, app):
        self.app = app
        self.user = os.environ.get("HR_USER", "")
        self.password = os.environ.get("HR_PASSWORD", "")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.user and self.password and scope["path"] != "/health":
            header = dict(scope["headers"]).get(b"authorization", b"").decode()
            try:
                user, _, password = base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")
            except ValueError:
                user = password = ""

            if not (hmac.compare_digest(user, self.user) and hmac.compare_digest(password, self.password)):
                denied = Response(
                    "Login required.", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="HR Review Portal"'},
                )
                return await denied(scope, receive, send)

        await self.app(scope, receive, send)


async def health(request: Request):
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/", index),
        Route("/health", health),
        Route("/api/employees", employees),
        Route("/api/employees/{employee_id}", employee_detail),
        Route("/api/employees/{employee_id}/documents/{document}", employee_document),
        Route("/api/employees/{employee_id}/status", update_status, methods=["POST"]),
        Route("/api/employees/{employee_id}/flags", request_changes, methods=["POST"]),
        Route("/api/employees/{employee_id}/verification", save_verification, methods=["POST"]),
    ],
    middleware=[Middleware(BasicAuth), Middleware(GZipMiddleware, minimum_size=1024)],
)


if __name__ == "__main__":
    print(f"HR Review Portal running at http://localhost:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
