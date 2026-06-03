#!/usr/bin/env python3
"""Refresh an existing OCI CLI session token before it expires.

This does not bypass OCI browser authentication. It only keeps an already
authenticated local session alive while OCI CLI can still refresh it.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-name", default=os.environ.get("OCI_CONFIG_PROFILE", "prof"))
    parser.add_argument("--config-file", default=os.environ.get("OCI_CONFIG_FILE", "~/.oci/config"))
    parser.add_argument("--min-ttl-seconds", type=int, default=900)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--authenticate-command",
        default=(
            "oci session authenticate --region uk-london-1 "
            "--tenancy-name bmc_operator_access --profile-name prof"
        ),
        help="Command to show when interactive re-authentication is required.",
    )
    args = parser.parse_args()

    while True:
        status = check_and_refresh(args)
        if status != 0 or not args.loop:
            return status
        time.sleep(max(30, args.interval_seconds))


def check_and_refresh(args: argparse.Namespace) -> int:
    config_file = Path(args.config_file).expanduser()
    token_file = security_token_file(config_file, args.profile_name)
    if token_file is None:
        print(
            f"OCI profile {args.profile_name!r} does not use security_token_file; "
            "no session refresh is needed."
        )
        return 0

    expires_at = token_expiry_epoch(token_file)
    now = int(time.time())
    if expires_at is None:
        print(f"Could not read OCI token expiry from {token_file}", file=sys.stderr)
        return 2

    ttl = expires_at - now
    if ttl > args.min_ttl_seconds:
        print(f"OCI session profile {args.profile_name!r} is valid for {ttl}s.")
        return 0

    if ttl <= 0:
        print(
            f"OCI session profile {args.profile_name!r} is expired. "
            "Interactive authentication is required:",
            file=sys.stderr,
        )
        print(f"  {args.authenticate_command}", file=sys.stderr)
        return 3

    command = ["oci", "session", "refresh", "--profile-name", args.profile_name]
    if config_file:
        command.extend(["--config-file", str(config_file)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        print("OCI session refresh failed before token expiry.", file=sys.stderr)
        detail = (completed.stderr or completed.stdout).strip()
        if detail:
            print(detail, file=sys.stderr)
        print("Interactive authentication may be required:", file=sys.stderr)
        print(f"  {args.authenticate_command}", file=sys.stderr)
        return completed.returncode

    refreshed_expiry = token_expiry_epoch(token_file)
    refreshed_ttl = refreshed_expiry - now if refreshed_expiry is not None else None
    if refreshed_ttl is None:
        print("OCI session refreshed, but new token expiry could not be read.")
    else:
        print(f"OCI session refreshed. New TTL: {refreshed_ttl}s.")
    return 0


def security_token_file(config_file: Path, profile_name: str) -> Path | None:
    parser = configparser.RawConfigParser()
    if not parser.read(config_file):
        raise SystemExit(f"Could not read OCI config file: {config_file}")
    if not parser.has_section(profile_name):
        raise SystemExit(f"OCI config profile not found: {profile_name}")
    raw = parser.get(profile_name, "security_token_file", fallback="")
    return Path(raw).expanduser() if raw else None


def token_expiry_epoch(token_file: Path) -> int | None:
    token = token_file.read_text(encoding="utf-8").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        parsed = json.loads(decoded)
        return int(parsed["exp"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
