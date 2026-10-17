import getpass
import os

from werkzeug.security import generate_password_hash

from app import DATABASE_PATH, get_db, init_db


def main():
    init_db()
    with get_db() as db:
        admin_exists = db.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
    if admin_exists:
        print(f"Administrator account already exists. Database: {DATABASE_PATH}")
        return

    username = os.environ.get("BIMS_ADMIN_USERNAME", "admin").strip()
    full_name = os.environ.get("BIMS_ADMIN_NAME", "System Administrator").strip()
    password = os.environ.get("BIMS_ADMIN_PASSWORD") or getpass.getpass(
        "Create a password for the first administrator (minimum 8 characters): "
    )
    if len(username) < 3 or len(username) > 40:
        raise SystemExit("BIMS_ADMIN_USERNAME must be between 3 and 40 characters.")
    if len(password) < 8:
        raise SystemExit("The administrator password must be at least 8 characters.")

    with get_db() as db:
        db.execute(
            """INSERT INTO users (username, password_hash, full_name, role)
               VALUES (?, ?, ?, 'admin')""",
            (username, generate_password_hash(password), full_name),
        )
    print(f"Administrator '{username}' created. Database: {DATABASE_PATH}")
    print("Start the portal with: python app.py")


if __name__ == "__main__":
    main()
