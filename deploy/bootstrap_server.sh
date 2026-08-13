#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo deploy/bootstrap_server.sh" >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
read -r -p "Public IPv4 address: " SERVER_IP
read -r -p "Let's Encrypt email: " CERT_EMAIL
read -r -p "Basic Auth username [kfcquant]: " WEB_USER
WEB_USER="${WEB_USER:-kfcquant}"
read -r -s -p "Basic Auth password: " WEB_PASSWORD
echo

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 nginx apache2-utils python3-venv python3-pip
systemctl enable --now docker nginx

id -u kfcops >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/kfcops --shell /usr/sbin/nologin kfcops
usermod -aG docker kfcops
install -d -o kfcops -g kfcops /var/lib/kfcops /opt/kfcquant/ops
install -d -o 10001 -g kfcops -m 2770 /var/lib/kfcquant/{data,reports,runtime,backups}
install -d -o kfcops -g kfcops -m 2770 /opt/kfcquant/research
install -d /var/www/certbot

cp "$PROJECT_ROOT/compose.yaml" /opt/kfcquant/research/compose.yaml
if [[ ! -f /opt/kfcquant/research/.env ]]; then
  cp "$PROJECT_ROOT/deploy/server.env.example" /opt/kfcquant/research/.env
  chmod 600 /opt/kfcquant/research/.env
fi
printf 'KFCQUANT_IMAGE_TAG=latest\n' > /opt/kfcquant/research/.release.env
chown -R kfcops:kfcops /opt/kfcquant/research

python3 -m venv /opt/kfcquant/ops/.venv
/opt/kfcquant/ops/.venv/bin/pip install --upgrade pip
/opt/kfcquant/ops/.venv/bin/pip install "$PROJECT_ROOT"

if [[ ! -f /etc/kfcquant/ops.env ]]; then
  install -d -m 700 /etc/kfcquant
  OPS_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > /etc/kfcquant/ops.env <<EOF
KFCOPS_SESSION_SECRET=$OPS_SECRET
KFCOPS_GITHUB_REPOSITORY=Dershine/KFCQuantitative
KFCOPS_DATABASE_PATH=/var/lib/kfcops/ops.sqlite3
KFCOPS_COMPOSE_DIRECTORY=/opt/kfcquant/research
KFCOPS_COMPOSE_FILE=/opt/kfcquant/research/compose.yaml
KFCOPS_RELEASE_ENV_FILE=/opt/kfcquant/research/.release.env
KFCOPS_RESEARCH_DATABASE=/var/lib/kfcquant/data/kfcquant.duckdb
KFCOPS_RESEARCH_LOCK=/var/lib/kfcquant/runtime/database.lock
KFCOPS_CERTIFICATE_PATH=/etc/letsencrypt/live/$SERVER_IP/cert.pem
KFCOPS_BACKUP_DIRECTORY=/var/lib/kfcquant/backups
EOF
  chmod 600 /etc/kfcquant/ops.env
fi

htpasswd -bc /etc/nginx/kfcquant.htpasswd "$WEB_USER" "$WEB_PASSWORD"
cat > /etc/nginx/sites-available/kfcquant <<EOF
server {
    listen 80;
    server_name $SERVER_IP;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 200 'KFCQuant certificate bootstrap'; add_header Content-Type text/plain; }
}
EOF
ln -sfn /etc/nginx/sites-available/kfcquant /etc/nginx/sites-enabled/kfcquant
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

python3 -m venv /opt/certbot
/opt/certbot/bin/pip install --upgrade 'certbot>=5.4'
/opt/certbot/bin/certbot certonly --non-interactive --agree-tos --email "$CERT_EMAIL" \
  --preferred-profile shortlived --webroot --webroot-path /var/www/certbot --ip-address "$SERVER_IP"

sed "s/__SERVER_IP__/$SERVER_IP/g" "$PROJECT_ROOT/deploy/nginx/kfcquant.conf.template" > /etc/nginx/sites-available/kfcquant
cp "$PROJECT_ROOT/deploy/systemd/kfcops.service" /etc/systemd/system/kfcops.service
cp "$PROJECT_ROOT/deploy/systemd/certbot-kfcquant.service" /etc/systemd/system/certbot-kfcquant.service
cp "$PROJECT_ROOT/deploy/systemd/certbot-kfcquant.timer" /etc/systemd/system/certbot-kfcquant.timer
systemctl daemon-reload
systemctl enable --now kfcops certbot-kfcquant.timer
nginx -t && systemctl reload nginx

echo "Bootstrap complete. Next:"
echo "1. Edit /opt/kfcquant/research/.env and /etc/kfcquant/ops.env."
echo "2. docker login ghcr.io with a read-only package token."
echo "3. Deploy a tested SHA from https://$SERVER_IP/ops/."
echo "4. Initialize history with docker compose run --rm research-worker kfcquant sync-eod --start YYYY-MM-DD --end YYYY-MM-DD."
