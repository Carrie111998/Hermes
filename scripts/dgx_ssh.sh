#!/usr/bin/env bash
set -u

# Canonical, fail-closed DGX SSH route for Windows/Codex and WSL callers.
# This file contains connection policy only. It must never contain a password,
# MFA code, private key, token, or host-key bypass.

readonly DGX_HOST="140.96.58.171"
readonly DGX_USER="cwliao"
readonly DGX_TARGET="${DGX_USER}@${DGX_HOST}"
readonly MODE="${1:-probe}"

if [[ $# -gt 0 ]]; then
    shift
fi

identity="${HERMES_DGX_IDENTITY:-${HOME}/.ssh/id_ed25519}"
runtime_dir="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
control_path="${runtime_dir}/hermes-dgx-%C"

common_ssh_opts=(
    -o ConnectTimeout=8
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=2
    -o StrictHostKeyChecking=yes
    -o ControlMaster=auto
    -o ControlPersist=10m
    -o "ControlPath=${control_path}"
    -o AddKeysToAgent=yes
)

# Do not force a missing preferred key. OpenSSH may have a working agent or a
# configured default identity, which is exactly the recovery path we want.
if [[ -r "${identity}" ]]; then
    common_ssh_opts+=( -i "${identity}" )
fi

reauth_required() {
    printf '%s\n' "REAUTH_REQUIRED: run scripts/dgx_ssh.sh auth or bootstrap interactively" >&2
    return 75
}

classify_ssh_failure() {
    local output="$1"
    local status="$2"

    if [[ "${output}" =~ [Hh]ost[[:space:]]key[[:space:]]verification[[:space:]]failed
        || "${output}" =~ REMOTE[[:space:]]HOST[[:space:]]IDENTIFICATION[[:space:]]HAS[[:space:]]CHANGED
        || "${output}" =~ [Oo]ffending.*key
        || "${output}" =~ [Nn]o.*host[[:space:]]key[[:space:]]is[[:space:]]known ]]; then
        printf '%s\n' "${output}" >&2
        printf '%s\n' "SSH_HOST_KEY_ERROR: strict host-key verification failed" >&2
        return "${status:-255}"
    fi

    if [[ "${output}" =~ [Pp]ermission[[:space:]]denied
        || "${output}" =~ [Nn]o[[:space:]]supported[[:space:]]authentication[[:space:]]methods
        || "${output}" =~ [Tt]oo[[:space:]]many[[:space:]]authentication[[:space:]]failures
        || "${output}" =~ sign_and_send_pubkey.*failed ]]; then
        printf '%s\n' "${output}" >&2
        reauth_required
        return $?
    fi

    printf '%s\n' "${output}" >&2
    return "${status:-255}"
}

probe() {
    local output status
    output=$(ssh "${common_ssh_opts[@]}" -o BatchMode=yes "${DGX_TARGET}" \
        "printf 'SSH_OK\\n'; hostname; id -un" 2>&1)
    status=$?
    if ((status == 0)); then
        printf '%s\n' "${output}"
        return 0
    fi
    classify_ssh_failure "${output}" "${status}"
}

interactive_auth() {
    local preflight_status status
    probe >/dev/null 2>&1
    preflight_status=$?
    if ((preflight_status == 0)); then
        printf '%s\n' "AUTH_REUSED: existing WSL SSH identity/agent is valid"
        return 0
    fi
    if ((preflight_status != 75)); then
        return "${preflight_status}"
    fi
    if (($#)); then
        ssh "${common_ssh_opts[@]}" -o BatchMode=no "${DGX_TARGET}" "$@"
        status=$?
    else
        ssh "${common_ssh_opts[@]}" -o BatchMode=no "${DGX_TARGET}"
        status=$?
    fi
    if [[ ${status} -eq 0 ]]; then
        printf '%s\n' "AUTH_OK: control connection may be reused for 10 minutes"
        return 0
    fi
    return "${status}"
}

bootstrap() {
    # Reuse any currently valid identity first. This avoids generating a new
    # key merely because the preferred path is absent.
    probe >/dev/null 2>&1
    local probe_status=$?
    if ((probe_status == 0)); then
        printf '%s\n' "AUTH_REUSED: existing WSL SSH identity/agent is valid"
        return 0
    fi
    if ((probe_status != 75)); then
        return "${probe_status}"
    fi

    mkdir -p "${HOME}/.ssh"
    chmod 700 "${HOME}/.ssh"
    if [[ ! -r "${identity}" ]]; then
        printf '%s\n' "No readable identity at ${identity}. Generating an Ed25519 key interactively." >&2
        if ! ssh-keygen -t ed25519 -f "${identity}" -C "hermes-dgx-recovery"; then
            printf '%s\n' "BOOTSTRAP_KEYGEN_FAILED: unable to create ${identity}" >&2
            return 70
        fi
    fi

    if ! command -v ssh-copy-id >/dev/null 2>&1; then
        printf '%s\n' "BLOCKED_TOOL_UNAVAILABLE: ssh-copy-id is required for bootstrap" >&2
        return 69
    fi

    printf '%s\n' "Interactive step: authorize the public key on DGX once; no password is stored." >&2
    if ! ssh-copy-id \
        -o ConnectTimeout=8 \
        -o StrictHostKeyChecking=yes \
        -i "${identity}.pub" "${DGX_TARGET}"; then
        reauth_required
        return $?
    fi

    probe
}

exec_remote() {
    # Non-interactive execution never prompts. A caller can handle exit 75 and
    # ask the operator to run auth/bootstrap in a real terminal.
    local output status
    output=$(ssh "${common_ssh_opts[@]}" -o BatchMode=yes "${DGX_TARGET}" true 2>&1)
    status=$?
    if ((status != 0)); then
        classify_ssh_failure "${output}" "${status}"
        return $?
    fi
    if (($#)); then
        ssh "${common_ssh_opts[@]}" -o BatchMode=yes "${DGX_TARGET}" "$@"
        local command_status=$?
        if ((command_status == 75)) && [[ "${HERMES_DGX_EXEC_PROTOCOL:-0}" == "1" ]]; then
            printf '%s\n' "HERMES_DGX_REMOTE_EXIT_STATUS=75" >&2
        fi
        return "${command_status}"
    else
        printf '%s\n' "USAGE: scripts/dgx_ssh.sh exec <remote-command>" >&2
        return 64
    fi
}

case "${MODE}" in
    probe)
        probe "$@"
        ;;
    auth)
        interactive_auth "$@"
        ;;
    bootstrap)
        bootstrap "$@"
        ;;
    exec)
        exec_remote "$@"
        ;;
    *)
        printf '%s\n' "USAGE: scripts/dgx_ssh.sh {probe|auth|bootstrap|exec} [command...]" >&2
        exit 64
        ;;
esac
