#!/usr/bin/env python3
"""Read-only iCloud web Reminders discovery; no MCP registration or mutations."""

import argparse
import getpass
import logging
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", required=True,
                        help="Private session directory outside this repository")
    args = parser.parse_args()
    session_dir = Path(args.session_dir).expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if session_dir == repository or repository in session_dir.parents:
        parser.error("session directory must be outside the repository")
    os.umask(0o077)
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if session_dir.stat().st_mode & 0o077:
        parser.error("session directory must have permissions 0700 or stricter")
    logging.disable(logging.CRITICAL)
    try:
        from pyicloud import PyiCloudService
    except ImportError:
        print("Install scripts/reminders-probe-requirements.txt in a dedicated venv.",
              file=sys.stderr)
        return 1
    try:
        account = input("Apple Account email: ").strip()
        password = getpass.getpass("Apple Account password (not stored by this script): ")
        api = PyiCloudService(account, password, cookie_directory=str(session_dir),
                             with_family=False, accept_terms=False)
        if api.requires_2fa:
            code = getpass.getpass("Apple verification code: ")
            if not api.validate_2fa_code(code):
                print("Verification failed.", file=sys.stderr)
                return 1
        if not api.is_trusted_session and not api.trust_session():
            print("Session trust failed.", file=sys.stderr)
            return 1
        print("Reading Reminders; approve an Apple device request if one appears.",
              flush=True)
        lists = list(api.reminders.lists())
        # Deliberately omit account identifiers, list titles, IDs and reminder text.
        print(f"Reminders list discovery succeeded: {len(lists)} lists.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Probe cancelled.", file=sys.stderr)
        return 1
    except Exception as error:
        # Upstream errors can contain account data and raw response bodies.
        print(f"Probe failed ({type(error).__name__}); no server feature enabled.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
