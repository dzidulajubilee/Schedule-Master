#!/usr/bin/env bash
# Schedule Master installer for a Linux VM (Debian/Ubuntu or RHEL/Rocky/Alma/Fedora).
#
#   sudo bash install.sh                          install / upgrade, https on this VM's own address(es)
#   sudo bash install.sh sched.lan 10.0.0.5        also answer to these names / IPs (kept in the certificate from then on)
#   sudo bash install.sh --new-ca sched2.lan       add a name later: makes a new private CA (PCs then trust the new ca.crt once)
#   sudo bash install.sh --allow any               let non-private (public) addresses reach the app too
#   sudo bash install.sh --allow 41.66.0.0/16      also allow one extra range on top of private addresses
#   sudo bash install.sh --uninstall               remove the service and nginx site (your data stays in /opt/schedulemaster)
#
# What it sets up
#   * the app in /opt/schedulemaster, run by systemd as its own unprivileged user, started at boot, restarted if it stops
#   * nginx on ports 80/443 (80 redirects to https) in front of it; the app itself only listens on 127.0.0.1:8766
#   * a private certificate authority (CA) made on this VM, limited to private addresses + the names given here, and an
#     https certificate it signs for the VM's addresses. Trust ca.crt once on each PC and https stays trusted when the
#     VM's IP changes: schedulemaster-address.timer re-issues the certificate within a minute - no re-install needed
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
REFRESH_BIN=/usr/local/sbin/schedulemaster-refresh-address           # keeps the certificate in step with the VM's addresses
REFRESH_UNIT=/etc/systemd/system/schedulemaster-address.service
REFRESH_TIMER=/etc/systemd/system/schedulemaster-address.timer
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this as root:  sudo bash install.sh"
command -v systemctl >/dev/null || die "systemd was not found; this installer needs it."

ALLOW=""
UNINSTALL=0
NEW_CA=0
NAMES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) UNINSTALL=1 ;;
    --new-ca) NEW_CA=1 ;;
    --allow) shift; [ $# -gt 0 ] || die "--allow needs a value (any, or e.g. 41.66.0.0/16)"; ALLOW="$1" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) die "Unknown option $1" ;;
    *) NAMES+=("$1") ;;
  esac
  shift
done

for n in "${NAMES[@]:-}"; do                            # they end up in the certificate, so only plain names/IPs
  [ -z "$n" ] || [[ "$n" =~ ^(\*\.)?[A-Za-z0-9.-]+$ ]] || die "'$n' is not a host name or IPv4 address."
done

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing the Schedule Master service and nginx site"
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  systemctl disable --now schedulemaster-address.timer 2>/dev/null || true
  rm -f "$UNIT" "$NGINX_CONF" "$NGINX_PROXY_INC" "$REFRESH_UNIT" "$REFRESH_TIMER" "$REFRESH_BIN"
  systemctl daemon-reload
  if command -v nginx >/dev/null && nginx -t >/dev/null 2>&1; then systemctl reload nginx 2>/dev/null || true; fi
  echo "Done. Your data is still in $APP_DIR (delete it yourself if you no longer need it)."
  echo "The certificate and private CA are still in $CERT_DIR (delete it too if PCs no longer need to trust ca.crt)."
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
# Mail relay settings used to live only in ScheduleMaster.py itself, so replacing it lost hand edits.
# They now belong in mail.json (never overwritten). One-time rescue: if there is no mail.json yet, copy any
# SMTP_* values in the installed ScheduleMaster.py that differ from the new version's defaults into it.
# The old file is only parsed (python ast), never run.
MAIL_JSON="$APP_DIR/mail.json"
if [ "$SRC_DIR" != "$APP_DIR" ] && [ -f "$APP_DIR/ScheduleMaster.py" ] && [ ! -e "$MAIL_JSON" ]; then
  rc=0
  "$PYTHON" -I - "$APP_DIR/ScheduleMaster.py" "$SRC_DIR/ScheduleMaster.py" "$MAIL_JSON.new" <<'PYEOF' || rc=$?
import ast, json, sys
KEYS = {"SMTP_HOST": "host", "SMTP_PORT": "port", "SMTP_FROM_NAME": "from_name", "SMTP_FROM_ADDR": "from_addr",
        "SMTP_USERNAME": "username", "SMTP_PASSWORD": "password", "SMTP_USE_TLS": "use_tls"}
def smtp_values(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    vals, odd = {}, []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in KEYS):
            try:
                vals[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                odd.append(node.targets[0].id)
    return vals, odd
try:
    old, odd = smtp_values(sys.argv[1])
except Exception as e:
    print(f"could not read the installed ScheduleMaster.py ({e})"); sys.exit(2)
new, _ = smtp_values(sys.argv[2])
keep = {KEYS[k]: v for k, v in old.items() if k in new and v != new[k]}
for k in odd:
    print(f"{k} is not a plain value in the installed ScheduleMaster.py - copy it into mail.json by hand")
if keep:
    with open(sys.argv[3], "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2)
        f.write("\n")
    shown = ", ".join(f"{k}=" + ("(set, not shown)" if k == "password" else json.dumps(v)) for k, v in keep.items())
    print(f"kept your mail relay settings in mail.json: {shown}")
sys.exit(2 if odd else 0)
PYEOF
  if [ -f "$MAIL_JSON.new" ]; then
    mv "$MAIL_JSON.new" "$MAIL_JSON"; chown "$APP_USER":"$APP_USER" "$MAIL_JSON"; chmod 600 "$MAIL_JSON"
  fi
  if [ "$rc" -ne 0 ]; then                              # keep the old file so nothing is lost
    cp -p "$APP_DIR/ScheduleMaster.py" "$APP_DIR/ScheduleMaster.py.before-upgrade"
    warn "Not every mail setting could be carried over automatically (see above). The previous version is kept as"
    warn "$APP_DIR/ScheduleMaster.py.before-upgrade - copy what you need into $MAIL_JSON."
  fi
fi
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  for f in ScheduleMaster.py ScheduleMaster.html README.md; do
    [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$APP_DIR/$f"
  done
  for f in staff.json records.json emails.json login.json mail.json; do   # your data/settings: only copied if not there yet
    if [ -f "$SRC_DIR/$f" ] && [ ! -e "$APP_DIR/$f" ]; then cp "$SRC_DIR/$f" "$APP_DIR/$f"; fi
  done
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$APP_DIR"/*.py "$APP_DIR"/*.html 2>/dev/null || true
[ -f "$APP_DIR/login.json" ] && chmod 600 "$APP_DIR/login.json"
[ -f "$APP_DIR/mail.json" ] && chmod 600 "$APP_DIR/mail.json"              # may hold the relay password
[ -f "$APP_DIR/ScheduleMaster.py.before-upgrade" ] && chmod 600 "$APP_DIR/ScheduleMaster.py.before-upgrade"

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

# ---------------------------------------------------------------- names / addresses
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

# ---------------------------------------------------------------- https: private CA (made once) + certificate
# PCs trust ca.crt once. The CA carries "name constraints": it can only vouch for private addresses
# (10/8, 172.16/12, 192.168/16, 127/8), localhost and the names listed in ca-names.txt, so it cannot be used
# to impersonate any other website to those PCs, even by someone holding ca.key. The server certificate
# itself is issued by $REFRESH_BIN (written below), which schedulemaster-address.timer re-runs every minute,
# so a new IP gets a new certificate - signed by the same CA - without re-running this script.
is_ip()      { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; }
private_ip() { local IFS=. a b _; read -r a b _ <<<"$1"
  [ "$a" = 10 ] || [ "$a" = 127 ] || { [ "$a" = 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; } || { [ "$a" = 192 ] && [ "$b" = 168 ]; }; }
in_list()    { local x="$1"; shift; local y; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }
CA_KEY="$CERT_DIR/ca.key"; CA_CRT="$CERT_DIR/ca.crt"; CA_NAMES="$CERT_DIR/ca-names.txt"
mkdir -p "$CERT_DIR"
chmod 755 "$CERT_DIR"
HAD_CA=0
if [ -f "$CA_KEY" ] && [ -f "$CA_CRT" ]; then HAD_CA=1; fi
MAKE_CA=0
if [ "$HAD_CA" -eq 0 ] || [ "$NEW_CA" -eq 1 ]; then MAKE_CA=1; fi
FROM_SELF_SIGNED=0                                      # upgrading an install that used the old self-signed certificate
if [ "$HAD_CA" -eq 0 ] && [ -f "$CERT_DIR/schedulemaster.crt" ]; then FROM_SELF_SIGNED=1; fi

# Names/IPs the CA covers besides private addresses + localhost. The existing list is always kept.
WANT=()
if [ -f "$CA_NAMES" ]; then
  while IFS= read -r n || [ -n "$n" ]; do
    n="${n%%#*}"; n="${n//[[:space:]]/}"
    if [ -n "$n" ]; then WANT+=("$n"); fi
  done < "$CA_NAMES"
fi
if [ "$MAKE_CA" -eq 1 ]; then
  # what the previous certificate covered (an older install's names and public IPs), this VM's hostname and
  # its current public IPs - the same things the old self-signed certificate was made for
  if [ -f "$CERT_DIR/names.txt" ]; then
    for e in $(tr ',' ' ' < "$CERT_DIR/names.txt"); do
      case "$e" in
        DNS:localhost) ;;
        DNS:*) WANT+=("${e#DNS:}") ;;
        IP:*) if is_ip "${e#IP:}" && ! private_ip "${e#IP:}"; then WANT+=("${e#IP:}"); fi ;;
      esac
    done
  fi
  for ip in "${IPS[@]:-}"; do
    if [ -n "$ip" ] && ! private_ip "$ip"; then WANT+=("$ip"); fi
  done
  if [ -n "$HOSTNAME_SHORT" ]; then WANT+=("$HOSTNAME_SHORT"); fi
fi
UNCOVERED=()                                            # names asked for that the existing CA cannot vouch for
for n in "${NAMES[@]:-}"; do
  [ -n "$n" ] || continue
  if [ "$MAKE_CA" -eq 1 ] || { is_ip "$n" && private_ip "$n"; }; then WANT+=("$n")   # private IPs: covered by any CA
  elif ! in_list "$n" "${WANT[@]:-}"; then UNCOVERED+=("$n"); fi
done
WANT_U=()
while IFS= read -r n; do WANT_U+=("$n"); done < <(printf '%s\n' "${WANT[@]:-}" | awk 'NF && !seen[$0]++')

if [ "$MAKE_CA" -eq 1 ]; then
  say "Creating the private CA for this install ($CA_CRT)"
  NC="permitted;IP:10.0.0.0/255.0.0.0,permitted;IP:172.16.0.0/255.240.0.0,permitted;IP:192.168.0.0/255.255.0.0"
  NC="$NC,permitted;IP:127.0.0.0/255.0.0.0,permitted;DNS:localhost"
  for n in "${WANT_U[@]:-}"; do
    [ -n "$n" ] || continue
    if is_ip "$n"; then
      if ! private_ip "$n"; then NC="$NC,permitted;IP:$n/255.255.255.255"; fi
    else
      NC="$NC,permitted;DNS:${n#\*.}"                    # a DNS constraint also covers that name's subdomains
    fi
  done
  CA_TMP="$(mktemp -d)"
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 -keyout "$CA_TMP/ca.key" -out "$CA_TMP/ca.crt" \
    -subj "/CN=Schedule Master private CA ${HOSTNAME_SHORT:-} $(date +%Y-%m-%d)" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" -addext "nameConstraints=critical,$NC" >"$CA_TMP/log" 2>&1 \
    || { cat "$CA_TMP/log" >&2; rm -rf "$CA_TMP"; die "Could not create the private CA (see above). Nothing was changed in $CERT_DIR."; }
  if [ "$HAD_CA" -eq 1 ]; then                          # --new-ca: keep the old one, renamed, in case it is wanted back
    STAMP="$(date +%Y%m%d-%H%M%S)"
    mv "$CA_KEY" "$CA_KEY.replaced-$STAMP"; mv "$CA_CRT" "$CA_CRT.replaced-$STAMP"
    warn "Replaced the private CA. The old one is kept as ca.key/ca.crt.replaced-$STAMP."
  fi
  install -m 600 "$CA_TMP/ca.key" "$CA_KEY"
  install -m 644 "$CA_TMP/ca.crt" "$CA_CRT"
  rm -rf "$CA_TMP"
fi
{
  echo "# Names/IPs the Schedule Master private CA may vouch for, besides private addresses and localhost."
  echo "# Written by install.sh; every certificate includes them. To add a name: sudo bash install.sh --new-ca NAME"
  printf '%s\n' "${WANT_U[@]:-}" | awk NF
} > "$CA_NAMES"
chmod 644 "$CA_NAMES"
for n in "${UNCOVERED[@]:-}"; do
  if [ -n "$n" ]; then warn "'$n' was not added: this install's private CA can't vouch for it. To add it, run:  sudo bash install.sh --new-ca $n   (each PC then trusts the new ca.crt once)"; fi
done

say "Installing the address refresher ($REFRESH_BIN)"
cat > "$REFRESH_BIN" <<'REFRESH_EOF'
#!/usr/bin/env bash
# Schedule Master - address refresher. Written by install.sh; run by schedulemaster-address.timer at boot and
# every minute (journalctl -u schedulemaster-address shows what it did).
#
# Keeps the https certificate in step with this VM's addresses. It issues a new certificate, signed by this
# install's private CA (/etc/ssl/schedulemaster/ca.crt), and reloads nginx when:
#   * the VM's addresses changed (a move, a new DHCP lease...) or the names in ca-names.txt changed
#   * the certificate is not signed by the private CA (the first run after upgrading from a self-signed one)
#   * the certificate expires within 30 days, or does not match its key
# Otherwise it does nothing. PCs that trust ca.crt also trust every certificate it issues.
# It only writes schedulemaster.key/.crt, names.txt and addresses.txt in /etc/ssl/schedulemaster - never the
# app, its data or the nginx configuration.
# Public (non-private) addresses are covered only if they are in ca-names.txt; others are logged as not covered.
#   schedulemaster-refresh-address              normal run
#   schedulemaster-refresh-address --no-reload  update the certificate but leave nginx alone (install.sh uses this)
#   schedulemaster-refresh-address --force      issue a new certificate even if nothing changed
set -euo pipefail
umask 077
CERT_DIR=/etc/ssl/schedulemaster
CA_KEY=$CERT_DIR/ca.key; CA_CRT=$CERT_DIR/ca.crt; CA_NAMES=$CERT_DIR/ca-names.txt
KEY=$CERT_DIR/schedulemaster.key; CRT=$CERT_DIR/schedulemaster.crt
NAMES_FILE=$CERT_DIR/names.txt          # what the current certificate was issued for
ADDR_FILE=$CERT_DIR/addresses.txt       # the VM's addresses last time we looked (to report changes once)
RELOAD=1; FORCE=0
for a in "$@"; do
  case "$a" in --no-reload) RELOAD=0 ;; --force) FORCE=1 ;; *) echo "unknown option: $a" >&2; exit 2 ;; esac
done
log() { echo "$*"; }

if [ ! -f "$CA_KEY" ] || [ ! -f "$CA_CRT" ]; then log "private CA not found in $CERT_DIR - run install.sh again"; exit 1; fi
exec 9>"$CERT_DIR/.refresh.lock"
flock -w 120 9 || { log "another run is still busy - skipped"; exit 0; }

is_ip()      { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; }
private_ip() { local IFS=. a b _; read -r a b _ <<<"$1"
  [ "$a" = 10 ] || [ "$a" = 127 ] || { [ "$a" = 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; } || { [ "$a" = 192 ] && [ "$b" = 168 ]; }; }

# --- what the certificate should cover now
addrs="$(hostname -I 2>/dev/null | xargs || true)"
entries=("DNS:localhost" "IP:127.0.0.1"); public=(); have_ip=0
for ip in $addrs; do
  is_ip "$ip" || continue                          # IPv4 only, like install.sh
  have_ip=1
  if private_ip "$ip"; then entries+=("IP:$ip"); else public+=("$ip"); fi
done
if [ -f "$CA_NAMES" ]; then
  while IFS= read -r n || [ -n "$n" ]; do
    n="${n%%#*}"; n="${n//[[:space:]]/}"
    [ -n "$n" ] || continue
    if is_ip "$n"; then entries+=("IP:$n"); else entries+=("DNS:$n"); fi
  done < "$CA_NAMES"
fi
SAN="$(printf '%s\n' "${entries[@]}" | awk '!seen[$0]++' | paste -sd, -)"

# no IPv4 address at all (network not up yet at boot): keep the current certificate rather than drop the IPs
if [ "$have_ip" -eq 0 ] && [ -s "$CRT" ] && [ "$FORCE" -eq 0 ]; then exit 0; fi

if [ "$(cat "$ADDR_FILE" 2>/dev/null || true)" != "$addrs" ]; then          # report each address change once
  printf '%s' "$addrs" > "$ADDR_FILE"; chmod 644 "$ADDR_FILE"
  uncovered=()
  for ip in "${public[@]:-}"; do
    if [ -n "$ip" ] && [[ ",$SAN," != *",IP:$ip,"* ]]; then uncovered+=("$ip"); fi
  done
  if [ ${#uncovered[@]} -gt 0 ]; then
    log "not covered by the certificate (not a private address, not in ca-names.txt): ${uncovered[*]}"
  fi
fi

# --- anything to do?
reasons=()
if [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; then
  reasons+=("no certificate yet")
else
  [ "$(cat "$NAMES_FILE" 2>/dev/null || true)" = "$SAN" ] || reasons+=("addresses/names changed")
  openssl verify -CAfile "$CA_CRT" "$CRT" >/dev/null 2>&1 || reasons+=("not signed by the private CA")
  openssl x509 -checkend $((30*86400)) -noout -in "$CRT" >/dev/null 2>&1 || reasons+=("expires within 30 days")
  [ "$(openssl x509 -noout -pubkey -in "$CRT" 2>/dev/null)" = "$(openssl pkey -pubout -in "$KEY" 2>/dev/null)" ] \
    || reasons+=("certificate does not match its key")
fi
if [ "$FORCE" -eq 1 ]; then reasons+=("--force"); fi
if [ ${#reasons[@]} -eq 0 ]; then exit 0; fi

# --- issue a new certificate (825 days, the most Apple devices accept), checked before it replaces the old one
old="$(cat "$NAMES_FILE" 2>/dev/null || echo none)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
if ! openssl req -new -newkey rsa:2048 -nodes -keyout "$tmp/key" -out "$tmp/csr" -subj "/CN=ScheduleMaster" >"$tmp/err" 2>&1; then
  log "could not create a key - kept the old certificate: $(tr '\n' ' ' < "$tmp/err" | head -c 300)"; exit 1
fi
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$SAN" > "$tmp/ext"
if ! openssl x509 -req -sha256 -days 825 -in "$tmp/csr" -CA "$CA_CRT" -CAkey "$CA_KEY" \
     -set_serial "0x$(openssl rand -hex 16)" -extfile "$tmp/ext" -out "$tmp/crt" >"$tmp/err" 2>&1; then
  log "could not sign the certificate - kept the old one: $(tr '\n' ' ' < "$tmp/err" | head -c 300)"; exit 1
fi
if ! openssl verify -CAfile "$CA_CRT" "$tmp/crt" >"$tmp/err" 2>&1; then
  log "the new certificate failed verification - kept the old one: $(tr '\n' ' ' < "$tmp/err" | head -c 300)"; exit 1
fi
install -m 600 "$tmp/key" "$KEY.new"; install -m 644 "$tmp/crt" "$CRT.new"
mv -f "$KEY.new" "$KEY"; mv -f "$CRT.new" "$CRT"
printf '%s' "$SAN" > "$NAMES_FILE"; chmod 644 "$NAMES_FILE"
why="$(printf '%s, ' "${reasons[@]}")"
log "https certificate issued for ${SAN//,/ } (${why%, }; previous: ${old//,/ })"

if [ "$RELOAD" -eq 1 ]; then
  if ! command -v nginx >/dev/null 2>&1; then
    log "nginx not found - nothing to reload"
  elif ! nginx -t >/dev/null 2>&1; then
    log "nginx configuration test failed - nginx not reloaded (it keeps serving the previous certificate)"; exit 1
  elif systemctl is-active --quiet nginx; then
    if systemctl reload nginx; then log "nginx reloaded"; else log "nginx reload failed"; exit 1; fi
  fi
fi
exit 0
REFRESH_EOF
chown root:root "$REFRESH_BIN" 2>/dev/null || true
chmod 755 "$REFRESH_BIN"
"$REFRESH_BIN" --no-reload || die "Could not issue the https certificate (see above)."

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
    server_name ~.;            # any name/address this VM is reached by, so a new IP needs no re-install
$ALLOW_LINES
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    $L443_6
    server_name ~.;            # any name/address this VM is reached by, so a new IP needs no re-install
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

# ---------------------------------------------------------------- address refresher (systemd timer)
say "Enabling schedulemaster-address.timer (new IP -> new certificate within a minute, no re-install)"
cat > "$REFRESH_UNIT" <<EOF
[Unit]
Description=Schedule Master - keep the https certificate in step with this VM's addresses
After=network.target

[Service]
Type=oneshot
ExecStart=$REFRESH_BIN
SyslogIdentifier=schedulemaster-address
# only what the refresher reports (address changes, new certificates, errors) reaches the journal -
# not systemd's routine start/finish lines every minute
SyslogLevel=notice
LogLevelMax=notice
PrivateTmp=true
ProtectHome=true
EOF
cat > "$REFRESH_TIMER" <<EOF
[Unit]
Description=Schedule Master - check this VM's addresses at boot and every minute

[Timer]
OnBootSec=15s
OnCalendar=minutely
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable schedulemaster-address.timer >/dev/null 2>&1 || warn "Could not enable schedulemaster-address.timer"
systemctl restart schedulemaster-address.timer || warn "Could not start schedulemaster-address.timer - after an IP change run:  sudo $REFRESH_BIN"

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
while IFS= read -r n; do echo "    https://$n/"; done < <(printf '%s\n' "${ALL[@]}" "${WANT_U[@]:-}" \
  | awk -v skip=" ${UNCOVERED[*]:-} " 'NF && !/^\*/ && index(skip, " " $0 " ") == 0 && !seen[$0]++')
COVERS="$(printf '%s\n' "${WANT_U[@]:-}" | awk NF | paste -sd' ' -)"
cat <<EOF

https trust: until a PC trusts this install's private CA, its browser warns about the certificate
("Advanced" > "Proceed" gets past it). To make it trusted for good - once per PC, and it stays trusted
when this VM's IP changes - copy $CERT_DIR/ca.crt to the PC and import it as a trusted root:
    Windows: double-click ca.crt > Install Certificate > Local Machine >
             "Place all certificates in the following store" > Trusted Root Certification Authorities
    macOS:   open it in Keychain Access (System keychain) > Trust > Always Trust
    Firefox: Settings > Privacy & Security > View Certificates > Authorities > Import (trust for websites)
The CA can only vouch for private addresses (10.x, 172.16-31.x, 192.168.x), localhost${COVERS:+ and: $COVERS}.
A new IP is picked up automatically within a minute (see:  journalctl -u schedulemaster-address).
EOF
if [ "$FROM_SELF_SIGNED" -eq 1 ] || { [ "$NEW_CA" -eq 1 ] && [ "$HAD_CA" -eq 1 ]; }; then
  echo
  warn "https is now signed by a new private CA. PCs that trusted the previous certificate (schedulemaster.crt,"
  warn "or an older ca.crt) will warn until they trust the new $CERT_DIR/ca.crt - remove the old one from"
  warn "them and import ca.crt instead (once; it keeps working through IP changes)."
fi
cat <<EOF

Sign in with admin / admin, then change it - create/edit login.json in $APP_DIR:
    sudo -u $APP_USER nano $APP_DIR/login.json
    { "username": "admin", "password": "your new password" }
(takes effect immediately, no restart needed - it's read fresh on every sign-in attempt)

Mail relay settings go in $APP_DIR/mail.json - kept across upgrades, read on every send (no restart):
    sudo -u $APP_USER nano $APP_DIR/mail.json
    { "host": "10.0.0.25", "port": 25, "from_addr": "rota@yourdomain.com" }
Also available: "from_name", "username", "password", "use_tls": true/false - leave out what you don't
need. Without mail.json the defaults at the top of ScheduleMaster.py apply (a local Postfix on port 25).

Useful commands
    systemctl status $SERVICE              is it running (it starts by itself at boot)
    journalctl -u $SERVICE -f              live log (logins, emails sent)
    systemctl restart $SERVICE             restart after replacing ScheduleMaster.py / .html
    sudo bash install.sh --uninstall       remove the service and nginx site (data is kept)
    journalctl -u schedulemaster-address   address changes / certificates re-issued (checked every minute)
EOF
