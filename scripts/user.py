#!/usr/bin/env python3
"""Create Kanidm people and issue web credential enrollment links."""

from __future__ import annotations

import argparse
import sys

from scripts.apply import (
    create_enrollment_link,
    ensure_idm_admin_login,
    kanidm_cli,
    load_config,
    load_or_create_secrets,
)

DEFAULT_USER_GROUPS = ("opencloud-user", "mail-users")


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
    print_enrollment_link(args.username, str(config["kanidm"]["domain"]))


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
        help="group membership; repeat as needed (defaults: opencloud-user, mail-users)",
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
