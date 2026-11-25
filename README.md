# HR Onboarding

Employee onboarding for a company: an **employee portal** where a new joiner fills in one form and uploads their documents (an AI model reads the ID documents and fills most of it), and an **HR review portal** where HR checks each application next to the documents it came from, flags what is wrong, and approves or rejects it.

Two small Starlette web servers, each serving a React page. They never call each other: both read and write the same records (DynamoDB and S3 on AWS, local folders on a laptop).

| Portal | Start command | Port |
|---|---|---|
| Employee onboarding portal | `python -m hr_onboarding.employee_portal` | 8000 |
| HR review portal (sign-in form, signed session cookie) | `python -m hr_onboarding.hr_portal` | 8001 |

## Project layout

```
.
├── src/hr_onboarding/        the application (a Python package)
│   ├── employee_portal.py      employee portal: AI extraction, validation, saving, the React form
│   ├── hr_portal.py            HR portal: list, review, flags, decisions, the React page
│   ├── bank_details.py         bank step (cheque, payslips, relieving letter) and its checks
│   ├── policies.py             company policies, statutory declaration, e-signature
│   ├── storage.py              where data lives: local folders, or DynamoDB + S3
│   ├── hr_tables.py            HR tables (employees, education, bank ...) for Athena
│   ├── hr_login.py             HR sign-in: password hash, authenticator code, session cookie
│   ├── security_headers.py     Content-Security-Policy and other response headers
│   ├── assets.py               builds the pages' JavaScript and serves the page files
│   ├── theme.py                light / dark / device theme
│   ├── jsonlog.py              JSON logs with no personal data
│   └── static/vendor/          React, the photo cropper and the company logo (our own copies)
├── tests/                    the test suite (pytest)
├── scripts/                  command-line tools (build pages, HR password, data moves)
├── deploy/cloudfront/        the two CloudFront Functions (night page, HR office-IP rule)
├── docs/                     AWS setup guide, how the code works, reference documents
├── samples/hr_tables/        HR table definitions for Athena and sample data
├── requirements/             runtime.txt, build.txt, dev.txt
├── Dockerfile                one image for both portals (AWS Lambda, arm64)
├── docker-compose.yml        both portals on one machine
└── pyproject.toml            project metadata and test settings
```

The pages' JSX is compiled once to plain files in `src/hr_onboarding/static/dist/` (`python scripts/build_assets.py`, or automatically at start when `mini-racer` is installed), because both pages run under a strict Content-Security-Policy: no inline code, nothing from other sites.

## Run it on your computer

Python 3.12 (pinned: `.python-version`, `pyproject.toml` and the Docker image all use 3.12). Data is kept in local folders (`submissions/`, `forms/`, `uploads/`, `hr_tables/`), so no AWS is needed.

```powershell
pip install -r requirements/runtime.txt -r requirements/build.txt   # mini-racer builds the pages' JavaScript once
$env:PYTHONPATH = "src"                       # the package lives in src/
# GEMINI_API_KEY=...  in a .env file in the project folder (the AI that reads documents)
$env:STORAGE_BACKEND = "local"
$env:PORT = 3000; python -m hr_onboarding.employee_portal    # employee portal   http://localhost:3000
$env:PORT = 3001; python -m hr_onboarding.hr_portal          # HR portal         http://localhost:3001
```

With no sign-in settings the HR portal is open (laptop only). To try the sign-in form locally set `HR_USERS` (a JSON map of login name to password hash and authenticator secret) and `SESSION_SECRET` (32+ characters), both made by `python scripts/make_hr_password_hash.py add priya --new-secret` (it shows a QR code for an authenticator app; add `--no-mfa` to skip the code on a laptop), and, for plain `http://localhost`, `HR_COOKIE_SECURE=0`. On AWS every HR user must have an authenticator: sign-in needs the login name, the password and the current 6-digit code.

Or run both in containers: `docker compose up --build` (employee portal on 8000, HR portal on 8001).

## Run the tests

```powershell
pip install -r requirements/dev.txt
python -m pytest
```

`pyproject.toml` puts `src/` on the import path for pytest. `tests/conftest.py` lowers the scrypt cost for the test run only (the application keeps and enforces the production cost); the whole suite takes about a minute. The tests use fake AWS services (moto) and a local JavaScript engine for the CloudFront Functions; nothing reaches real AWS.

## The AWS deployment (serverless)

Two AWS Lambda functions from one Docker image (arm64), **DynamoDB** for the records, **S3** for documents and the HR tables, **CloudFront** as the only way in. About 7 to 9 applications a day, roughly **$3 to $5 a month** on AWS (estimates, see the guide). It switches itself off from 12 am to 7 am India time.

- **[docs/AWS_SETUP_GUIDE.md](docs/AWS_SETUP_GUIDE.md)** is the step-by-step guide (written for someone new to AWS with a company IAM user): costs, security, privacy, every step, troubleshooting, and the **first deployment test checklist** (Part 11A). Start there.
- [docs/DOCUMENTATION.md](docs/DOCUMENTATION.md) explains how the code works.
- Build the image: `docker buildx build --platform linux/arm64 --provenance=false -t <ecr-uri>:<tag> --push .` (the `Dockerfile` header explains the two function commands).
- `scripts/migrate_mongo_to_dynamodb.py`: one-time move of old MongoDB data (dry run by default). `scripts/sync_tables.py`: rebuilds the HR tables from the saved records. `scripts/make_hr_password_hash.py`: makes the HR password hash.
- `samples/hr_tables/`: the HR table definitions for Athena (`athena_tables.sql`) and sample data.

## Where the data goes

| Data | Place |
|---|---|
| Each application and form | DynamoDB `hr-onboarding-applications`, `hr-onboarding-forms` |
| Employee IDs (`IGTPL001`...) | DynamoDB `hr-onboarding-counters` (atomic: no duplicates) |
| Uploaded documents | private S3 bucket, `onboarding/employees/<ID>/documents/` (they wait in `onboarding/pending/` for a day first) |
| Bank and sensitive HR tables | a second private S3 bucket |
| `employees` and `employee_education` tables | the shared analytics bucket (top level), read by Athena |

Nothing personal is written to logs: only the level, a short message and the employee ID.
