"""
Company policies the new employee signs during onboarding: the Employee NDA,
the Statutory Declaration (SDF), the Anti-Bribery Policy and the HR Policy.

employee_portal.py shows them as a "Policies" step. Each policy has two columns:
the document on the left and the employee's form on the right. Name, PAN
number, Aadhaar number and mobile number are shared with the Personal
Information step, so the "Auto-fill details" button there fills them here too.

Used by employee_portal.py:
    PAYLOAD                 policy texts for the page (JSON)
    CSS, SCRIPT             styles and React components injected into the page
    validate_policies()     server-side checks before a submission is saved
    build_policy_record()   the "policies" block of the saved submission
"""

import datetime
import re


# ==========================================================
# 1. POLICY TEXT
# ==========================================================

COMPANY = "Intensity Global Technologies Limited"

# {name} and {designation} are filled in on the page from the employee's form


def p(text):
    return {"type": "p", "text": text}


def h(text):
    return {"type": "h", "text": text}


def ol(*items, strong=False):
    return {"type": "ol", "items": list(items), "strong": strong}


def ul(*items):
    return {"type": "ul", "items": list(items)}


# Statutory Declaration points, lettered as in the original form (it has no (j))
STATUTORY_QUESTIONS = [
    ("a", "ever been found in unlawful activities"),
    ("b", "ever any FIR done against me"),
    ("c", "ever been convicted of an indictable offence under a law of India"),
    ("d", "ever become bankrupt or taken the benefit of any law for the relief of bankrupt "
          "debtors, or compounded with their creditors or made an assignment of their "
          "property for their benefit"),
    ("e", "ever been disqualified from managing corporations under Part 2D.6 of the "
          "Corporations Act"),
    ("f", "ever been convicted of a sexual offence"),
    ("g", "ever been convicted of an offence that involves fraud or dishonesty"),
    ("h", "ever been part of a company or body that has been wound up"),
    ("i", "ever been convicted of an offence against the Consumer Act"),
    ("k", "ever been in breach of a government contract"),
    ("l", "Any Bank/Financial Institution has a personal outstanding against me"),
]

ANSWERS = ("Yes", "No")

POLICIES = [
    {
        "key": "nda",
        "short": "NDA",
        "title": "Employee Non-Disclosure Agreement",
        "blocks": [
            p(f"FOR GOOD CONSIDERATION, and in consideration of being employed by {COMPANY}, "
              "I {name} hereby agree and acknowledge:"),
            ol(
                "That during the course of my employment there may be disclosed to me certain "
                "trade secrets of the Company; said trade secrets consisting but not necessarily "
                "limited to:",
            ),
            ul(
                "(a) Technical information: Methods, processes, formulae, compositions, systems, "
                "techniques, inventions, machines, computer programs and research projects.",
                "(b) Business information: Customer lists, pricing data, sources of supply, "
                "financial data and marketing, production, or merchandising systems or plans.",
            ),
            p("2. I agree that I shall not during, or at any time after the termination of my "
              "employment with the Company, use for myself or others, or disclose or divulge to "
              "others including future employees, any trade secrets, confidential information, "
              "or any other proprietary data of the Company in violation of this agreement."),
            p("3. That upon the termination of my employment from the Company:"),
            ul(
                "(a) I shall return to the Company all documents and property of the Company, "
                "including but not necessarily limited to: drawings, blueprints, reports, "
                "manuals, correspondence, customer lists, computer programs, and all other "
                "materials and all copies thereof relating in any way to the Company's business, "
                "or in any way obtained by me during the course of employ. I further agree that "
                "I shall not retain copies, notes or abstracts of the foregoing.",
                "(b) The Company may notify any future or prospective employer or third party of "
                "the existence of this agreement, and shall be entitled to full injunctive "
                "relief for any breach.",
                "(c) This agreement shall be binding upon me and my personal representatives and "
                "successors in interest, and shall inure to the benefit of the Company, its "
                "successors and assigns.",
            ),
        ],
    },
    {
        "key": "statutory",
        "short": "Statutory Declaration",
        "title": "Statutory Declaration",
        "blocks": [
            p("I {name} being appointed as {designation} in " + COMPANY + " agree to the "
              "Policies of the Company (IGTL) and providing the declaration on the points "
              "below. Answer each point Yes or No in the form."),
            h("Declaration points: have I"),
            ul(*(f"({letter}) {text}" for letter, text in STATUTORY_QUESTIONS)),
            h("In addition I undertake that:"),
            ol(
                "I will not use company's Name/details for any unlawful activity.",
                "I will not use Company's details as a guarantor in any of the financial "
                "transaction and I will be sole responsible to clear those transactions.",
                "I will not use Company's details as a guarantor in any of the personal matter.",
                "I will make sure that any dispute arises there due to personal matters will not "
                "affect company and I am here by standing the guarantee for that.",
                "I undertake that I shall be fully accountable for theft, loss, damage or any "
                "unauthorized use of laptop/desktop provided at the customer site or by the "
                "company.",
                "I hereby declare that I have given the above statements in my complete sense "
                "and company will not be responsible of any dispute arises due to incorrect "
                "statements or any unlawful act done by me.",
            ),
            h("I understand that:"),
            ol(
                "failure to provide true and correct information of a material nature in this "
                "declaration may result in cancellation or suspension of my Appointment;",
                "failure to fulfil these undertakings may result in sanctions being imposed "
                "including suspension or cancellation of my Appointment.",
                strong=True,
            ),
        ],
    },
    # Draft text: replace with IGTL's approved Anti-Bribery Policy before go-live
    {
        "key": "anti_bribery",
        "short": "Anti-Bribery",
        "title": "Anti-Bribery & Anti-Corruption Policy",
        "blocks": [
            p(f"{COMPANY} conducts its business honestly and does not tolerate bribery or "
              "corruption in any form. This policy applies to every employee, wherever they work, "
              "including at customer sites."),
            h("I, {name}, agree that I will:"),
            ol(
                "Not offer, promise, give, request or accept any bribe, kickback or improper "
                "payment, in cash or in kind, directly or through anyone else.",
                "Not make facilitation (\"speed\") payments to any government official to secure "
                "or speed up a routine action.",
                "Not give or accept gifts, meals, travel or hospitality that could influence, or "
                "appear to influence, a business decision. Any gift beyond a token value is "
                "declared to HR or my manager.",
                "Declare to HR any personal, financial or family interest that conflicts, or "
                "could appear to conflict, with my duties to the Company.",
                "Keep accurate records of expenses and transactions and never create false or "
                "misleading entries.",
                "Report any bribery request, offer or suspicion to my manager or HR as soon as I "
                "become aware of it.",
                "Comply with the Prevention of Corruption Act, 1988 and all other applicable "
                "anti-corruption laws.",
            ),
            h("I understand that:"),
            ul(
                "No employee will suffer for refusing to pay or accept a bribe, even if the "
                "Company loses business as a result.",
                "A breach of this policy may lead to disciplinary action up to termination of "
                "employment, and may be reported to the authorities.",
            ),
        ],
    },
    # Draft text: replace with IGTL's approved HR Policy before go-live
    {
        "key": "hr_policy",
        "short": "HR Policy",
        "title": "HR Policy Acknowledgement",
        "blocks": [
            p("I, {name}, joining " + COMPANY + " as {designation}, acknowledge that I have "
              "received and read the Company's HR Policy, and I agree to follow it and any "
              "updates the Company communicates to me."),
            h("Summary of key policies"),
            ol(
                "Working hours and attendance: I will follow the working hours, attendance and "
                "time-recording rules of my office or customer site.",
                "Leave: I will apply for leave in advance through the Company's process, as per "
                "the leave entitlement in my appointment letter.",
                "Code of conduct: I will behave professionally and respectfully with colleagues, "
                "customers and partners.",
                "Equal opportunity and anti-harassment: The Company does not tolerate "
                "discrimination or harassment, including sexual harassment under the POSH Act, "
                "2013. I will report any such behaviour to HR or the Internal Committee.",
                "Company assets and IT use: I will use Company laptops, email, systems and data "
                "only for work, keep my passwords private and follow the information-security "
                "rules.",
                "Confidentiality: I will protect Company and customer information as set out in "
                "the Employee Non-Disclosure Agreement.",
                "Outside employment: I will not take up other employment or business that "
                "conflicts with my role without written approval from HR.",
                "Separation: I will serve the notice period stated in my appointment letter and "
                "return all Company property before my last working day.",
            ),
            p("I understand that the Company may revise its HR policies from time to time, and "
              "that breaking them may lead to disciplinary action."),
        ],
    },
]

POLICY_KEYS = [policy["key"] for policy in POLICIES]

# Sent to the page as JSON
PAYLOAD = {
    "company": COMPANY,
    "policies": POLICIES,
    "statutory_questions": [
        {"key": letter, "text": text} for letter, text in STATUTORY_QUESTIONS
    ],
}


# ==========================================================
# 2. VALIDATION AND RECORD BUILDING
# ==========================================================

def same_name(first, second):
    """Compare names ignoring case and extra spaces."""

    def tidy(text):
        return " ".join(str(text or "").split()).lower()

    return tidy(first) != "" and tidy(first) == tidy(second)


def policy_section(data):

    section = data.get("policies")
    return section if isinstance(section, dict) else {}


def validate_policies(data):
    """Return the first error message for the Policies step, or None if it is complete."""

    section = policy_section(data)
    answers = section.get("statutory_answers") or {}
    accepted = section.get("accepted") or {}

    if not str(section.get("designation") or "").strip():
        return "Policies: please enter the designation you are being appointed as."

    unanswered = [
        f"({letter})" for letter, _ in STATUTORY_QUESTIONS
        if answers.get(letter) not in ANSWERS
    ]

    if unanswered:
        return "Statutory Declaration: please answer Yes or No for " + ", ".join(unanswered) + "."

    any_yes = any(answers.get(letter) == "Yes" for letter, _ in STATUTORY_QUESTIONS)

    if any_yes and not str(section.get("statutory_details") or "").strip():
        return "Statutory Declaration: please give details for the points you answered Yes."

    not_accepted = [policy["title"] for policy in POLICIES if accepted.get(policy["key"]) is not True]

    if not_accepted:
        return "Please read and accept: " + ", ".join(not_accepted)

    if not same_name(section.get("signature"), data.get("name")):
        return "Policies: your e-signature must be your full name exactly as entered above."

    return None


def build_policy_record(data):

    section = policy_section(data)
    answers = section.get("statutory_answers") or {}
    signed_on = datetime.date.today().strftime("%d/%m/%Y")

    return {
        "designation": section["designation"].strip(),
        "signature": " ".join(section["signature"].split()),
        "signed_on": signed_on,
        "accepted": {
            policy["key"]: {"title": policy["title"], "accepted": True, "date": signed_on}
            for policy in POLICIES
        },
        "statutory_declaration": {
            "answers": {
                f"({letter}) {text}": answers[letter]
                for letter, text in STATUTORY_QUESTIONS
            },
            "details": str(section.get("statutory_details") or "").strip(),
        },
    }


# ==========================================================
# 3. REACT FRONTEND (injected into employee_portal.PAGE)
# ==========================================================

CSS = r"""
/* ---------- Policies ---------- */
.policy-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
.policy-tab {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px;
  border: 1px solid var(--line-strong);
  border-radius: 99px;
  background: var(--surface);
  color: var(--ink-2);
  font-size: 13.5px; font-weight: 600;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.policy-tab:hover { border-color: var(--brand); color: var(--brand-600); }
.policy-tab.on { background: var(--brand); border-color: var(--brand); color: #fff; }
.policy-tab .tab-dot {
  width: 18px; height: 18px;
  border-radius: 50%;
  display: grid; place-items: center;
  border: 1.5px solid currentColor;
  font-size: 10px;
}
.policy-tab.done .tab-dot { background: var(--success); border-color: var(--success); color: #fff; }
.policy-split { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 20px; align-items: start; }
.policy-doc {
  max-height: 640px;
  overflow-y: auto;
  padding: 26px 28px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--paper);
  color: var(--ink-2);
  font-size: 13.5px;
  line-height: 1.65;
}
.policy-doc h3 { margin: 0 0 16px; text-align: center; font-size: 15px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink); }
.policy-doc h4 { margin: 18px 0 6px; font-size: 13.5px; color: var(--ink); }
.policy-doc p { margin: 0 0 10px; }
.policy-doc ol, .policy-doc ul { margin: 0 0 10px; padding-left: 22px; }
.policy-doc ul { list-style: none; padding-left: 12px; }
.policy-doc li { margin-bottom: 6px; }
.policy-doc ol.strong li { font-weight: 600; color: var(--ink); }
.policy-doc mark { padding: 0 4px; border-radius: 4px; background: var(--brand-50); color: var(--brand-600); font-weight: 600; }
.policy-doc mark.blank { background: var(--mark-bg); color: var(--mark-ink); }
.policy-sign { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--line-strong); display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 16px; font-size: 12.5px; }
.policy-sign span { color: var(--muted); }
.policy-sign strong { display: block; color: var(--ink); font-weight: 600; overflow-wrap: anywhere; }
.policy-sign .esign { font-family: "Segoe Script", "Brush Script MT", cursive; font-size: 17px; font-weight: 400; color: var(--brand-600); }
.policy-form { display: flex; flex-direction: column; gap: 14px; padding: 18px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface-2); }
.policy-form h4 { margin: 0; font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.policy-form .grid { gap: 14px; }
.yn-list { display: flex; flex-direction: column; gap: 2px; max-height: 340px; overflow-y: auto; margin: 0 -6px; padding: 0 6px; }
.yn-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 13px; color: var(--ink-2); }
.yn-row:last-child { border-bottom: none; }
.yn-row.missing { color: var(--danger); }
.yn-row .seg { flex: none; }
.yn-row .seg button { padding: 4px 11px; }
.yn-row .seg button.on.yes { background: var(--danger); }
.accept-box { display: flex; gap: 10px; align-items: flex-start; padding: 12px 14px; border: 1px solid var(--line-strong); border-radius: 10px; background: var(--surface); cursor: pointer; font-size: 13.5px; }
.accept-box.checked { border-color: var(--success-line); background: var(--success-50); }
.accept-box strong { display: block; }
.accept-box small { color: var(--muted); }
.policy-nav { display: flex; justify-content: space-between; gap: 10px; }
@media (max-width: 1100px) {
  .policy-split { grid-template-columns: minmax(0, 1fr); }
  .policy-doc { max-height: 420px; }
}
@media (max-width: 640px) {
  .policy-doc { padding: 18px 16px; }
  .policy-sign { grid-template-columns: minmax(0, 1fr); }
  .yn-row { flex-direction: column; align-items: flex-start; gap: 6px; }
}
"""

SCRIPT = r"""
// ---------------------------------------------------------------
// Policies (from policies.py)
// ---------------------------------------------------------------

const POLICY_DATA = __POLICIES__;
const POLICY_LIST = POLICY_DATA.policies;
const STATUTORY_QUESTIONS = POLICY_DATA.statutory_questions;

const INITIAL_POLICIES = {
  designation: "",
  signature: "",
  statutory_details: "",
  statutory_answers: Object.fromEntries(STATUTORY_QUESTIONS.map(({ key }) => [key, ""])),
  accepted: Object.fromEntries(POLICY_LIST.map(({ key }) => [key, false])),
};

const tidyName = (text) => String(text || "").trim().replace(/\s+/g, " ").toLowerCase();

const todayText = () => {
  const today = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(today.getDate())}/${pad(today.getMonth() + 1)}/${today.getFullYear()}`;
};

const statutoryHasYes = (policies) =>
  STATUTORY_QUESTIONS.some(({ key }) => policies.statutory_answers[key] === "Yes");

// The form fields each policy needs before it can be accepted
function policyReady(key, form) {
  const policies = form.policies;
  const shared =
    ["name", "pan_number", "aadhaar_number", "mobile"].every((field) => filled(form[field])) &&
    filled(policies.designation);
  if (key !== "statutory") return shared;
  return (
    shared &&
    STATUTORY_QUESTIONS.every(({ key: letter }) => policies.statutory_answers[letter]) &&
    (!statutoryHasYes(policies) || filled(policies.statutory_details))
  );
}

function policiesComplete(form) {
  const policies = form.policies;
  return (
    POLICY_LIST.every(({ key }) => policies.accepted[key] && policyReady(key, form)) &&
    tidyName(policies.signature) !== "" &&
    tidyName(policies.signature) === tidyName(form.name)
  );
}

// "{name}" in the policy text shows the employee's value, or a highlighted blank
function fillBlanks(text, values) {
  return text.split(/(\{\w+\})/).map((part, index) => {
    const match = /^\{(\w+)\}$/.exec(part);
    if (!match) return part;
    const value = values[match[1]];
    return filled(value)
      ? <mark key={index}>{value}</mark>
      : <mark key={index} className="blank">…………</mark>;
  });
}

function PolicyDocument({ policy, values, form }) {
  const policies = form.policies;
  return (
    <article className="policy-doc" aria-label={policy.title}>
      <h3>{policy.title}</h3>
      {policy.blocks.map((block, index) => {
        if (block.type === "h") return <h4 key={index}>{fillBlanks(block.text, values)}</h4>;
        if (block.type === "p") return <p key={index}>{fillBlanks(block.text, values)}</p>;
        const List = block.type === "ol" ? "ol" : "ul";
        return (
          <List key={index} className={block.strong ? "strong" : undefined}>
            {block.items.map((item, i) => <li key={i}>{fillBlanks(item, values)}</li>)}
          </List>
        );
      })}

      <div className="policy-sign">
        <div><span>Name</span><strong>{form.name || "—"}</strong></div>
        <div><span>Date</span><strong>{todayText()}</strong></div>
        <div><span>PAN number</span><strong>{form.pan_number || "—"}</strong></div>
        <div><span>Aadhaar number</span><strong>{form.aadhaar_number || "—"}</strong></div>
        <div><span>Phone number</span><strong>{form.mobile ? `${form.mobile_code} ${form.mobile}` : "—"}</strong></div>
        <div>
          <span>Signature</span>
          <strong className={policies.accepted[policy.key] && policies.signature ? "esign" : undefined}>
            {policies.accepted[policy.key] && policies.signature ? policies.signature : "—"}
          </strong>
        </div>
      </div>
    </article>
  );
}

function StatutoryQuestions({ policies, setPolicies, attempted }) {
  const setAnswer = (key, answer) =>
    setPolicies({ statutory_answers: { ...policies.statutory_answers, [key]: answer } });
  const hasYes = statutoryHasYes(policies);

  return (
    <>
      <h4>Declaration points</h4>
      <div className="yn-list">
        {STATUTORY_QUESTIONS.map(({ key, text }) => {
          const answer = policies.statutory_answers[key];
          return (
            <div className={"yn-row" + (attempted && !answer ? " missing" : "")} key={key}>
              <span>({key}) {text.charAt(0).toUpperCase() + text.slice(1)}</span>
              <div className="seg" role="group" aria-label={`Point (${key})`}>
                {["Yes", "No"].map((option) => (
                  <button
                    type="button"
                    key={option}
                    className={(answer === option ? "on" : "") + (option === "Yes" ? " yes" : "")}
                    aria-pressed={answer === option}
                    onClick={() => setAnswer(key, option)}
                  >{option}</button>
                ))}
              </div>
            </div>
          );
        })}
      </div>
      {hasYes && (
        <Field label="Details for the points answered Yes" required wide>
          <textarea
            value={policies.statutory_details}
            placeholder="Point, what happened, when, and the current status"
            onChange={(event) => setPolicies({ statutory_details: event.target.value })}
          />
        </Field>
      )}
    </>
  );
}

function Policies({ form, update, errors, autoBadge, bindPhone, attempted }) {
  const [active, setActive] = useState(POLICY_LIST[0].key);
  const policies = form.policies;
  const policy = POLICY_LIST.find(({ key }) => key === active);
  const index = POLICY_LIST.indexOf(policy);
  const next = POLICY_LIST[index + 1];
  const ready = policyReady(active, form);
  const accepted = policies.accepted[active];

  const setPolicies = (patch) => update("policies", { ...form.policies, ...patch });
  const setAccepted = (value) => setPolicies({ accepted: { ...policies.accepted, [active]: value } });
  const bindText = (key) => ({ value: form[key], onChange: (event) => update(key, event.target.value) });

  const signatureError =
    filled(policies.signature) && tidyName(policies.signature) !== tidyName(form.name)
      ? "Type your full name exactly as entered in Personal Information."
      : null;

  // Editing any field the policy depends on un-ticks it, so it is re-read before submitting
  useEffect(() => {
    if (accepted && !ready) setAccepted(false);
  }, [accepted, ready]);

  const values = { name: form.name, designation: policies.designation };

  return (
    <>
      <div className="policy-tabs" role="tablist" aria-label="Company policies">
        {POLICY_LIST.map((item, i) => {
          const done = policies.accepted[item.key];
          return (
            <button
              type="button"
              role="tab"
              key={item.key}
              aria-selected={item.key === active}
              className={"policy-tab" + (item.key === active ? " on" : "") + (done ? " done" : "")}
              onClick={() => setActive(item.key)}
            >
              <span className="tab-dot">{done ? <Icon name="check" size={11} /> : i + 1}</span>
              {item.short}
            </button>
          );
        })}
      </div>

      <div className="policy-split">
        <PolicyDocument policy={policy} values={values} form={form} />

        <div className="policy-form">
          <h4>Your details</h4>
          <div className="grid">
            <Field label="Full Name" required wide badge={autoBadge("name")}>
              <input {...bindText("name")} />
            </Field>
            <Field label="Designation Offered" required wide hint="As written in your offer letter">
              <input
                value={policies.designation}
                placeholder="e.g. Software Engineer"
                onChange={(event) => setPolicies({ designation: event.target.value })}
              />
            </Field>
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
            <Field label="Phone Number" required wide group error={errors.mobile}>
              <PhoneInput {...bindPhone("mobile")} />
            </Field>
          </div>

          {active === "statutory" && (
            <StatutoryQuestions policies={policies} setPolicies={setPolicies} attempted={attempted} />
          )}

          <h4>Sign</h4>
          <Field
            label="E-signature"
            required
            error={signatureError}
            hint="Type your full name. It signs all four policies."
          >
            <input
              value={policies.signature}
              placeholder={form.name || "Your full name"}
              onChange={(event) => setPolicies({ signature: event.target.value })}
            />
          </Field>

          <label className={"accept-box" + (accepted ? " checked" : "")}>
            <input
              type="checkbox"
              checked={accepted}
              disabled={!ready}
              onChange={(event) => setAccepted(event.target.checked)}
            />
            <span>
              <strong>I have read and agree to the {policy.title}.<span className="req">*</span></strong>
              <small>
                {ready
                  ? `Dated ${todayText()}.`
                  : active === "statutory"
                    ? "Fill in your details and answer every point first."
                    : "Fill in your details first."}
              </small>
            </span>
          </label>

          <div className="policy-nav">
            {index > 0 ? (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setActive(POLICY_LIST[index - 1].key)}>
                ← {POLICY_LIST[index - 1].short}
              </button>
            ) : <span />}
            {next && (
              <button
                type="button"
                className={"btn btn-sm " + (accepted ? "btn-primary" : "btn-ghost")}
                onClick={() => setActive(next.key)}
              >
                Next: {next.short} →
              </button>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
"""


def inject(page, payload_json):
    """Add the policy styles and components to the onboarding page."""

    return (
        page
        .replace("/* __POLICY_CSS__ */", CSS)
        .replace("// __POLICY_SCRIPT__", SCRIPT.replace("__POLICIES__", payload_json))
    )
