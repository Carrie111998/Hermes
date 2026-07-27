#!/usr/bin/env bash
#
# Report identity facts from INSIDE a container and create one probe file in a
# bind-mounted arena. It is a REPORTER, not a gate.
#
# Why it must not gate: every comparison available in here is made through the
# container's own user namespace, where the mapped UID always looks like the
# process UID. Measured on 2026-07-27 against the unpatched backend, this exact
# script printed
#
#     process_uid=1000 process_gid=1000
#     owner_uid=1000 owner_gid=1000
#
# while the host saw the very same file as uid=100999 — the bug it is supposed
# to help catch. An in-namespace check therefore certifies nothing about host
# ownership; only a stat() run OUTSIDE the namespace can. See
# tests/tools/test_rootless_podman_owner.py, which does exactly that and owns
# the pass/fail decision.
#
# Exit status: 0 when the probe file was created and the facts printed, 2 on a
# usage/environment error. Never 1 for an identity mismatch — that verdict is
# not this script's to make.
set -euo pipefail

arena="${1:-/workspace}"

if [[ ! -d "$arena" ]]; then
    printf 'uid_probe: arena is not a directory: %s\n' "$arena" >&2
    exit 2
fi

printf '%s\n' '== id =='
id

# The map is the honest evidence: `0 1000 1` + `1 100000 65536` is the default
# rootless mapping (container uid N>0 -> host 100000+N-1), whereas keep-id
# produces an identity entry for the caller's UID.
printf '%s\n' '== uid_map =='
if [[ -r /proc/self/uid_map ]]; then
    cat /proc/self/uid_map
else
    printf '%s\n' 'unavailable'
fi

printf '%s\n' '== gid_map =='
if [[ -r /proc/self/gid_map ]]; then
    cat /proc/self/gid_map
else
    printf '%s\n' 'unavailable'
fi

printf '%s\n' '== arena stat =='
stat -c 'path=%n uid=%u gid=%g owner=%U:%G mode=%a' -- "$arena"

probe_file="$(mktemp "${arena%/}/uid-probe.XXXXXX")"
printf 'uid_probe pid=%s\n' "$$" > "$probe_file"

printf '%s\n' '== probe stat =='
stat -c 'path=%n uid=%u gid=%g owner=%U:%G mode=%a' -- "$probe_file"

process_uid="$(id -u)"
process_gid="$(id -g)"
owner_uid="$(stat -c '%u' -- "$probe_file")"
owner_gid="$(stat -c '%g' -- "$probe_file")"

printf 'probe_file=%s\n' "$probe_file"
printf 'process_uid=%s process_gid=%s\n' "$process_uid" "$process_gid"
printf 'owner_uid=%s owner_gid=%s\n' "$owner_uid" "$owner_gid"

# Deliberately labelled `in_namespace_*`: it is a consistency note about the
# container's own view, NOT evidence about the host. Naming it plainly is what
# keeps a reader from mistaking it for the verdict.
if [[ "$owner_uid" == "$process_uid" && "$owner_gid" == "$process_gid" ]]; then
    printf '%s\n' 'in_namespace_owner_matches_process=yes'
else
    printf '%s\n' 'in_namespace_owner_matches_process=no'
fi

printf '%s\n' 'host_verification=required'
