#!/bin/bash
set -Eeuo pipefail
umask 077

REPO=${A4_REPO:-/root/ai-workspace/hermes-unified-ops/canary-v2026.8.27}
IMAGE=${A4_IMAGE_ID:-sha256:4971cad28a660869eb30c2354676b052c22dc0b85d4cf20d11e0f9e3ee21f972}
RUNTIME_UID=${A4_RUNTIME_UID:-10000}
RUNTIME_GID=${A4_RUNTIME_GID:-10000}
EVIDENCE_DIR=${A4_EVIDENCE_DIR:-/root/ai-workspace/hermes-capability-radar/implementation/gate-a4-kernel-evidence}

[[ $EUID -eq 0 ]] || { printf 'ERROR|root required\n' >&2; exit 2; }
[[ -f "$REPO/scripts/gate-a4-kernel-e2e.py" ]] || { printf 'ERROR|missing E2E script\n' >&2; exit 2; }
[[ "$(stat -fc %T /sys/fs/cgroup)" == cgroup2fs ]] || { printf 'ERROR|cgroup v2 required\n' >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { printf 'ERROR|offline image missing\n' >&2; exit 2; }

nonce="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
safe=${nonce//[^A-Za-z0-9]/}
slice="hermes-a4-docker-${safe}.slice"
anchor="hermes-a4-docker-anchor-${safe}.service"
container="hermes-a4-docker-${safe}"
auth_dir=$(mktemp -d)
protected=$(mktemp)
chmod 0755 "$auth_dir"
cgpath=''
mkdir -p "$EVIDENCE_DIR"
log="$EVIDENCE_DIR/docker-$nonce.log"
exec > >(tee -a "$log") 2>&1

docker ps --format '{{.ID}}|{{.Names}}' > "$protected"
anchor_started=0
cleanup_rc=0
cleanup() {
  original_rc=$?
  set +e
  docker rm -f "$container" >/dev/null 2>&1 || true
  if (( anchor_started )); then
    systemctl stop "$anchor" >/dev/null 2>&1 || cleanup_rc=1
    systemctl reset-failed "$anchor" >/dev/null 2>&1 || true
    systemctl stop "$slice" >/dev/null 2>&1 || true
  fi
  if [[ -n "$cgpath" ]]; then
    for _ in $(seq 1 100); do [[ ! -e "$cgpath" ]] && break; sleep 0.05; done
    [[ ! -e "$cgpath" ]] || { printf 'FAIL|cgroup_residue=%s\n' "$cgpath"; cleanup_rc=1; }
  fi
  while IFS='|' read -r id name; do
    [[ -n "$id" ]] || continue
    docker container inspect "$id" >/dev/null 2>&1 || {
      printf 'FAIL|preexisting_container_missing=%s|%s\n' "$id" "$name"
      cleanup_rc=1
    }
  done < "$protected"
  rm -rf "$auth_dir"
  rm -f "$protected"
  printf 'INFO|evidence=%s\n' "$log"
  if (( original_rc != 0 || cleanup_rc != 0 )); then exit 1; fi
}
trap cleanup EXIT INT TERM

systemd-run --quiet --unit="$anchor" --slice="$slice" --property=Type=simple --property=Delegate=yes -- /usr/bin/sleep infinity
anchor_started=1
slice_group=$(systemctl show -p ControlGroup --value "$slice")
[[ -n "$slice_group" ]]
cgpath="/sys/fs/cgroup$slice_group"
for _ in $(seq 1 100); do [[ -d "$cgpath" ]] && break; sleep 0.05; done
[[ -d "$cgpath" ]]
chown "$RUNTIME_UID:$RUNTIME_GID" "$cgpath" "$cgpath/cgroup.procs" "$cgpath/cgroup.subtree_control" "$cgpath/cgroup.threads"
chmod u+rwx "$cgpath"

container_id=$(docker run --detach --name "$container" --pull=never \
  --entrypoint /bin/sh \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=8m,mode=700,uid="$RUNTIME_UID",gid="$RUNTIME_GID" \
  --user "$RUNTIME_UID:$RUNTIME_GID" \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --cgroupns private \
  --cgroup-parent "$slice" \
  --pids-limit 64 \
  --memory 256m \
  --cpus 0.50 \
  --mount "type=bind,src=$cgpath,dst=/run/a3d-cgroup,readonly=false,bind-propagation=rprivate" \
  --mount "type=bind,src=$REPO,dst=/src,readonly=true,bind-propagation=rprivate" \
  --mount "type=bind,src=$auth_dir,dst=/run/a3d-auth,readonly=true,bind-propagation=rprivate" \
  --env PYTHONPATH=/src \
  "$IMAGE" -c '
    set -eu
    tries=0
    while [ ! -s /run/a3d-auth/scope ]; do
      tries=$((tries + 1)); [ "$tries" -lt 200 ] || exit 90; sleep 0.05
    done
    read -r scope_name scope_inode < /run/a3d-auth/scope
    root=/run/a3d-cgroup/$scope_name
    [ "$(stat -c %i "$root")" = "$scope_inode" ] || exit 91
    exec /opt/hermes/.venv/bin/python3 /src/scripts/gate-a4-kernel-e2e.py "$root"
  ')

host_pid=$(docker inspect --format '{{.State.Pid}}' "$container")
scope_group=$(awk -F: '$1 == "0" {print $3}' "/proc/$host_pid/cgroup")
case "$scope_group" in "$slice_group"/docker-*.scope) : ;; *) printf 'FAIL|scope=%s\n' "$scope_group"; exit 1 ;; esac
scope_name=${scope_group##*/}
scope_path="/sys/fs/cgroup$scope_group"
scope_inode=$(stat -c %i "$scope_path")
chown "$RUNTIME_UID:$RUNTIME_GID" "$scope_path" "$scope_path/cgroup.procs" "$scope_path/cgroup.subtree_control" "$scope_path/cgroup.threads"
chmod u+rwx "$scope_path"
printf '%s %s\n' "$scope_name" "$scope_inode" > "$auth_dir/scope"
chmod 0444 "$auth_dir/scope"

[[ "$(docker inspect --format '{{.HostConfig.Privileged}}' "$container")" == false ]]
[[ -z "$(docker inspect --format '{{.HostConfig.PidMode}}' "$container")" ]]
[[ "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$container")" == none ]]
[[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container")" == true ]]
[[ "$(docker inspect --format '{{.HostConfig.CgroupnsMode}}' "$container")" == private ]]
[[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$container")" == '["ALL"]' ]]
[[ "$(docker inspect --format '{{.Config.User}}' "$container")" == "$RUNTIME_UID:$RUNTIME_GID" ]]

set +e
wait_result=$(docker wait "$container")
wait_rc=$?
set -e
docker logs "$container"
[[ $wait_rc -eq 0 && "$wait_result" == 0 ]]
printf 'PASS|docker_backend_e2e container=%s image=%s scope_inode=%s\n' "$container_id" "$IMAGE" "$scope_inode"
docker rm "$container" >/dev/null
