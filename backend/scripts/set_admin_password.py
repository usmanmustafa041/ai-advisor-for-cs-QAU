"""Set the password of a portal account (default: the seeded administrator).

The database seed creates ``admin@cs.qau.edu.pk`` with a publicly known password,
so run this once right after the first deployment:

    docker compose exec backend python scripts/set_admin_password.py

The password is read from the terminal (never from the command line, so it does not
end up in shell history). All existing sessions of the account are revoked.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.core.auth import hash_password  # noqa: E402
from app.core.database import get_session_factory  # noqa: E402

MIN_LENGTH = 12


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default="admin@cs.qau.edu.pk")
    args = parser.parse_args()
    email = args.email.lower().strip()

    # Piped input (e.g. from PowerShell) can carry a trailing carriage return.
    password = getpass.getpass(f"New password for {email}: ").rstrip("\r")
    if len(password) < MIN_LENGTH:
        print(f"Password must be at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat the password: ").rstrip("\r") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1

    with get_session_factory()() as db:
        row = db.execute(
            text("UPDATE app_users SET password_hash=:hash, updated_at=NOW() "
                 "WHERE lower(email)=:email RETURNING id::text"),
            {"hash": hash_password(password), "email": email},
        ).mappings().one_or_none()
        if not row:
            db.rollback()
            print(f"No account with email {email}.", file=sys.stderr)
            return 1
        db.execute(
            text("UPDATE auth_sessions SET revoked_at=NOW() "
                 "WHERE user_id=:id AND revoked_at IS NULL"),
            {"id": row["id"]},
        )
        db.commit()
    print(f"Password updated for {email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
