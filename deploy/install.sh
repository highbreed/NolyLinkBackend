#!/usr/bin/env bash
# Installs/updates the kra-vscu-backend systemd units and nginx site on this box.
# Idempotent — safe to re-run after every `git pull` to production.
#
# NOT RUN AS PART OF WRITING THIS SCRIPT. This only touches the repo's own files until someone
# runs it with sudo on the target box, after the one-time setup in deploy/README.md is done.
#
# Usage: sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Must be run with sudo (systemd units + nginx sites-enabled need root)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_ROOT="/home/dev/web_files/kra_vscu/production/kra_vscu_backend"

UNITS=(
    kra-vscu-backend-api
    kra-vscu-backend-worker
    kra-vscu-backend-beat
)

echo "==> Checking production checkout exists at $PROD_ROOT"
if [[ ! -f "$PROD_ROOT/.env" ]]; then
    echo "Missing $PROD_ROOT/.env — follow deploy/README.md's one-time setup first." >&2
    exit 1
fi
if [[ ! -x "$PROD_ROOT/venv/bin/gunicorn" ]]; then
    echo "Missing $PROD_ROOT/venv/bin/gunicorn — run 'pip install -e .' in $PROD_ROOT/venv first." >&2
    exit 1
fi
mkdir -p "$PROD_ROOT/logs"
chown dev:dev "$PROD_ROOT/logs"

echo "==> Running migrations (alembic upgrade head)"
sudo -u dev "$PROD_ROOT/venv/bin/alembic" -c "$PROD_ROOT/alembic.ini" upgrade head

echo "==> Installing systemd units"
cp "$SCRIPT_DIR"/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

for unit in "${UNITS[@]}"; do
    echo "==> Enabling + (re)starting $unit"
    systemctl enable "$unit" --quiet
    if systemctl is-active --quiet "$unit"; then
        systemctl restart "$unit"
    else
        systemctl start "$unit"
    fi
done

echo "==> Installing nginx site"
install -m 0644 "$SCRIPT_DIR/nginx/kra-vscu-backend.conf" /etc/nginx/sites-available/kra-vscu-backend.conf
ln -sf /etc/nginx/sites-available/kra-vscu-backend.conf /etc/nginx/sites-enabled/kra-vscu-backend.conf

echo "==> Testing nginx config"
nginx -t

echo "==> Reloading nginx"
systemctl reload nginx

echo ""
echo "==> Done. Status:"
for unit in "${UNITS[@]}"; do
    systemctl is-active "$unit" | xargs -I{} echo "  $unit: {}"
done

echo ""
echo "Verify:"
echo "  curl --unix-socket /var/sockets/kra_vscu_backend_api.sock http://localhost/v1/health"
echo "  curl http://localhost/v1/health          # once DNS/nginx point at this box"
echo "  journalctl -u kra-vscu-backend-api -f"
