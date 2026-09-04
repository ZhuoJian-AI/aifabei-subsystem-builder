#!/bin/sh
set -eu

# This installer never reads OSS credential values in the host shell. The
# gateway process parses the root-only bind-mounted file after it starts.
umask 077

INSTALL_DIR=/opt/zhuojian/storage-gateway
MANAGED_MARKER=$INSTALL_DIR/.zhuojian-storage-gateway-managed
MARKER_VALUE=zhuojian-storage-gateway-installer-v1
SECRETS_FILE=/etc/zhuojian/oss-gateway.env
APPS_ENV_DIR=/etc/zhuojian/storage-apps
STATE_DIR=/var/lib/zhuojian-storage-gateway
WRAPPER_PATH=/usr/local/sbin/zhuojian-storage-gateway-admin
COMPOSE_PROJECT=zhuojian-storage-gateway
COMPOSE_SERVICE=zhuojian-storage-gateway
CONTAINER_NAME=zhuojian-storage-gateway
NETWORK_NAME=zhuojian-storage
APPLICATION_NETWORK_PREFIX=zhuojian-storage-
APPLICATION_NETWORK_ROLE=application-storage
RUNTIME_NETWORK_MANAGER=zhuojian-runtime-admin/v1
IMAGE_NAME=zhuojian/storage-gateway:local
STATE_MARKER=$STATE_DIR/.zhuojian-storage-gateway-managed
LOCK_DIR=/run/zhuojian-storage-gateway-installer
LOCK_FILE=$LOCK_DIR/install.lock
RUNTIME_LOCK_FILE=/run/lock/zhuojian-runtime-admin.lock

die() {
    echo "$1" >&2
    exit 2
}

[ "$(id -u)" -eq 0 ] || die "install.sh must run as root"

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

for required_command in chmod chown cmp dirname docker find flock grep id install mktemp python3 rm sleep stat; do
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

if [ -e "$RUNTIME_LOCK_FILE" ] || [ -L "$RUNTIME_LOCK_FILE" ]; then
    [ -f "$RUNTIME_LOCK_FILE" ] && [ ! -L "$RUNTIME_LOCK_FILE" ] || \
        die "refusing unsafe Runtime operation lock"
    [ "$(stat -c '%u:%g:%a' -- "$RUNTIME_LOCK_FILE")" = "0:0:600" ] || \
        die "Runtime operation lock must already be root:root mode 0600"
else
    install -o root -g root -m 0600 /dev/null "$RUNTIME_LOCK_FILE"
fi
exec 8<>"$RUNTIME_LOCK_FILE"
flock -n 8 || die "a Runtime deployment or storage operation is already running"

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
    scripts/migrate_legacy_storage_envs.py \
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
check_root_directory /etc/zhuojian/storage-apps
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
            .dockerignore|.zhuojian-storage-gateway-managed|Dockerfile|README.md|compose.yaml|pyproject.toml|requirements.txt|install.sh|bin|bin/zhuojian-storage-gateway-admin|scripts|scripts/migrate_legacy_storage_envs.py|src|src/zhuojian_storage_gateway|src/zhuojian_storage_gateway/__init__.py|src/zhuojian_storage_gateway/app.py|src/zhuojian_storage_gateway/cli.py|src/zhuojian_storage_gateway/config.py|src/zhuojian_storage_gateway/registry.py|src/zhuojian_storage_gateway/storage.py)
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

list_application_networks() {
    docker network ls \
        --filter "label=com.zhuojian.network-role=$APPLICATION_NETWORK_ROLE" \
        --format '{{.Name}}'
}

validate_application_network() {
    van_name=$1
    van_managed=$(docker network inspect --format \
        '{{ index .Labels "com.zhuojian.managed-by" }}' "$van_name" 2>/dev/null) || return 1
    van_role=$(docker network inspect --format \
        '{{ index .Labels "com.zhuojian.network-role" }}' "$van_name" 2>/dev/null) || return 1
    van_application=$(docker network inspect --format \
        '{{ index .Labels "com.zhuojian.application" }}' "$van_name" 2>/dev/null) || return 1
    van_enterprise=$(docker network inspect --format \
        '{{ index .Labels "com.zhuojian.enterprise" }}' "$van_name" 2>/dev/null) || return 1
    van_driver_scope=$(docker network inspect --format \
        '{{.Driver}}|{{.Scope}}' "$van_name" 2>/dev/null) || return 1
    [ "$van_managed" = "$RUNTIME_NETWORK_MANAGER" ] && \
    [ "$van_role" = "$APPLICATION_NETWORK_ROLE" ] && \
    [ "$van_name" = "$APPLICATION_NETWORK_PREFIX$van_application" ] && \
    [ "$van_driver_scope" = "bridge|local" ] || return 1
    printf '%s\n' "$van_application" | grep -Eq '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$' || return 1
    printf '%s\n' "$van_enterprise" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$' || return 1
    van_expected_app=zhuojian-$van_enterprise-$van_application
    van_members=$(docker network inspect --format \
        '{{range .Containers}}{{println .Name}}{{end}}' "$van_name" 2>/dev/null) || return 1
    for van_member in $van_members; do
        case "$van_member" in
            "$CONTAINER_NAME"|"$van_expected_app")
                ;;
            "$van_expected_app"-rollback-*)
                # Runtime updates may leave a stopped rollback container when
                # their best-effort post-commit cleanup fails.  The installer
                # holds the Runtime lock, so an exact, stopped, managed member
                # is safe to retain while the gateway is recreated.  A running,
                # unlabeled or cross-application lookalike remains a hard fail.
                van_rollback_identity=$(docker container inspect --format \
                    '{{ index .Config.Labels "com.zhuojian.managed-by" }}|{{ index .Config.Labels "com.zhuojian.application" }}|{{ index .Config.Labels "com.zhuojian.enterprise" }}|{{.State.Running}}' \
                    "$van_member" 2>/dev/null) || return 1
                [ "$van_rollback_identity" = \
                    "$RUNTIME_NETWORK_MANAGER|$van_application|$van_enterprise|false" ] || return 1
                ;;
            *)
                return 1
                ;;
        esac
    done
}

preflight_application_networks() {
    pan_networks=$(list_application_networks) || \
        die "failed to enumerate managed application storage networks"
    for pan_name in $pan_networks; do
        validate_application_network "$pan_name" || \
            die "refusing an untrusted application storage network: $pan_name"
    done
}

reattach_application_networks() {
    ran_networks=$(list_application_networks) || return 1
    for ran_name in $ran_networks; do
        validate_application_network "$ran_name" || return 1
        if ! docker network inspect --format \
            '{{range .Containers}}{{println .Name}}{{end}}' "$ran_name" | \
            grep -Fxq "$CONTAINER_NAME"; then
            docker network connect --alias "$CONTAINER_NAME" \
                "$ran_name" "$CONTAINER_NAME" >/dev/null 2>&1 || return 1
        fi
        validate_application_network "$ran_name" || return 1
    done
}

preflight_application_networks

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
ensure_install_directory "$INSTALL_DIR/scripts"
ensure_install_directory "$INSTALL_DIR/src"
ensure_install_directory "$INSTALL_DIR/src/zhuojian_storage_gateway"

# Compose configuration is part of a runnable gateway release.  Keep the exact
# previous managed file outside INSTALL_DIR before installing a new bundle so a
# failed cross-version upgrade can recreate the old image with its old mounts
# and env paths, not with the just-installed configuration.
ROLLBACK_CONFIG_DIR=
PREVIOUS_COMPOSE_FILE=
INSTALL_COMPLETED=0
IMAGE_BUILD_STARTED=0
CONTAINER_SWITCH_STARTED=0
CONTAINER_ROLLBACK_DONE=0
MIGRATION_STARTED=0

restore_compose_baseline() {
    [ -n "$PREVIOUS_COMPOSE_FILE" ] && [ -f "$PREVIOUS_COMPOSE_FILE" ] || return 0
    restored_compose_tmp=$(mktemp "$INSTALL_DIR/.compose.rollback.XXXXXX") || return 1
    if ! install -o root -g root -m 0644 \
        "$PREVIOUS_COMPOSE_FILE" "$restored_compose_tmp"; then
        rm -f -- "$restored_compose_tmp"
        return 1
    fi
    if ! mv -f -- "$restored_compose_tmp" "$INSTALL_DIR/compose.yaml"; then
        rm -f -- "$restored_compose_tmp"
        return 1
    fi
}

discard_rollback_config() {
    if [ -n "$ROLLBACK_CONFIG_DIR" ]; then
        rm -f -- "$ROLLBACK_CONFIG_DIR/compose.yaml"
        rmdir -- "$ROLLBACK_CONFIG_DIR" 2>/dev/null || true
        ROLLBACK_CONFIG_DIR=
        PREVIOUS_COMPOSE_FILE=
    fi
}

gateway_current_healthy() {
    [ "$(docker container inspect --format \
        '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$CONTAINER_NAME" 2>/dev/null || true)" = "running/healthy" ] && \
    docker exec "$CONTAINER_NAME" \
        python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" \
        >/dev/null 2>&1
}

restore_previous_image_tag() {
    [ "$IMAGE_BUILD_STARTED" -eq 1 ] || return 0
    [ -n "$previous_tag_image_id" ] || {
        IMAGE_BUILD_STARTED=0
        return 0
    }
    docker image inspect "$previous_tag_image_id" >/dev/null 2>&1 || return 1
    docker image tag "$previous_tag_image_id" "$IMAGE_NAME" >/dev/null 2>&1 || return 1
    IMAGE_BUILD_STARTED=0
}

rollback_interrupted_install() {
    [ "$INSTALL_COMPLETED" -ne 1 ] || return 0
    if [ "$MIGRATION_STARTED" -eq 1 ]; then
        # A normal signal may have interrupted Python between an atomic secret
        # move and its release-record update. Re-running is the documented
        # recovery path. Migration begins only after the new gateway passed its
        # health check. Once it may have moved an env file, the old image and
        # compose mount are no longer a compatible rollback target, so this is
        # an explicit forward-only transaction boundary.
        if python3 "$INSTALL_DIR/scripts/migrate_legacy_storage_envs.py" \
            >/dev/null 2>&1 && gateway_current_healthy; then
            INSTALL_COMPLETED=1
            IMAGE_BUILD_STARTED=0
            MIGRATION_STARTED=0
            return 0
        fi
        # Keep the already-selected new image/compose even when the recovery
        # probe is transiently unhealthy. A later installer retry resumes the
        # idempotent migration without ever pairing migrated secrets with the
        # old gateway mount layout.
        return 0
    fi
    if [ "$CONTAINER_SWITCH_STARTED" -eq 1 ] && \
       [ "$CONTAINER_ROLLBACK_DONE" -ne 1 ]; then
        if ! restore_previous >/dev/null 2>&1; then
            restore_previous_image_tag || true
            restore_compose_baseline || true
        fi
        return 0
    fi
    restore_previous_image_tag || true
    restore_compose_baseline || true
}

rollback_install_on_exit() {
    rie_status=$?
    trap - 0 1 2 15
    if [ "$INSTALL_COMPLETED" -ne 1 ]; then
        rollback_interrupted_install
    fi
    discard_rollback_config
    exit "$rie_status"
}

rollback_install_on_signal() {
    ris_status=$1
    trap - 0 1 2 15
    if [ "$INSTALL_COMPLETED" -ne 1 ]; then
        rollback_interrupted_install
    fi
    discard_rollback_config
    exit "$ris_status"
}

trap rollback_install_on_exit 0
trap 'rollback_install_on_signal 129' 1
trap 'rollback_install_on_signal 130' 2
trap 'rollback_install_on_signal 143' 15
if [ "$WAS_MANAGED" -eq 1 ] && [ "$container_exists" -eq 1 ]; then
    ROLLBACK_CONFIG_DIR=$(mktemp -d /run/zhuojian-storage-gateway-rollback.XXXXXX)
    PREVIOUS_COMPOSE_FILE=$ROLLBACK_CONFIG_DIR/compose.yaml
    install -o root -g root -m 0600 "$INSTALL_DIR/compose.yaml" "$PREVIOUS_COMPOSE_FILE"
fi

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
        scripts/migrate_legacy_storage_envs.py \
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

python3 "$INSTALL_DIR/scripts/migrate_legacy_storage_envs.py" --dry-run >/dev/null || \
    die "legacy storage environment preflight failed; no gateway container was changed"

compose() {
    docker compose \
        --project-name "$COMPOSE_PROJECT" \
        --project-directory "$INSTALL_DIR" \
        --file "$INSTALL_DIR/compose.yaml" \
        "$@"
}

previous_compose() {
    [ -n "$PREVIOUS_COMPOSE_FILE" ] && [ -f "$PREVIOUS_COMPOSE_FILE" ] || return 1
    docker compose \
        --project-name "$COMPOSE_PROJECT" \
        --project-directory "$INSTALL_DIR" \
        --file "$PREVIOUS_COMPOSE_FILE" \
        "$@"
}

previous_image_id=
previous_tag_image_id=
previous_was_healthy=0
if docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    previous_tag_image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE_NAME")
fi
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
    [ -n "$PREVIOUS_COMPOSE_FILE" ] && [ -f "$PREVIOUS_COMPOSE_FILE" ] || return 1
    docker image inspect "$previous_image_id" >/dev/null 2>&1 || return 1
    # Restore the persistent Compose baseline atomically before touching the
    # container.  A second retry must still know the old image's exact env and
    # mount layout after a cross-version failure.
    restore_compose_baseline || return 1
    # Restore the stable tag even when Compose failed before replacing the old
    # healthy container; otherwise a later `compose up` would retry the bad image.
    docker image tag "$previous_image_id" "$IMAGE_NAME" >/dev/null 2>&1 || return 1
    previous_compose up -d --no-deps --force-recreate "$COMPOSE_SERVICE" \
        >/dev/null 2>&1 || return 1
    reattach_application_networks || return 1
    if wait_for_health; then
        CONTAINER_ROLLBACK_DONE=1
        IMAGE_BUILD_STARTED=0
        CONTAINER_SWITCH_STARTED=0
        MIGRATION_STARTED=0
        return 0
    fi
    return 1
}

IMAGE_BUILD_STARTED=1
if ! compose build "$COMPOSE_SERVICE"; then
    die "gateway image build failed; no Docker resources were pruned"
fi

CONTAINER_SWITCH_STARTED=1
if ! compose up -d --no-deps "$COMPOSE_SERVICE"; then
    if restore_previous; then
        die "gateway start failed; the previous healthy image was restored"
    fi
    die "gateway start failed; no previous healthy image was available to restore"
fi

if ! reattach_application_networks; then
    if restore_previous; then
        die "gateway network reattachment failed; the previous healthy image was restored"
    fi
    die "gateway network reattachment failed; inspect only managed application networks"
fi

if ! wait_for_health || ! docker exec "$CONTAINER_NAME" \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" \
    >/dev/null 2>&1; then
    if restore_previous; then
        die "gateway health check failed; the previous healthy image was restored"
    fi
    die "gateway health check failed; inspect the container without printing its environment"
fi

MIGRATION_STARTED=1
if ! python3 "$INSTALL_DIR/scripts/migrate_legacy_storage_envs.py" >/dev/null; then
    die "legacy storage environment migration failed; the new gateway was retained so the idempotent migration can be retried safely"
fi

INSTALL_COMPLETED=1
IMAGE_BUILD_STARTED=0
MIGRATION_STARTED=0
discard_rollback_config
trap - 0 1 2 15

echo "ZhuoJian storage gateway is installed and locally healthy"
echo "install directory: $INSTALL_DIR"
echo "admin command: $WRAPPER_PATH"
