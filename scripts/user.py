#!/usr/bin/env python3
"""Create Kanidm people and issue web credential enrollment links."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.apply import (
    create_enrollment_link,
    ensure_idm_admin_login,
    kanidm_cli,
    load_config,
    load_or_create_secrets,
)

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
    for group in args.group:
        require_ok(
            kanidm_cli("group", "add-members", group, args.username),
            f"adding {args.username} to {group}",
        )
    sync_error: RuntimeError | None = None
    if wants_mailbox(args.group):
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
        default=list(DEFAULT_USER_GROUPS),
        help=(
            "group membership; repeat as needed (defaults: opencloud-user, mail-users). "
            "During a Microsoft move, pass only --group opencloud-user until that person's mail moves. "
            "mail-users is what creates their mailbox."
        ),
    )
    create.set_defaults(func=create_user)

    reset = subparsers.add_parser("reset", help="issue a new credential enrollment/reset link")
    reset.add_argument("username")
    reset.set_defaults(func=reset_user)

    args = parser.parse_args()
    try:
        args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
