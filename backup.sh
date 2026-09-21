#!/usr/bin/env bash
# backup.sh — create/list/export Kanidm backups via the shared easydeploy-lib pipeline
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

LIST_ONLY="false"
EXPORT_PATH=""
EXPORT_ONLY="false"
EXPORT_FROM_ARCHIVE=""
ENCRYPT_EXPORT="false"
COLD="false"
SCHEDULE_ONLY="false"

STATE_DIR="${SCRIPT_DIR}/.kanidm-easy-deploy"
BACKUP_STATE_DIR="${STATE_DIR}/backup"
BACKUP_STAGING_CURRENT="${BACKUP_STATE_DIR}/staging/current"
BORG_CONFIG_PATH="${BACKUP_STATE_DIR}/borgmatic.yaml"
SECRETS_FILE="${STATE_DIR}/secrets.yaml"

# The lib python helpers need PyYAML; prefer the kit venv (uv sync) over the
# bare system interpreter. EASYDEPLOY_BACKUP_PYTHON overrides both.
if [[ -z "${EASYDEPLOY_BACKUP_PYTHON:-}" ]]; then
	if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
		EASYDEPLOY_BACKUP_PYTHON="${SCRIPT_DIR}/.venv/bin/python"
	else
		EASYDEPLOY_BACKUP_PYTHON="python3"
	fi
fi
export EASYDEPLOY_BACKUP_PYTHON

print_help() {
	cat <<EOF
Usage:
  bash backup.sh [--cold]
  bash backup.sh --list
  bash backup.sh [--export PATH] [--export-only] [--encrypt] [--cold]
  bash backup.sh --export-from-archive ARCHIVE --export PATH [--encrypt]
  bash backup.sh --schedule

Options:
  (no flags)              Create a backup archive, prune, and check the repository.
  --cold                  Stop the Kanidm stack before staging and restart after.
                          Recommended: Kanidm's embedded database is file-based.
  --list                  List available archives in the configured repository.
  --export PATH           Also write a portable .tar.gz archive after the run.
  --export-only PATH      Stage payload and export without updating the repository.
  --export-from-archive   Re-export an existing Borg archive to a portable file.
  --encrypt               Encrypt the portable export (age, or openssl when
                          EASYDEPLOY_BACKUP_PASSPHRASE is set).
  --schedule              Reconcile the automatic backup systemd timer with deploy.yaml.
  -h, --help              Show this help message.
EOF
}

load_plan_json() {
	PLAN_JSON="$(mktemp)"
	easydeploy_backup_py "${EASYDEPLOY_LIB}/python/backup_plan.py" \
		--project-root "${SCRIPT_DIR}" --emit-plan-json >"${PLAN_JSON}"
}


plan_field() {
	"${EASYDEPLOY_BACKUP_PYTHON}" -c "import json,sys; print(json.load(open(sys.argv[1]))['$2'])" "$1"
}

plan_timer_name() {
	"${EASYDEPLOY_BACKUP_PYTHON}" - "${EASYDEPLOY_LIB}/python" "${SCRIPT_DIR}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from backup_plan import load_plan

print(load_plan(Path(sys.argv[2]))["timer_name"])
PY
}

plan_hook() {
	"${EASYDEPLOY_BACKUP_PYTHON}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["hooks"].get(sys.argv[2], ""))' "$1" "$2"
}

load_backup_settings() {
	[[ -f "${SCRIPT_DIR}/deploy.yaml" ]] || die "Missing ${SCRIPT_DIR}/deploy.yaml — copy deploy.yaml.example and run bash apply.sh first."
	eval "$(easydeploy_backup_settings_shell "${SCRIPT_DIR}/deploy.yaml")"
}

require_backup_enabled() {
	[[ "${BACKUP_ENABLED:-false}" == "true" ]] || die "Backups are disabled. Set backup.enabled=true in deploy.yaml."
}

require_command() {
	local cmd="$1"
	command -v "$cmd" &>/dev/null || die "Required command not found: ${cmd}"
}

list_archives() {
	info "Listing backup archives from ${BACKUP_REPO_URL}..."
	easydeploy_backup_list_archives "${BACKUP_REPO_URL}"
}

run_full_backup() {
	local plan_json=""
	local stop_hook start_hook archive_prefix
	stop_hook="$(plan_hook "${PLAN_JSON}" stop)"
	start_hook="$(plan_hook "${PLAN_JSON}" start)"
	archive_prefix="$(plan_field "${PLAN_JSON}" archive_prefix)"

	if [[ "${COLD}" == "true" ]]; then
		[[ -n "${stop_hook}" ]] || die "--cold requires a stop hook in the backup plan"
		info "Stopping stack for cold backup..."
		easydeploy_backup_run_hook "${SCRIPT_DIR}" "${stop_hook}"
		STACK_STOPPED="true"
	fi

	info "Staging backup payload..."
	easydeploy_backup_stage_payload "${SCRIPT_DIR}" "${BACKUP_STAGING_CURRENT}" "${BACKUP_REPO_URL}" "${ENCRYPT_EXPORT}"

	if [[ "${EXPORT_ONLY}" == "true" ]]; then
		easydeploy_backup_export_portable "${EXPORT_PATH}" "${BACKUP_STAGING_CURRENT}" "${ENCRYPT_EXPORT}"
		return 0
	fi

	easydeploy_backup_write_borgmatic_config "${BORG_CONFIG_PATH}" "${BACKUP_REPO_URL}" "${BACKUP_STAGING_CURRENT}" "${archive_prefix}"
	easydeploy_backup_repo_create "${BORG_CONFIG_PATH}"

	info "Creating backup archive..."
	borgmatic --config "${BORG_CONFIG_PATH}" create --stats

	info "Applying retention policy..."
	borgmatic --config "${BORG_CONFIG_PATH}" prune

	info "Checking repository consistency..."
	borgmatic --config "${BORG_CONFIG_PATH}" check

	if [[ -n "${EXPORT_PATH}" ]]; then
		easydeploy_backup_export_portable "${EXPORT_PATH}" "${BACKUP_STAGING_CURRENT}" "${ENCRYPT_EXPORT}"
	fi

	if [[ "${STACK_STOPPED}" == "true" ]]; then
		info "Restarting stack after cold backup..."
		easydeploy_backup_run_hook "${SCRIPT_DIR}" "${start_hook}"
		STACK_STOPPED="false"
	fi

	success "Backup completed successfully."
	list_archives
}

cleanup_staging() {
	local rc=$?
	rm -rf "${BACKUP_STAGING_CURRENT}" 2>/dev/null || true
	[[ -n "${PLAN_JSON:-}" ]] && rm -f "${PLAN_JSON}" 2>/dev/null || true

	if [[ "${STACK_STOPPED}" == "true" ]]; then
		info "Restarting stack after interrupted backup..."
		bash "${SCRIPT_DIR}/start.sh" || warn "Automatic restart failed; please run 'bash start.sh' manually."
	fi
	exit "$rc"
}

main() {
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--list)
				LIST_ONLY="true"
				;;
			--export)
				EXPORT_PATH="${2:-}"
				[[ -n "${EXPORT_PATH}" ]] || die "--export requires a path"
				shift
				;;
			--export-only)
				EXPORT_ONLY="true"
				if [[ -n "${2:-}" && "${2}" != --* ]]; then
					EXPORT_PATH="$2"
					shift
				fi
				;;
			--export-from-archive)
				EXPORT_FROM_ARCHIVE="${2:-}"
				[[ -n "${EXPORT_FROM_ARCHIVE}" ]] || die "--export-from-archive requires an archive name"
				shift
				;;
			--encrypt)
				ENCRYPT_EXPORT="true"
				;;
			--cold)
				COLD="true"
				;;
			--schedule)
				SCHEDULE_ONLY="true"
				;;
			-h|--help)
				print_help
				exit 0
				;;
			*)
				die "Unknown argument: $1"
				;;
		esac
		shift
	done

	require_command python3

	if [[ "${SCHEDULE_ONLY}" == "true" ]]; then
		load_backup_settings
		easydeploy_backup_py "${EASYDEPLOY_LIB}/python/backup_schedule.py" \
			--project-root "${SCRIPT_DIR}" \
			--deploy-yaml "${SCRIPT_DIR}/deploy.yaml" \
			--unit-name "$(plan_timer_name)"
		exit 0
	fi

	STACK_STOPPED="false"
	PLAN_JSON=""

	if [[ -n "${EXPORT_FROM_ARCHIVE}" ]]; then
		require_command borg
		load_backup_settings
		require_backup_enabled
		easydeploy_backup_repo_env "${SECRETS_FILE}"
		[[ -n "${EXPORT_PATH}" ]] || die "--export-from-archive requires --export PATH"
		EXPORT_FROM_ARCHIVE="$(easydeploy_backup_resolve_archive "${BACKUP_REPO_URL}" "${EXPORT_FROM_ARCHIVE}")"
		easydeploy_backup_export_from_archive "${BACKUP_REPO_URL}" "${EXPORT_FROM_ARCHIVE}" "${EXPORT_PATH}" "${ENCRYPT_EXPORT}"
		exit 0
	fi

	if [[ "${EXPORT_ONLY}" == "true" ]]; then
		[[ -n "${EXPORT_PATH}" ]] || die "--export-only requires --export PATH"
	fi

	if [[ "${EXPORT_ONLY}" != "true" ]]; then
		require_command borg
		require_command borgmatic
	fi

	load_backup_settings
	if [[ "${EXPORT_ONLY}" != "true" ]]; then
		require_backup_enabled
		easydeploy_backup_repo_env "${SECRETS_FILE}"
	fi

	if [[ "${LIST_ONLY}" == "true" ]]; then
		list_archives
		exit 0
	fi

	load_plan_json
	trap cleanup_staging EXIT
	run_full_backup
	exit 0
}

main "$@"
