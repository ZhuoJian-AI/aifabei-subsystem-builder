#!/bin/sh
set -eu

usage() {
    echo "usage: install.sh [--enable-ssh-https-multiplex --public-address <ECS-IP-or-hostname>] [--replace-stock-nginx-default]" >&2
    exit 2
}

PUBLIC_ADDRESS=""
ENABLE_SSH_HTTPS_MULTIPLEX=0
REPLACE_STOCK_NGINX_DEFAULT=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --public-address)
            [ "$#" -ge 2 ] || usage
            PUBLIC_ADDRESS=$2
            shift 2
            ;;
        --enable-ssh-https-multiplex)
            ENABLE_SSH_HTTPS_MULTIPLEX=1
            shift
            ;;
        --replace-stock-nginx-default)
            REPLACE_STOCK_NGINX_DEFAULT=1
            shift
            ;;
        *) usage ;;
    esac
done
if [ "$ENABLE_SSH_HTTPS_MULTIPLEX" -eq 1 ]; then
    [ -n "$PUBLIC_ADDRESS" ] || usage
elif [ -n "$PUBLIC_ADDRESS" ]; then
    echo "--public-address is only valid with --enable-ssh-https-multiplex" >&2
    exit 2
fi
[ "$(id -u)" -eq 0 ] || { echo "install.sh must run as root" >&2; exit 2; }

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for command in chmod cp git install mktemp mv nginx python3 rm stat systemctl systemd-tmpfiles docker certbot; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is missing: $command" >&2
        exit 2
    }
done

# The Runtime identity is pre-provisioned by the platform administrator.  This
# installer intentionally cannot create, print, rotate, or replace it.
[ -f /etc/zhuojian/runtime.json ] || { echo "missing /etc/zhuojian/runtime.json" >&2; exit 2; }
[ ! -L /etc/zhuojian/runtime.json ] || { echo "runtime.json must not be a symlink" >&2; exit 2; }
[ "$(stat -c '%u:%g' /etc/zhuojian/runtime.json)" = "0:0" ] || {
    echo "runtime.json must be root-owned" >&2
    exit 2
}
RUNTIME_MODE=$(stat -c '%a' /etc/zhuojian/runtime.json)
case "$RUNTIME_MODE" in
    600|640|644) ;;
    *) echo "runtime.json has an unsafe or unsupported mode" >&2; exit 2 ;;
esac
[ -f /etc/zhuojian/runtime-registration.key ] || { echo "missing existing Runtime credential" >&2; exit 2; }
[ ! -L /etc/zhuojian/runtime-registration.key ] || { echo "Runtime credential must not be a symlink" >&2; exit 2; }
[ "$(stat -c '%u:%a' /etc/zhuojian/runtime-registration.key)" = "0:600" ] || {
    echo "Runtime credential must already be root-owned mode 0600" >&2
    exit 2
}

PROFILE_MANAGEMENT_MODE=$(python3 - /etc/zhuojian/runtime.json <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        profile = json.load(handle)
    mode = profile["network"]["managementAccess"]["mode"]
except (OSError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit("runtime profile has no valid network.managementAccess.mode")
if mode not in {"standard-ssh", "ssh-https-multiplex"}:
    raise SystemExit("runtime profile management mode is unsupported")
print(mode)
PY
)
if [ "$ENABLE_SSH_HTTPS_MULTIPLEX" -eq 1 ]; then
    EFFECTIVE_MANAGEMENT_MODE=ssh-https-multiplex
else
    EFFECTIVE_MANAGEMENT_MODE=$PROFILE_MANAGEMENT_MODE
fi

ensure_directory() {
    target=$1
    mode=$2
    if [ -e "$target" ] || [ -L "$target" ]; then
        [ -d "$target" ] && [ ! -L "$target" ] || {
            echo "refusing non-directory path: $target" >&2
            exit 2
        }
        [ "$(stat -c '%u:%g' -- "$target")" = "0:0" ] || {
            echo "refusing non-root-owned managed directory: $target" >&2
            exit 2
        }
        chmod "$mode" -- "$target"
    else
        install -d -o root -g root -m "$mode" "$target"
    fi
    [ "$(stat -c '%u:%g:%a' -- "$target")" = "0:0:${mode#0}" ] || {
        echo "managed directory has unexpected owner or mode: $target" >&2
        exit 2
    }
}

ensure_directory /srv/zhuojian/repositories 0750
ensure_directory /srv/zhuojian/deployments 0750
ensure_directory /srv/zhuojian/data 0750
ensure_directory /srv/zhuojian/backups 0700
ensure_directory /etc/zhuojian 0700
ensure_directory /etc/zhuojian/apps 0700
ensure_directory /etc/zhuojian/storage-apps 0700
ensure_directory /var/lib/zhuojian/acme 0755
ensure_directory /run/zhuojian 0755

install -d -o root -g root -m 0750 /usr/local/lib/zhuojian
install -o root -g root -m 0750 "$SOURCE_DIR/runtime_admin.py" /usr/local/lib/zhuojian/runtime_admin.py
install -o root -g root -m 0750 "$SOURCE_DIR/zhuojian-runtime" /usr/local/sbin/zhuojian-runtime
install -o root -g root -m 0644 "$SOURCE_DIR/zhuojian-disk-monitor.service" /etc/systemd/system/zhuojian-disk-monitor.service
install -o root -g root -m 0644 "$SOURCE_DIR/zhuojian-disk-monitor.timer" /etc/systemd/system/zhuojian-disk-monitor.timer
install -o root -g root -m 0644 "$SOURCE_DIR/zhuojian-backup.service" /etc/systemd/system/zhuojian-backup.service
install -o root -g root -m 0644 "$SOURCE_DIR/zhuojian-backup.timer" /etc/systemd/system/zhuojian-backup.timer
install -o root -g root -m 0644 "$SOURCE_DIR/zhuojian-tmpfiles.conf" /etc/tmpfiles.d/zhuojian.conf
install -d -o root -g root -m 0755 /etc/systemd/system/docker.service.d
install -o root -g root -m 0644 "$SOURCE_DIR/docker-zhuojian-runtime-state.conf" /etc/systemd/system/docker.service.d/10-zhuojian-runtime-state.conf

# /run is a tmpfs and is empty after every reboot. Docker restores containers
# during boot, before the periodic disk monitor is guaranteed to run, so the
# shared read-only storage-state mount must exist during early boot.
systemd-tmpfiles --create /etc/tmpfiles.d/zhuojian.conf
[ -f /run/zhuojian/upload.lock ] && [ ! -L /run/zhuojian/upload.lock ] || {
    echo "shared upload lock is missing or unsafe" >&2
    exit 2
}
[ "$(stat -c '%u:%g:%a' -- /run/zhuojian/upload.lock)" = "0:0:444" ] || {
    echo "shared upload lock must be root:root mode 0444" >&2
    exit 2
}

# Do not compete with an administrator's existing default virtual host.  The
# optional replacement is deliberately limited to Ubuntu's untouched package
# default, which has already been reviewed by the administrator.  Its symlink
# is moved to a recoverable location instead of being deleted.
DENY_TARGET=/etc/nginx/conf.d/00-zhuojian-default-deny.conf
case "$EFFECTIVE_MANAGEMENT_MODE" in
    standard-ssh) DENY_SOURCE=$SOURCE_DIR/nginx-default-deny.conf ;;
    ssh-https-multiplex) DENY_SOURCE=$SOURCE_DIR/nginx-default-deny-multiplex.conf ;;
    *) echo "unsupported effective management profile" >&2; exit 2 ;;
esac
STOCK_LINK=/etc/nginx/sites-enabled/default
DISABLED_DIR=/etc/zhuojian/disabled-nginx-sites
DISABLED_STOCK_LINK=$DISABLED_DIR/default
MOVED_STOCK_DEFAULT=0
DENY_HAD_PREVIOUS=0
DENY_BACKUP=""
DENY_TOUCHED=0
NGINX_TRANSACTION_COMMITTED=0
RUNTIME_PROFILE=/etc/zhuojian/runtime.json
RUNTIME_BACKUP=""
RUNTIME_PATCH_STARTED=0

restore_runtime_profile() {
    [ "$RUNTIME_PATCH_STARTED" -eq 1 ] || return 0
    [ -n "$RUNTIME_BACKUP" ] && [ -f "$RUNTIME_BACKUP" ] || return 1
    runtime_rollback_tmp=$(mktemp /etc/zhuojian/.runtime.rollback.XXXXXX) || return 1
    if install -o root -g root -m "$RUNTIME_MODE" "$RUNTIME_BACKUP" "$runtime_rollback_tmp" && \
       mv -f -- "$runtime_rollback_tmp" "$RUNTIME_PROFILE"; then
        RUNTIME_PATCH_STARTED=0
        return 0
    fi
    rm -f -- "$runtime_rollback_tmp"
    return 1
}

rollback_nginx_guard() {
    if [ "$DENY_TOUCHED" -eq 1 ]; then
        if [ "$DENY_HAD_PREVIOUS" -eq 1 ] && [ -n "$DENY_BACKUP" ]; then
            install -o root -g root -m 0644 "$DENY_BACKUP" "$DENY_TARGET" || true
        else
            rm -f -- "$DENY_TARGET"
        fi
        DENY_TOUCHED=0
    fi
    if [ "$MOVED_STOCK_DEFAULT" -eq 1 ]; then
        if [ -L "$DISABLED_STOCK_LINK" ] && \
           [ ! -e "$STOCK_LINK" ] && [ ! -L "$STOCK_LINK" ]; then
            mv -- "$DISABLED_STOCK_LINK" "$STOCK_LINK" || true
        fi
        MOVED_STOCK_DEFAULT=0
    fi
    restore_runtime_profile || true
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
}

remove_transaction_backups() {
    [ -z "$DENY_BACKUP" ] || rm -f -- "$DENY_BACKUP"
    [ -z "$RUNTIME_BACKUP" ] || rm -f -- "$RUNTIME_BACKUP"
}

nginx_install_on_exit() {
    nioe_status=$?
    trap - 0 1 2 15
    if [ "$NGINX_TRANSACTION_COMMITTED" -ne 1 ]; then
        rollback_nginx_guard
    fi
    remove_transaction_backups
    exit "$nioe_status"
}

nginx_install_on_signal() {
    nios_status=$1
    trap - 0 1 2 15
    if [ "$NGINX_TRANSACTION_COMMITTED" -ne 1 ]; then
        rollback_nginx_guard
    fi
    remove_transaction_backups
    exit "$nios_status"
}

trap nginx_install_on_exit 0
trap 'nginx_install_on_signal 129' 1
trap 'nginx_install_on_signal 130' 2
trap 'nginx_install_on_signal 143' 15

if [ "$ENABLE_SSH_HTTPS_MULTIPLEX" -eq 1 ]; then
    RUNTIME_BACKUP=$(mktemp)
    cp -- "$RUNTIME_PROFILE" "$RUNTIME_BACKUP"
    chmod 0600 -- "$RUNTIME_BACKUP"
fi

if [ "$REPLACE_STOCK_NGINX_DEFAULT" -eq 1 ]; then
    if [ -L "$STOCK_LINK" ]; then
        [ "$(readlink -f -- "$STOCK_LINK")" = "/etc/nginx/sites-available/default" ] || {
            echo "refusing to replace a non-package Nginx default symlink" >&2
            exit 2
        }
        grep -Fq 'root /var/www/html;' "$STOCK_LINK" && \
        grep -Fq 'index.nginx-debian.html;' "$STOCK_LINK" || {
            echo "refusing to replace a customized Nginx default site" >&2
            exit 2
        }
        ensure_directory "$DISABLED_DIR" 0700
        [ ! -e "$DISABLED_STOCK_LINK" ] && [ ! -L "$DISABLED_STOCK_LINK" ] || {
            echo "disabled Nginx default backup already exists" >&2
            exit 2
        }
        mv -- "$STOCK_LINK" "$DISABLED_STOCK_LINK"
        MOVED_STOCK_DEFAULT=1
    elif [ ! -e "$DENY_TARGET" ] || [ ! -L "$DISABLED_STOCK_LINK" ]; then
        echo "stock Nginx default site was not found in the expected state" >&2
        exit 2
    fi
fi

nginx_has_guard_conflict() {
    mode=$1
    dump=$(mktemp)
    if ! nginx -T >"$dump" 2>&1; then
        rm -f -- "$dump"
        echo "existing Nginx configuration is invalid" >&2
        return 2
    fi
    if python3 - "$mode" "$dump" <<'PY'
import re
import sys

mode, path = sys.argv[1:]
text = open(path, "r", encoding="utf-8", errors="replace").read()
text = re.sub(r"#[^\n]*", "", text)
conflict = False
for arguments in re.findall(r"\blisten\s+([^;]+);", text):
    tokens = arguments.lower().split()
    if "default_server" not in tokens or not tokens:
        continue
    endpoint = tokens[0]
    if endpoint.isdigit():
        host, port = "*", int(endpoint)
    elif endpoint.startswith("[") and "]:" in endpoint:
        host, raw_port = endpoint.rsplit(":", 1)
        port = int(raw_port) if raw_port.isdigit() else -1
    elif ":" in endpoint:
        host, raw_port = endpoint.rsplit(":", 1)
        port = int(raw_port) if raw_port.isdigit() else -1
    else:
        continue
    if port == 80:
        conflict = True
    elif mode == "standard-ssh" and port == 443:
        conflict = True
    elif mode == "ssh-https-multiplex" and port == 8443 and host in {
        "*", "0.0.0.0", "[::]", "127.0.0.1", "[::1]"
    }:
        conflict = True
sys.exit(0 if conflict else 1)
PY
    then
        result=0
    else
        result=$?
    fi
    rm -f -- "$dump"
    return "$result"
}

NGINX_CHANGED=$MOVED_STOCK_DEFAULT
if [ -e "$DENY_TARGET" ] || [ -L "$DENY_TARGET" ]; then
    [ -f "$DENY_TARGET" ] && [ ! -L "$DENY_TARGET" ] || {
        echo "refusing non-regular managed Nginx guard: $DENY_TARGET" >&2
        exit 2
    }
    [ "$(stat -c '%u' -- "$DENY_TARGET")" = "0" ] || {
        echo "managed Nginx guard must be root-owned" >&2
        exit 2
    }
    grep -Eq '^# Managed by zhuojian-runtime-admin/v1; default guard profile=|^# Installed only when no other Nginx default_server already exists\.$' "$DENY_TARGET" || {
        echo "refusing to replace an unrecognized Nginx guard" >&2
        exit 2
    }
    DENY_BACKUP=$(mktemp)
    cp -- "$DENY_TARGET" "$DENY_BACKUP"
    DENY_HAD_PREVIOUS=1
    if ! cmp -s -- "$DENY_SOURCE" "$DENY_TARGET"; then
        DENY_TOUCHED=1
        install -o root -g root -m 0644 "$DENY_SOURCE" "$DENY_TARGET"
        NGINX_CHANGED=1
    fi
else
    if nginx_has_guard_conflict "$EFFECTIVE_MANAGEMENT_MODE"; then
        echo "an existing default_server conflicts with the required $EFFECTIVE_MANAGEMENT_MODE guard" >&2
        exit 2
    else
        conflict_status=$?
        [ "$conflict_status" -eq 1 ] || {
            exit 2
        }
    fi
    DENY_TOUCHED=1
    install -o root -g root -m 0644 "$DENY_SOURCE" "$DENY_TARGET"
    NGINX_CHANGED=1
fi

if [ "$NGINX_CHANGED" -eq 1 ] && { ! nginx -t || ! systemctl reload nginx; }; then
    rollback_nginx_guard
    remove_transaction_backups
    echo "default Host guard conflicted with existing Nginx configuration; rolled back" >&2
    exit 2
fi

# Preserve the already verified standard-SSH profile by default. Only an
# administrator who has separately installed and tested the HTTPS/SSH
# multiplexer may opt into the atomic 443 profile update. The credential is
# only stat'ed, never read.
if [ "$ENABLE_SSH_HTTPS_MULTIPLEX" -eq 1 ]; then
    RUNTIME_PATCH_STARTED=1
    if ! /usr/local/sbin/zhuojian-runtime patch-runtime --host "$PUBLIC_ADDRESS"; then
        rollback_nginx_guard
        remove_transaction_backups
        echo "Runtime profile update failed; Nginx guard was rolled back" >&2
        exit 2
    fi
fi
NGINX_TRANSACTION_COMMITTED=1
RUNTIME_PATCH_STARTED=0
remove_transaction_backups
trap - 0 1 2 15
/usr/local/sbin/zhuojian-runtime disk-check --write-state --always-success

systemctl daemon-reload
systemctl enable --now zhuojian-disk-monitor.timer
systemctl enable --now zhuojian-backup.timer

/usr/local/sbin/zhuojian-runtime doctor
