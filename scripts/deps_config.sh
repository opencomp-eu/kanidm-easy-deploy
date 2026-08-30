#!/usr/bin/env bash
# scripts/deps_config.sh — Kanidm Easy Deploy extra dependency keys
# (easydeploy-lib already installs docker, compose, openssl, curl, python3)

easydeploy_required_deps() {
	printf '%s\n' git
}
