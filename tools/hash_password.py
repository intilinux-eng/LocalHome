#!/usr/bin/env python3
"""Generate a password hash for config/secrets_web.json, so the dashboard
login password never needs to sit in that file as plain text.

Usage: python tools/hash_password.py
(prompts for the password; doesn't echo it or print it back)
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from werkzeug.security import generate_password_hash


def main() -> None:
    password = getpass.getpass("Password to hash: ")
    if not password:
        print("Empty password, aborting.", file=sys.stderr)
        sys.exit(1)
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Passwords didn't match, aborting.", file=sys.stderr)
        sys.exit(1)

    print("\nPaste this into config/secrets_web.json as \"password_hash\":\n")
    print(generate_password_hash(password))


if __name__ == "__main__":
    main()
