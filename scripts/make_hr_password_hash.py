"""
Make or change the HR portal's users (password hash and authenticator secret) and, when asked, a new
session secret, to store in SSM Parameter Store.

    python scripts/make_hr_password_hash.py add priya --new-secret         first user (+ a SESSION_SECRET)
    python scripts/make_hr_password_hash.py add rahul --map @users.json    add a person (or change a password)
    python scripts/make_hr_password_hash.py add rahul --map @users.json --reset-totp    a new phone: new secret
    python scripts/make_hr_password_hash.py remove rahul --map @users.json

`add` asks for the password (typed twice, not shown, at least 12 characters). For a new user it also makes
an authenticator secret (TOTP: Google Authenticator, Microsoft Authenticator, Authy, 1Password ...) and
prints an otpauth:// address (and a QR code in the terminal if the optional `qrcode` package is installed:
pip install qrcode). Scan it with the person's authenticator app, then type the 6-digit code the app shows:
nothing is printed for storing until the code matches, so a mis-scanned secret is caught here.

It prints the whole HR_USERS value: a JSON object of user name -> {"hash": salted scrypt hash, "totp": secret}.
Store it as a SecureString in /hr-onboarding/hr/HR_USERS (SecureString = encrypted with the KMS key). The
secret is in that value and on your screen while you enrol: do it on your own computer, show the QR code only
to the person it is for, and paste the value straight into the SSM console, not into chat, email or git.
A user name is a login name (letters, digits, dot, dash, underscore), not a person's name or e-mail.

--no-mfa leaves a user without a secret: only for trying the portal on a laptop; the AWS deployment
refuses to start with such a user. A new SESSION_SECRET (--new-secret) signs everybody out. Removing a user
takes effect at the next start of the HR function; to sign that person out at once, replace SESSION_SECRET too.
"""

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hr_login  # noqa: E402


def read_map(text):
    if not text:
        return {}

    if text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8")

    try:
        return hr_login.parse_users(text)
    except ValueError as error:
        sys.exit(f"--map: {error}.")


def show_enrolment(user, secret):
    """Print the otpauth address, and a QR code when the optional package is there."""

    uri = hr_login.otpauth_uri(user, secret)
    print()
    print(f"Authenticator for {user}. Scan this QR code with the app, or add it by hand:")

    try:
        import qrcode

        code = qrcode.QRCode(border=1)
        code.add_data(uri)
        code.make()
        code.print_ascii(invert=True)
    except ImportError:
        print("(no QR code: install the optional package with  pip install qrcode)")

    print("  otpauth address:", uri)
    print("  secret (to type in by hand):", secret)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("add", "remove"))
    parser.add_argument("user", help="the login name")
    parser.add_argument("--map", default="", help="the current HR_USERS JSON, or @file holding it")
    parser.add_argument("--new-secret", action="store_true", help="also print a new random SESSION_SECRET")
    parser.add_argument("--reset-totp", action="store_true", help="make a new authenticator secret for an existing user")
    parser.add_argument("--no-mfa", action="store_true", help="no authenticator secret (laptop only)")
    parser.add_argument("--no-confirm", action="store_true", help="do not ask for a code from the app")
    args = parser.parse_args()

    if not hr_login.USERNAME.fullmatch(args.user):
        sys.exit("The user name must be 1 to 40 letters, digits, dots, dashes or underscores.")

    users = read_map(args.map)

    if args.action == "remove":
        if args.user not in users:
            sys.exit("That user is not in the map.")
        users.pop(args.user)
        if not users:
            sys.exit("That would leave no HR user. Add the new user first, then remove the old one.")
    else:
        password = getpass.getpass(f"Password for {args.user}: ")

        if len(password) < hr_login.MIN_PASSWORD_LENGTH:
            sys.exit(f"The password must be at least {hr_login.MIN_PASSWORD_LENGTH} characters.")

        if getpass.getpass("Again: ") != password:
            sys.exit("The two entries are different.")

        previous = users.get(args.user, {}).get("totp")
        secret = None if args.no_mfa else (previous if previous and not args.reset_totp else hr_login.generate_totp_secret())

        if secret and secret != previous:
            show_enrolment(args.user, secret)

            if not args.no_confirm:
                typed = input("Code shown by the app now: ")
                if hr_login.check_totp(secret, typed) is None:
                    sys.exit("That code does not match. Nothing was made: run the command again and scan the new QR code.")

        users[args.user] = {"hash": hr_login.hash_password(password), "totp": secret}

    print()
    print("HR_USERS =", hr_login.dump_users(users))
    print("users    =", ", ".join(f"{name}{'' if user['totp'] else ' (NO authenticator)'}" for name, user in sorted(users.items())))

    if args.new_secret:
        print("SESSION_SECRET =", secrets.token_urlsafe(48))


if __name__ == "__main__":
    main()
