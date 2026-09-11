#!/usr/bin/env bash
# bootstrap-from-backup.sh — restore a portable backup on a fresh host
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 1 ]]; then
	echo "Usage: bash bootstrap-from-backup.sh <portable-archive> [restore.sh options...]" >&2
	echo "Example: bash bootstrap-from-backup.sh ~/kanidm-backup.tar.gz --encrypt --yes" >&2
	exit 1
fi

PORTABLE_ARCHIVE="$1"
shift

bash "${SCRIPT_DIR}/ensure-dependencies.sh"
bash "${SCRIPT_DIR}/restore.sh" --file "${PORTABLE_ARCHIVE}" "$@"
