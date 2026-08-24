#!/usr/bin/env python3
"""Generate cryptographically strong production secrets for Empyrean.

Generates 256-bit high-entropy keys suitable for SECRET_KEY and JWT_SECRET,
validating against Empyrean's strict production security guards.

Usage:
    python scripts/generate_secrets.py
    python scripts/generate_secrets.py --write-env
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

# Required parameters matching config/__init__.py
_MIN_SECRET_BYTES = 32
_MIN_SECRET_DISTINCT_CHARS = 5
_DEV_SECRETS = {
    "dev-secret-key",
    "dev-jwt-secret",
    "change-me-to-a-random-secret",
    "change-me-to-a-256-bit-random-secret",
}


def generate_secure_secret(length: int = 48) -> str:
    """Generate a high-entropy base64/hex token that passes all validation rules."""
    while True:
        token = secrets.token_urlsafe(length)
        # Verify it meets all criteria
        if (
            token not in _DEV_SECRETS
            and len(token.encode("utf-8")) >= _MIN_SECRET_BYTES
            and len(set(token)) >= _MIN_SECRET_DISTINCT_CHARS
        ):
            return token


def main():
    secret_key = generate_secure_secret(48)
    jwt_secret = generate_secure_secret(48)

    print("=" * 60)
    print("  Empyrean V2 - Production Secrets Generator")
    print("=" * 60)
    print(f"SECRET_KEY={secret_key}")
    print(f"JWT_SECRET={jwt_secret}")
    print("-" * 60)

    if "--write-env" in sys.argv:
        env_path = Path(__file__).resolve().parents[1] / ".env"
        example_path = Path(__file__).resolve().parents[1] / ".env.example"

        if not env_path.exists() and example_path.exists():
            content = example_path.read_text(encoding="utf-8")
        elif env_path.exists():
            content = env_path.read_text(encoding="utf-8")
        else:
            content = ""

        # Replace or append
        lines = content.splitlines()
        new_lines = []
        has_secret_key = False
        has_jwt_secret = False

        for line in lines:
            if line.startswith("SECRET_KEY="):
                new_lines.append(f"SECRET_KEY={secret_key}")
                has_secret_key = True
            elif line.startswith("JWT_SECRET="):
                new_lines.append(f"JWT_SECRET={jwt_secret}")
                has_jwt_secret = True
            else:
                new_lines.append(line)

        if not has_secret_key:
            new_lines.append(f"SECRET_KEY={secret_key}")
        if not has_jwt_secret:
            new_lines.append(f"JWT_SECRET={jwt_secret}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        print(f"Successfully updated {env_path.name} with new production secrets.")
    else:
        print("Tip: Run with --write-env to automatically write these into your .env file.")
    print("=" * 60)


if __name__ == "__main__":
    main()
