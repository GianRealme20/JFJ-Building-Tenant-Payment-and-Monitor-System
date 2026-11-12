"""
Change the simple admin username/password stored in .env.

Usage:
    python set_admin_password.py
        -> generates an easy-to-type password and prints it
    python set_admin_password.py --username myname
        -> changes the username
    python set_admin_password.py --password "mypassword"
        -> sets a password you choose

The plain-text credentials are written to .env so you can easily see them.
"""

import argparse
import os
import secrets
import string
from pathlib import Path

from dotenv import load_dotenv, set_key

ENV_PATH = Path(__file__).with_name(".env")
PASSWORD_ALPHABET = string.ascii_lowercase + string.digits
MIN_PASSWORD_LENGTH = 4


def generate_password(length=12):
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def main():
    parser = argparse.ArgumentParser(description="Change the admin login.")
    parser.add_argument("--username", help="Admin username (defaults to the current one).")
    parser.add_argument("--password", help="Password to use. Omit to auto-generate one.")
    parser.add_argument("--length", type=int, default=12, help="Generated password length (default 12).")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)

    username = (args.username or os.getenv("ADMIN_USERNAME") or "admin").strip()
    password = args.password or generate_password(max(args.length, MIN_PASSWORD_LENGTH))

    if not username or not password:
        raise SystemExit("Username and password cannot be blank.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    set_key(str(ENV_PATH), "ADMIN_USERNAME", username, quote_mode="always")
    set_key(str(ENV_PATH), "ADMIN_PASSWORD", password, quote_mode="always")

    print("=" * 54)
    print("  JFJ Renthouse admin login updated")
    print("=" * 54)
    print(f"  Username : {username}")
    print(f"  Password : {password}")
    print()
    print(f"  Saved in plain text to: {ENV_PATH}")
    print("  Restart the Flask server for the change to take effect.")
    print("=" * 54)


if __name__ == "__main__":
    main()
