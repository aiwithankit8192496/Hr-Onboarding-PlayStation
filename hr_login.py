"""
Sign-in for the HR portal: a login form and a signed session cookie.

Why not HTTP Basic: behind CloudFront's origin access control the Authorization header is replaced by
CloudFront's own signature, a browser re-sends Basic credentials on every request and cannot sign out,
and the password would have to sit in the function's settings. Here:

  - the password is stored only as a salted scrypt hash (HR_PASSWORD_HASH, an SSM SecureString);
  - a correct sign-in sets one cookie: HttpOnly, Secure, SameSite=Strict, valid 8 hours, signed
    (HMAC-SHA256) with SESSION_SECRET (another SSM SecureString). Nothing is stored on the server, so
    it works across any number of Lambda copies;
  - wrong attempts are slowed down and, after a few, refused for a while (per running copy).

Second factor: a time-based one-time code (TOTP, RFC 6238: an authenticator app, 6 digits, 30 seconds, the
previous and next code also accepted). Each user's secret sits in the same HR_USERS SSM SecureString as their
password hash. The code is asked together with the password, so a wrong password and a wrong code look
the same, both count toward the lock-out, and a code works once.

Settings: HR_USERS (a JSON map of user name -> {"hash": password hash, "totp": secret} (a bare hash string
means no second factor, which a real deployment refuses); one SSM SecureString; make or extend it with
scripts/make_hr_password_hash.py) and SESSION_SECRET (32 or more random characters). The earlier single
pair HR_USER + HR_PASSWORD_HASH still works as a map of one. Nothing here ever prints or logs a password, a hash or the secret.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
import urllib.parse

MIN_PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32

COOKIE_NAME = "hr_session"
SESSION_SECONDS = 8 * 60 * 60

# scrypt cost: about 64 MB and a fraction of a second on one core. A stored hash weaker than MIN_SCRYPT_N is not
# accepted. (The tests lower both numbers for speed in tests/conftest.py; nothing in the application does.)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_SCRYPT_N = 2 ** 14

TOTP_PERIOD = 30     # seconds a code lasts
TOTP_DIGITS = 6
TOTP_WINDOW = 1      # the code before and the code after the current one are accepted too (clock drift)
TOTP_ISSUER = "HR Review Portal"
TOTP_SECRET = re.compile(r"[A-Z2-7]{16,64}")  # base32, at least 80 bits

MAX_FAILURES = 5          # wrong sign-ins allowed ...
FAILURE_WINDOW = 300      # ... within this many seconds, per user name

USERNAME = re.compile(r"[A-Za-z0-9._-]{1,40}")  # a user name is a login name: no spaces, no @, so never a person's name or e-mail by habit
UNKNOWN_USER = "_unknown"  # failures for names that do not exist all count here


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- password hashes

def hash_password(password, salt=None):
    """'scrypt$N$r$p$salt$hash' for this password (a new random salt unless one is given)."""

    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def valid_hash_format(stored):
    if not isinstance(stored, str):
        return False

    parts = stored.split("$")
    try:
        return len(parts) == 6 and parts[0] == "scrypt" and all(int(value) > 0 for value in parts[1:4]) \
            and int(parts[1]) >= MIN_SCRYPT_N \
            and len(_unb64(parts[4])) >= 8 and len(_unb64(parts[5])) >= 16
    except ValueError:
        return False


def verify_password(password, stored):
    """True when `password` matches the stored hash. Never raises on a malformed hash."""

    if not valid_hash_format(stored):
        return False

    _, n, r, p, salt, expected = stored.split("$")

    try:
        digest = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(expected)),
            maxmem=256 * 1024 * 1024,
        )
    except (ValueError, MemoryError):
        return False

    return hmac.compare_digest(digest, _unb64(expected))


# ---------------------------------------------------------------- one-time codes (TOTP, RFC 6238)

def generate_totp_secret():
    """A new random secret: 160 bits as 32 base32 letters (what authenticator apps expect)."""

    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret, step):
    """The 6-digit code for one 30-second step (the step number is the Unix time divided by 30)."""

    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return f"{number:0{TOTP_DIGITS}d}"


def check_totp(secret, code, now=None):
    """The step number the code belongs to (this one, or one step either side), else None.

    Every candidate is compared, so the time taken does not show which step matched or how close a guess was.
    """

    code = re.sub(r"\s", "", str(code or ""))
    current = int((now if now is not None else time.time()) // TOTP_PERIOD)
    matched = None

    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if hmac.compare_digest(totp_code(secret, step), code):
            matched = step

    return matched


def otpauth_uri(user, secret, issuer=TOTP_ISSUER):
    """The otpauth:// address an authenticator app takes (as text or as a QR code)."""

    label = urllib.parse.quote(f"{issuer}:{user}", safe="")
    query = urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": TOTP_DIGITS, "period": TOTP_PERIOD},
        quote_via=urllib.parse.quote,
    )
    return f"otpauth://totp/{label}?{query}"


# ---------------------------------------------------------------- session cookie

def make_session(user, secret, now=None, lifetime=SESSION_SECONDS):
    """A signed cookie value: base64(payload).base64(HMAC-SHA256(payload))."""

    payload = _b64(json.dumps({"u": user, "exp": int((now if now is not None else time.time()) + lifetime)}).encode())
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def read_session(value, secret, now=None):
    """The user name inside a valid, unexpired cookie value, else None."""

    try:
        payload, signature = (value or "").split(".")
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()

        if not hmac.compare_digest(_unb64(signature), expected):
            return None

        data = json.loads(_unb64(payload))

        if int(data["exp"]) <= (now if now is not None else time.time()):
            return None

        return str(data["u"])
    except (ValueError, KeyError, TypeError):
        return None


def cookie_header(value, secure=True):
    """The Set-Cookie text for a session (value "" signs out)."""

    parts = [f"{COOKIE_NAME}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
    parts.append(f"Max-Age={SESSION_SECONDS if value else 0}")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# ---------------------------------------------------------------- slowing down guessing

class Throttle:
    """Counts wrong sign-ins per key (the user name tried) in memory."""

    def __init__(self, limit=MAX_FAILURES, window=FAILURE_WINDOW):
        self.limit, self.window, self.failures = limit, window, {}

    def blocked(self, key, now=None):
        now = now if now is not None else time.time()
        recent = [moment for moment in self.failures.get(key, []) if now - moment < self.window]
        self.failures[key] = recent
        return len(recent) >= self.limit

    def fail(self, key, now=None):
        self.failures.setdefault(key, []).append(now if now is not None else time.time())

    def clear(self, key):
        self.failures.pop(key, None)

    def use_step(self, key, step):
        """True the first time this one-time-code step is used for this user, False if it was already used."""

        used = self.__dict__.setdefault("steps", {})
        if used.get(key, -1) >= step:
            return False
        used[key] = step
        return True


# ---------------------------------------------------------------- the HR users

def parse_users(text):
    """{user name: {"hash": password hash, "totp": secret or None}} from the JSON text of HR_USERS.

    An entry is either {"hash": ..., "totp": ...} or a bare hash string (no second factor). ValueError,
    whose message never holds a value, when anything is unusable.
    """

    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("HR_USERS is not valid JSON") from None

    if not isinstance(data, dict) or not data:
        raise ValueError("HR_USERS must be a JSON object with at least one user")

    users = {}

    for name, entry in data.items():
        if not USERNAME.fullmatch(name):
            raise ValueError("HR_USERS has a user name that is not 1 to 40 letters, digits, dots, dashes or underscores")

        if isinstance(entry, str):
            entry = {"hash": entry}

        if not isinstance(entry, dict) or set(entry) - {"hash", "totp"}:
            raise ValueError("HR_USERS has an entry that is not a hash or an object with hash and totp")

        if not valid_hash_format(entry.get("hash")):
            raise ValueError("HR_USERS has a password that is not a hash made by make_hr_password_hash.py")

        secret = entry.get("totp")
        if secret is not None and not (isinstance(secret, str) and TOTP_SECRET.fullmatch(secret)):
            raise ValueError("HR_USERS has an authenticator secret that is not 16 to 64 base32 letters")

        users[name] = {"hash": entry["hash"], "totp": secret}

    return users


def dump_users(users):
    """The JSON text of HR_USERS for a {name: {"hash", "totp"}} map (a user without a secret stays a bare hash)."""

    return json.dumps(
        {name: ({"hash": u["hash"], "totp": u["totp"]} if u.get("totp") else u["hash"]) for name, u in users.items()},
        sort_keys=True,
    )


def load_users(environ=os.environ):
    """The HR users from HR_USERS, or from the single pair HR_USER + HR_PASSWORD_HASH; {} when neither is set."""

    text = (environ.get("HR_USERS") or "").strip()
    if text:
        return parse_users(text)

    user, stored = (environ.get("HR_USER") or "").strip(), (environ.get("HR_PASSWORD_HASH") or "").strip()
    return parse_users(json.dumps({user: stored})) if user and stored else {}


# ---------------------------------------------------------------- lock-out kept in DynamoDB

class DynamoThrottle:
    """The same counting as Throttle, kept in DynamoDB so it holds across every Lambda copy.

    One item per user in the counters table: key  login#<user name>,  `attempts` and `expires_at`
    (epoch seconds, 5 minutes after the first wrong try; the table's TTL removes old items, and an
    item past its time is treated as zero because TTL deletion can lag). Unknown names share one item.
    """

    def __init__(self, client, table, known, limit=MAX_FAILURES, window=FAILURE_WINDOW):
        self.client, self.table, self.known, self.limit, self.window = client, table, set(known), limit, window

    def _key(self, user):
        return {"name": {"S": "login#" + (user if user in self.known else UNKNOWN_USER)}}

    def blocked(self, user, now=None):
        now = int(now if now is not None else time.time())
        item = self.client.get_item(TableName=self.table, Key=self._key(user), ConsistentRead=True).get("Item")

        return bool(
            item and int(item.get("expires_at", {"N": "0"})["N"]) > now
            and int(item.get("attempts", {"N": "0"})["N"]) >= self.limit
        )

    def fail(self, user, now=None):
        now = int(now if now is not None else time.time())
        key = self._key(user)

        try:  # first wrong try of a window (no item yet, or an old one): start counting from 1
            self.client.update_item(
                TableName=self.table, Key=key,
                UpdateExpression="SET attempts = :one, expires_at = :until",
                ConditionExpression="attribute_not_exists(expires_at) OR expires_at <= :now",
                ExpressionAttributeValues={":one": {"N": "1"}, ":until": {"N": str(now + self.window)}, ":now": {"N": str(now)}},
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            self.client.update_item(
                TableName=self.table, Key=key, UpdateExpression="ADD attempts :one",
                ExpressionAttributeValues={":one": {"N": "1"}},
            )

    def use_step(self, user, step, now=None):
        """True the first time this one-time-code step is used for this user, False if it was used already.

        Kept on the same login#<user> item (UpdateItem only, so the IAM rule for that key is enough). The
        item gets an expires_at (if it has none) so the table's TTL removes it after the codes can no longer match.
        """

        now = int(now if now is not None else time.time())

        try:
            self.client.update_item(
                TableName=self.table, Key=self._key(user),
                UpdateExpression="SET last_step = :step, expires_at = if_not_exists(expires_at, :until)",
                ConditionExpression="attribute_not_exists(last_step) OR last_step < :step",
                ExpressionAttributeValues={":step": {"N": str(int(step))}, ":until": {"N": str(now + 600)}},
            )
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def clear(self, user):
        # UpdateItem only: the HR role has no DeleteItem
        self.client.update_item(
            TableName=self.table, Key=self._key(user), UpdateExpression="SET attempts = :zero",
            ExpressionAttributeValues={":zero": {"N": "0"}},
        )
