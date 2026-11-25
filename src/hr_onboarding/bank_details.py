"""
Bank details step of the onboarding portal: the employee uploads a bank proof
(cancelled cheque or passbook photo), the last 6 months' payslips and the
relieving letter. The AI model reads them to fill the bank details, and the details
are cross-checked against the PAN/Aadhaar information (name, address, DOB, PAN).

Used by employee_portal.py:
    extract_bank_documents()   /api/extract-bank: read documents + run checks
    read_uploads(), validate_uploads(), save_uploads()   files on submit
    validate_bank(), build_bank_record()                 form data on submit
    inject()                   adds CSS and the React component to the page
"""

import re
from concurrent.futures import ThreadPoolExecutor


# ==========================================================
# 1. CONFIGURATION
# ==========================================================

PAYSLIP_COUNT = 6

# Upload key -> (label, file type); payslips are saved as payslip_1.pdf ... payslip_6.pdf
DOCUMENTS = {
    "bank_proof": ("Bank Proof (Cheque / Passbook)", "jpg"),
    **{f"payslip_{n}": (f"Payslip {n}", "pdf") for n in range(1, PAYSLIP_COUNT + 1)},
    "relieving_letter": ("Relieving Letter", "pdf"),
}

FIELDS = {
    "account_holder": "Account Holder Name",
    "account_number": "Account Number",
    "ifsc": "IFSC Code",
    "bank_name": "Bank Name",
    "branch_address": "Bank Branch Address",
    "current_address": "Current Address (as per bank)",
}

IFSC_PATTERN = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")
ACCOUNT_PATTERN = re.compile(r"\d{9,18}")

TITLES = {"mr", "mrs", "ms", "miss", "shri", "sri", "smt", "kumari", "dr"}
COMPANY_WORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "the"}


# ==========================================================
# 2. AI EXTRACTION
# ==========================================================

def document_prompt(description, fields, notes):
    return f"""
    You are a document information extraction system.
    The images show {description}. Read them carefully and extract:
    {fields}
    {notes}
    Rules: extract only visible information, never guess, use "" when not found,
    dates as DD/MM/YYYY, no explanations, valid JSON only.
    """


def extract_bank_documents(ask_model, pdf_to_images, cheque, payslip, letter, identity):
    """Read whichever of the three documents were uploaded (the others are None) with the AI model
    and cross-check them with `identity`. The three reads are independent, so they run together."""

    bank_fields = {"account_holder": "", "account_number": "", "ifsc": "", "bank_name": "",
                   "branch_address": "", "current_address": ""}
    payslip_fields = {"employee_name": "", "pan_number": "", "dob": "", "employer": "", "month": ""}
    letter_fields = {"employee_name": "", "organisation": "", "designation": "", "last_working_day": ""}

    def read_bank():
        return ask_model(document_prompt(
            "an Indian bank cancelled cheque or passbook front page", bank_fields,
            "account_number: digits only. ifsc: 11 characters like SBIN0001234. "
            "current_address: the account holder's address if printed, else \"\".",
        ), [cheque], bank_fields)

    def read_payslip():
        return ask_model(document_prompt(
            "an employee salary payslip", payslip_fields, "month: the pay month, e.g. March 2026.",
        ), pdf_to_images(payslip, max_pages=1), payslip_fields)

    def read_letter():
        return ask_model(document_prompt(
            "an employee relieving letter", letter_fields,
            "organisation: the company issuing the letter.",
        ), pdf_to_images(letter, max_pages=1), letter_fields)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(read) if document else None
            for read, document in ((read_bank, cheque), (read_payslip, payslip), (read_letter, letter))
        ]
    bank, payslip_data, letter_data = (
        future.result() if future else dict.fromkeys(fields, "")
        for future, fields in zip(futures, (bank_fields, payslip_fields, letter_fields))
    )

    bank["account_number"] = re.sub(r"\D", "", bank["account_number"])
    bank["ifsc"] = re.sub(r"\s", "", bank["ifsc"]).upper()

    extracted = {"payslip": payslip_data, "relieving_letter": letter_data}

    return {"bank": bank, "extracted": extracted, "checks": verify(bank, extracted, identity)}


# ==========================================================
# 3. CROSS-CHECKS WITH PAN / AADHAAR
# ==========================================================

def words(text, drop=()):
    return [word for word in re.findall(r"[a-z0-9]+", str(text or "").lower()) if word not in drop]


def same_name(first, second):
    """Names match if every word of the shorter one is in the longer (initials allowed)."""

    a, b = sorted((words(first, TITLES), words(second, TITLES)), key=len)

    if not a:
        return None

    return all(any(w == v or (len(w) == 1 and v.startswith(w)) for v in b) for w in a)


def same_address(first, second):
    """Same PIN code (when both have one) and at least half the words in common."""

    pins = [set(re.findall(r"\b\d{6}\b", str(text or ""))) for text in (first, second)]

    if all(pins) and not pins[0] & pins[1]:
        return False

    a, b = ({w for w in words(text) if len(w) > 2} for text in (first, second))

    if not a or not b:
        return None

    return len(a & b) / min(len(a), len(b)) >= 0.5


def check(label, found, expected, compare, source):

    if not str(found or "").strip() or not str(expected or "").strip():
        result = None
    else:
        result = compare(found, expected)

    status = {True: "match", False: "mismatch", None: "missing"}[result]

    return {"check": label, "status": status, "document": str(found or ""), "expected": str(expected or ""), "source": source}


def verify(bank, extracted, identity):
    """Compare bank/payslip/relieving letter values with PAN, Aadhaar and form details."""

    payslip = extracted.get("payslip") or {}
    letter = extracted.get("relieving_letter") or {}
    name = identity.get("name")
    exact = lambda a, b: re.sub(r"\W", "", a).upper() == re.sub(r"\W", "", b).upper()
    company = lambda a, b: same_name(" ".join(words(a, COMPANY_WORDS)), " ".join(words(b, COMPANY_WORDS)))

    address = lambda a, b: same_address(a, b) or same_address(a, identity.get("current_address"))

    return [
        check("Account holder name", bank.get("account_holder"), name, same_name, "PAN / Aadhaar name"),
        check("Bank address", bank.get("current_address"), identity.get("permanent_address"), address, "Aadhaar address"),
        check("Payslip name", payslip.get("employee_name"), name, same_name, "PAN / Aadhaar name"),
        check("Payslip PAN", payslip.get("pan_number"), identity.get("pan_number"), exact, "PAN card"),
        check("Payslip date of birth", payslip.get("dob"), identity.get("dob"), exact, "PAN / Aadhaar DOB"),
        check("Relieving letter name", letter.get("employee_name"), name, same_name, "PAN / Aadhaar name"),
        check("Relieving letter organisation", letter.get("organisation"), identity.get("organisation"), company, "Last organisation"),
        check("Last working day", letter.get("last_working_day"), "" if str(identity.get("period_to")).lower() == "present" else identity.get("period_to"), exact, "Employment period 'To'"),
    ]


# ==========================================================
# 4. SUBMISSION
# ==========================================================

async def read_uploads(form, uploaded):
    """{document key: bytes or None}; payslips arrive as several "payslips" files."""

    payslips = [file for file in form.getlist("payslips") if hasattr(file, "read") and file.filename]
    files = {f"payslip_{n}": await file.read() for n, file in enumerate(payslips[:PAYSLIP_COUNT], 1)}

    for key in ("bank_proof", "relieving_letter"):
        file = uploaded(form, key)
        files[key] = await file.read() if file else None

    return files, len(payslips)


def validate_uploads(files, payslip_count, check_file):
    """Bank documents are optional; any that were uploaded must be the right kind of file."""

    for key, (label, file_type) in DOCUMENTS.items():
        if files.get(key):
            error = check_file(label, file_type, files[key])
            if error:
                return error

    return None


def save_uploads(files, put):
    """Store each uploaded bank document with put(filename, content); return key -> filename."""

    saved = {}

    for key, (_, file_type) in DOCUMENTS.items():
        if files.get(key):
            put(f"{key}.{file_type}", files[key])
            saved[key] = f"{key}.{file_type}"

    return saved


def bank_section(data):
    section = data.get("bank")
    return section if isinstance(section, dict) else {}


def validate_bank(data):

    """Every bank field is optional, but an account number or IFSC that was entered must be valid."""

    bank = bank_section(data)
    account_number = str(bank.get("account_number") or "").strip()
    ifsc = str(bank.get("ifsc") or "").strip().upper()

    if account_number and not ACCOUNT_PATTERN.fullmatch(account_number):
        return "Bank details - Account Number: must be 9 to 18 digits (or leave it blank)."

    if ifsc and not IFSC_PATTERN.fullmatch(ifsc):
        return "Bank details - IFSC Code: enter a valid 11-character code, for example SBIN0001234 (or leave it blank)."

    return None


def build_bank_record(data):

    bank = bank_section(data)
    details = {key: str(bank.get(key) or "").strip() for key in FIELDS}
    details["ifsc"] = details["ifsc"].upper()

    # The checks are re-run on the final typed values, so HR sees what was submitted
    identity = {key: data.get(key) for key in
                ("name", "dob", "pan_number", "permanent_address", "current_address", "organisation", "period_to")}
    extracted = bank.get("extracted") if isinstance(bank.get("extracted"), dict) else {}

    return {**details, "verification": verify(details, extracted, identity)}


# ==========================================================
# 5. REACT FRONTEND (injected into employee_portal.PAGE)
# ==========================================================

CSS = r"""
/* ---------- Bank details ---------- */
.bank-uploads { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 20px; align-items: start; }
.bank-uploads .stack { display: flex; flex-direction: column; gap: 14px; }
.payslip-list { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 6px; }
.payslip-list li { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); font-size: 13px; }
.payslip-list li span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.payslip-list li small { color: var(--muted); }
.checks { display: grid; gap: 6px; margin-top: 14px; }
.check-row { display: grid; grid-template-columns: 24px minmax(0, 1.1fr) minmax(0, 1.4fr); gap: 10px; align-items: start; padding: 9px 12px; border: 1px solid var(--line); border-radius: 10px; font-size: 13px; background: var(--surface); }
.check-row small { display: block; color: var(--muted); overflow-wrap: anywhere; }
.check-row .dot { width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; color: #fff; font-size: 12px; font-weight: 700; }
.check-row.match .dot { background: var(--success); }
.check-row.mismatch { border-color: var(--danger-line); background: var(--danger-50); }
.check-row.mismatch .dot { background: var(--danger); }
.check-row.missing .dot { background: var(--faint); }
@media (max-width: 900px) {
  .bank-uploads, .check-row { grid-template-columns: minmax(0, 1fr); }
}
"""

SCRIPT = r"""
// ---------------------------------------------------------------
// Bank details (from bank_details.py)
// ---------------------------------------------------------------

const PAYSLIP_COUNT = 6;

const INITIAL_BANK = {
  account_holder: "", account_number: "", ifsc: "", bank_name: "",
  branch_address: "", current_address: "", extracted: null,
};

// Bank details are optional (see OPTIONAL_SECTIONS: this step never blocks the employee from
// moving on), but it only shows as complete once the account has really been filled in
const bankComplete = (form) =>
  ["account_holder", "account_number", "ifsc", "bank_name"].every((key) => filled(form.bank[key]));

function PayslipUpload({ files, onChange }) {
  const inputRef = useRef(null);
  const [error, setError] = useState(null);

  const add = (picked) => {
    const valid = picked.filter((file) => FILE_KINDS.pdf.test(file) && file.size <= MAX_UPLOAD_MB * 1024 * 1024);
    setError(valid.length < picked.length ? `Only PDF files up to ${MAX_UPLOAD_MB} MB are allowed.` : null);
    onChange([...files, ...valid].slice(0, PAYSLIP_COUNT));
  };

  return (
    <div className="upload">
      <div className="upload-title">
        <span>Payslips (last 6 months)</span>
        <span className={"counter" + (files.length === PAYSLIP_COUNT ? " full" : "")}>{files.length}/{PAYSLIP_COUNT}</span>
      </div>
      <FlagNote name="payslips" />
      {files.length < PAYSLIP_COUNT && (
        <button
          type="button"
          className="drop row"
          onClick={() => inputRef.current?.click()}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => { event.preventDefault(); add([...event.dataTransfer.files]); }}
        >
          <span className="drop-icon"><Icon name="upload" size={20} /></span>
          <span>
            <strong>Add payslips</strong>
            <small>Select several at once · most recent month first · {FILE_KINDS.pdf.hint}</small>
          </span>
        </button>
      )}
      <input ref={inputRef} type="file" hidden multiple accept={FILE_KINDS.pdf.accept}
        onChange={(event) => { add([...event.target.files]); event.target.value = ""; }} />
      {error && <span className="file-error" role="alert">{error}</span>}
      <ul className="payslip-list">
        {files.map((file, index) => (
          <li key={index}>
            <Icon name="file" size={15} />
            <span title={file.name}>{index === 0 ? "Latest · " : ""}{file.name}</span>
            <small>{formatBytes(file.size)}</small>
            <button type="button" className="icon-btn" aria-label={`Remove ${file.name}`}
              onClick={() => onChange(files.filter((_, i) => i !== index))}>
              <Icon name="x" size={14} />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function BankChecks({ checks }) {
  const mark = { match: "✓", mismatch: "!", missing: "–" };
  const text = { match: "Matches", mismatch: "Does not match", missing: "Not found / not entered" };
  return (
    <div className="checks">
      {checks.map((item) => (
        <div className={`check-row ${item.status}`} key={item.check}>
          <span className="dot">{mark[item.status]}</span>
          <span><strong>{item.check}</strong><small>{text[item.status]} · vs {item.source}</small></span>
          <span><small>Document: {item.document || "—"}</small><small>Expected: {item.expected || "—"}</small></span>
        </div>
      ))}
    </div>
  );
}

function BankDetails({ form, update, files, setFile }) {
  const [reading, setReading] = useState(false);
  const [message, setMessage] = useState(null);
  const [checks, setChecks] = useState(null);
  const bank = form.bank;
  const ready = Boolean(files.bank_proof || files.payslips.length || files.relieving_letter);
  const setBank = (patch) => update("bank", { ...form.bank, ...patch });
  const bindBank = (key) => ({ value: bank[key], onChange: (event) => setBank({ [key]: event.target.value }), "data-key": `bank.${key}` });

  async function fetchDetails() {
    setReading(true);
    setMessage(null);
    const body = new FormData();
    if (files.bank_proof) body.append("bank_proof", await shrinkForReading(files.bank_proof));
    if (files.payslips.length) body.append("payslip", files.payslips[0]);
    if (files.relieving_letter) body.append("relieving_letter", files.relieving_letter);
    body.append("identity", JSON.stringify({
      name: form.name, dob: form.dob, pan_number: form.pan_number, organisation: form.organisation,
      permanent_address: form.permanent_address, current_address: form.current_address, period_to: form.period_to,
    }));
    try {
      const data = await postForm("/api/extract-bank", body);
      const fields = Object.fromEntries(Object.entries(data.bank).filter(([, value]) => filled(value)));
      update("bank", { ...form.bank, ...fields, extracted: data.extracted });
      setChecks(data.checks);
      const mismatches = data.checks.filter((item) => item.status === "mismatch").length;
      setMessage(mismatches
        ? { type: "error", text: `${mismatches} detail(s) do not match your PAN / Aadhaar information. Please review them below.` }
        : { type: "success", text: "Bank details fetched and checked against your PAN / Aadhaar. Please verify." });
    } catch (error) {
      setMessage({ type: "error", text: error.message });
    } finally {
      setReading(false);
    }
  }

  return (
    <>
      <div className="group">
        <GroupHead icon="file" title="Documents" description="Optional. Upload a cancelled cheque or passbook front page, your last 6 payslips and your relieving letter, then click Fetch bank details." />
        <div className="bank-uploads">
          <ImageUpload title="Bank Proof" side="Cheque / Passbook" icon="id" ratios={CARD_RATIOS} docKey="bank_proof"
            file={files.bank_proof} onChange={setFile("bank_proof")} />
          <div className="stack">
            <PayslipUpload files={files.payslips} onChange={setFile("payslips")} />
            <div>
              <div className="upload-title" style={{ marginBottom: 8 }}><span>Relieving Letter</span></div>
              <PdfUpload title="Relieving Letter" file={files.relieving_letter} onChange={setFile("relieving_letter")} docKey="relieving_letter" />
            </div>
          </div>
        </div>

        <div className="ai-panel">
          <span className="ai-icon"><Icon name="sparkles" size={20} /></span>
          <div>
            <strong>Fetch &amp; verify with Intensity</strong>
            <p>{ready
              ? "Reads the documents you uploaded, then checks name, address, DOB and PAN against your ID cards."
              : "Upload a bank proof, payslips or relieving letter first."}</p>
          </div>
          <button type="button" className={"btn btn-primary" + (reading ? " loading" : "")} disabled={!ready || reading} onClick={fetchDetails}>
            {reading ? <><span className="spinner" />Reading documents…</> : <><Icon name="sparkles" size={16} />Fetch bank details</>}
          </button>
        </div>
        {message && <Alert type={message.type}>{message.text}</Alert>}
        {checks && <BankChecks checks={checks} />}
      </div>

      <div className="group">
        <GroupHead icon="building" title="Bank account" description="Optional. Filled from your documents; correct anything that is wrong." />
        <div className="grid">
          <Field label="Account Holder Name"><input {...bindBank("account_holder")} /></Field>
          <Field label="Bank Name"><input {...bindBank("bank_name")} /></Field>
          <Field label="Account Number" hint="9 to 18 digits">
            <input data-key="bank.account_number" value={bank.account_number} inputMode="numeric" maxLength={18}
              onChange={(event) => setBank({ account_number: event.target.value.replace(/\D/g, "") })} />
          </Field>
          <Field label="IFSC Code" hint="e.g. SBIN0001234">
            <input data-key="bank.ifsc" value={bank.ifsc} maxLength={11}
              onChange={(event) => setBank({ ifsc: event.target.value.toUpperCase().replace(/\s/g, "") })} />
          </Field>
          <Field label="Bank Branch Address" wide><textarea {...bindBank("branch_address")} /></Field>
          <Field label="Current Address (as per bank)" wide><textarea {...bindBank("current_address")} /></Field>
        </div>
      </div>
    </>
  );
}
"""


def inject(page):
    """Add the bank styles and components to the onboarding page."""

    return page.replace("/* __BANK_CSS__ */", CSS).replace("// __BANK_SCRIPT__", SCRIPT)
