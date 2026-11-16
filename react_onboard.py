"""
Employee Onboarding Portal: React frontend served by a Starlette backend.

Same features as app_2.py (AI document extraction, validation, JSON
submissions), with the UI rewritten as React components.

Run:
    python react_onboard.py
Then open http://localhost:8000
"""

import datetime
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import base64
import copy
import hmac
import os
import secrets
import time
import http.client
import threading
import urllib.parse

import pypdfium2
import uvicorn
from PIL import Image
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import Bank_details as bank
import policies
import theme
import storage


# ==========================================================
# 1. APPLICATION CONFIGURATION
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger("onboarding")
# Records, the form as submitted (so an employee can reopen it when HR flags something) and
# uploaded documents live in storage.py: local folders, or S3 with STORAGE_BACKEND=s3
storage.check_config()  # refuse to start on an unsafe S3 setup (see storage.py)

# Application statuses set here; HR sets Approved / Rejected / Pending in hr_info.py
STATUS_NEW = "New"
STATUS_CHANGES = "Changes Requested"


def waiting_for_employee(record):
    """HR is waiting for this employee: the flags were sent back, or HR parked the application
    as Pending while flags are still open (the employee link has to keep working for both)."""

    status = record.get("application_status")
    if status == STATUS_CHANGES:
        return True
    return status == "Pending" and any(flag.get("status") == "open" for flag in record.get("flags", []))

EMPLOYEE_ID_PATTERN = re.compile(r"IGTPL\d{3,}|EMP-[0-9A-F]{8}")

# Uploaded document form key -> (label, file type), saved as uploads/<employee_id>/<key>.<type>
DOCUMENTS = {
    "aadhaar_front": ("Aadhaar Card (Front)", "jpg"),
    "aadhaar_back": ("Aadhaar Card (Back)", "jpg"),
    "pan": ("PAN Card (Front)", "jpg"),
    "photo": ("Profile Photograph", "jpg"),
    "education_certificate": ("Highest Education Certificate", "pdf"),
}

# Optional uploads: saved with the others when sent, but not required
OPTIONAL_DOCUMENTS = {
    "resume": ("Resume", "pdf"),
}

# Resume text sent to the AI model (a long resume is cut here; about 2,000 tokens)
MAX_RESUME_CHARS = 8000

# Leading bytes of each file type, so a renamed file of the wrong type is rejected
FILE_SIGNATURES = {"jpg": b"\xff\xd8\xff", "pdf": b"%PDF-"}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Employee IDs run IGTPL001, IGTPL002, ... (more digits once past 999)
EMPLOYEE_ID_PREFIX = "IGTPL"

def load_env_file(path):
    """Put KEY=VALUE lines of a .env file into os.environ (real environment variables win)."""

    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(BASE_DIR / ".env")

# The AI model runs on the Gemini API; GEMINI_API_KEY and GEMMA_MODEL come from .env
# The model id can be changed with GEMMA_MODEL; the default is below
GEMMA_MODEL = os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RETRIES = 4
GEMINI_HOST = urllib.parse.urlsplit(GEMINI_URL).netloc
GEMINI_TIMEOUT = 45  # a call that hangs is retried sooner than the old 120s wait
_connections = threading.local()

HOST = os.environ.get("HOST", "127.0.0.1")  # 0.0.0.0 inside a container
PORT = int(os.environ.get("PORT", "8000"))

FAMILY_COLUMNS = ["Name", "Age/Gender", "Relation", "Occupation"]

EDUCATION_COLUMNS = [
    "Qualification",
    "University/Institute",
    "Year of Passing",
    "% Marks",
    "Major Subject",
]

# The education table always has exactly these rows, in this order
EDUCATION_ROWS = ("10th", "12th", "Degree", "Certification")

# Certificates rarely print marks; the certificate name goes in Major Subject
CERTIFICATION_OPTIONAL_COLUMNS = ("% Marks",)

# Form label -> key in the submitted data (checked in this order)
REQUIRED_FIELDS = {
    "Full Name": "name",
    "Father's Name": "father_name",
    "Date of Birth": "dob",
    "Age": "age",
    "PAN Number": "pan_number",
    "Aadhaar Number": "aadhaar_number",
    "Mobile Number": "mobile",
    "Permanent Address": "permanent_address",
    "Current Address": "current_address",
    "Emergency Contact Name": "emergency_name",
    "Emergency Contact Relation": "emergency_relation",
    "Emergency Contact Phone": "emergency_phone",
    "Personal Email": "email",
    "Languages": "languages",
    "Hobbies": "hobbies",
    "Family Details": "family",
    "Education Details": "education",
    "Total Experience": "total_experience",
    "Organisation": "organisation",
    "Period From": "period_from",
    "Period To": "period_to",
    "Designation at Joining": "joining_designation",
    "Last Position Held": "last_designation",
    "Gross Salary at Joining": "joining_salary",
    "Gross Salary Last Drawn": "last_salary",
    "Job Responsibility": "job_responsibility",
    "Senior 1 Name": "ref1_name",
    "Senior 1 Designation": "ref1_designation",
    "Senior 1 Contact": "ref1_contact",
    "Senior 2 Name": "ref2_name",
    "Senior 2 Designation": "ref2_designation",
    "Senior 2 Contact": "ref2_contact",
}

# Fields the employee may leave blank (still saved, as empty text)
OPTIONAL_FIELDS = ("passport_number", "reason_for_leaving")

# "To" date of the employment period for someone still working there
PRESENT = "Present"

# Phone field -> label; each also has a "<key>_code" country dial code (default +91)
PHONE_FIELDS = {
    "mobile": "Mobile number",
    "emergency_phone": "Emergency contact phone",
    "ref1_contact": "Senior 1 contact number",
    "ref2_contact": "Senior 2 contact number",
}

DEFAULT_DIAL_CODE = "+91"

PAN_PATTERN = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
AADHAAR_PATTERN = re.compile(r"\d{12}")

# Accepted values for "Languages Known" (also sent to the page for suggestions)
LANGUAGES = sorted([
    # Indian languages
    "Assamese", "Bengali", "Bhojpuri", "Bodo", "Dogri", "English", "Gujarati",
    "Haryanvi", "Hindi", "Kannada", "Kashmiri", "Konkani", "Maithili", "Malayalam",
    "Manipuri", "Marathi", "Marwari", "Nepali", "Odia", "Punjabi", "Rajasthani",
    "Sanskrit", "Santali", "Sindhi", "Tamil", "Telugu", "Tulu", "Urdu",
    # Other languages
    "Arabic", "Burmese", "Cantonese", "Czech", "Danish", "Dutch", "Filipino",
    "Finnish", "French", "German", "Greek", "Hebrew", "Hungarian", "Indonesian",
    "Italian", "Japanese", "Korean", "Malay", "Mandarin", "Norwegian", "Pashto",
    "Persian", "Polish", "Portuguese", "Romanian", "Russian", "Sinhala", "Spanish",
    "Swahili", "Swedish", "Thai", "Tibetan", "Turkish", "Ukrainian", "Vietnamese",
])


# ==========================================================
# 2. DATE AND AGE FUNCTIONS
# ==========================================================

def parse_date(text):
    """A DD/MM/YYYY string as a date, or None if it is not a real date in that format."""

    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", (text or "").strip()):
        return None

    try:
        return datetime.datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def calculate_age(dob):

    birth_date = parse_date(dob)

    if not birth_date:
        return 0

    today = datetime.date.today()

    return (
        today.year - birth_date.year
        - ((today.month, today.day) < (birth_date.month, birth_date.day))
    )


# ==========================================================
# 3. AI DOCUMENT EXTRACTION
# ==========================================================

def shrink_image(image_data):

    # Shrink large photos: fewer image tokens = much faster on CPU
    image = Image.open(io.BytesIO(image_data)).convert("RGB")
    image.thumbnail((1024, 1024))  #resize into frame
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def extract_document_details(images, document_type):

    if document_type == "PAN":
        fields = {"name": "", "father_name": "", "dob": "", "pan_number": ""}
        sides = "the front side of the PAN card"
    else:
        fields = {"name": "", "dob": "", "gender": "", "address": "", "aadhaar_number": "", "mobile": ""}
        sides = (
            "the front side (name, DOB) and the back side (address) "
            "of the Aadhaar card"
        )

    prompt = f"""
    You are an identity document information extraction system.

    The images show {sides}. Read them carefully.

    Extract the following information:

    {json.dumps(fields)}

    Rules:

    1. Extract only information visible in the document.
    2. Never invent or guess information.
    3. Use an empty string if information is unavailable.
    4. Preserve original name spelling.
    5. Return DOB in DD/MM/YYYY format.
    6. Extract the complete visible Aadhaar address.
    7. pan_number is the 10-character Permanent Account Number, for example ABCDE1234F.
    8. aadhaar_number is the 12-digit Aadhaar number, not the 16-digit VID.
    9. mobile is the mobile number only if one is printed on the card.
    10. gender is "Male", "Female" or "Other", only if printed on the card (the Aadhaar
        front shows it next to the date of birth); empty for a PAN card.
    11. Do not include explanations.
    12. Return valid JSON only.

    Expected output:
            # converts the Python dictionary into JSON text.
    {json.dumps(fields)}
    """

    return ask_model(prompt, images, fields)


def gemini_generate(prompt, images=(), schema=None):
    """Send the prompt and JPEG images to the AI model on the Gemini API; return its JSON reply text."""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    # Minimal thinking: the model otherwise spends seconds on hidden reasoning tokens
    config = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingLevel": "minimal"},
    }
    if schema:
        config["responseSchema"] = schema

    parts = [{"text": prompt}] + [
        {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode()}}
        for image in images
    ]

    body = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": config}).encode()
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    path = urllib.parse.urlsplit(GEMINI_URL.format(model=GEMMA_MODEL)).path

    for attempt in range(GEMINI_RETRIES):
        # A kept-alive connection skips the TCP + TLS handshake on every call after the first
        connection = getattr(_connections, "gemini", None)
        if connection is None:
            connection = _connections.gemini = http.client.HTTPSConnection(
                GEMINI_HOST, timeout=GEMINI_TIMEOUT
            )

        try:
            connection.request("POST", path, body, headers)
            response = connection.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException):
            # Stale connection or a hung call: drop it and try again on a fresh one
            connection.close()
            _connections.gemini = None
            if attempt == GEMINI_RETRIES - 1:
                raise
            continue

        if response.status == 200:
            reply = json.loads(raw)
            break

        # The API sometimes answers 500/503/429 for a call that works on retry
        if response.status not in (429, 500, 502, 503) or attempt == GEMINI_RETRIES - 1:
            raise RuntimeError(f"Gemini API error {response.status}: {raw.decode(errors='replace')[:300]}")
        time.sleep(2 ** attempt)

    # The model may put its reasoning in "thought" parts ahead of the answer
    parts = reply["candidates"][0]["content"]["parts"]
    return "".join(part.get("text", "") for part in parts if not part.get("thought"))


def ask_model(prompt, images, fields):
    """Send the prompt and images to the model; return `fields` filled from its JSON reply."""

    extracted_data = json.loads(
        gemini_generate(prompt, [shrink_image(image) for image in images])
    )

    # The model occasionally wraps the object in a list
    if isinstance(extracted_data, list):
        extracted_data = next((item for item in extracted_data if isinstance(item, dict)), {})
    if not isinstance(extracted_data, dict):
        extracted_data = {}

    cleaned = {}
    for field in fields:
        value = extracted_data.get(field, "")
        value = value.strip() if isinstance(value, str) else ""
        cleaned[field] = "" if value.lower() == "null" else value

    return cleaned


def extract_employee_information(pan_bytes, aadhaar_front_bytes, aadhaar_back_bytes):

    # The two API calls are independent, so run them side by side
    with ThreadPoolExecutor(max_workers=2) as pool:
        pan_future = pool.submit(extract_document_details, [pan_bytes], "PAN")
        aadhaar_future = pool.submit(
            extract_document_details, [aadhaar_front_bytes, aadhaar_back_bytes], "AADHAAR"
        )
        pan_data = pan_future.result()
        aadhaar_data = aadhaar_future.result()

    # Keep the numbers only when they have the right shape, so a misread is left blank
    pan_number = re.sub(r"\s", "", pan_data.get("pan_number", "")).upper()
    aadhaar_number = re.sub(r"\D", "", aadhaar_data.get("aadhaar_number", ""))
    mobile = re.sub(r"\D", "", aadhaar_data.get("mobile", ""))[-10:]

    gender = aadhaar_data.get("gender", "").strip().capitalize()
    gender = {"M": "Male", "F": "Female", "Transgender": "Other"}.get(gender, gender)

    return {
        "name": pan_data.get("name") or aadhaar_data.get("name", ""),
        "father_name": pan_data.get("father_name", ""),
        "gender": gender if gender in ("Male", "Female", "Other") else "",
        "dob": pan_data.get("dob") or aadhaar_data.get("dob", ""),
        "address": aadhaar_data.get("address", ""),
        "pan_number": pan_number if PAN_PATTERN.fullmatch(pan_number) else "",
        "aadhaar_number": aadhaar_number if AADHAAR_PATTERN.fullmatch(aadhaar_number) else "",
        "mobile": mobile if re.fullmatch(r"[6-9]\d{9}", mobile) else "",
    }


# pdfium is not thread-safe: two PDFs opened at once (the bank step reads the payslip and the
# relieving letter together) corrupt its heap and kill the whole server, so one thread at a time
PDFIUM_LOCK = threading.Lock()


def pdf_to_images(pdf_bytes, max_pages=2):
    """Render the first pages of a PDF as JPEG bytes (works for scanned certificates too)."""

    images = []

    with PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf_bytes)

        try:
            for index in range(min(len(document), max_pages)):
                image = document[index].render(scale=2).to_pil().convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=90)
                images.append(buffer.getvalue())
        finally:
            document.close()

    return images


def normalise_degree(degree):
    """Spell B.Tech / B.E. the same way whatever the certificate prints."""

    compact = re.sub(r"[\s.]", "", degree).lower()

    if compact in ("btech", "bacheloroftechnology"):
        return "B.Tech"

    if compact in ("be", "bachelorofengineering"):
        return "B.E."

    return degree


def extract_education_details(pdf_bytes):
    """Read the certificate PDF; the result says which education row it belongs to."""

    fields = {
        "document_type": "",
        "certificate_name": "",
        "degree": "",
        "institute": "",
        "year": "",
        "marks": "",
        "major_subject": "",
    }

    prompt = f"""
    You are an education document information extraction system.

    The images show the pages of an uploaded education document. Read them carefully.

    Extract the following information:

    {json.dumps(fields)}

    Field meanings:

    - document_type: "degree" if this is a university degree certificate or
      final marksheet (for example B.Tech, B.E., B.Sc, B.Com, BCA).
      "certification" if it is a course or professional certificate (for example
      AWS, Microsoft, Google, NPTEL, Coursera, Udemy, a diploma or training course).
    - certificate_name: for a certification, the subject or name of the course,
      including the issuing technology or company when printed, for example
      "Data Science", "Machine Learning", "NVIDIA Deep Learning Fundamentals" or
      "AWS Certified Cloud Practitioner". Empty for a degree.
    - degree: for a degree, the short degree name, for example "B.Tech" or "B.E.".
      Empty for a certification.
    - institute: the university, institute or organisation that issued it.
    - year: the 4-digit year it was completed or issued.
    - marks: the percentage as a number without the % sign, for example "78.5".
      If only a CGPA/CPI is printed, return it like "8.2 CGPA". Empty if not printed.
    - major_subject: the main subject or branch, for example "Computer Science".

    Rules:

    1. Extract only information visible in the document.
    2. Never invent or guess information.
    3. Use an empty string if information is unavailable.
    4. Do not include explanations.
    5. Return valid JSON only.
    """

    result = ask_model(prompt, pdf_to_images(pdf_bytes), fields)

    is_certification = result["document_type"].lower() == "certification"

    if is_certification:
        return {
            "level": "Certification",
            "degree": "",
            "institute": result["institute"],
            "year": result["year"],
            "marks": result["marks"],
            # The certificate name (Data Science, AWS ...) is the major subject
            "major_subject": result["certificate_name"] or result["major_subject"],
        }

    return {
        "level": "Degree",
        "degree": normalise_degree(result["degree"]),
        "institute": result["institute"],
        "year": result["year"],
        "marks": result["marks"],
        "major_subject": result["major_subject"],
    }


def pdf_text(pdf_bytes, max_pages=4):
    """The text layer of the first pages of a PDF ("" for a scanned PDF)."""

    pages = []

    with PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf_bytes)

        try:
            for index in range(min(len(document), max_pages)):
                textpage = document[index].get_textpage()
                pages.append(textpage.get_text_range())
                textpage.close()
        finally:
            document.close()

    return "\n".join(pages).strip()


def resume_date(text):
    """DD/MM/YYYY from "DD/MM/YYYY" or "MM/YYYY" (the 1st of the month); "" unless a real past date."""

    text = text.strip()

    if re.fullmatch(r"\d{1,2}/\d{4}", text):
        text = "01/" + text.zfill(7)

    date = parse_date(text)

    return text if date and date <= datetime.date.today() else ""


# Short education codes in the model's reply -> rows of the education table
RESUME_LEVEL_CODES = {
    "10": "10th", "12": "12th", "UG": "Graduation", "PG": "Post Graduation", "CERT": "Certification",
}

# The model's reply is held to this shape by Ollama itself, so the prompt need not spell it
# out. Keys are short and each education entry is a list: every output token costs time.
TEXT = {"type": "string"}
RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "name": TEXT, "dob": TEXT, "gender": TEXT, "phone": TEXT, "email": TEXT,
        "langs": {"type": "array", "items": TEXT},
        "years": TEXT,
        "jobs": {"type": "array", "items": {"type": "array", "items": TEXT}},
        "work": TEXT,
        "edu": {"type": "array", "items": {"type": "array", "items": TEXT}},
    },
    "required": ["name", "dob", "gender", "phone", "email", "langs", "years", "jobs", "work", "edu"],
}

# Fixed text first: Ollama caches it, so after the first resume only the resume text and
# the reply cost time. The reply is the slow part, so the prompt asks for short values.
RESUME_PROMPT = """Extract details from the resume below. Use "" when something is not in the resume. Never guess.
- dob: date of birth, DD/MM/YYYY.
- gender: Male, Female or Other.
- phone, email: the candidate's own.
- langs: spoken languages, e.g. ["English","Hindi"]. Not programming languages.
- years: total years of work experience if the resume states it, e.g. "5".
- jobs: one list per role, copied as written: [org, from, to, title]. from/to: MM/YYYY; to is "" if current.
- work: what they do in their most recent role, at most 15 words.
- edu: one list per qualification, including certifications: [level, name, institute, year, marks, subject].
  level: "10" (Class X/SSC), "12" (Class XII/HSC), "UG" (bachelor's), "PG" (master's) or "CERT" (certification).
  name: "" for 10 and 12; else short, e.g. "B.Tech", "MBA" or the certification name.
  marks: e.g. "86" or "8.1 CGPA". subject: branch, e.g. "Computer Science", else "".
Reply with minified JSON on one line, no spaces or line breaks between items.
"""


def extract_resume_details(pdf_bytes):
    """Read a resume PDF and return the form values it holds (blank when not found)."""

    # Squeeze out layout whitespace: fewer prompt tokens for the same text
    text = re.sub(r"[ \t]+", " ", pdf_text(pdf_bytes))
    text = re.sub(r"\s*\n\s*", "\n", text).strip()[:MAX_RESUME_CHARS]

    # A scanned resume has no text layer, so the model reads the page images instead
    images = [] if text else pdf_to_images(pdf_bytes, max_pages=3)

    data = json.loads(gemini_generate(
        RESUME_PROMPT + (f"Resume:\n{text}" if text else "The images are the resume pages."),
        [shrink_image(image) for image in images],
        schema=RESUME_SCHEMA,
    ))

    return clean_resume(data if isinstance(data, dict) else {})


def clean_resume(data):
    """Keep only well-formed values from the model's reply, in the form's own formats."""

    def text(value):
        # Through the Gemini API the model writes "null" for a missing value
        value = value.strip() if isinstance(value, str) else ""
        return "" if value.lower() == "null" else value

    mobile = re.sub(r"\D", "", text(data.get("phone")))
    mobile = mobile[-10:] if len(mobile) >= 10 else ""

    gender = text(data.get("gender")).capitalize()

    languages, _ = normalise_languages(
        ",".join(item for item in data.get("langs") or [] if isinstance(item, str))
    )

    job = latest_job(data.get("jobs"))
    # Years stated on the resume win over the ones worked out from the job dates
    worked_out = job.pop("years", "")
    experience = re.sub(r"\D", "", text(data.get("years")))[:2] or worked_out

    education = []
    for entry in data.get("edu") or []:
        if not isinstance(entry, list):
            continue
        code, name, institute, year, marks, subject = ([text(value) for value in entry] + [""] * 6)[:6]
        level = RESUME_LEVEL_CODES.get(code.upper().removesuffix("TH"))
        if level and (name or institute or year):
            education.append({
                "level": level,
                # The 10th / 12th rows are named by the level alone
                "qualification": "" if level in ("10th", "12th") else normalise_degree(name),
                "institute": institute,
                "year": re.sub(r"\D", "", year)[:4],
                "marks": marks.rstrip("%").strip() if re.search(r"\d", marks) else "",
                "major_subject": subject,
            })

    email = text(data.get("email"))

    return {
        "name": text(data.get("name")),
        "dob": resume_date(text(data.get("dob"))),
        "gender": gender if gender in ("Male", "Female", "Other") else "",
        "mobile": mobile if re.fullmatch(r"[6-9]\d{9}", mobile) else "",
        "email": email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else "",
        "languages": ", ".join(languages),
        "total_experience": experience,
        **job,
        "job_responsibility": text(data.get("work")),
        "education": education,
    }


def latest_job(jobs):
    """Experience fields from the role list [org, from, to, title]: the latest employer,
    when the candidate joined it (all roles there count, so a promotion is not a new job),
    the first and last title there, and total years worked from the dates."""

    today = datetime.date.today()
    roles = []

    for role in jobs if isinstance(jobs, list) else []:
        if not isinstance(role, list):
            continue
        org, start, end, title = ([value.strip() if isinstance(value, str) else "" for value in role] + [""] * 4)[:4]
        start = resume_date(start)
        if org and start:
            end = resume_date(end)
            roles.append({
                "org": org, "title": title, "from": start, "to": end,
                "start": parse_date(start), "end": parse_date(end) or today,
            })

    if not roles:
        return {"organisation": "", "period_from": "", "period_to": "",
                "joining_designation": "", "last_designation": ""}

    company = lambda name: re.sub(r"\W|pvt|private|ltd|limited", "", name.lower())
    latest = max(roles, key=lambda role: (role["end"], role["start"]))
    there = sorted((role for role in roles if company(role["org"]) == company(latest["org"])),
                   key=lambda role: role["start"])
    worked_since = min(role["start"] for role in roles)

    return {
        "organisation": latest["org"],
        "period_from": there[0]["from"],
        "period_to": latest["to"] or PRESENT,
        "joining_designation": there[0]["title"],
        "last_designation": latest["title"] or there[0]["title"],
        "years": str((latest["end"] - worked_since).days // 365),
    }


# ==========================================================
# 4. VALIDATION AND RECORD BUILDING
# ==========================================================

def clean_rows(rows, columns, ignore=()):
    """Trim table rows and drop the ones left empty (ignoring `ignore` columns)."""

    cleaned = []

    for row in rows or []:

        row = {column: str(row.get(column) or "").strip() for column in columns}

        if any(row[column] for column in columns if column not in ignore):
            cleaned.append(row)

    return cleaned


def validate_document(key, content):
    """Return an error message for one uploaded document, or None if it is fine."""

    return check_file(*DOCUMENTS[key], content)


def check_file(label, file_type, content):

    if len(content) > MAX_UPLOAD_BYTES:
        return f"{label} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."

    if not content.startswith(FILE_SIGNATURES[file_type]):
        kind = "a PDF file" if file_type == "pdf" else "a JPG/JPEG image"
        return f"{label} must be {kind}."

    return None


def validate(data, family, education, documents):
    """Return the first error message, or None if the submission is valid."""

    values = {
        **data,
        "family": "filled" if family else "",
        "education": "filled" if education else "",
    }

    missing_fields = [
        label
        for label, key in REQUIRED_FIELDS.items()
        if not str(values.get(key) or "").strip()
    ]

    if missing_fields:
        return "Please complete: " + ", ".join(missing_fields)

    if any(not all(row.values()) for row in family):
        return "Please complete every column for each family member row you have filled."

    for row in education:
        # A certification often has no marks or major subject, so those are optional there
        optional = (
            CERTIFICATION_OPTIONAL_COLUMNS
            if row["Qualification"].lower().startswith("certification")
            else ()
        )
        if any(not value for column, value in row.items() if column not in optional):
            return (
                f"Please complete every column for the {row['Qualification'] or 'education'} row."
            )

    if data.get("gender") not in ("Male", "Female", "Other"):
        return "Please select your gender."

    if not PAN_PATTERN.fullmatch(data["pan_number"].strip().upper()):
        return "Enter a valid PAN number, for example ABCDE1234F."

    if not AADHAAR_PATTERN.fullmatch(re.sub(r"\s", "", data["aadhaar_number"])):
        return "Enter a valid 12-digit Aadhaar number."

    error = policies.validate_policies(data)
    if error:
        return error

    if not data.get("declaration"):
        return "Please accept the employee declaration."

    missing_documents = [
        label for key, (label, _) in DOCUMENTS.items() if not documents.get(key)
    ]

    if missing_documents:
        return "Please upload: " + ", ".join(missing_documents)

    for key, content in documents.items():
        error = validate_document(key, content)
        if error:
            return error

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", data["email"].strip()):
        return "Please enter a valid email address."

    for key, label in PHONE_FIELDS.items():
        error = validate_phone(data.get(f"{key}_code"), data[key], label)
        if error:
            return error

    _, unknown_languages = normalise_languages(data["languages"])

    if unknown_languages:
        return (
            "Languages Known must be languages only. Not recognised: "
            + ", ".join(unknown_languages)
        )

    # Dates
    today = datetime.date.today()
    birth_date = parse_date(data["dob"])

    if not birth_date or birth_date >= today:
        return "Enter a valid Date of Birth in DD/MM/YYYY format."

    period_from = parse_date(data["period_from"])
    is_present = data["period_to"].strip().lower() == PRESENT.lower()
    period_to = today if is_present else parse_date(data["period_to"])

    if not period_from or period_from > today:
        return "Enter a valid employment period 'From' date in DD/MM/YYYY format."

    if not period_to or period_to > today:
        return "Enter a valid employment period 'To' date in DD/MM/YYYY format, or choose Present."

    if period_to < period_from:
        return "Employment period 'To' date cannot be before the 'From' date."

    if period_from <= birth_date:
        return "Employment period 'From' date must be after your Date of Birth."

    # Whole numbers only (no decimals, signs or letters)
    if not re.fullmatch(r"\d{1,3}", data["age"].strip()):
        return "Age must be a whole number, for example 28."

    if not re.fullmatch(r"\d{1,2}", data["total_experience"].strip()):
        return "Total Experience must be a whole number of years, for example 5."

    actual_age = calculate_age(data["dob"])

    if actual_age != int(data["age"]):
        return f"Age does not match DOB. Calculated age is {actual_age}."

    if int(data["total_experience"]) >= actual_age:
        return "Total Experience cannot be more than your age."

    return None


def validate_phone(code, number, label):
    """Return an error message for a dial code + number, or None if they are fine."""

    code = (code or DEFAULT_DIAL_CODE).strip()
    number = re.sub(r"[\s-]", "", number or "")

    if not re.fullmatch(r"\+\d{1,4}", code):
        return f"{label}: please choose a valid country code."

    if code == "+91" and not re.fullmatch(r"[6-9]\d{9}", number):
        return f"{label}: enter a valid 10-digit Indian mobile number."

    if not re.fullmatch(r"\d{6,14}", number):
        return f"{label}: enter a valid phone number (digits only)."

    return None


def normalise_languages(text):
    """Split "english, Hindi" into (["English", "Hindi"], unknown entries)."""

    canonical = {language.lower(): language for language in LANGUAGES}
    known, unknown = [], []

    for entry in (part.strip() for part in (text or "").split(",")):
        if not entry:
            continue
        language = canonical.get(entry.lower())
        if language is None:
            unknown.append(entry)
        elif language not in known:
            known.append(language)

    return known, unknown


def next_employee_id():
    """One more than the highest IGTPL number already saved (reserved in S3, so servers never clash)."""

    pattern = re.compile(rf"{EMPLOYEE_ID_PREFIX}(\d+)")

    def numbers(ids):
        return [int(match.group(1)) for employee_id in ids if (match := pattern.fullmatch(employee_id))]

    return storage.get_store().claim_id(lambda number: f"{EMPLOYEE_ID_PREFIX}{number:03d}", numbers)


def build_record(data, family, education, employee_id=None):

    employee_id = employee_id or next_employee_id()

    return {
        "employee_id": employee_id,

        "personal_information": {
            "name": data["name"],
            "father_name": data["father_name"],
            "date_of_birth": data["dob"],
            "age": calculate_age(data["dob"]),
            "gender": data["gender"],
            "pan_number": data["pan_number"].strip().upper(),
            "aadhaar_number": re.sub(r"\s", "", data["aadhaar_number"]),
            "mobile": data["mobile"],
            "passport_number": data["passport_number"],
            "email": data["email"],
            "emergency_contact": {
                "name": data["emergency_name"],
                "relation": data["emergency_relation"],
                "phone": data["emergency_phone"],
            },
            "languages": data["languages"],
            "hobbies": data["hobbies"],
        },

        "address": {
            "permanent_address": data["permanent_address"],
            "current_address": data["current_address"],
        },

        "family_details": family,

        "education_details": education,

        "professional_details": {
            "total_experience": int(data["total_experience"]),
            "last_organization": data["organisation"],
            "last_designation": data["last_designation"],
            "employment_history": {
                "organisation": data["organisation"],
                "period": {
                    "from": data["period_from"],
                    "to": data["period_to"],
                },
                "designation": {
                    "at_joining": data["joining_designation"],
                    "last_position_held": data["last_designation"],
                },
                "gross_salary_drawn": {
                    "at_joining": data["joining_salary"],
                    "last_drawn": data["last_salary"],
                },
                "job_responsibility": data["job_responsibility"],
                "reason_for_leaving": data["reason_for_leaving"],
            },
        },

        "references": {
            "senior_1": {
                "name": data["ref1_name"],
                "designation": data["ref1_designation"],
                "contact": data["ref1_contact"],
            },
            "senior_2": {
                "name": data["ref2_name"],
                "designation": data["ref2_designation"],
                "contact": data["ref2_contact"],
            },
        },

        "policies": policies.build_policy_record(data),

        "application_status": STATUS_NEW,

        "submitted_at": datetime.datetime.now().isoformat(),
    }


# ==========================================================
# 5. API ENDPOINTS
# ==========================================================

def uploaded(form, name):
    """Return the uploaded file for `name`, or None if nothing was sent."""

    file = form.get(name)
    return file if hasattr(file, "read") and file.filename else None


async def index(request: Request):
    labels = {key: label for label, key in REQUIRED_FIELDS.items()}
    page = bank.inject(
        PAGE.replace("__LANGUAGES__", json.dumps(LANGUAGES)).replace("__FIELD_LABELS__", json.dumps(labels))
    )
    return HTMLResponse(theme.inject(policies.inject(page, json.dumps(policies.PAYLOAD))))


async def health(request: Request):
    """The load balancer's health check."""
    return JSONResponse({"ok": True})


async def extract_bank(request: Request):

    form = await request.form()
    keys = {"bank_proof": ("Bank Proof", "jpg"), "payslip": ("Payslip", "pdf"),
            "relieving_letter": ("Relieving Letter", "pdf")}
    files = [uploaded(form, key) for key in keys]

    # Every bank document is optional; read whichever were uploaded
    if not any(files):
        return JSONResponse(
            {"error": "Please upload at least one of: Bank Proof, Payslip, Relieving Letter."},
            status_code=400,
        )

    contents = [await file.read() if file else None for file in files]

    for (label, file_type), content in zip(keys.values(), contents):
        error = content and check_file(label, file_type, content)
        if error:
            return JSONResponse({"error": error}, status_code=400)

    try:
        identity = json.loads(form.get("identity") or "{}")
        result = await run_in_threadpool(
            bank.extract_bank_documents, ask_model, pdf_to_images, *contents, identity
        )
    except Exception:
        log.exception("Bank document extraction failed")
        return JSONResponse(
            {"error": "Intensity could not read the bank documents right now. Please try again, or fill the details by hand."},
            status_code=500,
        )

    return JSONResponse(result)


async def extract(request: Request):

    form = await request.form()
    keys = ("pan", "aadhaar_front", "aadhaar_back")
    files = [uploaded(form, key) for key in keys]

    if not all(files):
        return JSONResponse(
            {"error": "Please upload Aadhaar (front and back) and PAN card."},
            status_code=400,
        )

    contents = [await file.read() for file in files]

    for key, content in zip(keys, contents):
        error = validate_document(key, content)
        if error:
            return JSONResponse({"error": error}, status_code=400)

    try:
        result = await run_in_threadpool(extract_employee_information, *contents)
    except Exception:
        log.exception("Document extraction failed")
        return JSONResponse(
            {"error": "Intensity could not read your documents right now. Please try again, or fill the details by hand."},
            status_code=500,
        )

    return JSONResponse(result)


async def extract_education(request: Request):

    form = await request.form()
    file = uploaded(form, "education_certificate")

    if not file:
        return JSONResponse(
            {"error": "Please upload your highest education certificate (PDF)."},
            status_code=400,
        )

    content = await file.read()
    error = validate_document("education_certificate", content)

    if error:
        return JSONResponse({"error": error}, status_code=400)

    try:
        result = await run_in_threadpool(extract_education_details, content)
    except Exception:
        log.exception("Certificate extraction failed")
        return JSONResponse(
            {"error": "Intensity could not read the certificate right now. Please try again, or fill the row by hand."},
            status_code=500,
        )

    return JSONResponse(result)


async def extract_resume(request: Request):

    form = await request.form()
    file = uploaded(form, "resume")

    if not file:
        return JSONResponse({"error": "Please upload your resume (PDF)."}, status_code=400)

    content = await file.read()
    error = check_file(*OPTIONAL_DOCUMENTS["resume"], content)

    if error:
        return JSONResponse({"error": error}, status_code=400)

    try:
        result = await run_in_threadpool(extract_resume_details, content)
    except Exception:
        log.exception("Resume extraction failed")
        return JSONResponse(
            {"error": "Intensity could not read the resume right now. Please try again, or fill the details by hand."},
            status_code=500,
        )

    return JSONResponse(result)


async def submit(request: Request):

    form = await request.form()

    try:
        data = json.loads(form.get("data") or "{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid form data."}, status_code=400)

    # Normalise text fields so validation and the saved record see trimmed-safe strings
    # (0 stays "0": a fresher's total experience is a valid answer)
    for key in (*REQUIRED_FIELDS.values(), *OPTIONAL_FIELDS):
        if key not in ("family", "education"):
            value = data.get(key)
            data[key] = "" if value is None else str(value)

    if data.get("same_address"):
        data["current_address"] = data["permanent_address"]

    # An employee sending back an application HR flagged: same ID, same private link
    existing = None
    if form.get("employee_id"):
        existing = load_application(form.get("employee_id"), form.get("token"))
        if existing is None:
            return JSONResponse({"error": "This application link is not valid."}, status_code=403)
        if not waiting_for_employee(existing):
            already_sent = existing.get("application_status") == STATUS_NEW and existing.get("resubmitted_at")
            message = (
                "Your updated application has already been sent to HR. They will review it."
                if already_sent else "HR is not waiting for changes on this application."
            )
            return JSONResponse({"error": message}, status_code=409)

    snapshot = copy.deepcopy(data)

    family = clean_rows(data.get("family"), FAMILY_COLUMNS)
    education = clean_rows(
        data.get("education"), EDUCATION_COLUMNS, ignore=("Qualification",)
    )

    documents = {}
    for key in DOCUMENTS:
        file = uploaded(form, key)
        documents[key] = await file.read() if file else None

    optional_documents = {}
    for key in OPTIONAL_DOCUMENTS:
        file = uploaded(form, key)
        if file:
            optional_documents[key] = await file.read()

    bank_files, payslip_count = await bank.read_uploads(form, uploaded)

    error = (
        validate(data, family, education, documents)
        or next(
            filter(None, (check_file(*OPTIONAL_DOCUMENTS[key], content)
                          for key, content in optional_documents.items())),
            None,
        )
        or bank.validate_bank(data)
        or bank.validate_uploads(bank_files, payslip_count, check_file)
    )

    if error:
        return JSONResponse({"error": error}, status_code=400)

    # Save phones as "+91 9876543210" and languages with their proper spelling
    for key in PHONE_FIELDS:
        code = (data.get(f"{key}_code") or DEFAULT_DIAL_CODE).strip()
        data[key] = f"{code} {re.sub(r'[\s-]', '', data[key])}"

    data["languages"] = ", ".join(normalise_languages(data["languages"])[0])

    record = build_record(data, family, education, existing["employee_id"] if existing else None)

    now = datetime.datetime.now().isoformat()
    if existing:
        # Back to HR as New. The flags it answered are marked resolved, for HR to re-check.
        record["access_token"] = existing["access_token"]
        record["submitted_at"] = existing.get("submitted_at", record["submitted_at"])
        record["resubmitted_at"] = now
        record["resubmission_count"] = existing.get("resubmission_count", 0) + 1
        if existing.get("reviewed_at"):
            record["reviewed_at"] = existing["reviewed_at"]
        record["flags"] = [
            {**flag, "status": "resolved", "resolved_at": now} if flag.get("status") == "open" else flag
            for flag in existing.get("flags", [])
        ]
    else:
        record["access_token"] = secrets.token_urlsafe(16)
        record["flags"] = []

    # Keep the uploaded documents, in this employee's own folder, so HR can review them in hr_info.py
    store = storage.get_store()
    employee_id = record["employee_id"]

    record["documents"] = {}

    for key, (_, file_type) in DOCUMENTS.items():
        filename = f"{key}.{file_type}"
        store.put_document(employee_id, filename, documents[key])
        record["documents"][key] = filename

    for key, content in optional_documents.items():
        filename = f"{key}.{OPTIONAL_DOCUMENTS[key][1]}"
        store.put_document(employee_id, filename, content)
        record["documents"][key] = filename

    record["documents"].update(bank.save_uploads(bank_files, lambda name, content: store.put_document(employee_id, name, content)))
    record["bank_details"] = bank.build_bank_record(data)
    record["document_folder"] = store.folder(employee_id)

    # A document the employee removed while fixing the application is not kept
    for stored in store.list_documents(employee_id):
        if stored not in record["documents"].values():
            store.delete_document(employee_id, stored)

    store.put_json("form", employee_id, snapshot)
    storage.save_record(record)

    return JSONResponse(record)


def load_application(employee_id, token):
    """The saved record when `token` is the private key of that application, else None."""

    if not EMPLOYEE_ID_PATTERN.fullmatch(employee_id or ""):
        return None

    record = storage.get_store().get_json("submission", employee_id)

    if record is None:
        return None

    expected = str(record.get("access_token") or "")

    if not expected or not hmac.compare_digest(expected, str(token or "")):
        return None

    return record


async def application(request: Request):
    """The employee's own application: status, open HR flags and (when HR asked for
    changes) the form as submitted plus the list of uploaded documents."""

    employee_id = request.path_params["employee_id"]
    record = load_application(employee_id, request.query_params.get("token"))

    if record is None:
        return JSONResponse({"error": "This application link is not valid."}, status_code=403)

    status = record.get("application_status", STATUS_NEW)
    store = storage.get_store()
    editable = waiting_for_employee(record) and store.has_form(employee_id)

    return JSONResponse({
        "employee_id": employee_id,
        "name": record.get("personal_information", {}).get("name", ""),
        "status": "New" if status == "Pending HR Review" else status,
        "editable": editable,
        "flags": [flag for flag in record.get("flags", []) if flag.get("status") == "open"],
        "form": store.get_json("form", employee_id) if editable else None,
        "documents": [
            {"key": key, "filename": filename}
            for key, filename in record.get("documents", {}).items()
        ] if editable else [],
    })


async def application_document(request: Request):

    employee_id = request.path_params["employee_id"]
    record = load_application(employee_id, request.query_params.get("token"))
    filename = (record or {}).get("documents", {}).get(request.path_params["document"])

    if not filename:
        return JSONResponse({"error": "Document not found."}, status_code=404)

    content = storage.get_store().get_document(employee_id, filename)

    if content is None:
        return JSONResponse({"error": "Document not found."}, status_code=404)

    return Response(content, media_type="application/pdf" if filename.endswith(".pdf") else "image/jpeg")


# ==========================================================
# 6. REACT FRONTEND
# ==========================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Employee Onboarding</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏢</text></svg>">
<!-- __THEME_HEAD__ -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js" crossorigin></script>
<script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js" crossorigin></script>
<script src="https://unpkg.com/@babel/standalone@7.26.4/babel.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.2/cropper.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.2/cropper.min.js"></script>
<style>
:root {
  --bg: #e6e9ef;
  --surface: #f5f6f8;
  --surface-2: #eceef2;
  --ink: #0f172a;
  --ink-2: #334155;
  --muted: #5b6577;
  --faint: #8690a3;
  --line: #d5d9e1;
  --line-strong: #bcc2cd;
  --brand: #4f46e5;
  --brand-600: #4338ca;
  --brand-50: #e4e7fa;
  --brand-100: #d6dbf7;
  --success: #059669;
  --success-50: #ecfdf5;
  --danger: #dc2626;
  --danger-50: #fef2f2;
  --radius: 16px;
  --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.06);
  --shadow: 0 1px 3px rgba(15, 23, 42, 0.05), 0 12px 32px -16px rgba(15, 23, 42, 0.16);
  --ring: 0 0 0 4px rgba(79, 70, 229, 0.14);
  --gradient: linear-gradient(135deg, #6366f1, #0ea5e9);
  --topbar-bg: rgba(245, 246, 248, 0.85);
  --ai-bg: linear-gradient(135deg, #e4e8f8, #e3eef5);
  --flag-bg: #f8efe4;
  --flag-line: #e3b27d;
  --flag-ink: #9a3412;
  --row-hover: #eceef2;
  --brand-line: #c0c8ee;
  --danger-ink: #991b1b;
  --danger-line: #e8bcbc;
  --success-ink: #065f46;
  --success-line: #a9d8c4;
  --paper: #f7f6f2;
  --mark-bg: #fef3c7;
  --mark-ink: #92400e;
}
/* Dark theme: set on <html> by the script from theme.py (Default follows the device) */
:root[data-theme="dark"] {
  --bg: #0b1020;
  --surface: #131a2e;
  --surface-2: #182038;
  --ink: #e6eaf5;
  --ink-2: #c3cbe0;
  --muted: #98a2bd;
  --faint: #6b7690;
  --line: #263050;
  --line-strong: #34406a;
  --brand: #6366f1;
  --brand-600: #7c83f5;
  --brand-50: #1e2250;
  --brand-100: #2a2f6b;
  --success: #0d9f73;
  --success-50: #0b2e26;
  --danger: #ef4444;
  --danger-50: #3a1517;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.5);
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.4), 0 12px 32px -16px rgba(0, 0, 0, 0.7);
  --ring: 0 0 0 4px rgba(129, 140, 248, 0.3);
  --topbar-bg: rgba(11, 16, 32, 0.85);
  --ai-bg: linear-gradient(135deg, #1e1b4b, #082f49);
  --flag-bg: #33200f;
  --flag-line: #a85a1f;
  --flag-ink: #fdba74;
  --row-hover: #182038;
  --brand-line: #3b4180;
  --danger-ink: #fca5a5;
  --danger-line: #7f1d1d;
  --success-ink: #6ee7b7;
  --success-line: #14532d;
  --paper: #1a2036;
  --mark-bg: #4a3a0c;
  --mark-ink: #fde68a;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 14.5px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
button { font: inherit; cursor: pointer; }
a { color: var(--brand); }

/* ---------- Top bar ---------- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  background: var(--topbar-bg);
  backdrop-filter: saturate(180%) blur(12px);
  border-bottom: 1px solid var(--line);
}
.topbar-inner { max-width: 1240px; margin: 0 auto; padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
.brand { display: flex; align-items: center; gap: 12px; font-weight: 700; letter-spacing: -0.01em; }
.brand small { display: block; font-weight: 500; color: var(--muted); font-size: 12px; letter-spacing: 0; }
.brand-mark {
  width: 36px; height: 36px;
  border-radius: 11px;
  display: grid; place-items: center;
  color: #fff;
  background: var(--gradient);
  box-shadow: 0 8px 16px -8px rgba(79, 70, 229, 0.7);
}
.topbar-progress { margin-left: auto; display: flex; align-items: center; gap: 12px; font-size: 13px; color: var(--muted); }
.topbar-progress strong { color: var(--ink); }
.bar { width: 160px; height: 6px; background: var(--line); border-radius: 99px; overflow: hidden; }
.bar > span { display: block; height: 100%; background: var(--gradient); border-radius: inherit; transition: width 0.4s ease; }

/* ---------- Hero ---------- */
.hero {
  background:
    radial-gradient(900px 320px at 8% -30%, rgba(99, 102, 241, 0.6), transparent 60%),
    radial-gradient(700px 320px at 95% -10%, rgba(14, 165, 233, 0.45), transparent 60%),
    #0f172a;
  color: #fff;
}
.hero-inner { max-width: 1240px; margin: 0 auto; padding: 44px 24px 92px; }
.eyebrow {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 5px 12px;
  border-radius: 99px;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.16);
  font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
}
.hero h1 { margin: 16px 0 8px; font-size: clamp(26px, 4vw, 38px); letter-spacing: -0.025em; line-height: 1.15; }
.hero p { margin: 0; color: #cbd5e1; max-width: 640px; font-size: 15px; }
.hero-meta { display: flex; flex-wrap: wrap; gap: 10px 22px; margin-top: 22px; color: #e2e8f0; font-size: 13px; }
.hero-meta span { display: inline-flex; align-items: center; gap: 7px; }
.hero-meta svg { color: #93c5fd; }

/* ---------- Layout ---------- */
.layout {
  position: relative;
  max-width: 1240px;
  margin: -56px auto 0;
  padding: 0 24px 72px;
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr);
  gap: 28px;
  align-items: start;
}
.layout.single { grid-template-columns: minmax(0, 780px); justify-content: center; }
.content { display: flex; flex-direction: column; gap: 24px; min-width: 0; scroll-margin-top: 88px; }

.stepper {
  position: sticky;
  top: 88px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 18px 14px;
}
.stepper h2 { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 8px 10px; }
.stepper ol { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.step {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 10px;
  border-radius: 10px;
  color: var(--ink-2);
  text-decoration: none;
  font-weight: 500;
  font-size: 14px;
  transition: background 0.15s, color 0.15s;
}
.step:hover { background: var(--surface-2); }
.step.active { background: var(--brand-50); color: var(--brand-600); }
.step-dot {
  width: 26px; height: 26px; flex: none;
  border-radius: 50%;
  display: grid; place-items: center;
  font-size: 12px; font-weight: 700;
  border: 1.5px solid var(--line-strong);
  color: var(--muted);
  background: var(--surface);
  transition: 0.2s;
}
.step.active .step-dot { border-color: var(--brand); color: var(--brand); }
.step.done .step-dot { background: var(--success); border-color: var(--success); color: #fff; }
.stepper-foot { margin: 14px 8px 0; padding-top: 16px; border-top: 1px solid var(--line); font-size: 13px; color: var(--muted); }
.stepper-foot strong { color: var(--ink); font-size: 24px; letter-spacing: -0.02em; margin-right: 4px; }
.stepper-foot .bar { width: 100%; margin-top: 10px; }
button.step { width: 100%; border: none; background: none; text-align: left; }
button.step:disabled { cursor: not-allowed; opacity: 0.5; }
button.step:disabled:hover { background: none; }
.stepper .substeps { margin: 2px 0 6px 22px; padding-left: 14px; border-left: 1.5px solid var(--line); gap: 0; }
.substep { display: block; padding: 5px 8px; border-radius: 8px; color: var(--muted); font-size: 13px; text-decoration: none; }
.substep:hover { color: var(--ink); background: var(--surface-2); }
.substep.active { color: var(--brand-600); font-weight: 600; }
.substep.done::after { content: " ✓"; color: var(--success); }

/* ---------- Step navigation ---------- */
.step-panel { display: flex; flex-direction: column; gap: 24px; }
.step-panel[hidden] { display: none; }
.step-nav { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.step-strip {
  display: none;
  gap: 4px;
  padding: 10px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}
.step-strip button {
  flex: 1; min-width: 0;
  display: flex; flex-direction: column; align-items: center; gap: 5px;
  padding: 4px;
  border: none; background: none;
  font-size: 11.5px; font-weight: 600; color: var(--muted);
  text-align: center;
}
.step-strip button:disabled { cursor: not-allowed; opacity: 0.5; }
.step-strip button.active { color: var(--brand-600); }
.step-strip .active .step-dot { border-color: var(--brand); color: var(--brand); }
.step-strip .done .step-dot { background: var(--success); border-color: var(--success); color: #fff; }

/* ---------- Review ---------- */
.review { display: flex; flex-direction: column; gap: 18px; margin-bottom: 22px; }
.review-block { border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; }
.review-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.review-head h3 { margin: 0; font-size: 15px; display: flex; align-items: center; gap: 8px; }
.review-head h3 svg { color: var(--brand); }
.review-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 20px; }
.review-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.review-item.wide { grid-column: 1 / -1; }
.review-item span { font-size: 11.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
.review-item strong { font-weight: 500; white-space: pre-wrap; overflow-wrap: anywhere; }
.review-profile { display: flex; align-items: center; gap: 14px; margin-top: 16px; }
.review-profile img, .review-avatar { width: 64px; height: 80px; flex: none; border-radius: 10px; object-fit: cover; border: 1px solid var(--line); }
.review-avatar { display: grid; place-items: center; background: var(--surface-2); color: var(--faint); }
.review-profile div { display: flex; flex-direction: column; min-width: 0; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.review-profile strong { font-size: 17px; color: var(--ink); }
.review table { min-width: 560px; }
.review td { padding: 9px 12px; font-size: 13.5px; }
.review .doc-list { margin: 0; }
.review-empty { margin: 0; color: var(--muted); font-size: 13px; }
.doc-chip small { display: block; color: var(--muted); font-weight: 400; font-size: 12px; overflow-wrap: anywhere; }
.doc-chip.missing svg { color: var(--danger); }

/* ---------- Sections ---------- */
.section {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  scroll-margin-top: 88px;
}
.section-head { display: flex; align-items: flex-start; gap: 14px; padding: 20px 24px; border-bottom: 1px solid var(--line); }
.section-icon {
  width: 42px; height: 42px; flex: none;
  border-radius: 12px;
  display: grid; place-items: center;
  background: var(--brand-50);
  color: var(--brand);
}
.section-step { font-size: 11.5px; font-weight: 600; color: var(--brand); text-transform: uppercase; letter-spacing: 0.06em; }
.section-head h2 { margin: 1px 0 0; font-size: 17px; letter-spacing: -0.01em; }
.section-head p { margin: 2px 0 0; color: var(--muted); font-size: 13.5px; }
.section-head .pill { margin-left: auto; }
.section-body { padding: 24px; }

.pill {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 10px;
  border-radius: 99px;
  font-size: 12px; font-weight: 600;
  white-space: nowrap;
}
.pill.done { background: var(--success-50); color: var(--success); }
.pill.todo { background: var(--surface-2); color: var(--muted); border: 1px solid var(--line); }
.pill.brand { background: var(--brand-50); color: var(--brand-600); }

.group + .group { margin-top: 28px; padding-top: 24px; border-top: 1px dashed var(--line); }
.group-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.group-head h3 { margin: 0; font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.group-head h3 svg { color: var(--brand); }
.group-head p { margin: 3px 0 0; font-size: 13px; color: var(--muted); }
.sub-label { font-size: 11.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); margin: 22px 0 10px; }

/* ---------- Fields ---------- */
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 20px; }
.grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid + .grid { margin-top: 16px; }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field.wide { grid-column: 1 / -1; }
.label { font-size: 13px; font-weight: 600; color: var(--ink-2); display: flex; align-items: center; gap: 4px; }
.req { color: var(--danger); }
.auto-badge {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 1px 8px;
  border-radius: 99px;
  font-size: 11px; font-weight: 600;
  color: var(--brand-600);
  background: var(--brand-50);
}
input, select, textarea {
  font: inherit;
  color: var(--ink);
  background: var(--surface);
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  padding: 10px 12px;
  width: 100%;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
}
input::placeholder, textarea::placeholder { color: var(--faint); }
input:hover, select:hover, textarea:hover { border-color: var(--faint); }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--brand); box-shadow: var(--ring); }
input:disabled, textarea:disabled { background: var(--surface-2); color: var(--muted); border-color: var(--line); }
textarea { min-height: 88px; resize: vertical; }
select {
  appearance: none;
  padding-right: 36px;
  background: var(--surface) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E") no-repeat right 12px center;
}
input[type="checkbox"] { width: 18px; height: 18px; flex: none; margin: 1px 0 0; padding: 0; accent-color: var(--brand); cursor: pointer; }
.inline-check { grid-column: 1 / -1; display: flex; gap: 10px; align-items: center; cursor: pointer; font-size: 13.5px; color: var(--ink-2); }

.field-error { color: var(--danger); font-size: 12px; font-weight: 500; }
.field-hint { color: var(--muted); font-size: 12px; }
.field.has-error input, .field.has-error select, .field.has-error textarea,
.field.has-error .phone, .field.has-error .tags, .tags.invalid { border-color: var(--danger); }
.field.has-error input:focus { box-shadow: 0 0 0 4px rgba(220, 38, 38, 0.12); }

/* Language chips */
.tags {
  position: relative;
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  min-height: 42px;
  padding: 5px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--surface);
  cursor: text;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.tags:focus-within { border-color: var(--brand); box-shadow: var(--ring); }
.tags input { flex: 1; min-width: 140px; width: auto; padding: 4px; border: none; box-shadow: none !important; background: transparent; }
.tag {
  display: inline-flex; align-items: center; gap: 2px;
  padding: 3px 4px 3px 10px;
  border-radius: 99px;
  background: var(--brand-50);
  color: var(--brand-600);
  font-size: 13px; font-weight: 600;
}
.tag button { width: 20px; height: 20px; padding: 0; border: none; border-radius: 50%; display: grid; place-items: center; background: none; color: inherit; }
.tag button:hover { background: var(--brand-100); }
.suggestions {
  position: absolute; top: calc(100% + 6px); left: 0; right: 0; z-index: 25;
  max-height: 240px; overflow: auto;
  margin: 0; padding: 6px;
  list-style: none;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
}
.suggestions button { width: 100%; padding: 8px 10px; border: none; border-radius: 8px; background: none; text-align: left; font-size: 13.5px; color: var(--ink-2); }
.suggestions button:hover, .suggestions button.on { background: var(--brand-50); color: var(--brand-600); }

/* Phone with country code */
.phone {
  display: flex;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--surface);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.phone:focus-within { border-color: var(--brand); box-shadow: var(--ring); }
.phone input { min-width: 0; border: none; border-radius: 0 10px 10px 0; box-shadow: none !important; }
.phone-code {
  position: relative;
  display: flex; align-items: center; gap: 5px; flex: none;
  padding: 0 10px 0 12px;
  border-right: 1px solid var(--line);
  border-radius: 10px 0 0 10px;
  background: var(--surface-2);
  color: var(--ink-2);
  font-size: 13.5px;
  white-space: nowrap;
  cursor: pointer;
}
.phone-code span { font-size: 11px; font-weight: 700; color: var(--muted); }
.phone-code svg { color: var(--muted); }
.phone-code select { position: absolute; inset: 0; width: 100%; opacity: 0; cursor: pointer; }

/* ---------- Uploads ---------- */
.counter { font-size: 12px; font-weight: 600; color: var(--muted); white-space: nowrap; padding-top: 2px; }
.counter.full { color: var(--success); }
.upload-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.upload { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.upload-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 600; color: var(--ink-2); }
.upload-side { font-size: 11px; font-weight: 600; color: var(--muted); background: var(--surface-2); border: 1px solid var(--line); padding: 1px 8px; border-radius: 99px; }
.drop {
  position: relative;
  width: 100%;
  aspect-ratio: 4 / 3;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
  padding: 12px;
  border: 1.5px dashed var(--line-strong);
  border-radius: 12px;
  background: var(--surface-2);
  color: var(--muted);
  text-align: center;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}
button.drop:hover, .drop.dragging { border-color: var(--brand); background: var(--brand-50); color: var(--brand-600); }
.drop:focus-visible { outline: none; border-color: var(--brand); box-shadow: var(--ring); }
.drop strong { color: var(--ink-2); font-size: 13px; font-weight: 600; }
.drop small { font-size: 11.5px; }
.drop-icon {
  width: 40px; height: 40px;
  border-radius: 11px;
  display: grid; place-items: center;
  background: var(--surface);
  border: 1px solid var(--line);
  color: var(--brand);
  box-shadow: var(--shadow-sm);
}
.drop.filled { padding: 0; overflow: hidden; border: 1px solid var(--line); background: #0f172a; }
.drop.filled img { width: 100%; height: 100%; object-fit: cover; }
.drop.filled.dragging { outline: 2px solid var(--brand); outline-offset: 2px; }
.drop-overlay {
  position: absolute; left: 0; right: 0; bottom: 0;
  display: flex; align-items: center; gap: 6px;
  padding: 30px 8px 8px;
  background: linear-gradient(transparent, rgba(15, 23, 42, 0.88));
  color: #fff;
  font-size: 12px;
}
.drop-overlay span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.mini-btn {
  width: 28px; height: 28px; flex: none;
  border: none; border-radius: 8px;
  display: grid; place-items: center;
  background: rgba(255, 255, 255, 0.16);
  color: #fff;
  transition: background 0.15s;
}
.mini-btn:hover { background: rgba(255, 255, 255, 0.32); }
.ok-badge {
  position: absolute; top: 8px; right: 8px;
  width: 24px; height: 24px;
  border-radius: 50%;
  display: grid; place-items: center;
  background: var(--success);
  color: #fff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
.file-error { color: var(--danger); font-size: 12px; font-weight: 500; }

.drop.row { aspect-ratio: auto; flex-direction: row; justify-content: flex-start; gap: 16px; padding: 20px; text-align: left; }
.drop.row strong, .drop.row small { display: block; }
.drop.row strong { font-size: 14px; }
.pdf-card {
  display: flex; align-items: center; gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.pdf-card.dragging { border-color: var(--brand); box-shadow: var(--ring); }
.pdf-icon {
  position: relative;
  width: 44px; height: 52px; flex: none;
  border-radius: 9px;
  display: grid; place-items: center;
  background: var(--danger-50);
  color: var(--danger);
}
.pdf-icon b {
  position: absolute; bottom: 6px;
  font-size: 9px; font-weight: 800; letter-spacing: 0.04em;
  background: var(--danger); color: #fff;
  padding: 0 4px; border-radius: 3px;
}
.pdf-meta { flex: 1; min-width: 0; }
.pdf-meta strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
.pdf-meta small { display: inline-flex; align-items: center; gap: 4px; color: var(--success); font-weight: 500; font-size: 12.5px; }
.pdf-meta small span { color: var(--muted); }
.pdf-actions { display: flex; align-items: center; gap: 8px; }

/* ---------- AI autofill ---------- */
.ai-panel {
  display: flex; align-items: center; gap: 16px;
  margin-top: 20px;
  padding: 16px 18px;
  border-radius: 14px;
  background: var(--ai-bg);
  border: 1px solid var(--brand-100);
}
.ai-icon { width: 42px; height: 42px; flex: none; border-radius: 12px; display: grid; place-items: center; color: #fff; background: var(--gradient); }
.ai-panel strong { display: block; font-size: 14px; }
.ai-panel p { margin: 2px 0 0; font-size: 13px; color: var(--muted); }
.ai-panel .btn { margin-left: auto; }

/* ---------- Buttons ---------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 10px 16px;
  border: 1px solid transparent;
  border-radius: 10px;
  font-size: 14px; font-weight: 600;
  white-space: nowrap;
  text-decoration: none;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s, transform 0.05s;
}
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn:focus-visible { outline: none; box-shadow: var(--ring); }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn.loading { opacity: 0.85; cursor: progress; }
.flag-note { display: flex; gap: 8px; align-items: flex-start; margin-top: 6px; padding: 8px 10px; border-radius: 10px; background: var(--flag-bg); border: 1px solid var(--flag-line); color: var(--flag-ink); font-size: 13px; line-height: 1.4; }
.flag-note svg { flex: none; margin-top: 2px; }
.field.flagged input, .field.flagged textarea, .field.flagged select { border-color: #f97316; box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.14); }
.flag-banner { padding: 16px 20px; border-radius: var(--radius); background: var(--flag-bg); border: 1px solid var(--flag-line); color: var(--flag-ink); }
.flag-banner h3 { display: flex; align-items: center; gap: 8px; margin: 0 0 4px; font-size: 16px; }
.flag-banner p { margin: 0 0 8px; font-size: 13.5px; }
.flag-banner ul { margin: 0; padding-left: 20px; font-size: 14px; display: grid; gap: 4px; }
.emp-link { display: grid; gap: 8px; justify-items: center; margin: 18px auto 0; max-width: 560px; }
.emp-link small { color: var(--muted); }
.emp-link input { width: 100%; font-size: 12.5px; padding: 8px 10px; border: 1px solid var(--line-strong); border-radius: 8px; background: var(--surface-2); }
.btn-primary { background: var(--brand); color: #fff; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.16); }
.btn-primary:hover:not(:disabled) { background: var(--brand-600); }
.btn-ghost { background: var(--surface); color: var(--ink-2); border-color: var(--line-strong); }
.btn-ghost:hover:not(:disabled) { background: var(--surface-2); border-color: var(--faint); }
.btn-sm { padding: 6px 11px; font-size: 13px; border-radius: 8px; }
.btn-lg { width: 100%; padding: 14px 22px; font-size: 15px; border-radius: 12px; }
.icon-btn {
  width: 32px; height: 32px;
  border: none; border-radius: 8px;
  display: grid; place-items: center;
  background: none;
  color: var(--faint);
  transition: background 0.15s, color 0.15s;
}
.icon-btn:hover { background: var(--danger-50); color: var(--danger); }
.spinner {
  width: 15px; height: 15px;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
.spinner.dark { flex: none; margin-top: 1px; border-color: rgba(79, 70, 229, 0.25); border-top-color: var(--brand); }
.spinner.blue { flex: none; width: 18px; height: 18px; border-color: rgba(37, 99, 235, 0.2); border-top-color: #2563eb; }
.extract-status { display: inline-flex; align-items: center; gap: 8px; margin-left: 10px; font-weight: 600; color: #2563eb; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ---------- Tables ---------- */
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }
table { width: 100%; min-width: 640px; border-collapse: separate; border-spacing: 0; }
th {
  padding: 10px 12px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--line);
  text-align: left;
  font-size: 11.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--muted);
}
td { padding: 6px; border-bottom: 1px solid var(--line); }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--row-hover); }
td input { padding: 8px 10px; border-color: transparent; background: transparent; }
td input:hover { border-color: var(--line-strong); background: var(--surface); }
td input:focus { background: var(--surface); }
.age-gender { display: flex; gap: 6px; min-width: 190px; }
.age-gender input { width: 60px; flex: none; }
.age-gender select { flex: 1; min-width: 100px; padding: 8px 28px 8px 10px; background-position: right 8px center; }
.row-num { width: 40px; text-align: center; color: var(--faint); font-size: 12px; font-weight: 600; }
.table-actions { margin-top: 12px; }

/* ---------- References ---------- */
.ref-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.ref-card { border: 1px solid var(--line); border-radius: 14px; padding: 18px; background: var(--surface-2); display: flex; flex-direction: column; gap: 14px; }
.ref-card h3 { margin: 0; font-size: 14px; display: flex; align-items: center; gap: 10px; }
.ref-num { width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center; background: var(--brand); color: #fff; font-size: 12px; }

/* ---------- Declaration ---------- */
.declare {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 16px 18px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-2);
  cursor: pointer;
  transition: 0.15s;
}
.declare.checked { border-color: var(--brand-line); background: var(--brand-50); }
.declare strong { display: block; font-size: 14px; }
.declare span span { color: var(--muted); font-size: 13px; }
.submit-bar { margin-top: 20px; display: flex; flex-direction: column; gap: 10px; }
.submit-hint { font-size: 12.5px; color: var(--muted); text-align: center; }

/* ---------- Feedback ---------- */
.alert {
  display: flex; gap: 10px; align-items: flex-start;
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid;
  border-radius: 10px;
  font-size: 13.5px;
}
.alert svg { flex: none; margin-top: 1px; }
.alert.error { background: var(--danger-50); color: var(--danger-ink); border-color: var(--danger-line); }
.alert.success { background: var(--success-50); color: var(--success-ink); border-color: var(--success-line); }
.alert.info { background: var(--brand-50); color: var(--brand-600); border-color: var(--brand-100); }
details { margin-top: 12px; }
summary { cursor: pointer; color: var(--brand); font-weight: 600; font-size: 13px; }
pre {
  margin: 10px 0 0;
  padding: 16px;
  border-radius: 12px;
  background: #0f172a;
  color: #e2e8f0;
  font-size: 12.5px;
  overflow: auto;
  max-height: 420px;
  text-align: left;
}

/* ---------- Success ---------- */
.success { padding: 48px 32px; text-align: center; }
.success-icon {
  width: 76px; height: 76px;
  margin: 0 auto 20px;
  border-radius: 50%;
  display: grid; place-items: center;
  background: var(--success-50);
  color: var(--success);
  box-shadow: 0 0 0 12px rgba(5, 150, 105, 0.07);
  animation: pop 0.45s cubic-bezier(0.2, 0.9, 0.3, 1.4);
}
@keyframes pop { from { transform: scale(0.4); opacity: 0; } to { transform: scale(1); opacity: 1; } }
.success h2 { margin: 0 0 6px; font-size: 26px; letter-spacing: -0.02em; }
.success > p { margin: 0 auto; max-width: 480px; color: var(--muted); }
.emp-id {
  display: inline-flex; flex-direction: column; gap: 2px;
  margin: 22px 0 6px;
  padding: 12px 22px;
  border: 1px dashed var(--line-strong);
  border-radius: 12px;
  background: var(--surface-2);
}
.emp-id small { font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.emp-id strong { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 20px; letter-spacing: 0.04em; }
.doc-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin: 24px 0; text-align: left; }
.doc-chip { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; font-size: 13px; font-weight: 500; }
.doc-chip svg { color: var(--success); flex: none; }
.success-actions { display: flex; justify-content: center; flex-wrap: wrap; gap: 10px; }

/* ---------- Crop dialog ---------- */
.modal-backdrop {
  position: fixed; inset: 0; z-index: 100;
  display: grid; place-items: center;
  padding: 16px;
  background: rgba(15, 23, 42, 0.62);
  backdrop-filter: blur(4px);
  animation: fade 0.15s ease;
}
@keyframes fade { from { opacity: 0; } to { opacity: 1; } }
.modal {
  width: min(780px, 100%);
  max-height: calc(100vh - 32px);
  display: flex; flex-direction: column;
  overflow: hidden;
  background: var(--surface);
  border-radius: 18px;
  box-shadow: 0 28px 72px -16px rgba(0, 0, 0, 0.45);
  animation: rise 0.2s ease;
}
@keyframes rise { from { transform: translateY(12px) scale(0.98); opacity: 0; } to { transform: none; opacity: 1; } }
.modal-head { display: flex; align-items: center; gap: 12px; padding: 16px 20px; border-bottom: 1px solid var(--line); }
.modal-head h2 { margin: 0; font-size: 16px; letter-spacing: -0.01em; }
.modal-head p { margin: 1px 0 0; font-size: 13px; color: var(--muted); }
.modal-head .icon-btn { margin-left: auto; }
.crop-stage { height: min(58vh, 460px); background: #0f172a; }
.crop-stage img { display: block; max-width: 100%; }
.crop-toolbar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 12px 20px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--line);
}
.seg { display: inline-flex; gap: 2px; padding: 3px; background: var(--surface); border: 1px solid var(--line-strong); border-radius: 10px; }
.seg button { border: none; background: none; padding: 5px 12px; border-radius: 7px; font-size: 13px; font-weight: 600; color: var(--muted); }
.seg button:hover { color: var(--ink); }
.seg button.on { background: var(--brand); color: #fff; }
.tool-group { display: inline-flex; gap: 6px; margin-left: auto; }
.tool {
  width: 34px; height: 34px;
  border: 1px solid var(--line-strong); border-radius: 9px;
  display: grid; place-items: center;
  background: var(--surface);
  color: var(--ink-2);
  transition: border-color 0.15s, color 0.15s;
}
.tool:hover { border-color: var(--brand); color: var(--brand); }
.modal-foot { display: flex; align-items: center; gap: 10px; padding: 14px 20px; }
.modal-foot .first { margin-right: auto; }
.cropper-view-box { outline: 2px solid #818cf8; outline-color: rgba(129, 140, 248, 0.9); }
.cropper-point { background-color: #818cf8; }
.cropper-line { background-color: #818cf8; }

.footer { max-width: 1240px; margin: 0 auto; padding: 0 24px 40px; color: var(--faint); font-size: 12.5px; text-align: center; }

/* ---------- Responsive ---------- */
@media (max-width: 1100px) {
  .upload-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 960px) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .stepper { display: none; }
  .step-strip { display: flex; }
}
@media (max-width: 640px) {
  .topbar-inner, .hero-inner { padding-left: 16px; padding-right: 16px; }
  .hero-inner { padding-top: 32px; }
  .layout { padding: 0 16px 56px; }
  .brand small, .topbar-progress .label-text { display: none; }
  .bar { width: 80px; }
  .grid, .grid.three, .ref-grid, .review-grid { grid-template-columns: minmax(0, 1fr); }
  .review-block { padding: 16px 14px; }
  .section-head, .section-body { padding: 18px 16px; }
  .section-head .pill { display: none; }
  .ai-panel { flex-direction: column; align-items: flex-start; }
  .ai-panel .btn { margin-left: 0; width: 100%; }
  .pdf-card { flex-wrap: wrap; }
  .pdf-actions { width: 100%; }
  .drop.row { flex-direction: column; text-align: center; }
  .success { padding: 36px 18px; }
  .tool-group { margin-left: 0; }
  .modal-foot { flex-wrap: wrap; }
  .modal-foot .btn { flex: 1; }
  .modal-foot .first { margin-right: 0; flex-basis: 100%; }
}
@media (max-width: 420px) {
  .upload-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
/* __THEME_CSS__ */
/* __POLICY_CSS__ */
/* __BANK_CSS__ */
</style>
</head>
<body>
<div id="root"></div>

<script type="text/babel" data-presets="react">
const { useState, useEffect, useRef } = React;

const FAMILY_COLUMNS = ["Name", "Age/Gender", "Relation", "Occupation"];

const EDUCATION_COLUMNS = [
  "Qualification",
  "University/Institute",
  "Year of Passing",
  "% Marks",
  "Major Subject",
];

const emptyRow = (columns) => Object.fromEntries(columns.map((column) => [column, ""]));

// __THEME_SCRIPT__

// __POLICY_SCRIPT__

// __BANK_SCRIPT__

const INITIAL_FORM = {
  name: "", father_name: "", dob: "", age: "", gender: "Select",
  pan_number: "", aadhaar_number: "", mobile: "", mobile_code: "+91",
  passport_number: "", email: "", languages: "", hobbies: "",
  permanent_address: "", current_address: "", same_address: false,
  emergency_name: "", emergency_relation: "", emergency_phone: "", emergency_phone_code: "+91",
  family: Array.from({ length: 4 }, () => emptyRow(FAMILY_COLUMNS)),
  education: ["10th", "12th", "Graduation", "Certification"].map((qualification) => ({
    ...emptyRow(EDUCATION_COLUMNS),
    Qualification: qualification,
  })),
  total_experience: "", organisation: "", period_from: "", period_to: "",
  joining_designation: "", last_designation: "", joining_salary: "", last_salary: "",
  job_responsibility: "", reason_for_leaving: "",
  ref1_name: "", ref1_designation: "", ref1_contact: "", ref1_contact_code: "+91",
  ref2_name: "", ref2_designation: "", ref2_contact: "", ref2_contact_code: "+91",
  bank: INITIAL_BANK,
  policies: INITIAL_POLICIES,
  declaration: false,
};

// Filled in by the server from react_onboard.LANGUAGES
const LANGUAGES = __LANGUAGES__;

// Form key -> the field name shown in error messages (from REQUIRED_FIELDS on the server)
const FIELD_LABELS = __FIELD_LABELS__;

// Phone fields; each has a "<key>_code" dial code in the form
const PHONE_KEYS = ["mobile", "emergency_phone", "ref1_contact", "ref2_contact"];

const COUNTRIES = [
  { iso: "IN", name: "India", dial: "+91" },
  { iso: "AE", name: "United Arab Emirates", dial: "+971" },
  { iso: "AU", name: "Australia", dial: "+61" },
  { iso: "BD", name: "Bangladesh", dial: "+880" },
  { iso: "BH", name: "Bahrain", dial: "+973" },
  { iso: "BT", name: "Bhutan", dial: "+975" },
  { iso: "CN", name: "China", dial: "+86" },
  { iso: "DE", name: "Germany", dial: "+49" },
  { iso: "ES", name: "Spain", dial: "+34" },
  { iso: "FR", name: "France", dial: "+33" },
  { iso: "GB", name: "United Kingdom", dial: "+44" },
  { iso: "IE", name: "Ireland", dial: "+353" },
  { iso: "IT", name: "Italy", dial: "+39" },
  { iso: "JP", name: "Japan", dial: "+81" },
  { iso: "KW", name: "Kuwait", dial: "+965" },
  { iso: "LK", name: "Sri Lanka", dial: "+94" },
  { iso: "MY", name: "Malaysia", dial: "+60" },
  { iso: "NL", name: "Netherlands", dial: "+31" },
  { iso: "NP", name: "Nepal", dial: "+977" },
  { iso: "NZ", name: "New Zealand", dial: "+64" },
  { iso: "OM", name: "Oman", dial: "+968" },
  { iso: "QA", name: "Qatar", dial: "+974" },
  { iso: "RU", name: "Russia", dial: "+7" },
  { iso: "SA", name: "Saudi Arabia", dial: "+966" },
  { iso: "SG", name: "Singapore", dial: "+65" },
  { iso: "US", name: "United States / Canada", dial: "+1" },
  { iso: "ZA", name: "South Africa", dial: "+27" },
];

const INITIAL_FILES = {
  aadhaar_front: null, aadhaar_back: null, pan: null, photo: null, education_certificate: null,
  bank_proof: null, payslips: [], relieving_letter: null, resume: null,
};

// Form fields a resume can fill (gender and education are handled separately), with labels
const RESUME_FIELDS = {
  name: "name", dob: "date of birth", mobile: "mobile number", email: "email",
  languages: "languages", total_experience: "total experience", organisation: "last organisation",
  period_from: "employment period", period_to: "employment period",
  joining_designation: "designation", last_designation: "designation",
  job_responsibility: "job responsibility",
};

// Crop frame presets: ID cards are 85.6 x 54 mm, passport photos 35 x 45 mm
const CARD_RATIOS = [{ label: "ID card", value: 85.6 / 54 }, { label: "Free", value: NaN }];
const PHOTO_RATIOS = [{ label: "Passport", value: 35 / 45 }, { label: "Square", value: 1 }, { label: "Free", value: NaN }];

// Shown in the Personal Information section; keys match react_onboard.DOCUMENTS
const IDENTITY_DOCUMENTS = [
  { key: "aadhaar_front", title: "Aadhaar Card", side: "Front", icon: "id", ratios: CARD_RATIOS },
  { key: "aadhaar_back", title: "Aadhaar Card", side: "Back", icon: "id", ratios: CARD_RATIOS },
  { key: "pan", title: "PAN Card", side: "Front", icon: "id", ratios: CARD_RATIOS },
  { key: "photo", title: "Photograph", side: "Passport size", icon: "camera", ratios: PHOTO_RATIOS },
];

const DOCUMENT_LABELS = {
  aadhaar_front: "Aadhaar Card (Front)",
  aadhaar_back: "Aadhaar Card (Back)",
  pan: "PAN Card (Front)",
  photo: "Profile Photograph",
  education_certificate: "Highest Education Certificate",
  bank_proof: "Bank Proof (Cheque / Passbook)",
  ...Object.fromEntries([1, 2, 3, 4, 5, 6].map((n) => [`payslip_${n}`, `Payslip ${n}`])),
  relieving_letter: "Relieving Letter",
  resume: "Resume",
};

const MAX_UPLOAD_MB = 5;

// Client-side checks for a quick error message; the server re-checks the file contents
const FILE_KINDS = {
  image: {
    accept: ".jpg,.jpeg,image/jpeg",
    hint: `JPG / JPEG · max ${MAX_UPLOAD_MB} MB`,
    error: "Only JPG/JPEG images are allowed.",
    test: (file) => file.type === "image/jpeg" || /\.jpe?g$/i.test(file.name),
  },
  pdf: {
    accept: ".pdf,application/pdf",
    hint: `PDF only · max ${MAX_UPLOAD_MB} MB`,
    error: "Only PDF files are allowed for the certificate.",
    test: (file) => file.type === "application/pdf" || /\.pdf$/i.test(file.name),
  },
};

const SECTIONS = [
  { id: "personal", title: "Personal Information", subtitle: "Identity documents, basic details and contacts", icon: "user" },
  { id: "family", title: "Family Details", subtitle: "Members of your immediate family", icon: "users" },
  { id: "education", title: "Education", subtitle: "Your qualifications and highest certificate", icon: "cap" },
  { id: "experience", title: "Professional Experience", subtitle: "Details of your previous employment", icon: "briefcase" },
  { id: "references", title: "References", subtitle: "Two senior colleagues from your last organisation", icon: "chat" },
  { id: "bank", title: "Bank Details", subtitle: "Bank account, last 6 payslips and relieving letter", icon: "building" },
  { id: "policies", title: "Company Policies", subtitle: "Read and sign the NDA, Statutory Declaration, Anti-Bribery and HR Policy", icon: "file" },
  { id: "declaration", title: "Review & Submit", subtitle: "Check all your information, then confirm and submit", icon: "shield" },
];

const SECTION = Object.fromEntries(SECTIONS.map((section) => [section.id, section]));

// The wizard steps; each one shows one or more of the SECTIONS above
const STEPS = [
  { id: "personnel", title: "Personnel Information", sections: ["personal", "family", "education", "experience", "references"] },
  { id: "bank", title: "Bank Details", sections: ["bank"] },
  { id: "policies", title: "Company Policies", sections: ["policies"] },
  { id: "review", title: "Review & Submit", sections: ["declaration"] },
];

const STEP_OF = Object.fromEntries(
  STEPS.flatMap((step, index) => step.sections.map((id) => [id, index]))
);

// Sections the employee may skip: they never block Next, and do not count towards the progress %
const OPTIONAL_SECTIONS = ["bank"];
const REQUIRED_SECTIONS = SECTIONS.filter(({ id }) => !OPTIONAL_SECTIONS.includes(id));

const PERSONAL_KEYS = [
  "name", "father_name", "dob", "age", "pan_number", "aadhaar_number", "mobile",
  "email", "languages", "hobbies",
  "permanent_address", "current_address", "emergency_name", "emergency_relation", "emergency_phone",
];

const EXPERIENCE_KEYS = [
  "total_experience", "organisation", "period_from", "period_to", "joining_designation", "last_designation",
  "joining_salary", "last_salary", "job_responsibility",
];

const REFERENCE_KEYS = [1, 2].flatMap((n) => [`ref${n}_name`, `ref${n}_designation`, `ref${n}_contact`]);

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

const filled = (value) => String(value ?? "").trim() !== "";

const rowHasData = (row, ignore = []) =>
  Object.entries(row).some(([column, value]) => !ignore.includes(column) && filled(value));

// Typing "15081995" shows "15/08/1995": digits only, slashes added automatically
function maskDate(text) {
  const digits = text.replace(/\D/g, "").slice(0, 8);
  return [digits.slice(0, 2), digits.slice(2, 4), digits.slice(4)].filter(Boolean).join("/");
}

// A real calendar date from DD/MM/YYYY, or null
function parseDate(text) {
  const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec((text || "").trim());
  if (!match) return null;
  const [day, month, year] = [+match[1], +match[2], +match[3]];
  const date = new Date(year, month - 1, day);
  const real = date.getDate() === day && date.getMonth() === month - 1 && date.getFullYear() === year;
  return real ? date : null;
}

function phoneError(code, number, attempted) {
  if (!number) return null;
  if (code === "+91") {
    if (!/^[6-9]/.test(number)) return "Indian mobile numbers start with 6, 7, 8 or 9.";
    if (attempted && number.length !== 10) return "Enter a 10-digit mobile number.";
    return null;
  }
  if (attempted && (number.length < 6 || number.length > 14)) return "Enter 6 to 14 digits.";
  return null;
}

// Inline field errors. Half-typed values are only flagged after a submit attempt.
function fieldErrors(form, attempted) {
  const errors = {};
  const today = new Date();

  ["dob", "period_from", "period_to"].forEach((key) => {
    const value = form[key];
    if (!value || value === "Present" || (value.length < 10 && !attempted)) return;
    const date = parseDate(value);
    if (!date) errors[key] = "Use DD/MM/YYYY, for example 15/08/1995.";
    else if (date > today) errors[key] = "Date cannot be in the future.";
  });

  const dob = parseDate(form.dob);
  const from = parseDate(form.period_from);
  const to = parseDate(form.period_to);
  if (!errors.period_from && from && dob && from <= dob) errors.period_from = "Must be after your date of birth.";
  if (!errors.period_to && from && to && to < from) errors.period_to = "Cannot be before the 'From' date.";

  const age = calculateAge(form.dob);
  if (form.age && dob && !errors.dob && Number(form.age) !== age) {
    errors.age = `Should be ${age} as per your date of birth.`;
  }
  if (form.total_experience && form.age && Number(form.total_experience) >= Number(form.age)) {
    errors.total_experience = "Cannot be more than your age.";
  }

  PHONE_KEYS.forEach((key) => {
    const error = phoneError(form[`${key}_code`], form[key], attempted);
    if (error) errors[key] = error;
  });

  if (form.pan_number && (form.pan_number.length === 10 || attempted) && !/^[A-Z]{5}\d{4}[A-Z]$/.test(form.pan_number)) {
    errors.pan_number = "Use the format ABCDE1234F.";
  }
  if (form.aadhaar_number && attempted && form.aadhaar_number.length !== 12) {
    errors.aadhaar_number = "Enter all 12 digits.";
  }

  return errors;
}

// Put the father's name from the PAN card in the first family row
function withFather(family, fatherName) {
  if (!fatherName) return family;
  const father = { Name: fatherName, Relation: "Father" };
  if (!family.length) return [{ ...emptyRow(FAMILY_COLUMNS), ...father }];
  return family.map((row, index) => (index === 0 ? { ...row, ...father } : row));
}

// Fill the Graduation row (B.Tech / B.E. degree) or the Certification row
function withCertificate(education, details) {
  const level = details.level || "Graduation";
  const row = {
    Qualification: details.name ? `${level} (${details.name})` : level,
    "University/Institute": details.institute,
    "Year of Passing": details.year,
    "% Marks": details.marks,
    "Major Subject": details.major_subject,
  };
  const merge = (current) =>
    Object.fromEntries(Object.entries(current).map(([column, value]) => [column, row[column] || value]));
  const index = education.findIndex((current) =>
    current.Qualification.toLowerCase().startsWith(level.toLowerCase())
  );
  if (index === -1) return [...education, merge(emptyRow(EDUCATION_COLUMNS))];
  return education.map((current, i) => (i === index ? merge(current) : current));
}

// Fill the form from resume details. Only empty fields and empty education rows are
// filled, so anything typed or read from the ID cards is kept.
function withResume(current, data) {
  const next = { ...current };
  Object.keys(RESUME_FIELDS).forEach((key) => {
    if (filled(data[key]) && !filled(current[key])) next[key] = data[key];
  });
  if (next.dob !== current.dob) next.age = calculateAge(next.dob) ? String(calculateAge(next.dob)) : "";
  if (data.gender && current.gender === "Select") next.gender = data.gender;

  let education = current.education;
  (data.education || []).forEach((entry) => {
    const row = {
      Qualification: entry.qualification ? `${entry.level} (${entry.qualification})` : entry.level,
      "University/Institute": entry.institute,
      "Year of Passing": entry.year,
      "% Marks": entry.marks,
      // A certificate's name is its subject, as with the uploaded certificate
      "Major Subject": entry.major_subject || (entry.level === "Certification" ? entry.qualification : ""),
    };
    // Already in the table (for example after reading the same resume again)
    if (education.some((current) =>
      current.Qualification === row.Qualification && rowHasData(current, ["Qualification"])
    )) return;
    const level = entry.level.toLowerCase();
    const matching = education.filter((current) => current.Qualification.toLowerCase().startsWith(level));
    const index = education.findIndex((current) =>
      current.Qualification.toLowerCase().startsWith(level) && !rowHasData(current, ["Qualification"])
    );
    if (index !== -1) education = education.map((current, i) => (i === index ? row : current));
    else if (!matching.length || level === "certification") education = [...education, row];
  });
  next.education = education;

  return next;
}

const formatBytes = (bytes) =>
  bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

// Which sections are complete, for the progress stepper
function sectionCompletion(form, files) {
  const allFilled = (keys) => keys.every((key) => filled(form[key]));
  return {
    personal:
      allFilled(PERSONAL_KEYS) &&
      form.gender !== "Select" &&
      IDENTITY_DOCUMENTS.every(({ key }) => files[key]),
    family: form.family.some((row) => rowHasData(row)),
    education:
      form.education.some((row) => rowHasData(row, ["Qualification"])) &&
      Boolean(files.education_certificate),
    experience: allFilled(EXPERIENCE_KEYS),
    references: allFilled(REFERENCE_KEYS),
    bank: bankComplete(form, files),
    policies: policiesComplete(form),
    declaration: form.declaration,
  };
}

// The names of what is still missing in a section, so the error can say exactly what to fill
function missingItems(form, files, sectionId) {
  const emptyKeys = (keys) => keys.filter((key) => !filled(form[key])).map((key) => FIELD_LABELS[key] || key);
  const items = {
    personal: () => [
      ...emptyKeys(PERSONAL_KEYS),
      ...(form.gender === "Select" ? ["Gender"] : []),
      ...IDENTITY_DOCUMENTS.filter(({ key }) => !files[key]).map(({ key }) => DOCUMENT_LABELS[key]),
    ],
    family: () => ["Family Details (add at least one member)"],
    education: () => [
      ...(form.education.some((row) => rowHasData(row, ["Qualification"])) ? [] : ["Education Details"]),
      ...(files.education_certificate ? [] : [DOCUMENT_LABELS.education_certificate]),
    ],
    experience: () => emptyKeys(EXPERIENCE_KEYS),
    references: () => emptyKeys(REFERENCE_KEYS),
    policies: () => ["Company Policies (open and sign every policy)"],
    declaration: () => ["Employee Declaration"],
  };
  return (items[sectionId] || (() => []))();
}

// "Date of Birth (Use DD/MM/YYYY ...)" for every field that has an inline error
const invalidItems = (form) =>
  Object.entries(fieldErrors(form, true)).map(([key, message]) => `${FIELD_LABELS[key] || key} (${message})`);

// Age from a DD/MM/YYYY date, 0 if the date is invalid
function calculateAge(dob) {
  const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec((dob || "").trim());
  if (!match) return 0;
  const [day, month, year] = [+match[1], +match[2], +match[3]];
  const birth = new Date(year, month - 1, day);
  if (birth.getDate() !== day || birth.getMonth() !== month - 1) return 0;
  const today = new Date();
  let age = today.getFullYear() - year;
  if (today.getMonth() < month - 1 || (today.getMonth() === month - 1 && today.getDate() < day)) {
    age -= 1;
  }
  return Math.max(0, Math.min(age, 100));
}

async function postForm(url, body) {
  let response;
  try {
    response = await fetch(url, { method: "POST", body });
  } catch {
    throw new Error("Could not reach the server. Is react_onboard.py still running?");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function useObjectUrl(file) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    if (!file) { setUrl(null); return; }
    const objectUrl = URL.createObjectURL(file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);
  return url;
}

// The section nearest the top of the viewport, to highlight in the stepper
function useActiveSection(ids) {
  const [active, setActive] = useState(ids[0]);
  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length) setActive(visible[0].target.id);
      },
      { rootMargin: "-96px 0px -55% 0px" }
    );
    ids.forEach((id) => {
      const element = document.getElementById(id);
      if (element) observer.observe(element);
    });
    return () => observer.disconnect();
  }, [ids]);
  return active;
}

// Click-to-browse plus drag-and-drop, with type and size checks
function useFilePicker(kind, onChange) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState(null);
  const rules = FILE_KINDS[kind];

  const accept = (file) => {
    if (!file) return;
    if (!rules.test(file)) { setError(rules.error); return; }
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) { setError(`File is larger than ${MAX_UPLOAD_MB} MB.`); return; }
    setError(null);
    onChange(file);
  };

  return {
    error,
    dragging,
    browse: () => inputRef.current?.click(),
    remove: () => { setError(null); onChange(null); },
    dropProps: {
      onDragOver: (event) => { event.preventDefault(); setDragging(true); },
      onDragLeave: () => setDragging(false),
      onDrop: (event) => {
        event.preventDefault();
        setDragging(false);
        accept(event.dataTransfer.files[0]);
      },
    },
    input: (
      <input
        ref={inputRef}
        type="file"
        hidden
        accept={rules.accept}
        onChange={(event) => { accept(event.target.files[0]); event.target.value = ""; }}
      />
    ),
  };
}

// ---------------------------------------------------------------
// Icons (Feather-style strokes)
// ---------------------------------------------------------------

const ICONS = {
  upload: <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" /></>,
  check: <polyline points="20 6 9 17 4 12" />,
  file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></>,
  user: <><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></>,
  users: <><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></>,
  cap: <><path d="M22 10 12 5 2 10l10 5 10-5z" /><path d="M6 12v5c3 3 9 3 12 0v-5" /></>,
  briefcase: <><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /></>,
  chat: <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />,
  shield: <><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><polyline points="9 12 11 14 15 10" /></>,
  sparkles: <><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" /><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z" /></>,
  eye: <><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></>,
  id: <><rect x="2" y="5" width="20" height="14" rx="2" /><circle cx="8" cy="12" r="2" /><path d="M14 10h4M14 14h4" /></>,
  camera: <><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" /><circle cx="12" cy="13" r="4" /></>,
  refresh: <><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></>,
  trash: <><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" /><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /></>,
  plus: <><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></>,
  x: <><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>,
  pin: <><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" /><circle cx="12" cy="10" r="3" /></>,
  phone: <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z" />,
  clock: <><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></>,
  lock: <><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></>,
  download: <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></>,
  alert: <><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></>,
  info: <><circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" /></>,
  chevron: <polyline points="6 9 12 15 18 9" />,
  crop: <><path d="M6.13 1L6 16a2 2 0 0 0 2 2h15" /><path d="M1 6.13L16 6a2 2 0 0 1 2 2v15" /></>,
  rotateLeft: <><polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></>,
  rotateRight: <><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></>,
  zoomIn: <><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /><line x1="11" y1="8" x2="11" y2="14" /><line x1="8" y1="11" x2="14" y2="11" /></>,
  zoomOut: <><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /><line x1="8" y1="11" x2="14" y2="11" /></>,
  building: <><rect x="4" y="2" width="16" height="20" rx="2" /><path d="M9 22v-4h6v4M8 6h.01M16 6h.01M12 6h.01M8 10h.01M16 10h.01M12 10h.01M8 14h.01M16 14h.01M12 14h.01" /></>,
};

function Icon({ name, size = 18 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  );
}

// ---------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------

// What HR flagged on an application sent back for changes: form key / document key -> note
const FlagsContext = React.createContext({});

// The note HR left on a field, document or section (payslip_1..6 all show on "payslips")
function FlagNote({ name }) {
  const flags = React.useContext(FlagsContext);
  const note = flags[name] || (name === "payslips" && Object.entries(flags).find(([key]) => key.startsWith("payslip_"))?.[1]);
  if (!note) return null;
  return <div className="flag-note" role="alert"><Icon name="alert" size={15} /><span><b>HR asked you to fix this:</b> {note}</span></div>;
}

// The form key a field edits: bind() / bindValue() / bindPhone() put it on the control as data-key
function fieldKey(children) {
  let found = null;
  React.Children.forEach(children, (child) => {
    if (found || !React.isValidElement(child)) return;
    found = child.props["data-key"] || fieldKey(child.props.children);
  });
  return found;
}

// `group` renders a <div> instead of a <label>, for inputs made of several controls
function Field({ label, required, wide, badge, error, hint, group, children }) {
  const Tag = group ? "div" : "label";
  const flagKey = fieldKey(children);
  const flagged = Boolean(flagKey) && Boolean(React.useContext(FlagsContext)[flagKey]);
  return (
    <Tag className={"field" + (wide ? " wide" : "") + (error ? " has-error" : "") + (flagged ? " flagged" : "")}>
      <span className="label">
        {label}
        {required && <span className="req">*</span>}
        {badge && <span className="auto-badge"><Icon name="sparkles" size={11} />{badge}</span>}
      </span>
      {children}
      {error
        ? <span className="field-error" role="alert">{error}</span>
        : hint && <span className="field-hint">{hint}</span>}
      {flagKey && <FlagNote name={flagKey} />}
    </Tag>
  );
}

function Alert({ type, children }) {
  const icon = { error: "alert", success: "check", info: "info" }[type];
  return (
    <div className={`alert ${type === "loading" ? "info" : type}`} role={type === "error" ? "alert" : "status"}>
      {type === "loading" ? <span className="spinner dark" /> : <Icon name={icon} size={16} />}
      <div>{children}</div>
    </div>
  );
}

function DateInput({ value, onChange }) {
  return (
    <input
      value={value}
      placeholder="DD/MM/YYYY"
      inputMode="numeric"
      maxLength={10}
      onChange={(event) => onChange(maskDate(event.target.value))}
    />
  );
}

// Digits only: decimals, signs and letters are refused with a short warning
function WholeNumberInput({ value, onChange, maxLength, placeholder }) {
  const [warning, setWarning] = useState(false);
  return (
    <>
      <input
        value={value}
        inputMode="numeric"
        maxLength={maxLength}
        placeholder={placeholder}
        onBlur={() => setWarning(false)}
        onChange={(event) => {
          const text = event.target.value.trim();
          if (/^\d*$/.test(text)) {
            setWarning(false);
            onChange(text.replace(/^0+(?=\d)/, ""));
          } else {
            setWarning(true);
          }
        }}
      />
      {warning && <span className="field-error">Whole numbers only. No decimals or letters.</span>}
    </>
  );
}

// Chips picked from LANGUAGES; stored as "English, Hindi"
function LanguageInput({ value, onChange }) {
  const selected = value ? value.split(",").map((part) => part.trim()).filter(Boolean) : [];
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(null);
  const [highlight, setHighlight] = useState(0);
  const inputRef = useRef(null);

  const search = query.trim().toLowerCase();
  const startsWith = (language) => language.toLowerCase().startsWith(search);
  const matches = LANGUAGES
    .filter((language) => !selected.includes(language) && language.toLowerCase().includes(search))
    .sort((a, b) => startsWith(b) - startsWith(a))
    .slice(0, 8);

  const resolve = (entry) =>
    LANGUAGES.find((language) => language.toLowerCase() === entry.trim().toLowerCase());

  const commit = (entries) => {
    const next = [...selected];
    const unknown = [];
    entries.map((entry) => entry.trim()).filter(Boolean).forEach((entry) => {
      const language = resolve(entry);
      if (!language) unknown.push(entry);
      else if (!next.includes(language)) next.push(language);
    });
    if (next.length !== selected.length) onChange(next.join(", "));
    setQuery(unknown.join(", "));
    setError(unknown.length ? `Not a recognised language: ${unknown.join(", ")}. Pick one from the list.` : null);
    setHighlight(0);
  };

  const remove = (language) => onChange(selected.filter((item) => item !== language).join(", "));

  const onKeyDown = (event) => {
    if (event.key === "Enter" || event.key === "," || (event.key === "Tab" && search)) {
      if (event.key !== "Tab" || search) event.preventDefault();
      if (search) commit([matches[highlight] || query]);
    } else if (event.key === "Backspace" && !query && selected.length) {
      remove(selected[selected.length - 1]);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setHighlight((index) => Math.min(index + 1, matches.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((index) => Math.max(index - 1, 0));
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <>
      <div className={"tags" + (error ? " invalid" : "")} onClick={() => inputRef.current?.focus()}>
        {selected.map((language) => (
          <span className="tag" key={language}>
            {language}
            <button type="button" aria-label={`Remove ${language}`} onClick={() => remove(language)}>
              <Icon name="x" size={12} />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          value={query}
          aria-label="Add a language"
          placeholder={selected.length ? "Add another…" : "Type a language, e.g. Hindi"}
          onFocus={() => setOpen(true)}
          onBlur={() => { setOpen(false); if (search) commit([query]); }}
          onKeyDown={onKeyDown}
          onChange={(event) => {
            const text = event.target.value;
            setOpen(true);
            setHighlight(0);
            if (text.includes(",")) commit(text.split(","));
            else { setQuery(text); setError(null); }
          }}
        />
        {open && matches.length > 0 && (
          <ul className="suggestions" role="listbox">
            {matches.map((language, index) => (
              <li key={language}>
                <button
                  type="button"
                  className={index === highlight ? "on" : ""}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => commit([language])}
                >{language}</button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {error && <span className="field-error" role="alert">{error}</span>}
    </>
  );
}

function PhoneInput({ code, number, onCode, onNumber }) {
  const country = COUNTRIES.find((item) => item.dial === code) || COUNTRIES[0];
  const maxDigits = (dial) => (dial === "+91" ? 10 : 14);

  const setNumber = (text) => {
    let digits = text.replace(/\D/g, "");
    // A pasted "+91 98765 43210" should not keep the 91 as part of the number
    if (text.trim().startsWith("+") && digits.startsWith(code.slice(1))) digits = digits.slice(code.length - 1);
    onNumber(digits.slice(0, maxDigits(code)));
  };

  return (
    <div className="phone">
      <label className="phone-code" title={country.name}>
        <span>{country.iso}</span>
        <strong>{country.dial}</strong>
        <Icon name="chevron" size={14} />
        <select
          value={code}
          aria-label="Country code"
          onChange={(event) => {
            onCode(event.target.value);
            onNumber(number.slice(0, maxDigits(event.target.value)));
          }}
        >
          {COUNTRIES.map((item) => (
            <option key={item.iso} value={item.dial}>{item.name} ({item.dial})</option>
          ))}
        </select>
      </label>
      <input
        type="tel"
        inputMode="numeric"
        value={number}
        aria-label="Phone number"
        placeholder={code === "+91" ? "98765 43210" : "Phone number"}
        onChange={(event) => setNumber(event.target.value)}
      />
    </div>
  );
}

function Section({ id, done, children }) {
  const { title, subtitle, icon } = SECTION[id];
  return (
    <section className="section" id={id} aria-labelledby={`${id}-title`}>
      <header className="section-head">
        <span className="section-icon"><Icon name={icon} size={20} /></span>
        <div>
          <span className="section-step">Step {STEP_OF[id] + 1} of {STEPS.length}</span>
          <h2 id={`${id}-title`}>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <span className={`pill ${done ? "done" : "todo"}`}>
          {done ? <><Icon name="check" size={12} />Complete</> : "Pending"}
        </span>
      </header>
      <div className="section-body">
        <FlagNote name={id} />
        {children}
      </div>
    </section>
  );
}

function GroupHead({ icon, title, description, children }) {
  return (
    <div className="group-head">
      <div>
        <h3>{icon && <Icon name={icon} size={16} />}{title}</h3>
        {description && <p>{description}</p>}
      </div>
      {children}
    </div>
  );
}

// Sidebar on wide screens: the steps, plus links to the sections of the current step
function Stepper({ step, stepDone, canOpen, onOpen, completion, percent }) {
  const active = useActiveSection(STEPS[step].sections);
  return (
    <aside className="stepper" aria-label="Form progress">
      <h2>Onboarding steps</h2>
      <ol>
        {STEPS.map((item, index) => {
          const current = index === step;
          return (
            <li key={item.id}>
              <button
                type="button"
                className={"step" + (current ? " active" : "") + (stepDone[index] ? " done" : "")}
                aria-current={current ? "step" : undefined}
                disabled={!canOpen(index)}
                onClick={() => onOpen(index)}
              >
                <span className="step-dot">{stepDone[index] ? <Icon name="check" size={13} /> : index + 1}</span>
                {item.title}
              </button>
              {current && item.sections.length > 1 && (
                <ol className="substeps">
                  {item.sections.map((id) => (
                    <li key={id}>
                      <a
                        href={`#${id}`}
                        className={"substep" + (active === id ? " active" : "") + (completion[id] ? " done" : "")}
                      >{SECTION[id].title}</a>
                    </li>
                  ))}
                </ol>
              )}
            </li>
          );
        })}
      </ol>
      <div className="stepper-foot">
        <strong>{percent}%</strong>complete
        <div className="bar"><span style={{ width: `${percent}%` }} /></div>
      </div>
    </aside>
  );
}

// Compact step bar shown instead of the sidebar on narrow screens
function StepStrip({ step, stepDone, canOpen, onOpen }) {
  return (
    <nav className="step-strip" aria-label="Onboarding steps">
      {STEPS.map((item, index) => (
        <button
          type="button"
          key={item.id}
          className={(index === step ? "active" : "") + (stepDone[index] ? " done" : "")}
          aria-current={index === step ? "step" : undefined}
          disabled={!canOpen(index)}
          onClick={() => onOpen(index)}
        >
          <span className="step-dot">{stepDone[index] ? <Icon name="check" size={13} /> : index + 1}</span>
          {item.title}
        </button>
      ))}
    </nav>
  );
}

// Cropper.js dialog; hands back the cropped area as a JPEG File
function CropDialog({ file, title, ratios, onCancel, onApply, onSkip }) {
  const url = useObjectUrl(file);
  const imageRef = useRef(null);
  const cropperRef = useRef(null);
  const [ratio, setRatio] = useState(ratios[0].value);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!url) return;
    const cropper = new Cropper(imageRef.current, {
      viewMode: 1,
      aspectRatio: ratios[0].value,
      autoCropArea: 0.92,
      background: false,
      dragMode: "move",
      toggleDragModeOnDblclick: false,
    });
    cropperRef.current = cropper;
    return () => cropper.destroy();
  }, [url]);

  // Escape closes the dialog; the page behind it does not scroll
  useEffect(() => {
    const onKey = (event) => event.key === "Escape" && onCancel();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onCancel]);

  const chooseRatio = (value) => {
    setRatio(value);
    cropperRef.current?.setAspectRatio(value);
  };

  const apply = () => {
    const canvas = cropperRef.current?.getCroppedCanvas({
      maxWidth: 2000,
      maxHeight: 2000,
      fillColor: "#fff",
      imageSmoothingQuality: "high",
    });
    if (!canvas) return;
    setSaving(true);
    canvas.toBlob(
      (blob) => onApply(new File([blob], file.name, { type: "image/jpeg" })),
      "image/jpeg",
      0.92
    );
  };

  const tool = (icon, label, action) => (
    <button type="button" className="tool" title={label} aria-label={label} onClick={action}>
      <Icon name={icon} size={16} />
    </button>
  );

  return ReactDOM.createPortal(
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onCancel()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="crop-title">
        <div className="modal-head">
          <span className="section-icon"><Icon name="crop" size={18} /></span>
          <div>
            <h2 id="crop-title">Crop {title}</h2>
            <p>Drag to move the image, use the handles to resize the frame.</p>
          </div>
          <button type="button" className="icon-btn" aria-label="Close" onClick={onCancel}>
            <Icon name="x" size={18} />
          </button>
        </div>

        <div className="crop-stage">
          {url && <img ref={imageRef} src={url} alt={`${title} to crop`} />}
        </div>

        <div className="crop-toolbar">
          <div className="seg" role="group" aria-label="Crop shape">
            {ratios.map((option) => (
              <button
                type="button"
                key={option.label}
                className={Object.is(option.value, ratio) ? "on" : ""}
                aria-pressed={Object.is(option.value, ratio)}
                onClick={() => chooseRatio(option.value)}
              >{option.label}</button>
            ))}
          </div>
          <div className="tool-group">
            {tool("rotateLeft", "Rotate left", () => cropperRef.current?.rotate(-90))}
            {tool("rotateRight", "Rotate right", () => cropperRef.current?.rotate(90))}
            {tool("zoomIn", "Zoom in", () => cropperRef.current?.zoom(0.1))}
            {tool("zoomOut", "Zoom out", () => cropperRef.current?.zoom(-0.1))}
            {tool("refresh", "Reset", () => cropperRef.current?.reset())}
          </div>
        </div>

        <div className="modal-foot">
          <button type="button" className="btn btn-ghost first" onClick={onSkip}>Use original</button>
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="button" className={"btn btn-primary" + (saving ? " loading" : "")} disabled={saving} onClick={apply}>
            <Icon name="check" size={16} />Apply crop
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

function ImageUpload({ title, side, icon, ratios, file, onChange, docKey }) {
  const preview = useObjectUrl(file);
  // `original` is kept so re-cropping starts from the full image, not a crop of a crop
  const [original, setOriginal] = useState(null);
  const [cropping, setCropping] = useState(null);
  const picker = useFilePicker("image", (picked) => {
    if (picked) {
      setOriginal(picked);
      setCropping(picked);
    } else {
      setOriginal(null);
      onChange(null);
    }
  });
  const name = `${title} (${side})`;
  const dragClass = picker.dragging ? " dragging" : "";
  const closeCrop = React.useCallback(() => setCropping(null), []);

  return (
    <div className="upload">
      <div className="upload-title">
        <span>{title}<span className="req">*</span></span>
        <span className="upload-side">{side}</span>
      </div>
      {docKey && <FlagNote name={docKey} />}

      {file ? (
        <div className={"drop filled" + dragClass} {...picker.dropProps}>
          {preview && <img src={preview} alt={`${name} preview`} />}
          <span className="ok-badge"><Icon name="check" size={14} /></span>
          <div className="drop-overlay">
            <span title={file.name}>{file.name}</span>
            <button type="button" className="mini-btn" title="Crop" aria-label={`Crop ${name}`} onClick={() => setCropping(original || file)}>
              <Icon name="crop" size={14} />
            </button>
            <button type="button" className="mini-btn" title="Replace" aria-label={`Replace ${name}`} onClick={picker.browse}>
              <Icon name="refresh" size={14} />
            </button>
            <button type="button" className="mini-btn" title="Remove" aria-label={`Remove ${name}`} onClick={picker.remove}>
              <Icon name="trash" size={14} />
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className={"drop" + dragClass}
          aria-label={`Upload ${name}`}
          onClick={picker.browse}
          {...picker.dropProps}
        >
          <span className="drop-icon"><Icon name={icon} size={20} /></span>
          <strong>Click or drop image</strong>
          <small>{FILE_KINDS.image.hint}</small>
          <small>You can crop it next</small>
        </button>
      )}

      {picker.input}
      {picker.error && <span className="file-error" role="alert">{picker.error}</span>}

      {cropping && (
        <CropDialog
          file={cropping}
          title={name}
          ratios={ratios}
          onCancel={closeCrop}
          onApply={(cropped) => { setCropping(null); onChange(cropped); }}
          onSkip={() => { setCropping(null); onChange(cropping); }}
        />
      )}
    </div>
  );
}

function PdfUpload({ title, file, onChange, docKey }) {
  const url = useObjectUrl(file);
  const picker = useFilePicker("pdf", onChange);
  const dragClass = picker.dragging ? " dragging" : "";

  return (
    <div className="upload">
      {docKey && <FlagNote name={docKey} />}
      {file ? (
        <div className={"pdf-card" + dragClass} {...picker.dropProps}>
          <span className="pdf-icon"><Icon name="file" size={22} /><b>PDF</b></span>
          <div className="pdf-meta">
            <strong title={file.name}>{file.name}</strong>
            <small><Icon name="check" size={12} />Uploaded <span>· {formatBytes(file.size)}</span></small>
          </div>
          <div className="pdf-actions">
            {url && (
              <a className="btn btn-ghost btn-sm" href={url} target="_blank" rel="noopener">
                <Icon name="eye" size={15} />Preview
              </a>
            )}
            <button type="button" className="btn btn-ghost btn-sm" onClick={picker.browse}>
              <Icon name="refresh" size={14} />Replace
            </button>
            <button type="button" className="icon-btn" title="Remove" aria-label={`Remove ${title}`} onClick={picker.remove}>
              <Icon name="trash" size={16} />
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className={"drop row" + dragClass}
          aria-label={`Upload ${title} (PDF only)`}
          onClick={picker.browse}
          {...picker.dropProps}
        >
          <span className="drop-icon"><Icon name="upload" size={20} /></span>
          <span>
            <strong>Upload {title.toLowerCase()}</strong>
            <small>Drag &amp; drop or click to browse · {FILE_KINDS.pdf.hint}</small>
          </span>
        </button>
      )}

      {picker.input}
      {picker.error && <span className="file-error" role="alert">{picker.error}</span>}
    </div>
  );
}

// "Age/Gender" cell of the family table: an age box and a gender list, stored as "60/M" like before
const GENDER_CODES = { Male: "M", Female: "F", Other: "O" };

function AgeGenderCell({ value, label, onChange }) {
  const match = /^\s*(\d{0,3})\s*(?:\/\s*([MFO]))?\s*$/i.exec(value || "");

  // Older free text (for example "wad") stays editable as plain text instead of being lost
  if (!match) {
    return <input value={value} placeholder="—" aria-label={label} onChange={(event) => onChange(event.target.value)} />;
  }

  const age = match[1];
  const gender = Object.keys(GENDER_CODES).find((name) => GENDER_CODES[name] === (match[2] || "").toUpperCase()) || "";
  const compose = (nextAge, nextGender) =>
    nextGender ? `${nextAge}/${GENDER_CODES[nextGender]}` : nextAge;

  return (
    <div className="age-gender">
      <input
        value={age}
        inputMode="numeric"
        maxLength={3}
        placeholder="Age"
        aria-label={`Age, ${label}`}
        onChange={(event) => onChange(compose(event.target.value.replace(/\D/g, ""), gender))}
      />
      <select
        value={gender}
        aria-label={`Gender, ${label}`}
        onChange={(event) => onChange(compose(age, event.target.value))}
      >
        <option value="">Gender</option>
        {Object.keys(GENDER_CODES).map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
    </div>
  );
}

function EditableTable({ columns, rows, onChange, renderers = {} }) {
  const setCell = (index, column, value) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, [column]: value } : row)));

  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th className="row-num">#</th>
              {columns.map((column) => <th key={column}>{column}</th>)}
              <th aria-label="Remove row" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                <td className="row-num">{index + 1}</td>
                {columns.map((column) => (
                  <td key={column}>
                    {renderers[column] ? renderers[column]({
                      value: row[column],
                      label: `row ${index + 1}`,
                      onChange: (value) => setCell(index, column, value),
                    }) : (
                      <input
                        value={row[column]}
                        placeholder="—"
                        aria-label={`${column}, row ${index + 1}`}
                        onChange={(event) => setCell(index, column, event.target.value)}
                      />
                    )}
                  </td>
                ))}
                <td>
                  <button
                    type="button"
                    className="icon-btn"
                    title="Remove row"
                    aria-label={`Remove row ${index + 1}`}
                    onClick={() => onChange(rows.filter((_, i) => i !== index))}
                  ><Icon name="x" size={16} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onChange([...rows, emptyRow(columns)])}>
          <Icon name="plus" size={15} />Add row
        </button>
      </div>
    </>
  );
}

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (char) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
  );

const readAsDataUrl = (file) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });

// A standalone, printable HTML page with everything the employee submitted
function applicationPage(record, photoUrl) {
  const personal = record.personal_information || {};
  const emergency = personal.emergency_contact || {};
  const address = record.address || {};
  const professional = record.professional_details || {};
  const job = professional.employment_history || {};
  const references = record.references || {};

  const info = (label, value, wide) =>
    `<div class="info${wide ? " wide" : ""}"><span>${escapeHtml(label)}</span>` +
    `<strong>${escapeHtml(value === "" || value == null ? "—" : value)}</strong></div>`;
  const grid = (...items) => `<div class="grid">${items.join("")}</div>`;
  const section = (title, body) => `<section><h2>${escapeHtml(title)}</h2>${body}</section>`;
  const table = (rows) =>
    rows && rows.length
      ? `<table><thead><tr>${Object.keys(rows[0]).map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.map((row) => `<tr>${Object.values(row).map((value) => `<td>${escapeHtml(value || "—")}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      : `<p class="muted">None provided.</p>`;

  const submitted = record.submitted_at ? new Date(record.submitted_at).toLocaleString("en-IN") : "";
  const seniors = ["senior_1", "senior_2"].map((key) => references[key] || {});

  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Onboarding - ${escapeHtml(record.employee_id)}</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #f4f6fb; color: #0f172a; font: 14px/1.5 "Segoe UI", system-ui, sans-serif; }
  .page { max-width: 900px; margin: 24px auto; background: #fff; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden; }
  header { display: flex; gap: 20px; align-items: center; padding: 28px 32px; background: #0f172a; color: #fff; }
  header img { width: 96px; height: 120px; object-fit: cover; border-radius: 10px; border: 3px solid rgba(255,255,255,.2); }
  header h1 { margin: 0 0 4px; font-size: 24px; }
  header p { margin: 2px 0; color: #cbd5e1; }
  .id { display: inline-block; margin-top: 8px; padding: 4px 12px; border-radius: 8px; background: rgba(255,255,255,.12); font: 600 15px ui-monospace, Consolas, monospace; letter-spacing: .04em; }
  section { padding: 22px 32px; border-top: 1px solid #e2e8f0; }
  h2 { margin: 0 0 14px; font-size: 15px; color: #4338ca; text-transform: uppercase; letter-spacing: .05em; }
  h3 { margin: 18px 0 10px; font-size: 13.5px; color: #334155; }
  .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 20px; }
  .info { display: flex; flex-direction: column; min-width: 0; }
  .info.wide { grid-column: 1 / -1; }
  .info span { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: #64748b; }
  .info strong { font-weight: 500; overflow-wrap: anywhere; white-space: pre-wrap; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px; background: #f8fafc; color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; border-bottom: 1px solid #e2e8f0; }
  td { padding: 8px; border-bottom: 1px solid #e2e8f0; }
  .muted { color: #64748b; margin: 0; }
  .docs { display: flex; flex-wrap: wrap; gap: 8px; }
  .docs span { padding: 5px 10px; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 13px; }
  .declaration { color: #065f46; background: #ecfdf5; border: 1px solid #a7f3d0; padding: 10px 14px; border-radius: 10px; margin: 0; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } header { flex-direction: column; align-items: flex-start; } section, header { padding: 18px; } }
  @media print { body { background: #fff; } .page { margin: 0; border: none; border-radius: 0; } section { break-inside: avoid; } }
</style></head>
<body><div class="page">
<header>
  ${photoUrl ? `<img src="${photoUrl}" alt="Employee photograph">` : ""}
  <div>
    <h1>${escapeHtml(personal.name)}</h1>
    <p>Employee Onboarding Application</p>
    <p>Status: ${escapeHtml(record.application_status)}${submitted ? ` · Submitted ${escapeHtml(submitted)}` : ""}</p>
    <span class="id">${escapeHtml(record.employee_id)}</span>
  </div>
</header>
${section("Personal Information", grid(
  info("Full Name", personal.name),
  info("Father's Name", personal.father_name),
  info("Date of Birth", personal.date_of_birth),
  info("Age", personal.age),
  info("Gender", personal.gender),
  info("PAN Number", personal.pan_number),
  info("Aadhaar Number", personal.aadhaar_number),
  info("Mobile Number", personal.mobile),
  info("Passport Number", personal.passport_number),
  info("Personal Email", personal.email),
  info("Languages Known", personal.languages),
  info("Hobbies and Interests", personal.hobbies),
) + "<h3>Address</h3>" + grid(
  info("Permanent Address", address.permanent_address, true),
  info("Current Address", address.current_address, true),
) + "<h3>Emergency Contact</h3>" + grid(
  info("Name", emergency.name),
  info("Relation", emergency.relation),
  info("Phone Number", emergency.phone),
))}
${section("Family Details", table(record.family_details))}
${section("Education", table(record.education_details))}
${section("Professional Experience", grid(
  info("Total Experience (Years)", professional.total_experience),
  info("Last Organisation", job.organisation),
  info("Period", `${(job.period || {}).from || "—"} to ${(job.period || {}).to || "—"}`),
  info("Designation at Joining", (job.designation || {}).at_joining),
  info("Last Position Held", (job.designation || {}).last_position_held),
  info("Gross Salary at Joining", (job.gross_salary_drawn || {}).at_joining),
  info("Gross Salary Last Drawn", (job.gross_salary_drawn || {}).last_drawn),
  info("Job Responsibility", job.job_responsibility, true),
  info("Reason for Leaving", job.reason_for_leaving, true),
))}
${section("References", seniors.map((senior, index) =>
  `<h3>Senior Reference ${index + 1}</h3>` + grid(
    info("Full Name", senior.name),
    info("Designation", senior.designation),
    info("Contact Number", senior.contact),
  )).join(""))}
${section("Documents Uploaded", `<div class="docs">${Object.keys(record.documents || {})
  .map((key) => `<span>✓ ${escapeHtml(DOCUMENT_LABELS[key] || key)}</span>`).join("")}</div>`)}
${record.bank_details ? section("Bank Details", grid(
  info("Account Holder", record.bank_details.account_holder),
  info("Account Number", record.bank_details.account_number),
  info("IFSC Code", record.bank_details.ifsc),
  info("Bank Name", record.bank_details.bank_name),
  info("Branch Address", record.bank_details.branch_address, true),
  info("Current Address", record.bank_details.current_address, true),
)) : ""}
${record.policies ? section("Company Policies", grid(
  info("Designation Offered", record.policies.designation),
  info("E-signature", record.policies.signature),
  info("Signed On", record.policies.signed_on),
) + `<h3>Accepted</h3><div class="docs">${Object.values(record.policies.accepted || {})
  .map((policy) => `<span>✓ ${escapeHtml(policy.title)}</span>`).join("")}</div>` +
  "<h3>Statutory Declaration</h3>" +
  table(Object.entries((record.policies.statutory_declaration || {}).answers || {})
    .map(([point, answer]) => ({ Point: point, Answer: answer }))) +
  ((record.policies.statutory_declaration || {}).details
    ? grid(info("Details", record.policies.statutory_declaration.details, true))
    : "")
) : ""}
${section("Declaration", `<p class="declaration">✓ The employee confirmed that all the information provided is correct and complete.</p>`)}
</div></body></html>`;
}

function SubmissionResult({ record, photo, onReset }) {
  const applicationLink =
    `${location.origin}/?application=${record.employee_id}&token=${encodeURIComponent(record.access_token || "")}`;

  const download = async () => {
    const photoUrl = photo ? await readAsDataUrl(photo).catch(() => null) : null;
    const blob = new Blob([applicationPage(record, photoUrl)], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${record.employee_id}-onboarding-form.html`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="section success">
      <div className="success-icon"><Icon name="check" size={36} /></div>
      <h2>{record.resubmitted_at ? "Changes sent to HR" : "Onboarding submitted"}</h2>
      <p>
        Thank you, {record.personal_information.name}.{" "}
        {record.resubmitted_at
          ? "Your updated application is back with HR as a new review."
          : "HR will review your details and documents and get back to you shortly."}
      </p>

      <div className="emp-id">
        <small>Your Employee ID</small>
        <strong>{record.employee_id}</strong>
      </div>

      <div className="emp-link">
        <small>Keep this private link: it opens your application if HR asks for changes</small>
        <input readOnly value={applicationLink} onFocus={(event) => event.target.select()} />
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigator.clipboard?.writeText(applicationLink)}>
          Copy link
        </button>
      </div>

      <div className="doc-list">
        {Object.keys(record.documents || {}).map((key) => (
          <div className="doc-chip" key={key}>
            <Icon name="check" size={15} />{DOCUMENT_LABELS[key] || key}
          </div>
        ))}
      </div>

      <div className="success-actions">
        <button type="button" className="btn btn-primary" onClick={download}>
          <Icon name="download" size={16} />Download filled form
        </button>
        {!EDIT_LINK && <button type="button" className="btn btn-ghost" onClick={onReset}>Start a new submission</button>}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------
// Review (last step): everything entered so far, read-only
// ---------------------------------------------------------------

const phoneText = (form, key) => (filled(form[key]) ? `${form[`${key}_code`]} ${form[key]}` : "");

const capitalise = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function ReviewItem({ label, value, wide }) {
  return (
    <div className={"review-item" + (wide ? " wide" : "")}>
      <span>{label}</span>
      <strong>{filled(value) ? String(value) : "—"}</strong>
    </div>
  );
}

function ReviewTable({ rows }) {
  if (!rows.length) return <p className="review-empty">None provided.</p>;
  const columns = Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => <td key={column}>{filled(row[column]) ? row[column] : "—"}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// items: [{ label, ok, detail }]
function CheckChips({ items }) {
  return (
    <div className="doc-list">
      {items.map(({ label, ok, detail }) => (
        <div className={"doc-chip" + (ok ? "" : " missing")} key={label}>
          <Icon name={ok ? "check" : "alert"} size={15} />
          <span>{label}<small>{detail}</small></span>
        </div>
      ))}
    </div>
  );
}

function ReviewBlock({ icon, title, step, onEdit, children }) {
  return (
    <div className="review-block">
      <div className="review-head">
        <h3><Icon name={icon} size={16} />{title}</h3>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onEdit(step)}>Edit</button>
      </div>
      {children}
    </div>
  );
}

function Review({ form, files, onEdit }) {
  const photoUrl = useObjectUrl(files.photo);
  const bank = form.bank;
  const policies = form.policies;

  const fileItem = (key) => ({
    label: DOCUMENT_LABELS[key],
    ok: Boolean(files[key]),
    detail: files[key] ? files[key].name : "Not uploaded",
  });

  const statutory = STATUTORY_QUESTIONS.map(({ key, text }) => ({
    Point: `(${key}) ${capitalise(text)}`,
    Answer: policies.statutory_answers[key],
  }));

  return (
    <div className="review">
      <ReviewBlock icon="user" title="Personnel Information" step={0} onEdit={onEdit}>
        <div className="review-profile">
          {photoUrl
            ? <img src={photoUrl} alt="Your photograph" />
            : <span className="review-avatar"><Icon name="user" size={26} /></span>}
          <div>
            <strong>{form.name || "—"}</strong>
            <span>{form.email}</span>
            <span>{phoneText(form, "mobile")}</span>
          </div>
        </div>

        <div className="sub-label">Basic details</div>
        <div className="review-grid">
          <ReviewItem label="Full Name" value={form.name} />
          <ReviewItem label="Father's Name" value={form.father_name} />
          <ReviewItem label="Date of Birth" value={form.dob} />
          <ReviewItem label="Age" value={form.age} />
          <ReviewItem label="Gender" value={form.gender === "Select" ? "" : form.gender} />
          <ReviewItem label="PAN Number" value={form.pan_number} />
          <ReviewItem label="Aadhaar Number" value={form.aadhaar_number} />
          <ReviewItem label="Mobile Number" value={phoneText(form, "mobile")} />
          <ReviewItem label="Passport Number" value={form.passport_number} />
          <ReviewItem label="Personal Email" value={form.email} />
          <ReviewItem label="Languages Known" value={form.languages} />
          <ReviewItem label="Hobbies and Interests" value={form.hobbies} />
        </div>

        <div className="sub-label">Address</div>
        <div className="review-grid">
          <ReviewItem label="Permanent Address" value={form.permanent_address} wide />
          <ReviewItem label="Current Address" value={form.current_address} wide />
        </div>

        <div className="sub-label">Emergency contact</div>
        <div className="review-grid">
          <ReviewItem label="Name" value={form.emergency_name} />
          <ReviewItem label="Relation" value={form.emergency_relation} />
          <ReviewItem label="Phone Number" value={phoneText(form, "emergency_phone")} />
        </div>

        <div className="sub-label">Family members</div>
        <ReviewTable rows={form.family.filter((row) => rowHasData(row))} />

        <div className="sub-label">Education</div>
        <ReviewTable rows={form.education.filter((row) => rowHasData(row, ["Qualification"]))} />

        <div className="sub-label">Professional experience</div>
        <div className="review-grid">
          <ReviewItem label="Total Experience (Years)" value={form.total_experience} />
          <ReviewItem label="Last Organisation" value={form.organisation} />
          <ReviewItem label="Period" value={`${form.period_from || "—"} to ${form.period_to || "—"}`} />
          <ReviewItem label="Designation at Joining" value={form.joining_designation} />
          <ReviewItem label="Last Position Held" value={form.last_designation} />
          <ReviewItem label="Gross Salary at Joining" value={form.joining_salary} />
          <ReviewItem label="Gross Salary Last Drawn" value={form.last_salary} />
          <ReviewItem label="Job Responsibility" value={form.job_responsibility} wide />
          <ReviewItem label="Reason for Leaving" value={form.reason_for_leaving} wide />
        </div>

        <div className="sub-label">References</div>
        <div className="review-grid">
          {[1, 2].flatMap((n) => [
            <ReviewItem key={`${n}-name`} label={`Reference ${n} · Name`} value={form[`ref${n}_name`]} />,
            <ReviewItem key={`${n}-designation`} label={`Reference ${n} · Designation`} value={form[`ref${n}_designation`]} />,
            <ReviewItem key={`${n}-contact`} label={`Reference ${n} · Contact`} value={phoneText(form, `ref${n}_contact`)} />,
          ])}
        </div>

        <div className="sub-label">Documents</div>
        <CheckChips items={[
          ...(files.resume ? [fileItem("resume")] : []),
          ...IDENTITY_DOCUMENTS.map(({ key }) => fileItem(key)),
          fileItem("education_certificate"),
        ]} />
      </ReviewBlock>

      <ReviewBlock icon="building" title="Bank Details" step={1} onEdit={onEdit}>
        <div className="review-grid" style={{ marginTop: 16 }}>
          <ReviewItem label="Account Holder" value={bank.account_holder} />
          <ReviewItem label="Bank Name" value={bank.bank_name} />
          <ReviewItem label="Account Number" value={bank.account_number} />
          <ReviewItem label="IFSC Code" value={bank.ifsc} />
          <ReviewItem label="Branch Address" value={bank.branch_address} wide />
          <ReviewItem label="Current Address (as per bank)" value={bank.current_address} wide />
        </div>

        <div className="sub-label">Documents</div>
        <CheckChips items={[
          fileItem("bank_proof"),
          {
            label: "Payslips (last 6 months)",
            ok: files.payslips.length === PAYSLIP_COUNT,
            detail: `${files.payslips.length} of ${PAYSLIP_COUNT} uploaded`,
          },
          fileItem("relieving_letter"),
        ]} />
      </ReviewBlock>

      <ReviewBlock icon="file" title="Company Policies" step={2} onEdit={onEdit}>
        <div className="review-grid" style={{ marginTop: 16 }}>
          <ReviewItem label="Designation Offered" value={policies.designation} />
          <ReviewItem label="E-signature" value={policies.signature} />
          <ReviewItem label="Signed On" value={todayText()} />
        </div>

        <div className="sub-label">Policies</div>
        <CheckChips items={POLICY_LIST.map(({ key, title }) => ({
          label: title,
          ok: policies.accepted[key],
          detail: policies.accepted[key] ? "Read and accepted" : "Not accepted yet",
        }))} />

        <div className="sub-label">Statutory declaration</div>
        <ReviewTable rows={statutory} />
        {statutoryHasYes(policies) && (
          <div className="review-grid" style={{ marginTop: 14 }}>
            <ReviewItem label="Details for points answered Yes" value={policies.statutory_details} wide />
          </div>
        )}
      </ReviewBlock>
    </div>
  );
}

// ---------------------------------------------------------------
// Draft: a page refresh keeps every step except the one being filled in
// ---------------------------------------------------------------
// Text fields live in sessionStorage (gone when the tab closes). Uploaded files live in
// IndexedDB (sessionStorage is too small for them) and are deleted on a fresh visit.

const DRAFT_KEY = "onboarding_draft_v1";

// The private link HR's flags send the employee back through: /?application=IGTPL003&token=...
// The server holds that application, so the browser draft is not used for it.
const EDIT_LINK = (() => {
  const params = new URLSearchParams(location.search);
  return params.get("application") && params.get("token")
    ? { id: params.get("application"), token: params.get("token") }
    : null;
})();

// The application's uploaded documents, fetched back as Files so the form can send them again
async function fetchApplicationFiles(link, documents) {
  const files = { ...INITIAL_FILES };
  const payslips = [];
  await Promise.all(documents.map(async ({ key, filename }) => {
    const response = await fetch(`/api/application/${link.id}/documents/${key}?token=${encodeURIComponent(link.token)}`);
    if (!response.ok) return;
    const blob = await response.blob();
    const file = new File([blob], filename, { type: blob.type });
    if (key.startsWith("payslip_")) payslips[Number(key.slice(8)) - 1] = file;
    else if (key in files) files[key] = file;
  }));
  files.payslips = payslips.filter(Boolean);
  return files;
}

const STATUS_TEXT = {
  "New": "Your application has been received and is waiting for HR to review it.",
  "Pending": "HR has reviewed your application and kept it pending. They will contact you if they need anything.",
  "Approved": "Your application has been approved. Welcome aboard!",
  "Rejected": "HR has reviewed your application and could not take it forward.",
  "Changes Requested": "HR asked for changes, but this application can no longer be edited here. Please contact HR.",
};

function FlagBanner({ flags }) {
  if (!flags.length) return null;
  return (
    <div className="flag-banner" role="alert">
      <h3><Icon name="alert" size={18} />HR asked you to fix {flags.length === 1 ? "1 item" : `${flags.length} items`}</h3>
      <p>Update the highlighted fields or documents, then send the application back from the last step.</p>
      <ul>
        {flags.map((flag) => <li key={flag.id}><b>{flag.label}:</b> {flag.note}</li>)}
      </ul>
    </div>
  );
}

function ApplicationStatus({ edit }) {
  return (
    <div className="layout single" style={{ marginTop: 80 }}>
      <section className="section success">
        {edit.state === "loading" && <><span className="spinner dark" /><h2>Opening your application…</h2></>}
        {edit.state === "error" && <><h2>We could not open this application</h2><p>{edit.message}</p></>}
        {edit.state === "locked" && (
          <>
            <h2>Hello {edit.name}, your application is {edit.status.toLowerCase()}</h2>
            <p>{STATUS_TEXT[edit.status] || ""}</p>
            <div className="emp-id"><small>Employee ID</small><strong>{edit.id}</strong></div>
          </>
        )}
      </section>
    </div>
  );
}

// Form keys and file keys that belong to each wizard step (STEPS order)
const STEP_FORM_KEYS = [
  Object.keys(INITIAL_FORM).filter((key) => !["bank", "policies", "declaration"].includes(key)),
  ["bank"],
  ["policies"],
  ["declaration"],
];
const STEP_FILE_KEYS = [
  ["aadhaar_front", "aadhaar_back", "pan", "photo", "education_certificate", "resume"],
  ["bank_proof", "payslips", "relieving_letter"],
  [],
  [],
];

function loadDraft() {
  if (EDIT_LINK) return null;
  try {
    const draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY));
    if (!draft || !Number.isInteger(draft.step) || !STEP_FORM_KEYS[draft.step]) return null;
    const form = { ...INITIAL_FORM, ...draft.form };
    // The step the employee was on when the page reloaded starts empty again
    STEP_FORM_KEYS[draft.step].forEach((key) => { form[key] = INITIAL_FORM[key]; });
    const onFirstStep = draft.step === 0;
    return {
      step: draft.step,
      form,
      extracted: onFirstStep ? null : draft.extracted ?? null,
      resumeData: onFirstStep ? null : draft.resumeData ?? null,
    };
  } catch (error) {
    return null;
  }
}

function openFileStore() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("onboarding", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("files");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function saveDraftFiles(files) {
  const db = await openFileStore();
  await new Promise((resolve, reject) => {
    const transaction = db.transaction("files", "readwrite");
    transaction.objectStore("files").put(files, "all");
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
  db.close();
}

async function loadDraftFiles(step) {
  const db = await openFileStore();
  const stored = await new Promise((resolve, reject) => {
    const request = db.transaction("files").objectStore("files").get("all");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  db.close();
  const files = { ...INITIAL_FILES, ...stored };
  STEP_FILE_KEYS[step].forEach((key) => { files[key] = INITIAL_FILES[key]; });
  return files;
}

function clearDraft() {
  try { sessionStorage.removeItem(DRAFT_KEY); } catch (error) { /* storage blocked */ }
  try { indexedDB.deleteDatabase("onboarding"); } catch (error) { /* storage blocked */ }
}

// ---------------------------------------------------------------
// Main app
// ---------------------------------------------------------------

function App() {
  const [draft] = useState(loadDraft);
  const [form, setForm] = useState(draft ? draft.form : INITIAL_FORM);
  const [files, setFiles] = useState(INITIAL_FILES);
  // Saved files are loaded asynchronously; nothing is saved over them until they are back
  const [restored, setRestored] = useState(!draft);
  const [extracting, setExtracting] = useState(false);
  const [extractMessage, setExtractMessage] = useState(null);
  const [extracted, setExtracted] = useState(draft ? draft.extracted : null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [record, setRecord] = useState(null);
  const [attempted, setAttempted] = useState(false);
  const [readingCertificate, setReadingCertificate] = useState(false);
  const [certificateMessage, setCertificateMessage] = useState(null);
  const certificateRequest = useRef(0);
  const [resumeData, setResumeData] = useState(draft ? draft.resumeData : null);
  const [resumeMessage, setResumeMessage] = useState(null);
  const resumeRequest = useRef(0);
  const [step, setStep] = useState(draft ? draft.step : 0);
  const [stepError, setStepError] = useState(null);
  const firstRender = useRef(true);

  // After a refresh, bring back the uploaded files of the other steps. A brand-new visit
  // has no draft, so any files left by an earlier session are removed.
  // Opened from an HR link: load that application from the server
  const [edit, setEdit] = useState(EDIT_LINK ? { state: "loading", ...EDIT_LINK } : null);
  const [openFlags, setOpenFlags] = useState([]);

  useEffect(() => {
    if (!EDIT_LINK) return;
    fetch(`/api/application/${EDIT_LINK.id}?token=${encodeURIComponent(EDIT_LINK.token)}`)
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "This application link is not valid.");
        if (!data.editable) {
          setEdit({ state: "locked", ...EDIT_LINK, name: data.name, status: data.status });
          return;
        }
        const loaded = await fetchApplicationFiles(EDIT_LINK, data.documents);
        setForm({ ...INITIAL_FORM, ...data.form });
        setFiles(loaded);
        setOpenFlags(data.flags);
        setEdit({ state: "ready", ...EDIT_LINK, name: data.name });
      })
      .catch((error) => setEdit({ state: "error", ...EDIT_LINK, message: error.message }));
  }, []);

  useEffect(() => {
    if (EDIT_LINK) return;
    if (!draft) {
      try { indexedDB.deleteDatabase("onboarding"); } catch (error) { /* storage blocked */ }
      return;
    }
    loadDraftFiles(draft.step)
      .then(setFiles)
      .catch(() => {})
      .finally(() => setRestored(true));
  }, []);

  useEffect(() => {
    if (!restored || record || EDIT_LINK) return;
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ step, form, extracted, resumeData }));
    } catch (error) { /* storage blocked or full: the form still works, just not after a refresh */ }
  }, [restored, record, step, form, extracted, resumeData]);

  useEffect(() => {
    if (restored && !record && !EDIT_LINK) saveDraftFiles(files).catch(() => {});
  }, [restored, record, files]);

  useEffect(() => {
    if (record) window.scrollTo({ top: 0, behavior: "smooth" });
  }, [record]);

  // Each new step starts at the top of the form
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return; }
    document.querySelector(".content")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [step]);

  const errors = fieldErrors(form, attempted);
  const completion = sectionCompletion(form, files);
  const doneCount = REQUIRED_SECTIONS.filter(({ id }) => completion[id]).length;
  const percent = Math.round((doneCount / REQUIRED_SECTIONS.length) * 100);
  const stepDone = STEPS.map(({ sections }) => sections.every((id) => completion[id]));

  // What still stops the employee from leaving a step. All the inline field
  // checks (dates, phones, PAN, Aadhaar) are on Personnel Information fields.
  const stepBlockers = (index) => ({
    missing: STEPS[index].sections.filter((id) => !completion[id] && !OPTIONAL_SECTIONS.includes(id)),
    invalid: index === 0 && Object.keys(fieldErrors(form, true)).length > 0,
  });
  const stepReady = (index) => {
    const { missing, invalid } = stepBlockers(index);
    return !missing.length && !invalid;
  };
  // Earlier steps are always open; a later one only once every step before it is done
  const canOpen = (index) => index <= step || STEPS.slice(0, index).every((_, i) => stepReady(i));

  function goToStep(index) {
    setStepError(null);
    setAttempted(false);
    setStep(index);
  }

  function nextStep() {
    const { missing, invalid } = stepBlockers(step);
    if (!missing.length && !invalid) {
      goToStep(step + 1);
      return;
    }
    setAttempted(true);
    const missingNames = missing.flatMap((id) => missingItems(form, files, id));
    setStepError(
      [
        missing.length && `Please complete: ${missingNames.join(", ")}.`,
        invalid && `Please fix: ${invalidItems(form).join("; ")}.`,
      ].filter(Boolean).join(" ")
    );
    // The red highlights render on the next paint
    requestAnimationFrame(() => {
      const field = document.querySelector(".step-panel:not([hidden]) .field.has-error");
      if (field) field.scrollIntoView({ behavior: "smooth", block: "center" });
      else document.getElementById(missing[0])?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
  const uploadCount = IDENTITY_DOCUMENTS.filter(({ key }) => files[key]).length + (files.resume ? 1 : 0);
  const extractReady = Boolean(files.aadhaar_front && files.aadhaar_back && files.pan);

  const update = (key, value) =>
    setForm((current) => {
      const next = { ...current, [key]: value };
      if (key === "dob") next.age = calculateAge(value) ? String(calculateAge(value)) : "";
      if (next.same_address) next.current_address = next.permanent_address;
      return next;
    });

  const bind = (key) => ({ value: form[key], onChange: (event) => update(key, event.target.value), "data-key": key });

  const bindValue = (key) => ({ value: form[key], onChange: (value) => update(key, value), "data-key": key });

  const bindPhone = (key) => ({
    "data-key": key,
    code: form[`${key}_code`],
    number: form[key],
    onCode: (value) => update(`${key}_code`, value),
    onNumber: (value) => update(key, value),
  });

  // The certificate is read as soon as it is uploaded and fills the matching education row
  async function readCertificate(file) {
    const request = ++certificateRequest.current;
    setReadingCertificate(true);
    setCertificateMessage(null);
    const body = new FormData();
    body.append("education_certificate", file);
    try {
      const data = await postForm("/api/extract-education", body);
      if (request !== certificateRequest.current) return;
      setForm((current) => ({ ...current, education: withCertificate(current.education, data) }));
      setCertificateMessage({
        type: "success",
        text: `Filled the ${data.level} row from your ${data.level === "Certification" ? "certificate" : "degree"}. Please verify it in the table below.`,
      });
    } catch (error) {
      if (request === certificateRequest.current) setCertificateMessage({ type: "error", text: error.message });
    } finally {
      if (request === certificateRequest.current) setReadingCertificate(false);
    }
  }

  const setCertificate = (file) => {
    setFile("education_certificate")(file);
    if (file) {
      readCertificate(file);
    } else {
      certificateRequest.current += 1;
      setReadingCertificate(false);
      setCertificateMessage(null);
    }
  };

  const setFile = (key) => (file) => setFiles((current) => ({ ...current, [key]: file }));

  // The resume is read by "Extract information", together with the identity documents
  const setResume = (file) => {
    setFile("resume")(file);
    setResumeData(null);
    setResumeMessage(null);
  };

  function resumeSummary(data) {
    const found = [...new Set([
      ...Object.keys(RESUME_FIELDS).filter((key) => filled(data[key])).map((key) => RESUME_FIELDS[key]),
      ...(data.gender ? ["gender"] : []),
      ...(data.education.length ? ["education"] : []),
    ])];
    return found.length
      ? {
          type: "success",
          text: `Read from your resume: ${found.join(", ")}. Only empty fields were filled. ` +
            "Please check them here and under Education and Professional Experience.",
        }
      : { type: "error", text: "Could not find any details in this resume. Please fill the form by hand." };
  }

  // "Auto-filled" badge while a field still holds the value read by the AI
  const autoValues = extracted && {
    name: extracted.name,
    father_name: extracted.father_name,
    dob: extracted.dob,
    gender: extracted.gender,
    permanent_address: extracted.address,
    pan_number: extracted.pan_number,
    aadhaar_number: extracted.aadhaar_number,
    mobile: extracted.mobile,
  };
  const autoBadge = (key) => {
    if (!filled(form[key])) return null;
    if (autoValues && form[key] === autoValues[key]) return "Auto-filled";
    if (resumeData && form[key] === resumeData[key]) return "From resume";
    return null;
  };

  // One click reads the identity documents and the resume (whichever are uploaded) at the
  // same time. The ID cards are applied first; the resume then fills only what is still empty.
  async function extract() {
    const readIdentity = extractReady;
    const readResume = Boolean(files.resume);
    if (!readIdentity && !readResume) {
      setExtractMessage({ type: "error", text: "Please upload Aadhaar (front and back) and PAN card, or your resume." });
      return;
    }
    setExtracting(true);
    setExtractMessage(null);
    setResumeMessage(null);
    const request = ++resumeRequest.current;

    const identityBody = new FormData();
    if (readIdentity) ["aadhaar_front", "aadhaar_back", "pan"].forEach((key) => identityBody.append(key, files[key]));
    const resumeBody = new FormData();
    if (readResume) resumeBody.append("resume", files.resume);

    const [identity, resume] = await Promise.allSettled([
      readIdentity ? postForm("/api/extract", identityBody) : Promise.resolve(null),
      readResume ? postForm("/api/extract-resume", resumeBody) : Promise.resolve(null),
    ]);
    if (request !== resumeRequest.current) return;

    if (identity.status === "rejected") {
      setExtractMessage({ type: "error", text: identity.reason.message });
    } else if (identity.value) {
      const data = identity.value;
      // "13-12-1995" from the card becomes "13/12/1995"
      data.dob = maskDate(data.dob || "");
      setExtracted(data);
      setForm((current) => ({
        ...current,
        name: data.name,
        father_name: data.father_name,
        dob: data.dob,
        gender: data.gender || current.gender,
        age: calculateAge(data.dob) ? String(calculateAge(data.dob)) : "",
        permanent_address: data.address,
        current_address: data.address,
        // A number the AI could not read keeps what the employee already typed
        pan_number: data.pan_number || current.pan_number,
        aadhaar_number: data.aadhaar_number || current.aadhaar_number,
        mobile: data.mobile || current.mobile,
        family: withFather(current.family, data.father_name),
      }));
      setExtractMessage({
        type: "success",
        text:
          (data.father_name
            ? "Details extracted. Your father's name was also added to Family Details. Please verify."
            : "Details extracted. Please verify the highlighted fields below.") +
          " Your name, PAN and Aadhaar numbers are also filled in the Company Policies forms." +
          (data.mobile ? "" : " Your mobile number is not printed on the card, so please type it in."),
      });
    }

    if (resume.status === "rejected") {
      setResumeMessage({ type: "error", text: resume.reason.message });
    } else if (resume.value) {
      setResumeData(resume.value);
      setForm((current) => withResume(current, resume.value));
      setResumeMessage(resumeSummary(resume.value));
    }

    setExtracting(false);
  }

  async function submit(event) {
    event.preventDefault();
    // Enter in a field on an earlier step moves on instead of submitting
    if (step < STEPS.length - 1) {
      nextStep();
      return;
    }
    if (Object.keys(fieldErrors(form, true)).length) {
      setStep(0);
      setAttempted(true);
      setStepError(`Please fix: ${invalidItems(form).join("; ")}.`);
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    const body = new FormData();
    body.append("data", JSON.stringify(form));
    if (EDIT_LINK) {
      body.append("employee_id", EDIT_LINK.id);
      body.append("token", EDIT_LINK.token);
    }
    Object.entries(files).forEach(([key, file]) => {
      if (Array.isArray(file)) file.forEach((item) => body.append(key, item));
      else if (file) body.append(key, file);
    });
    try {
      setRecord(await postForm("/api/submit", body));
      clearDraft();
    } catch (error) {
      setSubmitError(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  function reset() {
    clearDraft();
    setForm(INITIAL_FORM);
    setFiles(INITIAL_FILES);
    setExtracted(null);
    setExtractMessage(null);
    setSubmitError(null);
    setRecord(null);
    setAttempted(false);
    setCertificateMessage(null);
    resumeRequest.current += 1;
    setResumeData(null);
    setResumeMessage(null);
    setExtracting(false);
    setStep(0);
    setStepError(null);
  }

  if (edit && edit.state !== "ready") return <ApplicationStatus edit={edit} />;

  // key -> HR's note, for the fields and documents the employee has to fix
  const flagNotes = Object.fromEntries(openFlags.map((flag) => [flag.key, flag.note]));

  return (
    <FlagsContext.Provider value={flagNotes}>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark"><Icon name="building" size={18} /></span>
            <span>Onboarding Portal<small>Build. Grow. Together.</small></span>
          </div>
          <div className="topbar-progress" aria-label={`${percent}% complete`}>
            <span className="label-text">
              {record ? "Submitted" : <>Step <strong>{step + 1}</strong> of {STEPS.length}</>}
            </span>
            <div className="bar"><span style={{ width: `${record ? 100 : percent}%` }} /></div>
          </div>
          <ThemeToggle />
        </div>
      </header>

      <div className="hero">
        <div className="hero-inner">
          <span className="eyebrow"><Icon name="sparkles" size={13} />New employee onboarding</span>
          <h1>{edit ? `Welcome back, ${edit.name}` : "Welcome aboard! Let's get you set up."}</h1>
          <p>
            {edit
              ? "HR reviewed your application and asked for a few changes. Fix the highlighted items, then send it back for review."
              : "Upload your documents and complete your details. Your resume and identity documents can auto-fill most of your information."}
          </p>
          <div className="hero-meta">
            <span><Icon name="clock" size={15} />About 10 minutes</span>
            <span><Icon name="file" size={15} />Aadhaar, PAN &amp; education certificate</span>
            <span><Icon name="lock" size={15} />Shared only with HR</span>
          </div>
        </div>
      </div>

      {record ? (
        <main className="layout single">
          <SubmissionResult record={record} photo={files.photo} onReset={reset} />
        </main>
      ) : (
        <div className="layout">
          <Stepper
            step={step}
            stepDone={stepDone}
            canOpen={canOpen}
            onOpen={goToStep}
            completion={completion}
            percent={percent}
          />

          <form className="content" onSubmit={submit} noValidate>
            <StepStrip step={step} stepDone={stepDone} canOpen={canOpen} onOpen={goToStep} />
            <FlagBanner flags={openFlags} />

            {/* Hidden steps stay mounted, so uploads, AI results and open tabs are kept */}
            {/* Step 1: Personnel information */}
            <div className="step-panel" hidden={step !== 0}>
            <Section id="personal" done={completion.personal}>
              <div className="group">
                <GroupHead
                  icon="id"
                  title="Upload your documents"
                  description="Upload your resume (PDF), Aadhaar card (front and back), PAN card and a passport-size photo, then click Extract information. Use clear, uncropped photos."
                >
                  <span className={"counter" + (uploadCount === IDENTITY_DOCUMENTS.length + 1 ? " full" : "")}>
                    {uploadCount}/{IDENTITY_DOCUMENTS.length + 1} uploaded
                  </span>
                </GroupHead>

                <PdfUpload title="Resume" file={files.resume} onChange={setResume} docKey="resume" />

                <div className="upload-grid" style={{ marginTop: 16 }}>
                  {IDENTITY_DOCUMENTS.map(({ key, title, side, icon, ratios }) => (
                    <ImageUpload
                      key={key}
                      docKey={key}
                      title={title}
                      side={side}
                      icon={icon}
                      ratios={ratios}
                      file={files[key]}
                      onChange={setFile(key)}
                    />
                  ))}
                </div>

                <div className="ai-panel">
                  <span className="ai-icon"><Icon name="sparkles" size={20} /></span>
                  <div>
                    <strong>
                      Extract information
                      {extracting && <span className="extract-status"><span className="spinner blue" />Extracting…</span>}
                    </strong>
                    <p>
                      {extractReady || files.resume
                        ? "Intensity reads the text on your documents: name, father's name, date of birth, address, PAN and Aadhaar numbers" +
                          (files.resume ? ", plus phone, email, languages, experience and education from your resume." : ".")
                        : "Upload your resume, Aadhaar (front & back), PAN and photo to fill your details automatically."}
                    </p>
                  </div>
                  <button
                    type="button"
                    className={"btn btn-primary" + (extracting ? " loading" : "")}
                    onClick={extract}
                    disabled={(!extractReady && !files.resume) || extracting}
                  >
                    {extracting
                      ? <><span className="spinner" />Extracting…</>
                      : <><Icon name="sparkles" size={16} />Extract information</>}
                  </button>
                </div>

                {extractMessage && <Alert type={extractMessage.type}>{extractMessage.text}</Alert>}
                {resumeMessage && <Alert type={resumeMessage.type}>{resumeMessage.text}</Alert>}

                {extracted && (
                  <details>
                    <summary>View extracted information</summary>
                    <pre>{JSON.stringify(extracted, null, 2)}</pre>
                  </details>
                )}
              </div>

              <div className="group">
                <GroupHead icon="user" title="Basic details" />
                <div className="grid">
                  <Field label="Full Name" required badge={autoBadge("name")}><input {...bind("name")} /></Field>
                  <Field label="Father's Name" required badge={autoBadge("father_name")}><input {...bind("father_name")} /></Field>
                </div>
                <div className="grid three">
                  <Field label="Date of Birth" required badge={autoBadge("dob")} error={errors.dob} hint="DD/MM/YYYY">
                    <DateInput {...bindValue("dob")} />
                  </Field>
                  <Field label="Age" required error={errors.age} hint="Calculated from your date of birth">
                    <WholeNumberInput maxLength={3} placeholder="e.g. 28" {...bindValue("age")} />
                  </Field>
                  <Field label="Gender" required badge={autoBadge("gender")}>
                    <select {...bind("gender")}>
                      {["Select", "Male", "Female", "Other"].map((option) => <option key={option}>{option}</option>)}
                    </select>
                  </Field>
                </div>
                <div className="grid three">
                  <Field label="PAN Number" required badge={autoBadge("pan_number")} error={errors.pan_number}>
                    <input
                      value={form.pan_number}
                      maxLength={10}
                      placeholder="ABCDE1234F"
                      onChange={(event) => update("pan_number", event.target.value.toUpperCase().replace(/\s/g, ""))}
                    />
                  </Field>
                  <Field label="Aadhaar Number" required badge={autoBadge("aadhaar_number")} error={errors.aadhaar_number}>
                    <input
                      value={form.aadhaar_number}
                      inputMode="numeric"
                      maxLength={12}
                      placeholder="12 digits"
                      onChange={(event) => update("aadhaar_number", event.target.value.replace(/\D/g, ""))}
                    />
                  </Field>
                  <Field label="Mobile Number" required group badge={autoBadge("mobile")} error={errors.mobile}>
                    <PhoneInput {...bindPhone("mobile")} />
                  </Field>
                </div>
                <div className="grid">
                  <Field label="Passport Number"><input {...bind("passport_number")} /></Field>
                  <Field label="Personal Email Address" required badge={autoBadge("email")}>
                    <input type="email" placeholder="you@example.com" {...bind("email")} />
                  </Field>
                  <Field label="Languages Known" required group badge={autoBadge("languages")} hint="Pick from the list. Press Enter to add.">
                    <LanguageInput {...bindValue("languages")} />
                  </Field>
                  <Field label="Hobbies and Interests" required><input {...bind("hobbies")} /></Field>
                </div>
              </div>

              <div className="group">
                <GroupHead icon="pin" title="Address" />
                <div className="grid">
                  <Field label="Permanent Address" required wide badge={autoBadge("permanent_address")}>
                    <textarea {...bind("permanent_address")} />
                  </Field>
                  <label className="inline-check">
                    <input
                      type="checkbox"
                      checked={form.same_address}
                      onChange={(event) => update("same_address", event.target.checked)}
                    />
                    <span>Current address is the same as permanent address</span>
                  </label>
                  <Field label="Current Address" required wide>
                    <textarea {...bind("current_address")} disabled={form.same_address} />
                  </Field>
                </div>
              </div>

              <div className="group">
                <GroupHead icon="phone" title="Emergency contact" description="Who should we contact in case of an emergency?" />
                <div className="grid three">
                  <Field label="Name" required><input {...bind("emergency_name")} /></Field>
                  <Field label="Relation" required><input {...bind("emergency_relation")} /></Field>
                  <Field label="Phone Number" required group error={errors.emergency_phone}>
                    <PhoneInput {...bindPhone("emergency_phone")} />
                  </Field>
                </div>
              </div>
            </Section>

            {/* 2. Family */}
            <Section id="family" done={completion.family}>
              <GroupHead
                title="Family members"
                description="Your father's name from the PAN card is filled in the first row. Add the rest, or leave unused rows empty."
              />
              <EditableTable
                columns={FAMILY_COLUMNS}
                rows={form.family}
                onChange={(rows) => update("family", rows)}
                renderers={{ "Age/Gender": (props) => <AgeGenderCell {...props} /> }}
              />
            </Section>

            {/* 3. Education */}
            <Section id="education" done={completion.education}>
              <div className="group">
                <GroupHead
                  icon="file"
                  title={<>Highest education certificate<span className="req">*</span></>}
                  description="Start here. Upload a B.Tech / B.E. degree or a course certificate, and Intensity fills the Graduation or Certification row below."
                >
                  <span className="pill brand">PDF only</span>
                </GroupHead>
                <PdfUpload
                  title="Highest Education Certificate"
                  docKey="education_certificate"
                  file={files.education_certificate}
                  onChange={setCertificate}
                />

                {readingCertificate && (
                  <Alert type="loading">Intensity is reading your certificate…</Alert>
                )}
                {!readingCertificate && certificateMessage && (
                  <Alert type={certificateMessage.type}>{certificateMessage.text}</Alert>
                )}
                {files.education_certificate && !readingCertificate && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ marginTop: 12 }}
                    onClick={() => readCertificate(files.education_certificate)}
                  >
                    <Icon name="sparkles" size={15} />Read certificate again
                  </button>
                )}
              </div>

              <div className="group">
                <GroupHead
                  title="Qualifications"
                  description="Fill in each qualification you hold. % Marks and Major Subject are optional for Certification."
                />
                <EditableTable
                  columns={EDUCATION_COLUMNS}
                  rows={form.education}
                  onChange={(rows) => update("education", rows)}
                />
              </div>
            </Section>

            {/* 4. Experience */}
            <Section id="experience" done={completion.experience}>
              <div className="grid">
                <Field label="Total Experience (Years)" required badge={autoBadge("total_experience")} error={errors.total_experience} hint="Whole years, e.g. 5">
                  <WholeNumberInput maxLength={2} placeholder="e.g. 5" {...bindValue("total_experience")} />
                </Field>
                <Field label="Last Organisation" required badge={autoBadge("organisation")}><input {...bind("organisation")} /></Field>
              </div>

              <div className="sub-label">Period</div>
              <div className="grid">
                <Field label="From" required badge={autoBadge("period_from")} error={errors.period_from} hint="DD/MM/YYYY">
                  <DateInput {...bindValue("period_from")} />
                </Field>
                <Field label="To" required group badge={autoBadge("period_to")} error={errors.period_to} hint="DD/MM/YYYY, or tick Present">
                  {form.period_to === "Present"
                    ? <input value="Present" readOnly data-key="period_to" />
                    : <DateInput {...bindValue("period_to")} />}
                  <label className="inline-check">
                    <input
                      type="checkbox"
                      checked={form.period_to === "Present"}
                      onChange={(event) => update("period_to", event.target.checked ? "Present" : "")}
                    />
                    <span>Present (currently working here)</span>
                  </label>
                </Field>
              </div>

              <div className="sub-label">Designation</div>
              <div className="grid">
                <Field label="At the Time of Joining" required badge={autoBadge("joining_designation")}><input {...bind("joining_designation")} /></Field>
                <Field label="Last Position Held" required badge={autoBadge("last_designation")}><input {...bind("last_designation")} /></Field>
              </div>

              <div className="sub-label">Gross Salary Drawn</div>
              <div className="grid">
                <Field label="At the Time of Joining" required><input placeholder="₹" {...bind("joining_salary")} /></Field>
                <Field label="Last Drawn" required><input placeholder="₹" {...bind("last_salary")} /></Field>
              </div>

              <div className="sub-label">Role</div>
              <div className="grid">
                <Field label="Job Responsibility" required wide badge={autoBadge("job_responsibility")}><textarea {...bind("job_responsibility")} /></Field>
                <Field label="Reason for Leaving" wide><textarea {...bind("reason_for_leaving")} /></Field>
              </div>
            </Section>

            {/* 5. References */}
            <Section id="references" done={completion.references}>
              <div className="ref-grid">
                {[1, 2].map((n) => (
                  <div className="ref-card" key={n}>
                    <h3><span className="ref-num">{n}</span>Senior reference {n}</h3>
                    <Field label="Full Name" required><input {...bind(`ref${n}_name`)} /></Field>
                    <Field label="Designation" required><input {...bind(`ref${n}_designation`)} /></Field>
                    <Field label="Contact Number" required group error={errors[`ref${n}_contact`]}>
                      <PhoneInput {...bindPhone(`ref${n}_contact`)} />
                    </Field>
                  </div>
                ))}
              </div>
            </Section>
            </div>

            {/* Step 2: Bank details (Bank_details.py) */}
            <div className="step-panel" hidden={step !== 1}>
            <Section id="bank" done={completion.bank}>
              <BankDetails form={form} update={update} files={files} setFile={setFile} />
            </Section>
            </div>

            {/* Step 3: Company policies (policies.py) */}
            <div className="step-panel" hidden={step !== 2}>
            <Section id="policies" done={completion.policies}>
              <Policies
                form={form}
                update={update}
                errors={errors}
                autoBadge={autoBadge}
                bindPhone={bindPhone}
                attempted={attempted}
              />
            </Section>
            </div>

            {/* Step 4: Review everything, then declare and submit */}
            <div className="step-panel" hidden={step !== 3}>
            <Section id="declaration" done={completion.declaration}>
              <Review form={form} files={files} onEdit={goToStep} />

              <label className={"declare" + (form.declaration ? " checked" : "")}>
                <input
                  type="checkbox"
                  checked={form.declaration}
                  onChange={(event) => update("declaration", event.target.checked)}
                />
                <span>
                  <strong>I confirm that all the information provided is correct and complete.<span className="req">*</span></strong>
                  <span>Any false information may lead to cancellation of my employment.</span>
                </span>
              </label>

              {submitError && <Alert type="error">{submitError}</Alert>}

              <div className="submit-bar">
                <button
                  type="submit"
                  className={"btn btn-primary btn-lg" + (submitting ? " loading" : "")}
                  disabled={submitting}
                >
                  {submitting ? <><span className="spinner" />Submitting…</> : "Submit onboarding"}
                </button>
                <span className="submit-hint">{doneCount} of {REQUIRED_SECTIONS.length} required sections complete</span>
                <button type="button" className="btn btn-ghost" onClick={() => goToStep(2)}>
                  ← Back to {STEPS[2].title}
                </button>
              </div>
            </Section>
            </div>

            {step < STEPS.length - 1 && (
              <>
                {stepError && <Alert type="error">{stepError}</Alert>}
                <div className="step-nav">
                  {step > 0 ? (
                    <button type="button" className="btn btn-ghost" onClick={() => goToStep(step - 1)}>
                      ← Back
                    </button>
                  ) : <span />}
                  <button type="button" className="btn btn-primary btn-lg" onClick={nextStep}>
                    Next: {STEPS[step + 1].title} →
                  </button>
                </div>
              </>
            )}
          </form>
        </div>
      )}

      <footer className="footer">Your information is used only for HR onboarding and is kept confidential.</footer>
    </FlagsContext.Provider>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
</script>
</body>
</html>
"""


# ==========================================================
# 7. SERVER
# ==========================================================

app = Starlette(
    routes=[
        Route("/", index),
        Route("/health", health),
        Route("/api/extract", extract, methods=["POST"]),
        Route("/api/extract-education", extract_education, methods=["POST"]),
        Route("/api/extract-bank", extract_bank, methods=["POST"]),
        Route("/api/extract-resume", extract_resume, methods=["POST"]),
        Route("/api/application/{employee_id}", application),
        Route("/api/application/{employee_id}/documents/{document}", application_document),
        Route("/api/submit", submit, methods=["POST"]),
    ],
    # The page is a few hundred KB of HTML/JSX; gzip cuts the transfer several-fold
    middleware=[Middleware(GZipMiddleware, minimum_size=1024)],
)


if __name__ == "__main__":
    print(f"Employee Onboarding Portal running at http://localhost:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
