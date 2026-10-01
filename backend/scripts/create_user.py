"""Create an account (or reset its password) without the sign-up page.

    python -m backend.scripts.create_user alice@example.com            # prompts for the password
    docker compose exec app python -m backend.scripts.create_user alice@example.com
"""
from __future__ import annotations

import argparse
import getpass
import sys
import uuid

from backend.apps.api.auth import hash_password, normalize_email
from backend.core.db import close_pool, get_pool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--password", help="omit to be prompted")
    args = parser.parse_args()
    password = args.password or getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        print("Password must be at least 8 characters", file=sys.stderr)
        return 1
    email = normalize_email(args.email)
    pool = get_pool()
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO app.users (id, email, password_hash) VALUES (%s, %s, %s)"
                " ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash"
                " RETURNING id, (xmax = 0) AS created",
                (str(uuid.uuid4()), email, hash_password(password)),
            ).fetchone()
    finally:
        close_pool()
    print(f"{'Created' if row['created'] else 'Updated password for'} {email} ({row['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
