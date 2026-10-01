#!/usr/bin/env bash
# restore.sh — restore kanidm-easy-deploy from a Borg archive or portable backup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

ARCHIVE_NAME=""
LATEST="false"
PORTABLE_FILE=""
ASSUME_YES="false"
KEEP_STOPPED="false"
LIST_ONLY="false"
ENCRYPTED_FILE="false"
STACK_STOPPED="false"
RESTORE_STAGE=""
PLAN_JSON=""

STATE_DIR="${SCRIPT_DIR}/.kanidm-easy-deploy"
BACKUP_STATE_DIR="${STATE_DIR}/backup"
RESTORE_ROOT="${BACKUP_STATE_DIR}/restore"
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
  bash restore.sh --archive <archive-name> [--yes] [--keep-stopped]
  bash restore.sh --latest [--yes] [--keep-stopped]
  bash restore.sh --file <portable-archive> [--encrypt] [--passphrase-file PATH] [--yes] [--keep-stopped]
  bash restore.sh --list

Options:
  --archive NAME        Archive name to restore, or a unique archive ID prefix.
  --latest              Restore the newest archive in the repository.
  --file PATH           Restore from a portable .tar.gz (or encrypted) archive.
  --list                List available archive names in the configured repository.
  --encrypt             The portable file is encrypted (age or openssl).
  --passphrase-file F   File containing the passphrase for a non-interactive decrypt.
  --yes                 Skip destructive confirmation prompt.
  --keep-stopped        Do not restart services after restore.
  -h, --help            Show this help message.

Restore overwrites deploy.yaml, .kanidm-easy-deploy/, and kanidm.data_dir,
then re-runs apply.sh and restarts the stack.
EOF
}

require_command() {
	local cmd="$1"
	command -v "$cmd" &>/dev/null || die "Required command not found: ${cmd}"
}

load_plan_json() {
	PLAN_JSON="$(mktemp)"
	easydeploy_backup_py "${EASYDEPLOY_LIB}/python/backup_plan.py" \
		--project-root "${SCRIPT_DIR}" --emit-plan-json >"${PLAN_JSON}"
}

load_backup_settings() {
	[[ -f "${SCRIPT_DIR}/deploy.yaml" ]] || die "Missing ${SCRIPT_DIR}/deploy.yaml — copy deploy.yaml.example and run bash apply.sh first."
	eval "$(easydeploy_backup_settings_shell "${SCRIPT_DIR}/deploy.yaml")"
	[[ "${BACKUP_ENABLED:-false}" == "true" ]] || die "Backups are disabled. Set backup.enabled=true in deploy.yaml before restoring."
}

confirm_restore() {
	[[ "${ASSUME_YES}" == "true" ]] && return 0

	local target="${ARCHIVE_NAME:-${PORTABLE_FILE}}"
	echo
	warn "Restore will overwrite current Kanidm state with '${target}'."
	local confirm
	ask_yn confirm "Continue with restore?" "n"
	[[ "$confirm" == "y" ]] || return 1

	local final_confirm
	ask_yn final_confirm "Final confirmation: restore now?" "n"
	[[ "$final_confirm" == "y" ]]
}

list_archives() {
	info "Listing backup archives from ${BACKUP_REPO_URL}..."
	easydeploy_backup_list_archives "${BACKUP_REPO_URL}"
}

resolve_archive_name() {
	local requested="$1"
	easydeploy_backup_resolve_archive "${BACKUP_REPO_URL}" "${requested}"
}

stack_running() {
	docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^kanidm'
}

stop_stack() {
	if stack_running; then
		info "Stopping Kanidm stack before restore..."
		bash "${SCRIPT_DIR}/stop.sh"
		STACK_STOPPED="true"
	elif [[ -n "${ARCHIVE_NAME}" || "${LATEST}" == "true" ]]; then
		info "Stack not detected as running — running stop hook to be safe..."
		bash "${SCRIPT_DIR}/stop.sh" || warn "Stop hook failed; continuing."
		STACK_STOPPED="true"
	else
		info "No running Kanidm containers detected — skipping stop before restore."
	fi
}

extract_payload() {
	mkdir -p "${RESTORE_ROOT}"
	RESTORE_STAGE="$(mktemp -d "${RESTORE_ROOT}/restore.XXXXXX")"

	if [[ -n "${PORTABLE_FILE}" ]]; then
		if [[ "${ENCRYPTED_FILE}" == "true" ]]; then
			info "Decrypting and extracting portable archive..."
			easydeploy_backup_decrypt_stream "${PORTABLE_FILE}" | tar -xf - -C "${RESTORE_STAGE}"
		else
			easydeploy_backup_extract_portable "${PORTABLE_FILE}" "${RESTORE_STAGE}"
		fi
	else
		info "Extracting archive '${ARCHIVE_NAME}'..."
		(
			cd "${RESTORE_STAGE}"
			borg extract "${BACKUP_REPO_URL}::${ARCHIVE_NAME}" payload
		)
	fi

	[[ -d "${RESTORE_STAGE}/payload" ]] || die "Backup does not contain the expected payload/ directory."
}

cleanup_and_restart() {
	local rc=$?

	[[ -n "${RESTORE_STAGE}" ]] && rm -rf "${RESTORE_STAGE}" 2>/dev/null || true
	[[ -n "${PLAN_JSON}" ]] && rm -f "${PLAN_JSON}" 2>/dev/null || true

	if [[ "${STACK_STOPPED}" == "true" && "${KEEP_STOPPED}" != "true" ]]; then
		info "Restarting services after restore flow..."
		if ! bash "${SCRIPT_DIR}/start.sh"; then
			warn "Automatic restart failed; please run 'bash start.sh' manually."
			rc=1
		fi
	fi

	exit "$rc"
}

main() {
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--archive)
				ARCHIVE_NAME="${2:-}"
				[[ -n "${ARCHIVE_NAME}" ]] || die "--archive requires a value"
				shift
				;;
			--latest)
				LATEST="true"
				;;
			--file)
				PORTABLE_FILE="${2:-}"
				[[ -n "${PORTABLE_FILE}" ]] || die "--file requires a value"
				shift
				;;
			--list)
				LIST_ONLY="true"
				;;
			--encrypt)
				ENCRYPTED_FILE="true"
				;;
			--passphrase-file)
				PASSPHRASE_FILE="${2:-}"
				[[ -n "${PASSPHRASE_FILE}" ]] || die "--passphrase-file requires a value"
				export PASSPHRASE_FILE
				shift
				;;
			--yes)
				ASSUME_YES="true"
				;;
			--keep-stopped)
				KEEP_STOPPED="true"
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

	if [[ -n "${PORTABLE_FILE}" && ( -n "${ARCHIVE_NAME}" || "${LATEST}" == "true" ) ]]; then
		die "Provide either --file or --archive/--latest, not both."
	fi
	if [[ -n "${ARCHIVE_NAME}" && "${LATEST}" == "true" ]]; then
		die "Provide either --archive or --latest, not both."
	fi

	if [[ "${LIST_ONLY}" == "true" ]]; then
		require_command borg
		load_backup_settings
		easydeploy_backup_repo_env "${SECRETS_FILE}"
		list_archives
		exit 0
	fi

	if [[ -z "${PORTABLE_FILE}" && -z "${ARCHIVE_NAME}" && "${LATEST}" != "true" ]]; then
		die "Provide --archive <archive-name>, --latest, --file <portable-archive>, or use --list"
	fi

	require_command docker

	load_plan_json

	if [[ -z "${PORTABLE_FILE}" ]]; then
		require_command borg
		load_backup_settings
		easydeploy_backup_repo_env "${SECRETS_FILE}"
		if [[ "${LATEST}" == "true" ]]; then
			ARCHIVE_NAME="$(borg list --short --last 1 "${BACKUP_REPO_URL}")"
			[[ -n "${ARCHIVE_NAME}" ]] || die "No archives found in ${BACKUP_REPO_URL}."
		else
			ARCHIVE_NAME="$(resolve_archive_name "${ARCHIVE_NAME}")"
		fi
	fi

	confirm_restore || {
		info "Restore cancelled."
		exit 0
	}

	trap cleanup_and_restart EXIT

	stop_stack

	extract_payload

	info "Restoring Kanidm payload..."
	easydeploy_backup_restore_payload "${SCRIPT_DIR}" "${RESTORE_STAGE}/payload" "${PLAN_JSON}"

	success "Restore completed successfully."
}

main "$@"
