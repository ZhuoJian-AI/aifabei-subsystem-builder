#!/bin/sh
set -eu

# This installer never reads OSS credential values in the host shell. The
# gateway process parses the root-only bind-mounted file after it starts.
umask 077

INSTALL_DIR=/opt/zhuojian/storage-gateway
MANAGED_MARKER=$INSTALL_DIR/.zhuojian-storage-gateway-managed
MARKER_VALUE=zhuojian-storage-gateway-installer-v1
SECRETS_FILE=/etc/zhuojian/oss-gateway.env
APPS_ENV_DIR=/etc/zhuojian/apps
STATE_DIR=/var/lib/zhuojian-storage-gateway
WRAPPER_PATH=/usr/local/sbin/zhuojian-storage-gateway-admin
COMPOSE_PROJECT=zhuojian-storage-gateway
COMPOSE_SERVICE=zhuojian-storage-gateway
CONTAINER_NAME=zhuojian-storage-gateway
NETWORK_NAME=zhuojian-storage
IMAGE_NAME=zhuojian/storage-gateway:local
STATE_MARKER=$STATE_DIR/.zhuojian-storage-gateway-managed
LOCK_DIR=/run/zhuojian-storage-gateway-installer
LOCK_FILE=$LOCK_DIR/install.lock

die() {
    echo "$1" >&2
    exit 2
}

[ "$(id -u)" -eq 0 ] || die "install.sh must run as root"

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

for required_command in chmod chown cmp dirname docker find flock grep id install mktemp rm sleep stat; do
    command -v "$required_command" >/dev/null 2>&1 || \
        die "required command is missing: $required_command"
done

# Serialise installation before inspecting or changing any persistent gateway
# resource. The private /run directory prevents lock-path substitution.
if [ -e "$LOCK_DIR" ] || [ -L "$LOCK_DIR" ]; then
    [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] || \
        die "refusing non-directory or symlink lock path: $LOCK_DIR"
    [ "$(stat -c '%u:%g:%a' -- "$LOCK_DIR")" = "0:0:700" ] || \
        die "lock directory must already be root:root mode 0700"
else
    install -d -o root -g root -m 0700 "$LOCK_DIR"
fi
if [ -e "$LOCK_FILE" ] || [ -L "$LOCK_FILE" ]; then
    [ -f "$LOCK_FILE" ] && [ ! -L "$LOCK_FILE" ] || \
        die "refusing non-regular or symlink install lock"
    [ "$(stat -c '%u:%g:%a' -- "$LOCK_FILE")" = "0:0:600" ] || \
        die "install lock must already be root:root mode 0600"
else
    install -o root -g root -m 0600 /dev/null "$LOCK_FILE"
fi
exec 9<>"$LOCK_FILE"
flock -n 9 || die "another storage gateway installation is already running"

docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || die "the Docker daemon is unavailable"

source_file() {
    sf_path=$SOURCE_DIR/$1
    [ -f "$sf_path" ] && [ ! -L "$sf_path" ] || \
        die "required installer asset is missing or unsafe: $1"
}

for required_asset in \
    .dockerignore \
    Dockerfile \
    README.md \
    compose.yaml \
    pyproject.toml \
    requirements.txt \
    install.sh \
    bin/zhuojian-storage-gateway-admin \
    src/zhuojian_storage_gateway/__init__.py \
    src/zhuojian_storage_gateway/app.py \
    src/zhuojian_storage_gateway/cli.py \
    src/zhuojian_storage_gateway/config.py \
    src/zhuojian_storage_gateway/registry.py \
    src/zhuojian_storage_gateway/storage.py
do
    source_file "$required_asset"
done

# Refuse secret indirection and weak metadata. Contents are deliberately never
# sourced, printed, copied, or passed as command-line arguments by this script.
[ -e "$SECRETS_FILE" ] || [ -L "$SECRETS_FILE" ] || \
    die "missing $SECRETS_FILE; create and populate it before installation"
[ -f "$SECRETS_FILE" ] && [ ! -L "$SECRETS_FILE" ] || \
    die "$SECRETS_FILE must be a regular file, not a symlink"
[ "$(stat -c '%u:%g:%a' -- "$SECRETS_FILE")" = "0:0:600" ] || \
    die "$SECRETS_FILE must already be owned by root:root with mode 0600"
[ -s "$SECRETS_FILE" ] || die "$SECRETS_FILE must be populated before installation"

check_root_directory() {
    crd_path=$1
    if [ -e "$crd_path" ] || [ -L "$crd_path" ]; then
        [ -d "$crd_path" ] && [ ! -L "$crd_path" ] || \
            die "refusing non-directory or symlink path: $crd_path"
        [ "$(stat -c '%u:%g' -- "$crd_path")" = "0:0" ] || \
            die "directory must already be owned by root:root: $crd_path"
        crd_mode=$(stat -c '%a' -- "$crd_path")
        [ $((0$crd_mode & 0022)) -eq 0 ] || \
            die "directory must not be writable by group or other: $crd_path"
    fi
}

ensure_private_directory() {
    epd_path=$1
    epd_mode=$2
    epd_stat_mode=${epd_mode#0}
    if [ -e "$epd_path" ] || [ -L "$epd_path" ]; then
        [ -d "$epd_path" ] && [ ! -L "$epd_path" ] || \
            die "refusing non-directory or symlink path: $epd_path"
        [ "$(stat -c '%u:%g:%a' -- "$epd_path")" = "0:0:$epd_stat_mode" ] || \
            die "directory must already be root:root mode $epd_mode: $epd_path"
    else
        install -d -o root -g root -m "$epd_mode" "$epd_path"
    fi
}

check_root_directory /opt
check_root_directory /opt/zhuojian
check_root_directory /usr/local
check_root_directory /usr/local/sbin
check_root_directory /etc
check_root_directory /etc/zhuojian
check_root_directory /etc/zhuojian/apps
check_root_directory /var/lib

WAS_MANAGED=0
if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
    [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] || \
        die "refusing non-directory or symlink install path: $INSTALL_DIR"
    [ "$(stat -c '%u:%g' -- "$INSTALL_DIR")" = "0:0" ] || \
        die "existing install directory must be owned by root:root"
    install_dir_mode=$(stat -c '%a' -- "$INSTALL_DIR")
    [ $((0$install_dir_mode & 0022)) -eq 0 ] || \
        die "existing install directory must not be writable by group or other"
    if [ -e "$MANAGED_MARKER" ] || [ -L "$MANAGED_MARKER" ]; then
        [ -f "$MANAGED_MARKER" ] && [ ! -L "$MANAGED_MARKER" ] || \
            die "install ownership marker is not a regular file"
        [ "$(stat -c '%u:%g:%a' -- "$MANAGED_MARKER")" = "0:0:644" ] || \
            die "install ownership marker has unsafe metadata"
        grep -Fxq "$MARKER_VALUE" "$MANAGED_MARKER" || \
            die "install ownership marker is not recognized"
        WAS_MANAGED=1
    elif [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        die "refusing to overwrite an unowned non-empty install directory: $INSTALL_DIR"
    fi
fi

if [ "$WAS_MANAGED" -eq 1 ]; then
    [ -z "$(find "$INSTALL_DIR" -type l -print -quit)" ] || \
        die "managed install directory contains a symlink"
    if ! find "$INSTALL_DIR" -mindepth 1 -print | while IFS= read -r managed_path; do
        managed_relative=${managed_path#"$INSTALL_DIR"/}
        case "$managed_relative" in
            .dockerignore|.zhuojian-storage-gateway-managed|Dockerfile|README.md|compose.yaml|pyproject.toml|requirements.txt|install.sh|bin|bin/zhuojian-storage-gateway-admin|src|src/zhuojian_storage_gateway|src/zhuojian_storage_gateway/__init__.py|src/zhuojian_storage_gateway/app.py|src/zhuojian_storage_gateway/cli.py|src/zhuojian_storage_gateway/config.py|src/zhuojian_storage_gateway/registry.py|src/zhuojian_storage_gateway/storage.py)
                ;;
            *) exit 1 ;;
        esac
    done; then
        die "managed install directory contains an unexpected build-context entry"
    fi
fi

if [ -e "$STATE_DIR" ] || [ -L "$STATE_DIR" ]; then
    [ -d "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ] || \
        die "refusing non-directory or symlink state path: $STATE_DIR"
    [ "$(stat -c '%u:%g' -- "$STATE_DIR")" = "0:0" ] || \
        die "existing state directory must be owned by root:root"
    if [ -e "$STATE_MARKER" ] || [ -L "$STATE_MARKER" ]; then
        [ "$WAS_MANAGED" -eq 1 ] || die "state ownership marker has no matching install"
        [ -f "$STATE_MARKER" ] && [ ! -L "$STATE_MARKER" ] || \
            die "state ownership marker is not a regular file"
        [ "$(stat -c '%u:%g:%a' -- "$STATE_MARKER")" = "0:0:600" ] || \
            die "state ownership marker has unsafe metadata"
        grep -Fxq "$MARKER_VALUE" "$STATE_MARKER" || \
            die "state ownership marker is not recognized"
    elif [ -n "$(find "$STATE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        die "refusing to use a non-empty state directory not owned by this installer"
    fi
fi

if [ -e "$WRAPPER_PATH" ] || [ -L "$WRAPPER_PATH" ]; then
    [ -f "$WRAPPER_PATH" ] && [ ! -L "$WRAPPER_PATH" ] || \
        die "refusing non-regular or symlink admin wrapper: $WRAPPER_PATH"
    [ "$(stat -c '%u:%g:%a' -- "$WRAPPER_PATH")" = "0:0:750" ] || \
        die "existing admin wrapper must be root:root mode 0750"
    wrapper_reference=$SOURCE_DIR/bin/zhuojian-storage-gateway-admin
    if [ "$WAS_MANAGED" -eq 1 ]; then
        [ -f "$INSTALL_DIR/bin/zhuojian-storage-gateway-admin" ] && \
        [ ! -L "$INSTALL_DIR/bin/zhuojian-storage-gateway-admin" ] || \
            die "managed wrapper reference is missing or unsafe"
        wrapper_reference=$INSTALL_DIR/bin/zhuojian-storage-gateway-admin
    fi
    if ! cmp -s "$wrapper_reference" "$WRAPPER_PATH"; then
        die "refusing to overwrite an admin wrapper not owned by this installer"
    fi
fi

container_exists=0
if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    container_exists=1
    container_project=$(docker container inspect --format \
        '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER_NAME")
    container_service=$(docker container inspect --format \
        '{{ index .Config.Labels "com.docker.compose.service" }}' "$CONTAINER_NAME")
    container_workdir=$(docker container inspect --format \
        '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER_NAME")
    [ "$container_project" = "$COMPOSE_PROJECT" ] && \
    [ "$container_service" = "$COMPOSE_SERVICE" ] && \
    [ "$container_workdir" = "$INSTALL_DIR" ] || \
        die "refusing to replace an unknown container named $CONTAINER_NAME"
fi

if docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    network_project=$(docker network inspect --format \
        '{{ index .Labels "com.docker.compose.project" }}' "$NETWORK_NAME")
    network_key=$(docker network inspect --format \
        '{{ index .Labels "com.docker.compose.network" }}' "$NETWORK_NAME")
    [ "$network_project" = "$COMPOSE_PROJECT" ] && [ "$network_key" = "storage" ] || \
        die "refusing to use an unknown Docker network named $NETWORK_NAME"
fi

if docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    image_project=$(docker image inspect --format \
        '{{ index .Config.Labels "com.docker.compose.project" }}' "$IMAGE_NAME")
    image_service=$(docker image inspect --format \
        '{{ index .Config.Labels "com.docker.compose.service" }}' "$IMAGE_NAME")
    [ "$image_project" = "$COMPOSE_PROJECT" ] && [ "$image_service" = "$COMPOSE_SERVICE" ] || \
        die "refusing to replace an unknown image tagged $IMAGE_NAME"
fi

if [ ! -e /opt/zhuojian ]; then
    install -d -o root -g root -m 0755 /opt/zhuojian
fi
if [ ! -e /usr/local/sbin ]; then
    install -d -o root -g root -m 0755 /usr/local/sbin
fi
if [ ! -e "$INSTALL_DIR" ]; then
    install -d -o root -g root -m 0755 "$INSTALL_DIR"
fi
if [ "$WAS_MANAGED" -eq 0 ]; then
    marker_tmp=$(mktemp "$INSTALL_DIR/.managed-marker.XXXXXX")
    trap 'rm -f -- "${marker_tmp:-}"' 0 1 2 15
    printf '%s\n' "$MARKER_VALUE" > "$marker_tmp"
    chown root:root "$marker_tmp"
    chmod 0644 "$marker_tmp"
    install -o root -g root -m 0644 "$marker_tmp" "$MANAGED_MARKER"
    rm -f -- "$marker_tmp"
    marker_tmp=
    trap - 0 1 2 15
fi

ensure_private_directory /etc/zhuojian 0700
ensure_private_directory "$APPS_ENV_DIR" 0700
ensure_private_directory "$STATE_DIR" 0700
ensure_private_directory "$STATE_DIR/tmp" 0700
if [ ! -e "$STATE_MARKER" ] && [ ! -L "$STATE_MARKER" ]; then
    state_marker_tmp=$(mktemp "$STATE_DIR/.managed-marker.XXXXXX")
    trap 'rm -f -- "${state_marker_tmp:-}"' 0 1 2 15
    printf '%s\n' "$MARKER_VALUE" > "$state_marker_tmp"
    chown root:root "$state_marker_tmp"
    chmod 0600 "$state_marker_tmp"
    install -o root -g root -m 0600 "$state_marker_tmp" "$STATE_MARKER"
    rm -f -- "$state_marker_tmp"
    state_marker_tmp=
    trap - 0 1 2 15
fi

ensure_install_directory() {
    eid_path=$1
    if [ -e "$eid_path" ] || [ -L "$eid_path" ]; then
        [ -d "$eid_path" ] && [ ! -L "$eid_path" ] || \
            die "refusing non-directory or symlink install path: $eid_path"
        [ "$(stat -c '%u:%g' -- "$eid_path")" = "0:0" ] || \
            die "install path must be owned by root:root: $eid_path"
        chmod 0755 "$eid_path"
    else
        install -d -o root -g root -m 0755 "$eid_path"
    fi
}

ensure_install_directory "$INSTALL_DIR/bin"
ensure_install_directory "$INSTALL_DIR/src"
ensure_install_directory "$INSTALL_DIR/src/zhuojian_storage_gateway"

install_asset() {
    ia_relative=$1
    ia_mode=$2
    ia_destination=$INSTALL_DIR/$ia_relative
    if [ -e "$ia_destination" ] || [ -L "$ia_destination" ]; then
        [ -f "$ia_destination" ] && [ ! -L "$ia_destination" ] || \
            die "refusing unsafe managed file path: $ia_destination"
    fi
    install -o root -g root -m "$ia_mode" "$SOURCE_DIR/$ia_relative" "$ia_destination"
}

if [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
    for runtime_asset in \
        .dockerignore \
        Dockerfile \
        README.md \
        compose.yaml \
        pyproject.toml \
        requirements.txt \
        src/zhuojian_storage_gateway/__init__.py \
        src/zhuojian_storage_gateway/app.py \
        src/zhuojian_storage_gateway/cli.py \
        src/zhuojian_storage_gateway/config.py \
        src/zhuojian_storage_gateway/registry.py \
        src/zhuojian_storage_gateway/storage.py
    do
        install_asset "$runtime_asset" 0644
    done
    install_asset install.sh 0750
    install_asset bin/zhuojian-storage-gateway-admin 0750
fi

install -o root -g root -m 0750 \
    "$INSTALL_DIR/bin/zhuojian-storage-gateway-admin" "$WRAPPER_PATH"

compose() {
    docker compose \
        --project-name "$COMPOSE_PROJECT" \
        --project-directory "$INSTALL_DIR" \
        --file "$INSTALL_DIR/compose.yaml" \
        "$@"
}

previous_image_id=
previous_was_healthy=0
if [ "$container_exists" -eq 1 ]; then
    previous_image_id=$(docker container inspect --format '{{.Image}}' "$CONTAINER_NAME")
    previous_state=$(docker container inspect --format \
        '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$CONTAINER_NAME")
    if [ "$previous_state" = "running/healthy" ]; then
        previous_was_healthy=1
    fi
fi

wait_for_health() {
    wfh_attempt=0
    while [ "$wfh_attempt" -lt 45 ]; do
        wfh_state=$(docker container inspect --format \
            '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$CONTAINER_NAME" 2>/dev/null || true)
        [ "$wfh_state" = "running/healthy" ] && return 0
        case "$wfh_state" in
            exited/*|dead/*|removing/*) return 1 ;;
        esac
        wfh_attempt=$((wfh_attempt + 1))
        sleep 2
    done
    return 1
}

restore_previous() {
    [ "$previous_was_healthy" -eq 1 ] || return 1
    docker image inspect "$previous_image_id" >/dev/null 2>&1 || return 1
    # Restore the stable tag even when Compose failed before replacing the old
    # healthy container; otherwise a later `compose up` would retry the bad image.
    docker image tag "$previous_image_id" "$IMAGE_NAME" >/dev/null 2>&1 || return 1
    current_image_id=$(docker container inspect --format '{{.Image}}' \
        "$CONTAINER_NAME" 2>/dev/null || true)
    current_state=$(docker container inspect --format \
        '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$CONTAINER_NAME" 2>/dev/null || true)
    if [ "$current_image_id" = "$previous_image_id" ] && \
       [ "$current_state" = "running/healthy" ]; then
        return 0
    fi
    compose up -d --no-deps --force-recreate "$COMPOSE_SERVICE" >/dev/null 2>&1 || return 1
    wait_for_health
}

if ! compose build "$COMPOSE_SERVICE"; then
    die "gateway image build failed; no Docker resources were pruned"
fi

if ! compose up -d --no-deps "$COMPOSE_SERVICE"; then
    if restore_previous; then
        die "gateway start failed; the previous healthy image was restored"
    fi
    die "gateway start failed; no previous healthy image was available to restore"
fi

if ! wait_for_health || ! docker exec "$CONTAINER_NAME" \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" \
    >/dev/null 2>&1; then
    if restore_previous; then
        die "gateway health check failed; the previous healthy image was restored"
    fi
    die "gateway health check failed; inspect the container without printing its environment"
fi

echo "ZhuoJian storage gateway is installed and locally healthy"
echo "install directory: $INSTALL_DIR"
echo "admin command: $WRAPPER_PATH"
