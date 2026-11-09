#!/usr/bin/env bash
# Fully self-contained bootstrap for a BRAND NEW remote server: assumes NOTHING is installed
# yet (no Python 3.11, no Node.js, no Postgres, no Redis, no nginx, no 'dev' user, no checkout)
# and builds the whole stack from a bare Ubuntu 22.04/24.04 box up to a running kra-vscu-backend
# API plus its admin-portal frontend (NolyLinkAdmin), served from the same domain — the
# frontend has no CORS middleware to rely on, so it must share the API's origin.
# Set ENABLE_FRONTEND=false to deploy the API only.
#
# This is a different script from deploy/install.sh (which assumes it's running on the
# existing shared dev box, where Postgres/Redis/the 'dev' user/the git remote already exist
# and sibling services are already running). Use THIS script only for a new, dedicated
# standalone server.
#
# Usage (as root, on the target server):
#   curl -fsSL https://raw.githubusercontent.com/highbreed/NolyLinkBackend/main/deploy/bootstrap_server.sh -o bootstrap_server.sh
#   DOMAIN=kra-vscu.example.com CERTBOT_EMAIL=ops@example.com sudo -E ./bootstrap_server.sh
#
# Everything below is overridable via environment variables; sane defaults are used otherwise.
# Re-running this script is safe — every step checks whether it already happened.
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (env var overrides)
# ---------------------------------------------------------------------------
APP_USER="${APP_USER:-dev}"
APP_ROOT="${APP_ROOT:-/opt/kra-vscu-backend}"
GIT_REPO_URL="${GIT_REPO_URL:-https://github.com/highbreed/NolyLinkBackend.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"

DOMAIN="${DOMAIN:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
ENABLE_TLS="${ENABLE_TLS:-true}"      # set to "false" to skip certbot entirely

KRA_ENV="${KRA_ENV:-sandbox}"          # DO NOT set to "production" without CONFIRM_PRODUCTION=yes
CONFIRM_PRODUCTION="${CONFIRM_PRODUCTION:-}"

DB_NAME="${DB_NAME:-kra_vscu_production}"
DB_USER="${DB_USER:-kra_vscu_prod}"

FRONTEND_ROOT="${FRONTEND_ROOT:-/opt/kra-vscu-frontend}"
FRONTEND_GIT_REPO_URL="${FRONTEND_GIT_REPO_URL:-https://github.com/highbreed/NolyLinkAdmin.git}"
FRONTEND_GIT_BRANCH="${FRONTEND_GIT_BRANCH:-main}"
ENABLE_FRONTEND="${ENABLE_FRONTEND:-true}"   # set to "false" to deploy the API only

PYTHON_PKG="python3.11"
SOCKET_PATH="/var/sockets/kra_vscu_backend_api.sock"

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "Must be run as root (sudo -E ./bootstrap_server.sh)." >&2
    exit 1
fi

if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
    echo "This script targets Ubuntu 22.04/24.04 only (apt-based, systemd, nginx packaging" >&2
    echo "assumptions throughout). Detected: $(grep PRETTY_NAME /etc/os-release 2>/dev/null)." >&2
    exit 1
fi

if [[ -z "$DOMAIN" ]]; then
    if [[ -t 0 ]]; then
        read -rp "Domain this server will be reachable at (DNS must already point here): " DOMAIN
    fi
    if [[ -z "$DOMAIN" ]]; then
        echo "DOMAIN is required, e.g.: DOMAIN=kra-vscu.example.com sudo -E ./bootstrap_server.sh" >&2
        exit 1
    fi
fi

if [[ "$ENABLE_TLS" == "true" && -z "$CERTBOT_EMAIL" ]]; then
    if [[ -t 0 ]]; then
        read -rp "Email for Let's Encrypt renewal notices: " CERTBOT_EMAIL
    fi
    if [[ -z "$CERTBOT_EMAIL" ]]; then
        echo "CERTBOT_EMAIL is required when ENABLE_TLS=true (or set ENABLE_TLS=false)." >&2
        exit 1
    fi
fi

if [[ "$KRA_ENV" == "production" && "$CONFIRM_PRODUCTION" != "yes" ]]; then
    echo "KRA_ENV=production requested but CONFIRM_PRODUCTION=yes was not set — refusing." >&2
    echo "Going live against KRA's real production endpoints must be a deliberate, separate" >&2
    echo "decision, not a side effect of running this bootstrap script." >&2
    exit 1
fi

echo "=============================================================================="
echo " kra-vscu-backend — fresh server bootstrap"
echo "   App root     : $APP_ROOT"
echo "   App user     : $APP_USER (locked-down, no login)"
echo "   Domain       : $DOMAIN"
echo "   TLS          : $ENABLE_TLS"
echo "   KRA_ENV      : $KRA_ENV"
echo "   Git (API)    : $GIT_REPO_URL @ $GIT_BRANCH"
echo "   Frontend     : $ENABLE_FRONTEND ($FRONTEND_GIT_REPO_URL @ $FRONTEND_GIT_BRANCH)"
echo "=============================================================================="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "==> [1/12] Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y

if ! apt-get install -y "$PYTHON_PKG" "${PYTHON_PKG}-venv" "${PYTHON_PKG}-dev"; then
    echo "==> $PYTHON_PKG not in default repos — adding deadsnakes PPA"
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y
    apt-get install -y "$PYTHON_PKG" "${PYTHON_PKG}-venv" "${PYTHON_PKG}-dev"
fi

apt-get install -y \
    build-essential libpq-dev git curl ca-certificates gnupg \
    postgresql postgresql-contrib \
    redis-server \
    nginx

if [[ "$ENABLE_TLS" == "true" ]]; then
    apt-get install -y certbot python3-certbot-nginx
fi

if [[ "$ENABLE_FRONTEND" == "true" ]] && ! command -v node &>/dev/null; then
    echo "==> Node.js not found — installing Node 20.x (NodeSource) for the frontend build"
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi

systemctl enable --now postgresql
systemctl enable --now redis-server
systemctl enable --now nginx

# ---------------------------------------------------------------------------
# 2. Service account
# ---------------------------------------------------------------------------
echo "==> [2/12] Creating service account '$APP_USER'"
mkdir -p "$APP_ROOT"
if ! id -u "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --home-dir "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
else
    echo "    '$APP_USER' already exists — leaving as-is."
fi

# ---------------------------------------------------------------------------
# 3. Checkout
# ---------------------------------------------------------------------------
echo "==> [3/12] Cloning application"
if [[ -d "$APP_ROOT/.git" ]]; then
    echo "    Checkout already exists at $APP_ROOT — pulling latest $GIT_BRANCH instead of cloning."
    sudo -u "$APP_USER" git -C "$APP_ROOT" fetch origin "$GIT_BRANCH"
    sudo -u "$APP_USER" git -C "$APP_ROOT" checkout "$GIT_BRANCH"
    sudo -u "$APP_USER" git -C "$APP_ROOT" pull --ff-only origin "$GIT_BRANCH"
else
    git clone --branch "$GIT_BRANCH" "$GIT_REPO_URL" "$APP_ROOT"
    chown -R "$APP_USER:$APP_USER" "$APP_ROOT"
fi

# ---------------------------------------------------------------------------
# 4. Python environment
# ---------------------------------------------------------------------------
echo "==> [4/12] Building virtualenv + installing dependencies"
sudo -u "$APP_USER" bash -c "
    cd '$APP_ROOT'
    $PYTHON_PKG -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install .
"
mkdir -p "$APP_ROOT/logs"
chown "$APP_USER:$APP_USER" "$APP_ROOT/logs"

# ---------------------------------------------------------------------------
# 5. Frontend (admin portal) — same origin as the API, no CORS anywhere by design
# ---------------------------------------------------------------------------
if [[ "$ENABLE_FRONTEND" == "true" ]]; then
    echo "==> [5/12] Cloning + building frontend"
    mkdir -p "$FRONTEND_ROOT"
    if [[ -d "$FRONTEND_ROOT/.git" ]]; then
        echo "    Checkout already exists at $FRONTEND_ROOT — pulling latest $FRONTEND_GIT_BRANCH."
        sudo -u "$APP_USER" git -C "$FRONTEND_ROOT" fetch origin "$FRONTEND_GIT_BRANCH"
        sudo -u "$APP_USER" git -C "$FRONTEND_ROOT" checkout "$FRONTEND_GIT_BRANCH"
        sudo -u "$APP_USER" git -C "$FRONTEND_ROOT" pull --ff-only origin "$FRONTEND_GIT_BRANCH"
    else
        git clone --branch "$FRONTEND_GIT_BRANCH" "$FRONTEND_GIT_REPO_URL" "$FRONTEND_ROOT"
        chown -R "$APP_USER:$APP_USER" "$FRONTEND_ROOT"
    fi
    sudo -u "$APP_USER" bash -c "cd '$FRONTEND_ROOT' && npm ci && npm run build"
else
    echo "==> [5/12] Skipping frontend (ENABLE_FRONTEND=false)"
fi

# ---------------------------------------------------------------------------
# 6. Postgres role + database (idempotent)
# ---------------------------------------------------------------------------
echo "==> [6/12] Configuring Postgres"
DB_PASSWORD_NEW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9')"

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    echo "    Role '$DB_USER' already exists — reusing it, not resetting its password."
    DB_IS_NEW=false
else
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD_NEW';"
    DB_IS_NEW=true
fi

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    echo "    Database '$DB_NAME' already exists."
else
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
fi

# ---------------------------------------------------------------------------
# 7. Redis password (idempotent — only set once, first run)
# ---------------------------------------------------------------------------
echo "==> [7/12] Configuring Redis"
REDIS_CONF="/etc/redis/redis.conf"
REDIS_PASSWORD_NEW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9')"

if grep -qE '^requirepass\s+\S+' "$REDIS_CONF" 2>/dev/null; then
    echo "    Redis already has a requirepass set — leaving it alone."
    REDIS_IS_NEW=false
else
    echo "requirepass $REDIS_PASSWORD_NEW" >> "$REDIS_CONF"
    systemctl restart redis-server
    REDIS_IS_NEW=true
fi

# ---------------------------------------------------------------------------
# 8. .env (only generated once — never overwritten on re-run)
# ---------------------------------------------------------------------------
echo "==> [8/12] Writing .env"
ENV_FILE="$APP_ROOT/.env"

if [[ -f "$ENV_FILE" ]]; then
    echo "    $ENV_FILE already exists — leaving it untouched."
else
    if [[ "$DB_IS_NEW" != true || "$REDIS_IS_NEW" != true ]]; then
        echo "WARNING: .env is missing but the Postgres role or Redis password already existed" >&2
        echo "from a prior partial run — the password used below may not match what's really" >&2
        echo "configured. Verify DATABASE_URL/REDIS_URL in $ENV_FILE by hand after this finishes." >&2
    fi

    cp "$APP_ROOT/.env.example" "$ENV_FILE"

    CMC_KEY="$(sudo -u "$APP_USER" "$APP_ROOT/venv/bin/python" -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
    JWT_SECRET="$(sudo -u "$APP_USER" "$APP_ROOT/venv/bin/python" -c \
        "import secrets; print(secrets.token_urlsafe(32))")"
    ADMIN_KEY="$(sudo -u "$APP_USER" "$APP_ROOT/venv/bin/python" -c \
        "import secrets; print(secrets.token_urlsafe(32))")"

    sed -i \
        -e "s|^APP_ENV=.*|APP_ENV=production|" \
        -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASSWORD_NEW}@localhost:5432/${DB_NAME}|" \
        -e "s|^REDIS_URL=.*|REDIS_URL=redis://:${REDIS_PASSWORD_NEW}@localhost:6379/0|" \
        -e "s|^CELERY_BROKER_URL=.*|CELERY_BROKER_URL=redis://:${REDIS_PASSWORD_NEW}@localhost:6379/1|" \
        -e "s|^CELERY_RESULT_BACKEND=.*|CELERY_RESULT_BACKEND=redis://:${REDIS_PASSWORD_NEW}@localhost:6379/2|" \
        -e "s|^KRA_ENV=.*|KRA_ENV=${KRA_ENV}|" \
        -e "s|^CMC_KEY_ENCRYPTION_KEY=.*|CMC_KEY_ENCRYPTION_KEY=${CMC_KEY}|" \
        -e "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=${JWT_SECRET}|" \
        -e "s|^ADMIN_API_KEY=.*|ADMIN_API_KEY=${ADMIN_KEY}|" \
        "$ENV_FILE"

    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    cat <<EOF

=============================================================================
SAVE THESE NOW — shown only this once (they're also in $ENV_FILE, mode 600):
  Postgres password : $DB_PASSWORD_NEW
  Redis password    : $REDIS_PASSWORD_NEW
  ADMIN_API_KEY      : $ADMIN_KEY
=============================================================================

EOF
fi

# ---------------------------------------------------------------------------
# 9. Migrations
# ---------------------------------------------------------------------------
echo "==> [9/12] Running migrations"
sudo -u "$APP_USER" bash -c "cd '$APP_ROOT' && venv/bin/alembic upgrade head"

# ---------------------------------------------------------------------------
# 10. systemd units
# ---------------------------------------------------------------------------
echo "==> [10/12] Installing systemd units"
mkdir -p /var/sockets
chown "$APP_USER:$APP_USER" /var/sockets

cat > /etc/systemd/system/kra-vscu-backend-api.service <<EOF
[Unit]
Description=KRA VSCU/OSCU Gateway - API (Gunicorn + UvicornWorker)
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_ROOT
Environment="PATH=$APP_ROOT/venv/bin:/usr/bin"
EnvironmentFile=$APP_ROOT/.env
StandardOutput=append:$APP_ROOT/logs/api.log
StandardError=append:$APP_ROOT/logs/api.log
ExecStartPre=/bin/mkdir -p /var/sockets
ExecStartPre=/bin/chown $APP_USER:$APP_USER /var/sockets
ExecStartPre=$APP_ROOT/venv/bin/alembic upgrade head
ExecStart=$APP_ROOT/venv/bin/python -m gunicorn \\
  app.main:app \\
  --worker-class uvicorn.workers.UvicornWorker \\
  --workers 4 \\
  --bind unix:$SOCKET_PATH \\
  --access-logfile - \\
  --error-logfile -

Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/kra-vscu-backend-worker.service <<EOF
[Unit]
Description=KRA VSCU/OSCU Gateway - Celery worker (outbox + reference-data pollers)
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_ROOT
Environment="PATH=$APP_ROOT/venv/bin:/usr/bin"
EnvironmentFile=$APP_ROOT/.env
StandardOutput=append:$APP_ROOT/logs/worker.log
StandardError=append:$APP_ROOT/logs/worker.log
ExecStart=$APP_ROOT/venv/bin/celery -A app.workers.celery_app worker --loglevel=info --concurrency=4

Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/kra-vscu-backend-beat.service <<EOF
[Unit]
Description=KRA VSCU/OSCU Gateway - Celery beat scheduler
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_ROOT
Environment="PATH=$APP_ROOT/venv/bin:/usr/bin"
EnvironmentFile=$APP_ROOT/.env
StandardOutput=append:$APP_ROOT/logs/beat.log
StandardError=append:$APP_ROOT/logs/beat.log
ExecStart=$APP_ROOT/venv/bin/celery -A app.workers.celery_app beat --loglevel=info

Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

for unit in kra-vscu-backend-api kra-vscu-backend-worker kra-vscu-backend-beat; do
    systemctl enable "$unit" --quiet
    if systemctl is-active --quiet "$unit"; then
        systemctl restart "$unit"
    else
        systemctl start "$unit"
    fi
done

# ---------------------------------------------------------------------------
# 11. nginx site
# ---------------------------------------------------------------------------
echo "==> [11/12] Installing nginx site for $DOMAIN"
if [[ "$ENABLE_FRONTEND" == "true" ]]; then
    FRONTEND_LOCATION_BLOCK=$(cat <<EOF
    # Admin portal SPA — same origin as the API below (it has no CORS middleware, by design;
    # the frontend only ever fetches relative /v1, /admin, /oauth paths).
    root $FRONTEND_ROOT/dist;
    location / {
        try_files \$uri \$uri/ /index.html;
    }
EOF
)
else
    FRONTEND_LOCATION_BLOCK=$(cat <<EOF
    location / {
        proxy_pass http://unix:$SOCKET_PATH;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
EOF
)
fi

cat > "/etc/nginx/sites-available/kra-vscu-backend.conf" <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    access_log /var/log/nginx/kra-vscu-backend-access.log;
    error_log /var/log/nginx/kra-vscu-backend-error.log;

    location /v1/ {
        proxy_pass http://unix:$SOCKET_PATH;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /oauth/ {
        proxy_pass http://unix:$SOCKET_PATH;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /admin/ {
        proxy_pass http://unix:$SOCKET_PATH;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

$FRONTEND_LOCATION_BLOCK
}
EOF

ln -sf /etc/nginx/sites-available/kra-vscu-backend.conf /etc/nginx/sites-enabled/kra-vscu-backend.conf
nginx -t
systemctl reload nginx

# ---------------------------------------------------------------------------
# 12. TLS
# ---------------------------------------------------------------------------
if [[ "$ENABLE_TLS" == "true" ]]; then
    echo "==> [12/12] Obtaining TLS certificate for $DOMAIN"
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect
else
    echo "==> [12/12] Skipping TLS (ENABLE_TLS=false) — plain HTTP only."
fi

# ---------------------------------------------------------------------------
# Optional firewall touch-up (only if ufw is already active — never force-enables it)
# ---------------------------------------------------------------------------
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow OpenSSH >/dev/null 2>&1 || true
    ufw allow "Nginx Full" >/dev/null 2>&1 || true
fi

echo ""
echo "=============================================================================="
echo " Done. Status:"
for unit in kra-vscu-backend-api kra-vscu-backend-worker kra-vscu-backend-beat; do
    printf "   %-28s %s\n" "$unit" "$(systemctl is-active "$unit")"
done
echo ""
echo " Verify:"
echo "   curl --unix-socket $SOCKET_PATH http://localhost/v1/health"
if [[ "$ENABLE_TLS" == "true" ]]; then
    echo "   curl https://$DOMAIN/v1/health"
    if [[ "$ENABLE_FRONTEND" == "true" ]]; then
        echo "   open https://$DOMAIN/          # admin portal"
    fi
else
    echo "   curl http://$DOMAIN/v1/health"
    if [[ "$ENABLE_FRONTEND" == "true" ]]; then
        echo "   open http://$DOMAIN/          # admin portal"
    fi
fi
echo "   journalctl -u kra-vscu-backend-api -f"
echo ""
echo " Still to do by hand (never automated):"
echo "   - Create the first AdminUser: POST /admin/users using the ADMIN_API_KEY above, then"
echo "     log into the admin portal with it."
echo "   - KRA_ENV is '$KRA_ENV'. Going to production is a separate, deliberate decision —"
echo "     see the KRA environment section of deploy/README.md for everything that implies."
echo "   - VSCU_DVC_SRL_NO_SBX / VSCU_DVC_SRL_NO_PROD in .env are still blank — fill in once"
echo "     KRA issues them for this integration."
echo "=============================================================================="
