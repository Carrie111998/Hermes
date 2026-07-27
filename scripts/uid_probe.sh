#!/usr/bin/env bash
set -euo pipefail

arena="${1:-/workspace}"

if [[ ! -d "$arena" ]]; then
    printf 'uid_probe: arena is not a directory: %s\n' "$arena" >&2
    exit 2
fi

printf '%s\n' '== id =='
id

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

if [[ "$owner_uid" != "$process_uid" || "$owner_gid" != "$process_gid" ]]; then
    printf 'uid_probe: owner mismatch: process=%s:%s owner=%s:%s\n' \
        "$process_uid" "$process_gid" "$owner_uid" "$owner_gid" >&2
    exit 1
fi

printf '%s\n' 'owner_matches_process=yes'
