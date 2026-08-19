#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/bootstrap_server.sh" >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPOSITORY_DIR=/opt/kfcquant/repository
RELEASES_DIR=/opt/kfcquant/releases
CURRENT_RELEASE=/opt/kfcquant/current
REPOSITORY_URL="$(git -C "$PROJECT_ROOT" remote get-url origin)"

SERVER_NAME="${KFCQUANT_SERVER_NAME:-}"
TLS_MODE="${KFCQUANT_TLS_MODE:-letsencrypt}"
CERT_EMAIL="${KFCQUANT_CERT_EMAIL:-}"
WEB_USER="${KFCQUANT_WEB_USER:-kfcquant}"
GENERATE_WEB_PASSWORD="${KFCQUANT_GENERATE_WEB_PASSWORD:-false}"
CREDENTIAL_OUTPUT="${KFCQUANT_CREDENTIAL_OUTPUT:-/root/kfcquant-initial-credentials}"
WEB_PASSWORD_FILE="${KFCQUANT_WEB_PASSWORD_FILE:-}"
PIP_INDEX_URL="${KFCQUANT_PIP_INDEX_URL:-}"

if [[ -z "$SERVER_NAME" ]]; then
  read -r -p "Public IPv4 address or domain: " SERVER_NAME
fi
if [[ "$TLS_MODE" == "letsencrypt" && -z "$CERT_EMAIL" ]]; then
  read -r -p "Let's Encrypt email: " CERT_EMAIL
fi
if [[ -n "$WEB_PASSWORD_FILE" ]]; then
  WEB_PASSWORD="$(head -n 1 "$WEB_PASSWORD_FILE")"
elif [[ "$GENERATE_WEB_PASSWORD" == true ]]; then
  WEB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
else
  read -r -s -p "Basic Auth password: " WEB_PASSWORD
  echo
fi

if [[ ! "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Server name must be an IPv4 address or DNS name" >&2
  exit 64
fi
if [[ "$TLS_MODE" != "letsencrypt" && "$TLS_MODE" != "self-signed" ]]; then
  echo "KFCQUANT_TLS_MODE must be letsencrypt or self-signed" >&2
  exit 64
fi
if [[ ! "$WEB_USER" =~ ^[A-Za-z0-9._-]+$ || -z "$WEB_PASSWORD" ]]; then
  echo "Basic Auth username and password must be non-empty and safe" >&2
  exit 64
fi
PIP_INDEX_ARGS=()
if [[ -n "$PIP_INDEX_URL" ]]; then
  if [[ ! "$PIP_INDEX_URL" =~ ^https://[A-Za-z0-9./_-]+$ ]]; then
    echo "KFCQUANT_PIP_INDEX_URL must be an HTTPS package index URL" >&2
    exit 64
  fi
  PIP_INDEX_ARGS=(--index-url "$PIP_INDEX_URL")
fi

if [[ "$TLS_MODE" == "self-signed" ]]; then
  TLS_CERTIFICATE=/etc/kfcquant/tls/fullchain.pem
  TLS_CERTIFICATE_KEY=/etc/kfcquant/tls/privkey.pem
else
  TLS_CERTIFICATE="/etc/letsencrypt/live/$SERVER_NAME/fullchain.pem"
  TLS_CERTIFICATE_KEY="/etc/letsencrypt/live/$SERVER_NAME/privkey.pem"
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git nginx apache2-utils openssl python3 python3-venv python3-pip sudo
systemctl enable --now nginx

python3 - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit("KFCQuant requires Python 3.12 or newer")
PY

getent group kfcquant >/dev/null || groupadd --system kfcquant
id -u kfcquant >/dev/null 2>&1 || useradd --system --gid kfcquant --create-home \
  --home-dir /var/lib/kfcquant --shell /usr/sbin/nologin kfcquant
id -u kfcops >/dev/null 2>&1 || useradd --system --gid kfcquant --create-home \
  --home-dir /var/lib/kfcops --shell /usr/sbin/nologin kfcops
usermod -aG kfcquant kfcops

install -d -o kfcops -g kfcquant -m 2770 /opt/kfcquant "$RELEASES_DIR"
install -d -o kfcops -g kfcquant -m 2770 /var/lib/kfcops /var/lib/kfcops/assurance
install -d -o kfcquant -g kfcquant -m 2770 \
  /var/lib/kfcquant/data /var/lib/kfcquant/data/raw \
  /var/lib/kfcquant/reports /var/lib/kfcquant/runtime /var/lib/kfcquant/backups
install -d /var/www/certbot

if [[ ! -d "$REPOSITORY_DIR/.git" ]]; then
  git clone --no-hardlinks "$PROJECT_ROOT" "$REPOSITORY_DIR"
  git -C "$REPOSITORY_DIR" remote set-url origin "$REPOSITORY_URL"
fi
chown -R kfcops:kfcquant "$REPOSITORY_DIR" "$RELEASES_DIR"
chmod -R g+rX "$REPOSITORY_DIR"

SOURCE_SHA="$(runuser -u kfcops -- git -C "$REPOSITORY_DIR" rev-parse HEAD)"
INITIAL_RELEASE="$RELEASES_DIR/$SOURCE_SHA"
if [[ -e "$CURRENT_RELEASE" || -L "$CURRENT_RELEASE" ]]; then
  ACTIVE_RELEASE="$(readlink -f "$CURRENT_RELEASE" || true)"
  if [[ "$ACTIVE_RELEASE" != "$INITIAL_RELEASE" ]]; then
    echo "Bootstrap cannot replace an existing Active Release; use deploy_server.sh" >&2
    exit 1
  fi
fi
if [[ ! -x "$INITIAL_RELEASE/.venv/bin/kfcquant" ]]; then
  if [[ -e "$INITIAL_RELEASE" ]]; then
    echo "Incomplete initial Release already exists: $INITIAL_RELEASE" >&2
    exit 1
  fi
  runuser -u kfcops -- git -C "$REPOSITORY_DIR" worktree add --detach "$INITIAL_RELEASE" "$SOURCE_SHA"
  runuser -u kfcops -- python3 -m venv "$INITIAL_RELEASE/.venv"
  runuser -u kfcops -- "$INITIAL_RELEASE/.venv/bin/python" -m pip install \
    "${PIP_INDEX_ARGS[@]}" --upgrade pip
  runuser -u kfcops -- "$INITIAL_RELEASE/.venv/bin/python" -m pip install \
    "${PIP_INDEX_ARGS[@]}" --requirement "$INITIAL_RELEASE/requirements.lock"
  runuser -u kfcops -- "$INITIAL_RELEASE/.venv/bin/python" -m pip install \
    --no-build-isolation --no-deps "$INITIAL_RELEASE"
fi
BUILD_TIME="$(runuser -u kfcops -- git -C "$REPOSITORY_DIR" show -s --format=%cI "$SOURCE_SHA")"
DEPENDENCY_LOCK_SHA256="$(sha256sum "$INITIAL_RELEASE/requirements.lock" | awk '{print $1}')"
runuser -u kfcops -- "$INITIAL_RELEASE/.venv/bin/kfcops" write-release-manifest \
  "$INITIAL_RELEASE" "$SOURCE_SHA" "$BUILD_TIME"
printf 'KFCQUANT_SOURCE_SHA=%s\nKFCQUANT_BUILD_TIME=%s\nKFCQUANT_RELEASE_MANIFEST=.release-manifest.json\nKFCQUANT_DEPENDENCY_LOCK_SHA256=%s\n' \
  "$SOURCE_SHA" "$BUILD_TIME" "$DEPENDENCY_LOCK_SHA256" > "$INITIAL_RELEASE/.release.env"
chown kfcops:kfcquant "$INITIAL_RELEASE/.release.env"
chmod 640 "$INITIAL_RELEASE/.release.env"

install -d -o root -g kfcquant -m 750 /etc/kfcquant
if [[ ! -f /etc/kfcquant/research.env ]]; then
  cp "$INITIAL_RELEASE/deploy/server.env.example" /etc/kfcquant/research.env
  chmod 640 /etc/kfcquant/research.env
  chown root:kfcquant /etc/kfcquant/research.env
fi

if [[ ! -f /etc/kfcquant/ops.env ]]; then
  OPS_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > /etc/kfcquant/ops.env <<EOF
KFCOPS_SESSION_SECRET=$OPS_SECRET
KFCOPS_GITHUB_REPOSITORY=Dershine/KFCQuantitative
KFCOPS_GITHUB_TOKEN=
KFCOPS_DATABASE_PATH=/var/lib/kfcops/ops.sqlite3
KFCOPS_DEPLOYMENT_LOCK=/var/lib/kfcops/deploy.lock
KFCOPS_REPOSITORY_DIRECTORY=$REPOSITORY_DIR
KFCOPS_RELEASES_DIRECTORY=$RELEASES_DIR
KFCOPS_CURRENT_RELEASE_LINK=$CURRENT_RELEASE
KFCOPS_BUILDER_PYTHON=/usr/bin/python3
KFCOPS_SERVICE_CONTROL_COMMAND=/usr/local/sbin/kfcquant-service-control
KFCOPS_RESEARCH_DATABASE=/var/lib/kfcquant/data/kfcquant.duckdb
KFCOPS_RESEARCH_LOCK=/var/lib/kfcquant/runtime/database.lock
KFCOPS_CERTIFICATE_PATH=$TLS_CERTIFICATE
KFCOPS_BACKUP_DIRECTORY=/var/lib/kfcquant/backups
KFCOPS_ASSURANCE_DIRECTORY=/var/lib/kfcops/assurance
KFCOPS_METRICS_PATH=/var/lib/kfcquant/runtime/observability-metrics.jsonl
KFCOPS_RAW_DATA_DIRECTORY=/var/lib/kfcquant/data/raw
EOF
  chmod 640 /etc/kfcquant/ops.env
  chown root:kfcquant /etc/kfcquant/ops.env
fi

upsert_ops_setting() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" /etc/kfcquant/ops.env; then
    sed -i "s|^${key}=.*|${key}=${value}|" /etc/kfcquant/ops.env
  else
    printf '%s=%s\n' "$key" "$value" >> /etc/kfcquant/ops.env
  fi
}
upsert_ops_setting KFCOPS_REPOSITORY_DIRECTORY "$REPOSITORY_DIR"
upsert_ops_setting KFCOPS_RELEASES_DIRECTORY "$RELEASES_DIR"
upsert_ops_setting KFCOPS_CURRENT_RELEASE_LINK "$CURRENT_RELEASE"
upsert_ops_setting KFCOPS_BUILDER_PYTHON /usr/bin/python3
chmod 640 /etc/kfcquant/ops.env
chown root:kfcquant /etc/kfcquant/ops.env

install -o root -g root -m 755 "$PROJECT_ROOT/deploy/kfcquant-service-control" \
  /usr/local/sbin/kfcquant-service-control
install -o root -g root -m 755 "$PROJECT_ROOT/deploy/kfcquant-admin" /usr/local/sbin/kfcquant-admin
cat > /etc/sudoers.d/kfcquant-service-control <<'EOF'
kfcops ALL=(root) NOPASSWD: /usr/local/sbin/kfcquant-service-control *
EOF
chmod 440 /etc/sudoers.d/kfcquant-service-control
visudo -cf /etc/sudoers.d/kfcquant-service-control

cp "$INITIAL_RELEASE/deploy/systemd/kfcquant-worker.service" /etc/systemd/system/kfcquant-worker.service
cp "$INITIAL_RELEASE/deploy/systemd/kfcquant-web.service" /etc/systemd/system/kfcquant-web.service
cp "$INITIAL_RELEASE/deploy/systemd/kfcops.service" /etc/systemd/system/kfcops.service
cp "$INITIAL_RELEASE/deploy/systemd/kfcquant-assurance.service" /etc/systemd/system/kfcquant-assurance.service
cp "$INITIAL_RELEASE/deploy/systemd/kfcquant-assurance.timer" /etc/systemd/system/kfcquant-assurance.timer
if [[ "$TLS_MODE" == "letsencrypt" ]]; then
  cp "$INITIAL_RELEASE/deploy/systemd/certbot-kfcquant.service" /etc/systemd/system/certbot-kfcquant.service
  cp "$INITIAL_RELEASE/deploy/systemd/certbot-kfcquant.timer" /etc/systemd/system/certbot-kfcquant.timer
fi
systemctl daemon-reload

set -a
# shellcheck disable=SC1091
source /etc/kfcquant/research.env
set +a
(
  cd "$INITIAL_RELEASE"
  runuser -u kfcquant --preserve-environment -- "$INITIAL_RELEASE/.venv/bin/kfcquant" migrate
)
if [[ -f /var/lib/kfcquant/data/kfcquant.duckdb ]]; then
  chmod 660 /var/lib/kfcquant/data/kfcquant.duckdb
fi

htpasswd -bc /etc/nginx/kfcquant.htpasswd "$WEB_USER" "$WEB_PASSWORD"
if [[ "$GENERATE_WEB_PASSWORD" == true ]]; then
  install -m 600 /dev/null "$CREDENTIAL_OUTPUT"
  printf 'username=%s\npassword=%s\n' "$WEB_USER" "$WEB_PASSWORD" > "$CREDENTIAL_OUTPUT"
fi

if [[ "$TLS_MODE" == "letsencrypt" ]]; then
  cat > /etc/nginx/sites-available/kfcquant <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 200 'KFCQuant certificate bootstrap'; add_header Content-Type text/plain; }
}
EOF
  ln -sfn /etc/nginx/sites-available/kfcquant /etc/nginx/sites-enabled/kfcquant
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx

  python3 -m venv /opt/certbot
  /opt/certbot/bin/pip install "${PIP_INDEX_ARGS[@]}" --upgrade 'certbot>=5.4'
  if [[ "$SERVER_NAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    /opt/certbot/bin/certbot certonly --non-interactive --agree-tos --email "$CERT_EMAIL" \
      --preferred-profile shortlived --webroot --webroot-path /var/www/certbot --ip-address "$SERVER_NAME"
  else
    /opt/certbot/bin/certbot certonly --non-interactive --agree-tos --email "$CERT_EMAIL" \
      --webroot --webroot-path /var/www/certbot -d "$SERVER_NAME"
  fi
else
  install -d -o root -g kfcquant -m 750 /etc/kfcquant/tls
  if [[ "$SERVER_NAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    TLS_SAN="IP:${SERVER_NAME}"
  else
    TLS_SAN="DNS:${SERVER_NAME}"
  fi
  openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 825 \
    -keyout "$TLS_CERTIFICATE_KEY" -out "$TLS_CERTIFICATE" \
    -subj "/CN=$SERVER_NAME" -addext "subjectAltName=$TLS_SAN"
  chown root:kfcquant "$TLS_CERTIFICATE" "$TLS_CERTIFICATE_KEY"
  chmod 640 "$TLS_CERTIFICATE" "$TLS_CERTIFICATE_KEY"
fi

sed -e "s|__SERVER_IP__|$SERVER_NAME|g" \
  -e "s|__TLS_CERTIFICATE__|$TLS_CERTIFICATE|g" \
  -e "s|__TLS_CERTIFICATE_KEY__|$TLS_CERTIFICATE_KEY|g" \
  "$PROJECT_ROOT/deploy/nginx/kfcquant.conf.template" \
  > /etc/nginx/sites-available/kfcquant
ln -sfn /etc/nginx/sites-available/kfcquant /etc/nginx/sites-enabled/kfcquant
rm -f /etc/nginx/sites-enabled/default
nginx -t
ln -sfn "$INITIAL_RELEASE" "$CURRENT_RELEASE"
systemctl enable --now kfcquant-worker kfcquant-web kfcops kfcquant-assurance.timer
if [[ "$TLS_MODE" == "letsencrypt" ]]; then
  systemctl enable --now certbot-kfcquant.timer
fi
systemctl reload nginx

echo "Bootstrap complete. Next:"
echo "1. Fill LLM_API_KEY in /etc/kfcquant/research.env."
echo "2. Fill KFCOPS_GITHUB_TOKEN in /etc/kfcquant/ops.env for a private repository."
echo "3. Restart services: systemctl restart kfcquant-worker kfcquant-web kfcops."
echo "4. Initialize history with: sudo kfcquant-admin sync-eod --start YYYY-MM-DD --end YYYY-MM-DD."
echo "5. Future updates: sudo bash $CURRENT_RELEASE/deploy/deploy_server.sh."
