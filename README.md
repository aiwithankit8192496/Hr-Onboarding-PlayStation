# HR Onboarding

Employee onboarding for a company: an **employee portal** where a new joiner fills in one form and uploads their documents (an AI model reads the ID documents and fills most of it), and an **HR review portal** where HR checks each application next to the documents it came from, flags what is wrong, and approves or rejects it.

Two small Starlette web servers, each serving a React page:

| File | What it is | Port |
|---|---|---|
| `react_onboard.py` | Employee onboarding portal | 8000 |
| `hr_info.py` | HR review portal (sign-in form, signed session cookie) | 8001 |

The pages' JSX is compiled once to plain files in `static/dist/` (`python scripts/build_assets.py`, or automatically at start when `mini-racer` is installed), because both pages run under a strict Content-Security-Policy: no inline code, nothing from other sites (React and the photo cropper are copied into `static/vendor/`).

Supporting modules: `assets.py` (builds and serves the page files), `security_headers.py` (the CSP and other headers), `storage.py` (where data lives), `hr_tables.py` and `sync_tables.py` (the HR tables for Athena), `hr_login.py` (HR sign-in), `Bank_details.py`, `policies.py`, `theme.py` (light/dark/default), `jsonlog.py` (JSON logs with no personal data).

## Run it on your computer

Python 3.12 (pinned: `.python-version`, `pyproject.toml` and the Docker image all use 3.12). Data is kept in local folders (`submissions/`, `forms/`, `uploads/`, `hr_tables/`), so no AWS is needed.

```powershell
pip install -r requirements-aws.txt -r requirements-build.txt   # mini-racer builds the pages' JavaScript once
# GEMINI_API_KEY=...  in a .env file next to the code (the AI that reads documents)
$env:PORT=3000; python react_onboard.py      # employee portal   http://localhost:3000
$env:PORT=3001; python hr_info.py            # HR portal         http://localhost:3001
```

With no sign-in settings the HR portal is open (laptop only). To try the sign-in form locally set `HR_USERS` (a JSON map of login name to password hash and authenticator secret) and `SESSION_SECRET` (32+ characters), both made by `python scripts/make_hr_password_hash.py add priya --new-secret` (it shows a QR code for an authenticator app; add `--no-mfa` to skip the code on a laptop), and, for plain `http://localhost`, `HR_COOKIE_SECURE=0`. On AWS every HR user must have an authenticator: sign-in needs the login name, the password and the current 6-digit code.

## Run the tests

```powershell
pip install -r requirements-dev.txt
python -m pytest tests
```

`tests/conftest.py` lowers the scrypt cost for the test run only (the application keeps and enforces the production cost); the whole suite takes about 45 seconds.

The tests use fake AWS services (moto) and a local JavaScript engine for the CloudFront Functions; nothing reaches real AWS.

## The AWS deployment (serverless)

Two AWS Lambda functions from one Docker image (arm64), **DynamoDB** for the records, **S3** for documents and the HR tables, **CloudFront** as the only way in. About 7 to 9 applications a day, roughly **$3 to $5 a month** on AWS (estimates, see the guide). It switches itself off from 12 am to 7 am India time.

- **[AWS_SETUP_GUIDE.md](AWS_SETUP_GUIDE.md)** is the step-by-step guide (written for someone new to AWS with a company IAM user): costs, security, privacy, every step, troubleshooting, and the **first deployment test checklist** (Part 11A). Start there.
- `Dockerfile`: the image (Python 3.12.16, the Lambda Web Adapter).
- `cloudfront/`: the two CloudFront Functions (night page, HR office-IP rule).
- `scripts/migrate_mongo_to_dynamodb.py`: one-time move of old MongoDB data (dry run by default); `scripts/make_hr_password_hash.py`: makes the HR password hash.
- `Data/sources_hr/`: the HR table definitions for Athena (`athena_tables.sql`) and sample data.
- `main.tf`: **an old design (ECS and MongoDB). Do not apply it.**
- `archive/`: earlier designs and their guides (one EC2 server with MongoDB; the ECS workflow diagram). Kept for reference only.
- `docker-compose.aws.yml`, `Caddyfile`, `DEPLOY_AWS.md`: the "one EC2 server" option (B in the guide), written for the older MongoDB settings.

## Where the data goes

| Data | Place |
|---|---|
| Each application and form | DynamoDB `hr-onboarding-applications`, `hr-onboarding-forms` |
| Employee IDs (`IGTPL001`...) | DynamoDB `hr-onboarding-counters` (atomic: no duplicates) |
| Uploaded documents | private S3 bucket, `onboarding/employees/<ID>/documents/` (they wait in `onboarding/pending/` for a day first) |
| Bank and sensitive HR tables | a second private S3 bucket |
| `employees` and `employee_education` tables | the shared analytics bucket (top level), read by Athena |

Nothing personal is written to logs: only the level, a short message and the employee ID.
