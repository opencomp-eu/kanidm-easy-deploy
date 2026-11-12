#!/usr/bin/env python3
"""Create Kanidm people and issue web credential enrollment links."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.apply import (
    configured_groups,
    create_enrollment_link,
    ensure_idm_admin_login,
    kanidm_cli,
    load_config,
    load_or_create_secrets,
)

# Groups a new person joins when --group is not given, if this workspace has them.
DEFAULT_USER_GROUPS = ("opencloud-user", "mail-users")
MAIL_USERS_GROUP = "mail-users"
KIT_ROOT = Path(__file__).resolve().parent.parent


def require_ok(result, action: str, *, allow_existing: bool = False) -> None:
    output = f"{result.stdout}\n{result.stderr}".strip()
    lowered = output.lower()
    if result.returncode == 0:
        return
    if allow_existing and ("already exists" in lowered or "duplicate" in lowered):
        return
    raise RuntimeError(f"{action} failed:\n{output}")


def authenticate() -> tuple[dict, dict]:
    config = load_config()
    secrets = load_or_create_secrets()
    if not ensure_idm_admin_login(secrets["IDM_ADMIN_PASSWORD"]):
        raise RuntimeError("could not authenticate as idm_admin")
    return config, secrets


def default_user_groups(config: dict) -> list[str]:
    """DEFAULT_USER_GROUPS that this workspace actually has (OpenCloud groups only exist with OpenCloud)."""
    available = set(configured_groups(config))
    return [group for group in DEFAULT_USER_GROUPS if group in available]


def wants_mailbox(groups: list[str]) -> bool:
    """mail-users is what makes Stalwart create a mailbox."""
    return any(group.strip().casefold() == MAIL_USERS_GROUP for group in groups)


def stalwart_kit_root(start: Path = KIT_ROOT) -> Path | None:
    """Sibling stalwart-easy-deploy, when this host also runs mail."""
    root = start.parent / "stalwart-easy-deploy"
    if (root / "deploy.yaml").is_file() and (root / "scripts" / "groupware.py").is_file():
        return root
    return None


def mailbox_sync_command(root: Path) -> list[str]:
    """Run Stalwart's sync and wait if one is already in progress.

    A sync that started before this person existed would otherwise finish
    without their mailbox, and Nextcloud Mail would have nothing to sign in to.
    """
    python = root / ".venv" / "bin" / "python"
    if python.is_file():
        return [str(python), "-m", "scripts.groupware", "--wait"]
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--project", str(root), "python", "-m", "scripts.groupware", "--wait"]


def sync_mailbox(root: Path | None = None, runner=subprocess.run) -> None:
    kit = root if root is not None else stalwart_kit_root()
    if kit is None:
        return
    print("Creating the Stalwart mailbox…")
    result = runner(mailbox_sync_command(kit), cwd=kit, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Stalwart did not create the mailbox. Nextcloud Mail cannot connect until it exists. "
            "Re-run this command, or run apply.sh in stalwart-easy-deploy."
        )


def create_user(args: argparse.Namespace) -> None:
    config, _ = authenticate()
    groups = list(args.group) if args.group else default_user_groups(config)
    require_ok(
        kanidm_cli("person", "create", args.username, args.display_name),
        f"creating {args.username}",
        allow_existing=True,
    )
    if args.email:
        require_ok(
            kanidm_cli("person", "update", args.username, "--mail", args.email),
            f"setting mail for {args.username}",
        )
    require_ok(
        kanidm_cli("person", "posix", "set", args.username),
        f"enabling POSIX for {args.username}",
        allow_existing=True,
    )
    for group in groups:
        require_ok(
            kanidm_cli("group", "add-members", group, args.username),
            f"adding {args.username} to {group}",
        )
    sync_error: RuntimeError | None = None
    if wants_mailbox(groups):
        try:
            sync_mailbox()
        except RuntimeError as exc:
            sync_error = exc
    print_enrollment_link(args.username, str(config["kanidm"]["domain"]))
    if sync_error is not None:
        raise sync_error


def print_enrollment_link(username: str, domain: str) -> None:
    link = create_enrollment_link(username, domain)
    if not link:
        raise RuntimeError(f"could not create credential enrollment link for {username}")
    print(f"\nCredential enrollment link for {username} (valid up to 24 hours):")
    print(link)
    print("\nOpen this link to set the person's web/OIDC password and optional MFA.")


def reset_user(args: argparse.Namespace) -> None:
    config, _ = authenticate()
    print_enrollment_link(args.username, str(config["kanidm"]["domain"]))


def _attr_values(entry: dict[str, Any], key: str) -> list[str]:
    value = entry.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
    else:
        return []
    return [item.strip() for item in items if item and item.strip()]


def person_emails_from_list(payload: Any, username: str) -> list[str]:
    """Pick mail addresses for username from `kanidm person list --output json`."""
    if isinstance(payload, str):
        text = payload.strip()
        start = text.find("[")
        if start < 0:
            raise ValueError("Kanidm person list did not return a JSON array")
        payload = json.loads(text[start:])
    if isinstance(payload, dict):
        payload = payload.get("entries") or payload.get("result") or []
    if not isinstance(payload, list):
        raise ValueError("Kanidm person list JSON must be an array")
    wanted = username.strip().casefold()
    for item in payload:
        if not isinstance(item, dict):
            continue
        entry = item.get("attrs") if isinstance(item.get("attrs"), dict) else item
        if not isinstance(entry, dict):
            continue
        names = {value.casefold() for value in _attr_values(entry, "name")}
        if wanted not in names:
            continue
        return list(dict.fromkeys(email.casefold() for email in _attr_values(entry, "mail")))
    return []


def person_emails_from_get(text: str) -> list[str]:
    """Parse `mail:` lines from `kanidm person get` text output."""
    emails: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().casefold() != "mail":
            continue
        address = value.strip().casefold()
        if address and address not in emails:
            emails.append(address)
    return emails


def lookup_person_emails(username: str) -> list[str]:
    listed = kanidm_cli("person", "list", "--name", "idm_admin", "--output", "json")
    if listed.returncode == 0 and listed.stdout.strip():
        emails = person_emails_from_list(listed.stdout, username)
        if emails:
            return emails
    got = kanidm_cli("person", "get", username, "--name", "idm_admin")
    output = f"{got.stdout}\n{got.stderr}"
    if got.returncode != 0:
        raise RuntimeError(f"Could not read Kanidm person {username!r}:\n{output.strip()}")
    emails = person_emails_from_get(output)
    if not emails:
        raise RuntimeError(
            f"Kanidm person {username!r} has no mail attribute. "
            f"Set one with: bash user.sh create {username} \"Display Name\" --email {username}@example.com"
        )
    return emails


def show_mail(args: argparse.Namespace) -> None:
    authenticate()
    emails = lookup_person_emails(args.username)
    if args.json:
        print(json.dumps({"username": args.username, "emails": emails}))
        return
    for email in emails:
        print(email)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage EasyDeploy Kanidm people")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a person and enrollment link")
    create.add_argument("username")
    create.add_argument("display_name")
    create.add_argument("--email", default="")
    create.add_argument(
        "--group",
        action="append",
        default=None,
        help=(
            "group membership; repeat as needed. Replaces the defaults, which are whichever of "
            "opencloud-user and mail-users this workspace has. "
            "mail-users is what creates their mailbox, so during a Microsoft move leave it out "
            "until that person's mail moves."
        ),
    )
    create.set_defaults(func=create_user)

    reset = subparsers.add_parser("reset", help="issue a new credential enrollment/reset link")
    reset.add_argument("username")
    reset.set_defaults(func=reset_user)

    mail = subparsers.add_parser("mail", help="print the person's mail address(es)")
    mail.add_argument("username")
    mail.add_argument("--json", action="store_true", help="print JSON instead of one address per line")
    mail.set_defaults(func=show_mail)

    args = parser.parse_args()
    try:
        args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
