"""
Create (or repair) an admin account interactively — no hardcoded credentials.

Prompts for username, email, and password (hidden input) so every developer
and deployment can provision their own admin without putting secrets in
``.env``. Non-interactive automation should use ``BOOTSTRAP_ADMIN_USERNAME`` /
``BOOTSTRAP_ADMIN_PASSWORD`` instead (see docs/configuration.md).

Behaviour:
- New username                  -> creates the account with role='admin'
- Existing username             -> promotes to admin / re-activates; password
                                   is never touched
- Existing + ``--reset-password`` -> additionally sets a fresh password
- Case-variant username exists  -> refused (never hijacks another account, M91)

Password policy: >= 8 characters with upper, lower, digit, and symbol, within
bcrypt's 72-byte limit. The password is never logged or written to any file.

Safety:
- Refuses to run against ``APP_ENV=production`` unless ``--force`` is given.

Usage::

    python scripts/create_admin.py
    python scripts/create_admin.py --reset-password
    python scripts/create_admin.py --username myadmin --email me@example.com
"""

import argparse
import getpass
import logging
import re
import string
import sys
from pathlib import Path

# Make the project root importable (works regardless of CWD)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import get_config  # noqa: E402

from pydantic import EmailStr, TypeAdapter, ValidationError  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from models import User, get_sync_db  # noqa: E402
from models.helpers import hash_password  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("create_admin")

_ERR_EXIT = 3
# Mirrors api/schemas.py — letters, digits, underscores only.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MIN_USERNAME_LEN = 3
_MAX_USERNAME_LEN = 50
# Mirrors api/schemas.py MAX_PASSWORD_LEN — bcrypt's input ceiling.
_MAX_PASSWORD_CHARS = 72
_MAX_PASSWORD_BYTES = 72
_MIN_PASSWORD_LEN = 8

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def _prompt_username(cli_value: str | None) -> str:
    """Return a validated username, prompting when not given via --username."""
    if cli_value is not None:
        problems = _username_problems(cli_value)
        if problems:
            logger.error("Invalid --username: %s", "; ".join(problems))
            sys.exit(_ERR_EXIT)
        return cli_value.strip()
    while True:
        value = input("Username: ")
        problems = _username_problems(value)
        if not problems:
            return value.strip()
        print(f"  Invalid username: {'; '.join(problems)}")


def _username_problems(value: str) -> list[str]:
    problems: list[str] = []
    value = value.strip()
    if not (_MIN_USERNAME_LEN <= len(value) <= _MAX_USERNAME_LEN):
        problems.append(
            f"must be {_MIN_USERNAME_LEN}-{_MAX_USERNAME_LEN} characters"
        )
    elif not _USERNAME_RE.fullmatch(value):
        problems.append("may contain only letters, digits, and underscores")
    return problems


def _prompt_email(cli_value: str | None) -> str:
    """Return a validated, lowercased email, prompting when needed."""
    if cli_value is not None:
        try:
            return str(_EMAIL_ADAPTER.validate_python(cli_value.strip())).lower()
        except Exception:
            logger.error("Invalid --email: %r is not a valid email address", cli_value)
            sys.exit(_ERR_EXIT)
    while True:
        value = input("Email: ").strip()
        try:
            return str(_EMAIL_ADAPTER.validate_python(value)).lower()
        except Exception:
            print("  Invalid email address — try again.")


def _password_problems(value: str) -> list[str]:
    """Return the list of policy violations for *value* (empty = valid)."""
    problems: list[str] = []
    if len(value) < _MIN_PASSWORD_LEN:
        problems.append(f"at least {_MIN_PASSWORD_LEN} characters")
    if len(value) > _MAX_PASSWORD_CHARS:
        problems.append(f"at most {_MAX_PASSWORD_CHARS} characters")
    if len(value.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        problems.append(f"at most {_MAX_PASSWORD_BYTES} bytes in UTF-8 (bcrypt limit)")
    if not any(c.isupper() for c in value):
        problems.append("an uppercase letter")
    if not any(c.islower() for c in value):
        problems.append("a lowercase letter")
    if not any(c.isdigit() for c in value):
        problems.append("a digit")
    if not any(c in string.punctuation for c in value):
        problems.append("a symbol")
    return problems


def _prompt_password() -> str:
    """Prompt for a policy-compliant password twice (hidden input)."""
    while True:
        password = getpass.getpass("Password: ")
        problems = _password_problems(password)
        if problems:
            print(f"  Password too weak — needs: {', '.join(problems)}.")
            continue
        if getpass.getpass("Confirm password: ") != password:
            print("  Passwords do not match — try again.")
            continue
        return password


def create_admin(
    username: str | None,
    email: str | None,
    reset_password: bool,
    force: bool = False,
) -> None:
    cfg = get_config()

    # ── Production guard ──────────────────────────────────────────────────
    # Mirrors scripts/seed.py: minting an admin in production is destructive,
    # so it is refused unless the operator explicitly passes --force.
    if cfg.APP_ENV == "production" and not force:
        logger.error(
            "Refusing to create an admin in the '%s' database. Set "
            "APP_ENV=development, or re-run with --force if you really "
            "intend to modify production.",
            cfg.APP_ENV,
        )
        sys.exit(_ERR_EXIT)

    username = _prompt_username(username)
    email = _prompt_email(email)

    with get_sync_db() as session:
        existing = session.scalar(select(User).where(User.username == username))

        if existing is None:
            # M91 (mirrors seed.py / api/auth.py): only an EXACT username
            # match may ever be touched. A case-variant row belongs to
            # whoever registered it — refuse and let the operator resolve it.
            case_variant = session.scalar(
                select(User.username).where(
                    func.lower(User.username) == username.lower()
                )
            )
            if case_variant is not None:
                logger.error(
                    "User '%s' already exists (case-variant of '%s') — refusing "
                    "to create or promote it. Choose a different username.",
                    case_variant, username,
                )
                sys.exit(_ERR_EXIT)

            email_clash = session.scalar(
                select(User.username).where(User.email == email)
            )
            if email_clash is not None:
                logger.error(
                    "Email '%s' is already used by user '%s' — refusing to "
                    "create a duplicate. Use --email with another address.",
                    email, email_clash,
                )
                sys.exit(_ERR_EXIT)

            password = _prompt_password()
            session.add(
                User(
                    username=username,
                    email=email,
                    password_hash=hash_password(password),
                    role="admin",
                    is_active=True,
                    notification_prefs={"email_on_critical": True},
                )
            )
            session.commit()
            # Log the username only — never the plaintext password.
            logger.info("Created admin user: '%s'", username)
        else:
            changed = False
            if existing.role != "admin":
                existing.role = "admin"
                changed = True
            if not existing.is_active:
                existing.is_active = True
                changed = True
            if reset_password:
                existing.password_hash = hash_password(_prompt_password())
                changed = True
            if changed:
                session.commit()
            if reset_password:
                logger.info("Password reset for admin user: '%s'", username)
            elif existing.role == "admin" and existing.is_active:
                logger.info(
                    "Admin user '%s' already exists — password left unchanged. "
                    "Re-run with --reset-password to set a new one.",
                    username,
                )
            else:
                logger.info("Promoted/reactivated existing user: '%s'", username)

    logger.info("Done. Log in via POST /api/v1/auth/login (or the frontend).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Interactively create (or repair) an admin account. "
        "Never uses hardcoded credentials; the password is prompted for "
        "with hidden input and never logged.",
    )
    parser.add_argument("--username", help="admin username (prompted if omitted)")
    parser.add_argument("--email", help="admin email (prompted if omitted)")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="also reset the password when the username already exists",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow running even when APP_ENV=production (use with care)",
    )
    args = parser.parse_args()

    try:
        create_admin(
            username=args.username,
            email=args.email,
            reset_password=args.reset_password,
            force=args.force,
        )
    except (KeyboardInterrupt, EOFError):
        # EOFError: stdin is not a terminal (CI/piped run) — point at the
        # non-interactive path instead of dumping a traceback.
        print()
        logger.error(
            "Aborted. For non-interactive environments set "
            "BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD instead "
            "(see docs/configuration.md)."
        )
        sys.exit(_ERR_EXIT)
    except SystemExit:
        raise
    except ValidationError as e:
        # Missing/invalid .env — actionable message, not a pydantic traceback.
        logger.error(
            "Configuration error — copy .env.example to .env and set the "
            "required values (see docs/configuration.md):"
        )
        for err in e.errors():
            logger.error("  %s: %s", ".".join(str(loc) for loc in err["loc"]), err["msg"])
        sys.exit(1)
    except SQLAlchemyError as e:
        # DB down / bad DATABASE_URL — report plainly instead of dumping the
        # driver traceback.
        logger.error("Database error: %s", e)
        logger.error(
            "Check that PostgreSQL is running and that DATABASE_URL in .env "
            "points at it (then run `alembic upgrade head` on a fresh DB)."
        )
        sys.exit(1)
    except Exception as e:
        logger.exception("create_admin failed: %s", e)
        sys.exit(1)
