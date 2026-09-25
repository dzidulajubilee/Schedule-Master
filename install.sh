#!/usr/bin/env bash
# Schedule Master installer for a Linux VM (Debian/Ubuntu or RHEL/Rocky/Alma/Fedora).
#
#   sudo bash install.sh                          install / upgrade, https on this VM's own address(es)
#   sudo bash install.sh sched.lan 10.0.0.5        also answer to these names / IPs (added to the certificate)
#   sudo bash install.sh --allow any               let non-private (public) addresses reach the app too
#   sudo bash install.sh --allow 41.66.0.0/16      also allow one extra range on top of private addresses
#   sudo bash install.sh --uninstall               remove the service and nginx site (your data stays in /opt/schedulemaster)
#
# What it sets up
#   * the app in /opt/schedulemaster, run by systemd as its own unprivileged user, started at boot, restarted if it stops
#   * nginx on ports 80/443 (80 redirects to https) in front of it; the app itself only listens on 127.0.0.1:8766
#   * a self-signed https certificate for the VM's addresses (browsers show a one-time warning)
#   * by default, nginx only accepts connections from private/loopback addresses (RFC1918 + localhost) -
#     use --allow to widen that
# Re-running it is safe: code is updated, your staff/records/emails/login files are never overwritten.
#
# Login: the app has its own sign-in screen (default admin / admin - see the note printed at the end
# about login.json). This script does not touch application-level authentication; it only controls
# which network addresses can reach nginx at all.
set -euo pipefail

APP_DIR=/opt/schedulemaster
APP_USER=schedulemaster
SERVICE=schedulemaster
APP_PORT=8766
CERT_DIR=/etc/ssl/schedulemaster
NGINX_CONF=/etc/nginx/conf.d/schedulemaster.conf
NGINX_PROXY_INC=/etc/nginx/schedulemaster-proxy.inc
UNIT=/etc/systemd/system/${SERVICE}.service
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this as root:  sudo bash install.sh"
command -v systemctl >/dev/null || die "systemd was not found; this installer needs it."

ALLOW=""
UNINSTALL=0
NAMES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) UNINSTALL=1 ;;
    --allow) shift; [ $# -gt 0 ] || die "--allow needs a value (any, or e.g. 41.66.0.0/16)"; ALLOW="$1" ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    -*) die "Unknown option $1" ;;
    *) NAMES+=("$1") ;;
  esac
  shift
done

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing the Schedule Master service and nginx site"
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT" "$NGINX_CONF" "$NGINX_PROXY_INC"
  systemctl daemon-reload
  if command -v nginx >/dev/null && nginx -t >/dev/null 2>&1; then systemctl reload nginx 2>/dev/null || true; fi
  echo "Done. Your data is still in $APP_DIR (delete it yourself if you no longer need it)."
  echo "The certificate is still in $CERT_DIR."
  exit 0
fi

# ---------------------------------------------------------------- checks
for f in ScheduleMaster.py ScheduleMaster.html; do
  [ -f "$SRC_DIR/$f" ] || die "$f is not next to install.sh (looked in $SRC_DIR)."
done

# ---------------------------------------------------------------- packages
need=()
command -v python3  >/dev/null || need+=(python3)
command -v openssl  >/dev/null || need+=(openssl)
command -v curl     >/dev/null || need+=(curl)
command -v nginx    >/dev/null || need+=(nginx)
if [ ${#need[@]} -gt 0 ]; then
  say "Installing: ${need[*]}"
  if   command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y "${need[@]}"
  elif command -v dnf     >/dev/null; then dnf install -y "${need[@]}"
  elif command -v yum     >/dev/null; then yum install -y "${need[@]}"
  else die "No apt/dnf/yum found. Install ${need[*]} yourself, then run this again."; fi
fi
PYTHON="$(command -v python3)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 6))' || die "Python 3.6 or newer is needed."

# ---------------------------------------------------------------- app files
say "Installing the app into $APP_DIR"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  for f in ScheduleMaster.py ScheduleMaster.html README.md; do
    [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$APP_DIR/$f"
  done
  for f in staff.json records.json emails.json login.json; do      # your data: only copied if not there yet
    if [ -f "$SRC_DIR/$f" ] && [ ! -e "$APP_DIR/$f" ]; then cp "$SRC_DIR/$f" "$APP_DIR/$f"; fi
  done
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$APP_DIR"/*.py "$APP_DIR"/*.html 2>/dev/null || true
[ -f "$APP_DIR/login.json" ] && chmod 600 "$APP_DIR/login.json"

# ---------------------------------------------------------------- systemd service (starts at boot)
say "Creating the systemd service (starts at boot, restarts if it stops)"
cat > "$UNIT" <<EOF
[Unit]
Description=Schedule Master shift planner
After=network-online.target postfix.service
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=SM_HOST=127.0.0.1
Environment=SM_PORT=$APP_PORT
Environment=SM_TRUST_PROXY=1
ExecStart=$PYTHON $APP_DIR/ScheduleMaster.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

# ---------------------------------------------------------------- names for the certificate / nginx
IPS=()
for ip in $(hostname -I 2>/dev/null || true); do
  case "$ip" in *.*.*.*) IPS+=("$ip") ;; esac          # IPv4 only
done
HOSTNAME_SHORT="$(hostname 2>/dev/null || true)"
ALL=()
for n in "${NAMES[@]:-}" "${IPS[@]:-}" "$HOSTNAME_SHORT"; do
  [ -n "$n" ] && ALL+=("$n")
done
[ ${#ALL[@]} -gt 0 ] || die "Could not work out this VM's address. Run again and pass it:  sudo bash install.sh 10.0.0.5"
UNIQ=($(printf '%s\n' "${ALL[@]}" | awk '!seen[$0]++'))
SERVER_NAMES="${UNIQ[*]}"

SAN="DNS:localhost,IP:127.0.0.1"
for n in "${UNIQ[@]}"; do
  if [[ "$n" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then SAN="$SAN,IP:$n"; else SAN="$SAN,DNS:$n"; fi
done

# ---------------------------------------------------------------- https certificate (self-signed)
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/schedulemaster.crt" ] || [ ! -f "$CERT_DIR/schedulemaster.key" ] || [ "$(cat "$CERT_DIR/names.txt" 2>/dev/null)" != "$SAN" ]; then
  say "Creating a self-signed certificate for: $SERVER_NAMES"
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
    -keyout "$CERT_DIR/schedulemaster.key" -out "$CERT_DIR/schedulemaster.crt" \
    -subj "/CN=ScheduleMaster" -addext "subjectAltName=$SAN" >/dev/null 2>&1
  printf '%s' "$SAN" > "$CERT_DIR/names.txt"
fi
chmod 600 "$CERT_DIR/schedulemaster.key"
chmod 644 "$CERT_DIR/schedulemaster.crt"

# ---------------------------------------------------------------- who's allowed to connect (nginx-side)
# Kept in its own file so a plain re-run (upgrade, no flags) remembers the choice from last time,
# instead of quietly resetting back to private-only.
ALLOW_STATE="$CERT_DIR/allow.txt"
if [ -z "$ALLOW" ] && [ -f "$ALLOW_STATE" ]; then ALLOW="$(cat "$ALLOW_STATE")"; fi
printf '%s' "$ALLOW" > "$ALLOW_STATE"
ALLOW_LINES=""
if [ "$ALLOW" != "any" ]; then
  ALLOW_LINES="    allow 127.0.0.1;
    allow ::1;
    allow 10.0.0.0/8;
    allow 172.16.0.0/12;
    allow 192.168.0.0/16;
"
  if [ -n "$ALLOW" ]; then ALLOW_LINES="${ALLOW_LINES}    allow $ALLOW;
"; fi
  ALLOW_LINES="${ALLOW_LINES}    deny all;"
  say "nginx will only accept connections from private/loopback addresses${ALLOW:+ plus $ALLOW}"
else
  say "nginx will accept connections from any address (--allow any)"
fi

# ---------------------------------------------------------------- nginx
say "Configuring nginx"
L80_6=""; L443_6=""
if [ -e /proc/net/if_inet6 ] && [ -n "$(cat /proc/net/if_inet6 2>/dev/null)" ]; then   # only listen on IPv6 if this VM has it
  L80_6="listen [::]:80;"; L443_6="listen [::]:443 ssl;"
fi
cat > "$NGINX_PROXY_INC" <<EOF
# used by $NGINX_CONF
proxy_pass http://127.0.0.1:$APP_PORT;
proxy_set_header Host \$http_host;
proxy_set_header X-Forwarded-For \$remote_addr;
proxy_set_header X-Forwarded-Proto \$scheme;
proxy_redirect off;
proxy_read_timeout 120s;
EOF
cat > "$NGINX_CONF" <<EOF
# Schedule Master: https in front of the app on 127.0.0.1:$APP_PORT. Written by install.sh.
limit_req_zone \$binary_remote_addr zone=schedulemaster_login:1m rate=12r/m;

server {
    listen 80;
    $L80_6
    server_name $SERVER_NAMES;
$ALLOW_LINES
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    $L443_6
    server_name $SERVER_NAMES;
$ALLOW_LINES

    ssl_certificate     $CERT_DIR/schedulemaster.crt;
    ssl_certificate_key $CERT_DIR/schedulemaster.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:schedulemaster_ssl:5m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    server_tokens off;
    client_max_body_size 2m;

    # slow down password guessing (the app also locks an address out after 5 wrong attempts)
    location = /api/login {
        limit_req zone=schedulemaster_login burst=6 nodelay;
        limit_req_status 429;
        include $NGINX_PROXY_INC;
    }

    location / {
        include $NGINX_PROXY_INC;
    }
}
EOF
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
  command -v setsebool >/dev/null && setsebool -P httpd_can_network_connect 1 || warn "SELinux is enforcing; allow nginx to reach the app: setsebool -P httpd_can_network_connect 1"
fi
nginx -t || die "nginx rejected the configuration (see above). The app service is running; nginx was not changed further."
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx

# ---------------------------------------------------------------- firewall
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  say "Opening ports 80 and 443 in ufw"; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  say "Opening http/https in firewalld"; firewall-cmd --permanent --add-service=http --add-service=https >/dev/null; firewall-cmd --reload >/dev/null
else
  warn "No active ufw/firewalld found. If this VM has another firewall, open TCP 80 and 443."
fi

# ---------------------------------------------------------------- check
say "Checking"
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sk --max-time 3 https://127.0.0.1/api/ping 2>/dev/null | grep -q ScheduleMaster; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -eq 1 ]; then
  echo "Schedule Master answers through nginx over https."
else
  warn "No answer yet. Look at:  systemctl status $SERVICE   and   journalctl -u $SERVICE -n 50"
fi

echo
echo "Open Schedule Master at:"
for n in "${UNIQ[@]}"; do echo "    https://$n/"; done
cat <<EOF

First visit: the browser warns that the certificate is not trusted (it is self-signed). Choose
"Advanced" > "Proceed", or copy $CERT_DIR/schedulemaster.crt to your PC and install it as a trusted
certificate to make the warning go away for good.

Sign in with admin / admin, then change it - create/edit login.json in $APP_DIR:
    sudo -u $APP_USER nano $APP_DIR/login.json
    { "username": "admin", "password": "your new password" }
(takes effect immediately, no restart needed - it's read fresh on every sign-in attempt)

Mail relay settings live inside ScheduleMaster.py itself (the SMTP_* constants near the top),
not a separate file:
    sudo -u $APP_USER nano $APP_DIR/ScheduleMaster.py      (edit the SMTP_HOST / SMTP_PORT / etc. lines)
    sudo systemctl restart $SERVICE                        (needed - these are only read at startup)
Note: re-running this installer to upgrade replaces ScheduleMaster.py with the new version, which
would undo hand edits to those SMTP_* lines - keep a copy of your settings if you customize them.

Useful commands
    systemctl status $SERVICE              is it running (it starts by itself at boot)
    journalctl -u $SERVICE -f              live log (logins, emails sent)
    systemctl restart $SERVICE             restart after replacing ScheduleMaster.py / .html
    sudo bash install.sh --uninstall       remove the service and nginx site (data is kept)
EOF
