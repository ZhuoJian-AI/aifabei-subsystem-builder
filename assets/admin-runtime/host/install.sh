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

for command in python3 git docker nginx certbot systemctl systemd-tmpfiles install; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is missing: $command" >&2
        exit 2
    }
done

# The Runtime identity is pre-provisioned by the platform administrator.  This
# installer intentionally cannot create, print, rotate, or replace it.
[ -f /etc/zhuojian/runtime.json ] || { echo "missing /etc/zhuojian/runtime.json" >&2; exit 2; }
[ ! -L /etc/zhuojian/runtime.json ] || { echo "runtime.json must not be a symlink" >&2; exit 2; }
[ -f /etc/zhuojian/runtime-registration.key ] || { echo "missing existing Runtime credential" >&2; exit 2; }
[ ! -L /etc/zhuojian/runtime-registration.key ] || { echo "Runtime credential must not be a symlink" >&2; exit 2; }
[ "$(stat -c '%u:%a' /etc/zhuojian/runtime-registration.key)" = "0:600" ] || {
    echo "Runtime credential must already be root-owned mode 0600" >&2
    exit 2
}

ensure_directory() {
    target=$1
    mode=$2
    if [ -e "$target" ]; then
        [ -d "$target" ] && [ ! -L "$target" ] || {
            echo "refusing non-directory path: $target" >&2
            exit 2
        }
    else
        install -d -o root -g root -m "$mode" "$target"
    fi
}

ensure_directory /srv/zhuojian/repositories 0750
ensure_directory /srv/zhuojian/deployments 0750
ensure_directory /srv/zhuojian/data 0750
ensure_directory /srv/zhuojian/backups 0700
ensure_directory /etc/zhuojian 0700
ensure_directory /etc/zhuojian/apps 0700
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

# Do not compete with an administrator's existing default virtual host.  The
# optional replacement is deliberately limited to Ubuntu's untouched package
# default, which has already been reviewed by the administrator.  Its symlink
# is moved to a recoverable location instead of being deleted.
DENY_TARGET=/etc/nginx/conf.d/00-zhuojian-default-deny.conf
STOCK_LINK=/etc/nginx/sites-enabled/default
DISABLED_DIR=/etc/zhuojian/disabled-nginx-sites
DISABLED_STOCK_LINK=$DISABLED_DIR/default
MOVED_STOCK_DEFAULT=0
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

if [ ! -e "$DENY_TARGET" ] && ! grep -Rqs --include='*.conf' 'default_server' /etc/nginx; then
    install -o root -g root -m 0644 "$SOURCE_DIR/nginx-default-deny.conf" "$DENY_TARGET"
    if ! nginx -t || ! systemctl reload nginx; then
        rm -f -- "$DENY_TARGET"
        if [ "$MOVED_STOCK_DEFAULT" -eq 1 ]; then
            mv -- "$DISABLED_STOCK_LINK" "$STOCK_LINK"
        fi
        nginx -t
        systemctl reload nginx
        echo "default Host guard conflicted with existing Nginx configuration; rolled back" >&2
        exit 2
    fi
elif [ "$MOVED_STOCK_DEFAULT" -eq 1 ]; then
    mv -- "$DISABLED_STOCK_LINK" "$STOCK_LINK"
    echo "another Nginx default_server exists; stock site restored" >&2
    exit 2
fi

# Preserve the already verified standard-SSH profile by default. Only an
# administrator who has separately installed and tested the HTTPS/SSH
# multiplexer may opt into the atomic 443 profile update. The credential is
# only stat'ed, never read.
if [ "$ENABLE_SSH_HTTPS_MULTIPLEX" -eq 1 ]; then
    /usr/local/sbin/zhuojian-runtime patch-runtime --host "$PUBLIC_ADDRESS"
fi
/usr/local/sbin/zhuojian-runtime disk-check --write-state --always-success

systemctl daemon-reload
systemctl enable --now zhuojian-disk-monitor.timer
systemctl enable --now zhuojian-backup.timer

/usr/local/sbin/zhuojian-runtime doctor
