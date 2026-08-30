#!/usr/bin/env bash
# scripts/lib.sh — shared utilities for kanidm-easy-deploy

_lib_sh_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_ked_project_root="$(cd "${_lib_sh_dir}/.." && pwd)"

# shellcheck source=easydeploy-lib/lib/init.sh
source "${_ked_project_root}/easydeploy-lib/lib/init.sh"

export PATH="${HOME}/.local/bin:${PATH}"
