#!/usr/bin/env bash
# Deploy script run ON 192.168.9.102:
# 1) install nginx (arm64/bionic)
# 2) reconfigure Flask backend to bind internal port 8011
# 3) write nginx config: 8010 -> /scrcpy/ (WS) to 131:8200, rest to Flask:8011
# 4) restart backend + start nginx, verify
set -e
BD=/home/firefly/cloud-phone-board/backend
LOG=/home/firefly/deploy_nginx.log
exec > >(tee -a "$LOG") 2>&1

echo "====[deploy_nginx] START $(date)===="

# ---- 0) install nginx if missing ----
if ! command -v nginx >/dev/null 2>&1; then
  echo "[1] installing nginx ..."
  sudo apt-get update -qq || true
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx nginx-full || \
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
fi
echo "[nginx] version: $(nginx -v 2>&1)"

# ---- 1) change backend PORT to 8011 (internal) ----
# rewrite PORT in .env to 8011
if grep -q '^PORT=8010' "$BD/.env"; then
  sed -i 's/^PORT=8010$/PORT=8011/' "$BD/.env"
  echo "[2] backend PORT -> 8011 (updated .env)"
else
  echo "[2] .env PORT: $(grep '^PORT=' "$BD/.env" || echo none)"
fi

# ---- 2) write nginx site config ----
NGINX_SITE=/etc/nginx/sites-available/cloudphone
NGINX_ENABLED=/etc/nginx/sites-enabled/cloudphone
sudo tee "$NGINX_SITE" >/dev/null <<'NGINX'
# cloud-phone-board reverse proxy: 102:8010
#   /scrcpy/  -> ws-scrcpy on 131:8200 (WebSocket upgrade)
#   all else  -> Flask on 127.0.0.1:8011

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 8010;
    server_name _;

    # ws-scrcpy (streaming + page + websocket)
    location /scrcpy/ {
        proxy_pass http://192.168.9.131:8200/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }

    # Flask backend (API + React SPA + assets)
    location / {
        proxy_pass http://127.0.0.1:8011;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX

# (bionic default site config may exist under /etc/nginx/sites-available/default with listen 80)
sudo ln -sf "$NGINX_SITE" "$NGINX_ENABLED"

# ---- 3) restart backend with new PORT ----
echo "[3] restarting backend (PORT=8011) ..."
bash /home/firefly/start_backend.sh || true
sleep 3
echo "backend health:"; curl -s --noproxy '*' http://127.0.0.1:8011/api/health || echo "  backend not on 8011 yet"

# ---- 4) nginx config test + reload ----
echo "[4] nginx config test ..."
sudo nginx -t
echo "--- reload / start nginx ---"
sudo systemctl enable nginx 2>/dev/null || true
sudo systemctl restart nginx 2>/dev/null || sudo service nginx restart || sudo nginx

sleep 2
echo "--- nginx listening: ---"
ss -tlnp | grep ':8010' || true

echo "====[deploy_nginx] DONE $(date)===="
