# HR Onboarding Portal: Technical Documentation

An employee onboarding system with two web apps:

- **Employee portal** (`react_onboard.py`, port 8000): a 4-step form where the employee uploads their resume, Aadhaar, PAN, photo and other documents, and an AI model reads the text from them to fill the form.
- **HR portal** (`hr_info.py`, port 8001): HR reviews every submission and its documents, can **flag** anything that is wrong, and approves, rejects or keeps it pending.

Both are Python (Starlette + Uvicorn) servers that serve a React page. There is no database: records are JSON files and documents are files on disk.

> The old `README.md`, `pyproject.toml` and `requirements.txt` still describe an earlier Streamlit/Tesseract version. They do not match the code described here (see section 9).

---

## 1. Files and what each one does

| File | Role |
|---|---|
| `react_onboard.py` | Employee portal: backend (AI calls, validation, saving) and the React form, all in one file |
| `hr_info.py` | HR portal: list, review, flags, status decisions |
| `Bank_details.py` | Bank step: the AI model reads cheque/payslip/relieving letter, cross-checks them against PAN/Aadhaar; also its React components |
| `policies.py` | The 4 company policies, the statutory declaration questions, e-signature validation; also its React components |
| `.env` | `GEMINI_API_KEY` and `GEMMA_MODEL` (not committed; listed in `.gitignore`) |
| `submissions/<id>.json` | The saved application record (what HR sees) |
| `forms/<id>.json` | The form exactly as the employee submitted it (used to reopen it after a flag) |
| `uploads/<id>/` | The employee's uploaded documents |
| `Data/` | Original document templates (joining form, NDA, statutory declaration) |

---

## 2. The AI model

### Which model

| Setting | Value |
|---|---|
| Model | **Gemma 4**, `gemma-4-26b-a4b-it` (default). The larger `gemma-4-31b-it` is also available. |
| Hosting | Google **Gemini API**, called directly over HTTPS (no SDK, only Python's `urllib`) |
| Endpoint | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` |
| Auth | API key in the `x-goog-api-key` header, read from `GEMINI_API_KEY` |
| Switching model | change `GEMMA_MODEL` in `.env` and restart |

The code lives in `gemini_generate()` and `ask_model()` in `react_onboard.py`. The first sends one request; the second turns the JSON reply into a clean dictionary of the fields wanted.

### Parameters sent with every request

| Parameter | Value | Why |
|---|---|---|
| `temperature` | `0` | Same input gives the same answer. Extraction should not be creative. |
| `responseMimeType` | `application/json` | The model must reply with JSON, so it can be parsed directly. |
| `responseSchema` | only for resume reading | Forces the exact shape of the resume reply (`RESUME_SCHEMA`). |
| `thinkingConfig.thinkingLevel` | `minimal` | The model otherwise spends seconds on hidden reasoning. In testing, a small call dropped from about 5.6 s to about 1.3 s. |

**Not set** (the API defaults apply): `topP`, `topK`, `maxOutputTokens`.

### How reliability is handled
- **Retries:** up to **4 attempts** on HTTP 429, 500, 502 and 503 (the API sometimes returns these and succeeds on retry). Waits 1 s, 2 s, then 4 s between attempts.
- **Timeout:** 120 seconds per request.
- **Reasoning text** returned as `thought` parts is dropped; only the answer is used.
- **Bad replies:** a reply wrapped in a list is unwrapped, the string `"null"` becomes an empty value, and non-text values become empty. Numbers are only kept when they match the right pattern (PAN, Aadhaar, mobile), so a misread is left blank for the employee to type.

### What the model reads

| Document | Fields extracted | Input sent |
|---|---|---|
| PAN card | name, father's name, date of birth, PAN number | 1 image |
| Aadhaar card | name, date of birth, gender, address, Aadhaar number, mobile (if printed) | 2 images (front, back) |
| Education certificate | document type (degree or certification), degree, institute, year, marks, major subject / certificate name | first **2** PDF pages as images |
| Resume | name, DOB, gender, phone, email, languages, total years, job list, latest-role summary, education list | the PDF's text (first 4 pages); a scanned resume is sent as images (first **3** pages) |
| Cheque / passbook | account holder, account number, IFSC, bank name, branch address, address | 1 image |
| Latest payslip | employee name, PAN, DOB, employer, month | first page |
| Relieving letter | employee name, organisation, designation, last working day | first page |

The prompts tell the model to extract only what is visible, never guess, and return `""` when unknown.

---

## 3. Numbers and limits used in the code

### Model and speed
| Number | Meaning |
|---|---|
| 0 | temperature |
| 4 | API attempts on a transient error (waits 1 s, 2 s, 4 s) |
| 120 s | request timeout |
| 1024 x 1024 px | images are shrunk to fit this box before being sent (fewer image tokens = faster) |
| JPEG quality 90 | for shrunk images and rendered PDF pages |
| render scale 2 | PDF pages are rendered at 2x for readability |
| 2 / 3 / 1 | pages read: certificate 2, scanned resume 3, payslip and relieving letter 1 each |
| 4 pages | pages of a text resume read for its text layer |
| 8000 characters | resume text is cut here (about 2,000 tokens) |
| 2 workers | PAN and Aadhaar are read in parallel |
| 3 workers | bank proof, payslip and relieving letter are read in parallel |

### Uploads
| Number | Meaning |
|---|---|
| 5 MB | maximum size of any uploaded file |
| JPG: bytes `FF D8 FF`, PDF: `%PDF-` | the file's real type is checked, so a renamed file is rejected |
| 6 | payslips accepted (the last six months) |
| 2000 x 2000 px, quality 0.92 | largest size and quality of a cropped image |
| 85.6 : 54 and 35 : 45 | crop shapes for ID cards and passport photos |

### Form validation rules
| Field | Rule |
|---|---|
| PAN | `AAAAA9999A` (5 letters, 4 digits, 1 letter) |
| Aadhaar | exactly 12 digits |
| Indian mobile (+91) | 10 digits starting with 6 to 9 |
| Other countries | 6 to 14 digits; dial code is `+` and 1 to 4 digits |
| Dates | `DD/MM/YYYY`, a real calendar date, not in the future |
| Age | whole number up to 3 digits, must match the date of birth |
| Total experience | whole years, up to 2 digits, must be less than age |
| Employment period | "From" is after date of birth; "To" is after "From" or is **Present** |
| Account number | 9 to 18 digits (only checked if filled) |
| IFSC | 4 letters, `0`, 6 letters/digits (only checked if filled) |
| E-signature | must match the employee's full name |

Optional fields: passport number, reason for leaving, resume, and the whole Bank Details step.

### Identity and security
| Item | Value |
|---|---|
| Employee ID | `IGTPL` + 3 digits (`IGTPL001`, `IGTPL002`, ...), one more than the highest saved. Older records use `EMP-XXXXXXXX`. |
| Private application link | token of `secrets.token_urlsafe(16)` (128 bits of randomness), compared in constant time |
| Servers | `127.0.0.1` only; employee port **8000**, HR port **8001** |

### Form structure
| Item | Number |
|---|---|
| Wizard steps | 4: Personnel Information, Bank Details, Company Policies, Review & Submit |
| Form sections | 8 |
| Education rows | 4 fixed: 10th, 12th, Graduation, Certification |
| Family table | 4 starting rows, 4 columns |
| Policies | 4: NDA, Statutory Declaration, Anti-Bribery & Anti-Corruption, HR Policy Acknowledgement |
| Statutory questions | 11 (points a to l, there is no j), each Yes or No |
| Statuses | 5: New, Changes Requested, Pending, Approved, Rejected |

### Similarity checks (bank documents against PAN/Aadhaar)
- Names match if every word of the shorter name appears in the longer one (initials allowed, titles like Mr/Mrs/Dr ignored).
- Addresses match if the PIN codes agree (when both have one) and **at least 50%** of the words (longer than 2 letters) overlap.

### Browser storage
| Item | Value |
|---|---|
| Draft text | `sessionStorage`, key `onboarding_draft_v1` (cleared when the tab closes) |
| Draft files | IndexedDB database `onboarding`, store `files` |

---

## 4. Workflow

### 4.1 Employee: filling the form

1. **Upload documents** (step 1): resume (optional), Aadhaar front and back, PAN, passport photo. Images can be cropped.
2. Click **Extract information**. The server reads the Aadhaar and PAN and the resume at the same time, then returns the values.
   - The ID cards fill their fields first; the resume then fills **only fields still empty**, so ID data wins.
   - Fields filled by the AI are labelled "Auto-filled" or "From resume".
3. The employee reviews and completes the rest of step 1: basic details, address, emergency contact, family, education (with certificate upload, which the AI also reads), experience, two references.
4. **Bank Details** (step 2, all optional): upload cheque, payslips, relieving letter and click **Fetch bank details**. The AI fills the bank fields and a table of checks shows which values match the ID cards.
5. **Company Policies** (step 3): read and accept each policy, answer the 11 statutory questions, type the e-signature.
6. **Review & Submit** (step 4): check everything and tick the declaration.

Behaviour of the form:
- **Next** is blocked until the step is complete. The message lists the exact fields to fix ("Please complete: PAN Number, Mobile Number...", "Please fix: Date of Birth (Use DD/MM/YYYY...)").
- **Refresh keeps earlier steps.** The page is saved as the employee types; after a refresh everything is restored except the step they were on.

### 4.2 What happens on submit

1. The server validates everything again (the browser checks are not trusted).
2. A new Employee ID is created and a private token is generated.
3. Files are saved to `uploads/<id>/`, the record to `submissions/<id>.json`, and the form as submitted to `forms/<id>.json`.
4. The status is **New**.
5. The employee sees the success page with their ID and their **private link**.

### 4.3 HR review and the flag loop

```
Employee submits
      |
      v
    NEW  ----------------------------+
      |                              |
      | HR reviews                   | Employee fixes and
      v                              | sends it back
 +----+-----------+-----------+      |
 |    |           |           |      |
 v    v           v           |      |
Approved  Rejected  Pending   |      |
                              v      |
                  CHANGES REQUESTED -+
                  (HR flagged items,
                   waiting for employee)
```

1. HR opens an application and checks the data and documents.
2. For anything wrong, HR clicks **Flag** beside that field or document and writes a note. Family, Education, Bank and Policies can be flagged as a whole.
3. HR clicks **Send back to employee for changes**. The status becomes **Changes Requested**. HR copies the **employee link** and sends it.
4. The employee opens the link. Their form reopens with their data and documents; a banner lists everything flagged and the flagged fields are highlighted with HR's note.
5. The employee fixes the items and resubmits. The same Employee ID is kept, the flags are marked as answered, and the status goes back to **New** (shown with a "Resubmitted" tag and sorted to the top).
6. HR re-checks (answered flags show "Employee updated this, please re-check") and chooses **Approve**, **Reject** or **Mark as Pending**. This can repeat.

Rules:
- **Approve** is blocked while flags are open.
- A resubmission is only accepted when the status is Changes Requested, with the correct token.
- After HR decides, the employee link shows a read-only status.
- Applications saved before this feature have no token or saved form, so they cannot be flagged.

---

## 5. Important components

### `react_onboard.py` (backend)
| Function | Purpose |
|---|---|
| `load_env_file` | reads `.env` into environment variables |
| `gemini_generate` | one AI request with retries, returns the JSON reply text |
| `ask_model` | calls `gemini_generate` and returns only the wanted fields, cleaned |
| `extract_employee_information` | reads PAN and Aadhaar in parallel and merges the result (validates number formats) |
| `extract_education_details` | reads the certificate and decides which table row it belongs to |
| `extract_resume_details`, `clean_resume`, `latest_job` | reads the resume; picks the latest employer, joining date, titles and total years from the job list |
| `validate`, `validate_phone`, `normalise_languages` | server-side validation of the whole submission |
| `build_record` | builds the saved record from the form data |
| `submit` | validates, saves files, record and form copy; handles first submissions and resubmissions |
| `load_application`, `application`, `application_document` | the employee's private link: token check, form and document access |
| `PAGE` | the React front end as one string; sections for fields, uploads (with cropper), stepper, review, draft saving, flag notes |

### `hr_info.py`
| Function | Purpose |
|---|---|
| `load_record`, `save_record` | read and write a submission (maps the old "Pending HR Review" status to New) |
| `employees`, `employee_detail`, `detail_payload` | the list and the full view, with documents and the employee link |
| `update_status` | Approve, Reject or Pending (Approve is blocked with open flags) |
| `request_changes` | saves HR's flags and sets Changes Requested |
| `PAGE` | React front end: sidebar list with filters, detail view, `FlagControl` for flagging |

### `Bank_details.py` and `policies.py`
- `extract_bank_documents` reads the three bank documents in parallel; `verify` builds the 8 checks (account holder, bank address, payslip name / PAN / DOB, relieving letter name / organisation, last working day).
- `validate_bank` checks account number and IFSC only if filled; `build_bank_record` stores the values and a fresh verification.
- `validate_policies` checks designation, statutory answers, acceptance of all 4 policies and the e-signature; `build_policy_record` stores what was signed and when.

---

## 6. API endpoints

**Employee portal (port 8000)**

| Method and path | Purpose |
|---|---|
| `GET /` | the form |
| `POST /api/extract` | read Aadhaar (front, back) and PAN |
| `POST /api/extract-resume` | read the resume |
| `POST /api/extract-education` | read the education certificate |
| `POST /api/extract-bank` | read any uploaded bank documents and run the checks |
| `POST /api/submit` | submit the application (a resubmission also sends `employee_id` and `token`) |
| `GET /api/application/{id}?token=` | the employee's own application, flags and form |
| `GET /api/application/{id}/documents/{key}?token=` | one of their uploaded documents |

**HR portal (port 8001)**

| Method and path | Purpose |
|---|---|
| `GET /` | the HR page |
| `GET /api/employees` | list of applications (newest activity first) |
| `GET /api/employees/{id}` | full record, documents, employee link |
| `GET /api/employees/{id}/documents/{key}` | a document |
| `POST /api/employees/{id}/status` | set Approved, Rejected or Pending |
| `POST /api/employees/{id}/flags` | send flags to the employee |

---

## 7. Saved record (`submissions/<id>.json`)

Main parts: `employee_id`, `personal_information`, `address`, `family_details`, `education_details`, `professional_details`, `references`, `bank_details` (with `verification` checks), `policies`, `documents` (field to file name), `application_status`, `submitted_at`, `reviewed_at`, and for flags: `access_token`, `flags`, `resubmitted_at`, `resubmission_count`.

Each flag: `id`, `key` (the form field or document), `label`, `note`, `raised_at`, `status` (`open` or `resolved`), `resolved_at`.

---

## 8. Running it

1. Put the key in `.env`:
   ```
   GEMINI_API_KEY=<your key>
   GEMMA_MODEL=gemma-4-26b-a4b-it
   ```
2. Install what the code uses: `starlette`, `uvicorn`, `pillow`, `pypdfium2` (the Python standard library covers the rest).
3. Start both apps from the project folder:
   ```powershell
   python react_onboard.py   # employee portal, http://localhost:8000
   python hr_info.py         # HR portal,       http://localhost:8001
   ```
4. Restart an app after changing its code; the page is built once when the server starts.

The pages load React 18.3.1, Babel Standalone 7.26.4 and Cropper.js 1.6.2 from CDNs in the browser, so the browser needs internet access.

---

## 9. Known limits and things to know

- **Stale project files:** `README.md`, `pyproject.toml` and `requirements.txt` describe the old Streamlit/Tesseract version (and Python 3.15). The apps were run here on Python 3.12. They should be updated to match the real dependencies.
- **API key:** it sits in `.env` in plain text and was shared in a chat, so rotate it in Google AI Studio when you are done testing.
- **No login:** the employee portal has no accounts. The private link is the only access to a flagged application, and HR's portal has no password (it only listens on the local machine).
- **Local storage only:** data is JSON files and folders, with no database, backups or user accounts.
- **Draft files in the browser:** uploaded files are kept in the browser (IndexedDB) so a refresh does not lose them, and are deleted on submit or on a fresh visit.
- **Extraction is a first draft:** The AI's output can be wrong, so the employee reviews every auto-filled value, and HR reviews the result.
