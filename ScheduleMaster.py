#!/usr/bin/env python3
"""
Schedule Master local server.
Serves ScheduleMaster.html and its companion files from this folder, and exposes:
  GET  /api/staff    -> reads staff.json ({"staff": [...]})
  PUT  /api/staff    -> writes staff.json (body: {"staff": [...]})
  GET  /api/records  -> reads records.json ({"records": [...], "version": "<fingerprint>"})
  PUT  /api/records  -> writes records.json (body: {"records": [...], "baseVersion": "<fingerprint>"})
                        -- full replace, same pattern as /api/staff; the client sends the whole array
                        back each time (e.g. after adding or deleting one record). If baseVersion is
                        sent and records.json changed since, nothing is written: 409 with the current
                        {"records", "version"}. Without baseVersion (older pages) it writes as before.
  GET  /api/emails   -> reads emails.json ({"emails": {"Full Name": "addr@example.com", ...},
                        "paused": ["Full Name", ...]} - "paused" = kept out of send-to-everyone)
  PUT  /api/emails   -> writes emails.json (body: {"emails": {...}, "paused": [...]}; "paused" is
                        optional - leave it out to keep the current list) -- full replace, same
                        pattern as /api/staff.
  POST /api/send-shifts -> emails each person in emails.json their own dates and shift times
                        for one month. Body: {"year":2026,"month":8,"monthLabel":"September 2026",
                        "shiftTypes":[{"id":...,"name":...,"start":...,"end":...}, ...],
                        "shifts":{"YYYY-MM-DD":{"Full Name":"shiftTypeId", ...}, ...},
                        "leave":{"Full Name":["YYYY-MM-DD", ...], ...}, "palette":"classic"}. Sends one individual
                        email per saved address (nobody sees anyone else's schedule) through the
                        SMTP relay configured in mail.json / below - plain text + HTML body, plus two attachments named
                        Shifts_YYYY-MM_<Full Name>.pdf (one-page month calendar, colored per "palette",
                        see SCREEN_PALETTES) and .ics (importable calendar) - and returns {"sent":[names],
                        "skipped":[{"name":...,"reason":...}], "failed":[{"name":...,"error":...}]}.

  GET  /api/session  -> {"authenticated": true/false} for the current browser (checks the session
                        cookie). Never requires auth itself — it's how the page decides whether to
                        show the login screen.
  GET  /api/ping     -> {"ok": true, "app": "ScheduleMaster"}, unauthenticated. Used by the
                        installer's health check.
  POST /api/login    -> body {"username":..., "password":...}, checked against login.json (default
                        admin/admin if that file doesn't exist). On success sets a session cookie
                        good for 30 days and returns {"ok": true}; on failure, 401. An address gets
                        locked out for 15 minutes after 5 wrong attempts in a row (429 while locked).
  POST /api/logout   -> clears the current session cookie.
  POST /api/admin    -> {"password"}: checks the admin password for the Algorithm tab (same lockout as sign-in).
  POST /api/mail     -> the Mail server page (signed in, plus the admin password). Body {"action": ...}:
                        "unlock" {"password"} checks the password (same lockout as sign-in) and opens the
                        page for this session for 10 minutes; "save" {"mail": {...}} writes mail.json;
                        "test" {"to"} sends one test email; "lock" closes the page again.

  Every other /api/* route above requires a valid session cookie (401 if missing/expired) — the
  static page itself (ScheduleMaster.html, this file's assets) is not gated, so the browser can
  always load the app shell and show the login screen; only the data behind it is protected.

No third-party packages required (standard library only).
Run this file (python3 ScheduleMaster.py), then a browser tab opens
automatically at ScheduleMaster.html. Default login is admin / admin — see login.json.
"""
import calendar
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import smtplib
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape

FOLDER = os.path.dirname(os.path.abspath(__file__))
STAFF_FILE = os.path.join(FOLDER, "staff.json")
RECORDS_FILE = os.path.join(FOLDER, "records.json")
EMAILS_FILE = os.path.join(FOLDER, "emails.json")
LOGIN_FILE = os.path.join(FOLDER, "login.json")
MAIL_FILE = os.path.join(FOLDER, "mail.json")
HTML_FILE = "ScheduleMaster.html"
PORT = 8743

# ---------- login / session settings ----------
# SM_HOST / SM_PORT / SM_TRUST_PROXY are read from the environment so a systemd unit (see install.sh)
# can pin them; left unset, this behaves exactly like the old zero-config double-click version below.
HOST = os.environ.get("SM_HOST", "127.0.0.1")
TRUST_PROXY = os.environ.get("SM_TRUST_PROXY") == "1"  # trust X-Forwarded-For/-Proto from nginx
COOKIE_NAME = "sm_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60

# ---------- mail relay settings ----------
# These are the DEFAULTS. Put your own settings in mail.json next to this file instead of editing
# them here: install.sh never overwrites mail.json, so they survive upgrades, and it is read fresh
# on every send (no restart needed). Any key you leave out keeps the default below. Example:
#   {"host": "10.0.0.25", "port": 587, "from_name": "Schedule Master", "from_addr": "rota@example.com",
#    "username": "rota", "password": "secret", "use_tls": true}
# Keys starting with "_" are ignored (use them for notes). See read_mail_settings().
# Defaults assume a Postfix instance on this same machine, listening on 25, accepting mail from
# localhost with no login (the common setup for an internal relay). Set "host" to your relay's
# address if it runs elsewhere on the network instead.
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25
SMTP_FROM_NAME = "Schedule Master"
SMTP_FROM_ADDR = "scheduler@localhost"
# If your relay requires STARTTLS and/or a login, set "use_tls" and "username" + "password" -
# open_smtp() below uses them.
SMTP_USERNAME = None
SMTP_PASSWORD = None
SMTP_USE_TLS = False
SMTP_SECURITY = None   # "none", "starttls" or "ssl"; None = follow SMTP_USE_TLS (True -> "starttls")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# ---------- shift-schedule email content ----------
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
DOWS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
FONT = "Arial,Helvetica,sans-serif"
# ---------- email colors (message body AND attached PDF), per calendar color scheme ----------
# Both use each scheme's exact ON-SCREEN colors - the same chips the person sees in the app's calendar
# (light mode). Unlike the app's own Print button, the emailed PDF does NOT swap the pastel schemes for
# the fixed print-safe set: the chosen scheme is reproduced exactly. Each entry: [chip-1, chip-2, chip-3, leave], each a
# (background, text) pair, copied from the matching :root / :root[data-palette=...] CSS in
# ScheduleMaster.html - if a scheme's colors change there, change them here too. Shift types cycle through
# chip-1/2/3 by position (1,4,7.. / 2,5,8.. / 3,6,9..) exactly like the app; Leave has its own color.
SCREEN_PALETTES = {
    "classic": [("#e6edfd", "#2e6fec"), ("#fbeadb", "#c9781a"), ("#ece8fb", "#6a52d6"), ("#fbe6e3", "#c94a3f")],
    "ocean": [("#dcf3f6", "#0f7a8c"), ("#dde8fb", "#1f4e8c"), ("#dbe9e9", "#3c6e71"), ("#f8ded9", "#b23a2e")],
    "sunset": [("#fbe0e4", "#d1495b"), ("#fbeedc", "#e08e2b"), ("#f0e3fb", "#8a4fc9"), ("#f5dbd8", "#a3271f")],
    "forest": [("#dff0e2", "#2f6b3a"), ("#f0e8d4", "#8a6a2f"), ("#dcefef", "#2f6b6b"), ("#f8ded9", "#b23b2e")],
    "berry": [("#f9dde9", "#a3245e"), ("#e9e2fb", "#5b3fa0"), ("#dde8fb", "#1f4e8c"), ("#f2c9c4", "#8f1710")],
    "slate": [("#dbe8f4", "#2f5d8c"), ("#f0e6d8", "#8a5a2f"), ("#dcefe9", "#3f7d72"), ("#f6ddd8", "#a3392e")],
    "deep": [("#123a66", "#eaf2ff"), ("#7a4416", "#fff2df"), ("#4a2170", "#f5ecff"), ("#e8514f", "#ffffff")],
    "deep-bold": [("#3b76e8", "#ffffff"), ("#e8960b", "#000000"), ("#16a34a", "#ffffff"), ("#e8514f", "#ffffff")],
    "deep-emerald": [("#0f5c42", "#e9fff5"), ("#7a1a56", "#ffe9f7"), ("#2c2f7a", "#eef0ff"), ("#e8514f", "#ffffff")],
    "deep-crimson": [("#8c1e2b", "#ffeef0"), ("#7a4a0f", "#fff5e6"), ("#1f3a5f", "#eaf2ff"), ("#e8514f", "#ffffff")],
}



def read_staff():
    if not os.path.exists(STAFF_FILE):
        return {"staff": []}
    with open(STAFF_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"staff": []}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it, file is left untouched
    if not isinstance(data, dict) or not isinstance(data.get("staff"), list):
        raise ValueError('staff.json must look like {"staff": ["Name One", "Name Two"]}')
    return data


def write_staff(data):
    if not isinstance(data, dict) or not isinstance(data.get("staff"), list):
        raise ValueError('Expected {"staff": [...]}')
    names = []
    seen = set()
    for n in data["staff"]:
        n = str(n).strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    # atomic write: temp file then replace, so a crash mid-write can't corrupt staff.json
    tmp = STAFF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"staff": sorted(names)}, f, indent=2)
    os.replace(tmp, STAFF_FILE)
    return {"staff": sorted(names)}


# records.json is saved as a whole list, so a page holding an out-of-date copy (a tab opened earlier, or
# another person) could otherwise erase records saved since it loaded. Every read returns a "version"
# (a fingerprint of the file's contents); a PUT that sends the version it started from is refused with
# 409 if the file has changed since, and gets the current list back instead. A PUT without a version
# (an older cached copy of the page) is still accepted as before. RECORDS_LOCK makes check+write atomic.
RECORDS_LOCK = threading.Lock()


class RecordsConflict(Exception):
    def __init__(self, current):
        super().__init__("records changed since this page loaded them")
        self.current = current


def _records_raw():
    if not os.path.exists(RECORDS_FILE):
        return b""
    with open(RECORDS_FILE, "rb") as f:
        return f.read()


def _records_version(raw):
    import hashlib
    return hashlib.sha256(raw.strip()).hexdigest()[:16]


def read_records():
    raw_bytes = _records_raw()
    version = _records_version(raw_bytes)
    raw = raw_bytes.decode("utf-8").strip()
    if not raw:
        return {"records": [], "version": version}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it, file is left untouched
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError('records.json must look like {"records": [...]}')
    return {"records": data["records"], "version": version}


def write_records(data):
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError('Expected {"records": [...]}')
    with RECORDS_LOCK:
        base = data.get("baseVersion")
        if base is not None:
            current = read_records()  # raises ValueError if records.json is unreadable -> nothing written
            if base != current["version"]:
                raise RecordsConflict(current)
        # full replace, same as staff: the client already merged its add/delete into the array
        # it sends. Atomic write: temp file then replace, so a crash mid-write can't corrupt records.json
        tmp = RECORDS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": data["records"]}, f, indent=2)
        os.replace(tmp, RECORDS_FILE)
        return {"records": data["records"], "version": _records_version(_records_raw())}


def read_emails():
    if not os.path.exists(EMAILS_FILE):
        return {"emails": {}, "paused": []}
    with open(EMAILS_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"emails": {}, "paused": []}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it, file is left untouched
    if not isinstance(data, dict) or not isinstance(data.get("emails"), dict):
        raise ValueError('emails.json must look like {"emails": {"Full Name": "addr@example.com"}}')
    # Optional "paused": names whose address is kept but who are left out of "send to everyone".
    # Absent (every file written before this feature) means nobody is paused. A malformed value is an
    # error rather than being ignored - silently ignoring it would email people who were turned off.
    paused = data.get("paused", [])
    if not isinstance(paused, list) or not all(isinstance(n, str) for n in paused):
        raise ValueError('"paused" in emails.json must be a list of names')
    data["paused"] = paused
    return data


def write_emails(data):
    if not isinstance(data, dict) or not isinstance(data.get("emails"), dict):
        raise ValueError('Expected {"emails": {"Full Name": "addr@example.com"}}')
    cleaned = {}
    for name, addr in data["emails"].items():
        name = str(name).strip()
        addr = str(addr).strip()
        if name and addr:
            cleaned[name] = addr
    if "paused" in data:
        paused = data["paused"]
        if not isinstance(paused, list):
            raise ValueError('"paused" must be a list of names')
    else:
        # a client that doesn't know about pausing (e.g. an older cached page) keeps the current list
        try:
            paused = read_emails().get("paused", [])
        except (OSError, ValueError):
            paused = []
    # only people who still have an address can be paused; deleting an address clears its pause too
    paused = sorted({str(n).strip() for n in paused if str(n).strip() in cleaned})
    out = {"emails": cleaned}
    if paused:  # key only written when used, so files stay byte-for-byte the same format otherwise
        out["paused"] = paused
    # atomic write: temp file then replace, so a crash mid-write can't corrupt emails.json
    tmp = EMAILS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, EMAILS_FILE)
    return {"emails": cleaned, "paused": paused}


def read_mail_settings():
    """Mail relay settings: the SMTP_* defaults above, overridden by whatever mail.json sets.
    Raises ValueError (with a readable reason) if mail.json exists but can't be used - the caller then
    sends nothing rather than quietly falling back to a different relay."""
    cfg = {"host": SMTP_HOST, "port": SMTP_PORT, "from_name": SMTP_FROM_NAME, "from_addr": SMTP_FROM_ADDR,
           "username": SMTP_USERNAME, "password": SMTP_PASSWORD, "use_tls": SMTP_USE_TLS, "security": SMTP_SECURITY}
    if not os.path.exists(MAIL_FILE):
        return _finish_security(cfg)
    with open(MAIL_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return _finish_security(cfg)
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it
    if not isinstance(data, dict):
        raise ValueError('mail.json must look like {"host": "10.0.0.25", "port": 25, "from_addr": "rota@example.com"}')
    unknown = sorted(k for k in data if k not in cfg and not str(k).startswith("_"))
    if unknown:  # most likely a typo (e.g. "pasword") - better to say so than to ignore it
        raise ValueError(f"unknown setting(s) {', '.join(unknown)} - known: {', '.join(cfg)}")
    for key, val in data.items():
        if key.startswith("_"):
            continue
        if key == "port":
            if isinstance(val, str) and val.strip().isdigit():
                val = int(val.strip())
            if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 65535:
                raise ValueError('"port" must be a number from 1 to 65535')
        elif key == "use_tls":
            if not isinstance(val, bool):
                raise ValueError('"use_tls" must be true or false (no quotes)')
        elif key == "security":
            if val is not None and val not in ("none", "starttls", "ssl"):
                raise ValueError('"security" must be "none", "starttls" or "ssl"')
        elif key in ("username", "password"):
            if val is not None and not isinstance(val, str):
                raise ValueError(f'"{key}" must be text in quotes, or null')
            val = val or None  # "" means none
        else:  # host, from_name, from_addr
            if not isinstance(val, str) or (key != "from_name" and not val.strip()):
                raise ValueError(f'"{key}" must be text in quotes')
            if any(c in val for c in "\r\n"):
                raise ValueError(f'"{key}" must be on one line')
            val = val.strip()
        cfg[key] = val
    return _finish_security(cfg)


def _finish_security(cfg):
    """"security" wins when set; otherwise the older "use_tls" decides (true = STARTTLS, false = none)."""
    if not cfg.get("security"):
        cfg["security"] = "starttls" if cfg.get("use_tls") else "none"
    cfg["use_tls"] = cfg["security"] == "starttls"
    return cfg


MAIL_FORM_KEYS = ("host", "port", "security", "username", "from_addr", "from_name")
FROM_RE = re.compile(r"^[^\s@<>\",;:()\[\]\\]+@[^\s@<>\",;:()\[\]\\]+$")


def mail_form(cfg):
    """What the Mail server page shows: never the password, only whether one is saved."""
    out = {k: cfg.get(k) for k in MAIL_FORM_KEYS}
    out["username"] = out["username"] or ""
    out["has_password"] = bool(cfg.get("password"))
    return out


def clean_mail_form(data):
    """Check what the Mail server page sent. Returns only the keys it may change in mail.json."""
    if not isinstance(data, dict):
        raise ValueError("Bad mail settings.")

    def text(key, label, limit, required=False):
        v = data.get(key, "")
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise ValueError(f"Bad value for {label}.")
        v = v.strip()
        if "\r" in v or "\n" in v or len(v) > limit:
            raise ValueError(f"{label} is too long or has line breaks.")
        if required and not v:
            raise ValueError(f"{label} is empty.")
        return v

    out = {"host": text("host", "SMTP server", 253, True), "from_addr": text("from_addr", "From address", 254, True),
           "from_name": text("from_name", "From name", 100)}
    user = text("username", "Username", 254)
    out["username"] = user or None
    try:
        out["port"] = int(data.get("port"))
    except (TypeError, ValueError):
        raise ValueError("Port must be a number.")
    if not 1 <= out["port"] <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    sec = str(data.get("security", "")).lower()
    if sec not in ("none", "starttls", "ssl"):
        raise ValueError("Security must be None, STARTTLS or SSL/TLS.")
    out["security"] = sec
    out["use_tls"] = sec == "starttls"     # kept in step so the file still reads the same in older versions
    if not FROM_RE.match(out["from_addr"]):
        raise ValueError(f'"{out["from_addr"]}" is not a valid From address.')
    pw = data.get("password", "")
    if not isinstance(pw, str) or len(pw) > 1000 or "\r" in pw or "\n" in pw:
        raise ValueError("Bad password.")
    if pw:                                  # empty = keep the saved password
        out["password"] = pw
    elif data.get("clear_password") is True:
        out["password"] = None
    return out


def write_mail_settings(changes):
    """Apply the page's changes to mail.json. Keys it doesn't show (notes starting with _) are kept."""
    data = {}
    if os.path.exists(MAIL_FILE):
        try:
            with open(MAIL_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            loaded = json.loads(raw) if raw else {}
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}                       # an unreadable file is replaced
    data.update(changes)
    tmp = MAIL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)                # may hold the relay password
    except OSError:
        pass
    os.replace(tmp, MAIL_FILE)


PBKDF2_ITERS = 200000
LOGIN_HELP = ('To change the password, add a line "password": "your new password" (comma after the line above '
              'it) and save. It is turned into a hash on the next sign-in, and everyone is signed out.')


def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ITERS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERS, salt.hex(), dk.hex())


def check_hash(pw, stored):
    try:
        algo, iters, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk, bytes.fromhex(want))
    except (ValueError, TypeError):
        return False


def _write_login(user, pw_hash):
    tmp = LOGIN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"username": user, "password_hash": pw_hash, "_help": LOGIN_HELP}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, LOGIN_FILE)


def read_login():
    """{"username", "hash"} or {"username", "plain"} (only for the built-in admin / admin default, or when a new
    plain password could not be written back as a hash). A plain "password" in login.json is replaced by a
    salted hash here, and everyone is signed out, so passwords never stay on disk as plain text."""
    if not os.path.exists(LOGIN_FILE):
        return {"username": "admin", "plain": "admin"}
    with open(LOGIN_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"username": "admin", "plain": "admin"}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it
    bad = 'login.json must look like {"username": "admin", "password": "your password"}'
    if not isinstance(data, dict):
        raise ValueError(bad)
    user = str(data.get("username") or "admin")
    pw = data.get("password")
    if isinstance(pw, str) and pw:
        pw_hash = hash_password(pw)
        try:
            _write_login(user, pw_hash)
        except OSError as e:
            print(f"WARNING: could not save the new password in login.json as a hash ({e}) - it stays as plain text.")
            return {"username": user, "plain": pw}
        with _auth_lock:
            _SESSIONS.clear()           # a new password signs everyone out
        print("login.json: new password saved as a hash; everyone was signed out.")
        return {"username": user, "hash": pw_hash}
    pw_hash = data.get("password_hash")
    if not isinstance(pw_hash, str) or pw_hash.count("$") != 3:
        raise ValueError(bad)
    return {"username": user, "hash": pw_hash}


def password_ok(creds, password):
    """Constant-time check against a hash, or against the plain admin / admin default."""
    if "hash" in creds:
        return check_hash(password, creds["hash"])
    return hmac.compare_digest(password.encode("utf-8"), creds["plain"].encode("utf-8"))


# in-memory session store + login-attempt lockout, guarded by one lock since the server is threaded.
# Nothing here is persisted to disk on purpose - restarting the service simply signs everyone out.
_auth_lock = threading.Lock()
_SESSIONS = {}   # token -> expiry (epoch seconds)
_FAILED = {}     # client ip -> [attempt count, locked-until epoch (0 if not locked)]


def create_session():
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        _SESSIONS[token] = time.time() + SESSION_TTL_SECONDS
    return token


def valid_session(token):
    if not token:
        return False
    with _auth_lock:
        expiry = _SESSIONS.get(token)
        if expiry is None:
            return False
        if expiry < time.time():
            del _SESSIONS[token]
            return False
        return True


def drop_session(token):
    with _auth_lock:
        _SESSIONS.pop(token, None)
        _MAIL_OPEN.pop(token, None)


MAIL_UNLOCK_SECONDS = 10 * 60   # the Mail server page stays open this long after the password or the last save
_MAIL_OPEN = {}                 # session token -> mail page open until (epoch seconds)


def set_mail_open(token, on):
    with _auth_lock:
        if on and token in _SESSIONS:
            _MAIL_OPEN[token] = time.time() + MAIL_UNLOCK_SECONDS
        else:
            _MAIL_OPEN.pop(token, None)


def mail_open(token):
    with _auth_lock:
        return token in _SESSIONS and _MAIL_OPEN.get(token, 0) > time.time()


def is_locked_out(ip):
    with _auth_lock:
        count, locked_until = _FAILED.get(ip, (0, 0))
        if locked_until and locked_until > time.time():
            return True
        if locked_until and locked_until <= time.time():
            _FAILED.pop(ip, None)
        return False


def record_login_failure(ip):
    with _auth_lock:
        count, _locked_until = _FAILED.get(ip, (0, 0))
        count += 1
        locked_until = time.time() + LOCKOUT_SECONDS if count >= MAX_LOGIN_ATTEMPTS else 0
        _FAILED[ip] = (count, locked_until)


def clear_login_failures(ip):
    with _auth_lock:
        _FAILED.pop(ip, None)


def _person_days(name, year, month, shift_types, shifts, leave):
    """One person's {day-of-month: shift_type_id-or-"__leave__"} for one month, plus per-type counts."""
    by_id = {t["id"]: t for t in shift_types}
    days = {}
    for ds, day_shifts in shifts.items():
        shift_id = day_shifts.get(name)
        if not shift_id or shift_id not in by_id:
            continue
        y, m, d = (int(x) for x in ds.split("-"))
        if y == year and m == month:
            days[d] = shift_id
    for ds in leave.get(name, []):
        y, m, d = (int(x) for x in ds.split("-"))
        if y == year and m == month:
            days[d] = "__leave__"
    return days, by_id


def shift_marks(shift_types):
    """The short mark each shift type gets on the rota (email and PDF): its first letter, or two letters when
    that would be ambiguous (two types starting alike, or one starting with L, which stands for leave)."""
    names = {t["id"]: (str(t.get("name") or "?").strip() or "?") for t in shift_types}
    marks, used = {}, set()
    for t in shift_types:
        nm = names[t["id"]]
        one = nm[:1].upper()
        clash = sum(1 for n in names.values() if n[:1].upper() == one) > 1 or one == "L"
        mark = (one + nm[1:2].lower()) if clash and len(nm) > 1 else one
        while mark in used:
            mark += "'"
        marks[t["id"]] = mark
        used.add(mark)
    marks["__leave__"] = "L" if "L" not in used else "Lv"
    return marks


def build_shift_message(name, year, month, month_label, shift_types, shifts, leave, palette="classic"):
    """Build (subject, text_body, html_body) for one person's slice of a month's shifts + leave.
    The html calendar is colored with the selected scheme's on-screen colors (SCREEN_PALETTES)."""
    colors = SCREEN_PALETTES.get(palette) or SCREEN_PALETTES["classic"]
    days, by_id = _person_days(name, year, month, shift_types, shifts, leave)
    type_order = [t["id"] for t in shift_types]
    counts = {tid: sum(1 for v in days.values() if v == tid) for tid in type_order}
    n_leave = sum(1 for v in days.values() if v == "__leave__")
    n_shifts = sum(counts.values())
    first_name = (name.split() or ["there"])[0]
    subject = f"Your {month_label} shift schedule"

    def label_for(v):
        if v == "__leave__":
            return "Leave"
        t = by_id.get(v)
        return f"{t['name']} ({t['start']}\u2013{t['end']})" if t else v

    # ---- plain-text (always sent, and shown by clients that can't/won't render html) ----
    breakdown = ", ".join(f"{counts[tid]} {by_id[tid]['name']}" for tid in type_order if counts[tid]) or "0"
    summary_line = f"{n_shifts} shift(s): {breakdown}" + (f", {n_leave} leave day(s)" if n_leave else "") + "."
    text_lines = [f"Hi {first_name},", "", f"Your schedule for {month_label} - {summary_line}", ""]
    if not days:
        text_lines.append("You have no shifts or leave scheduled this month.")
    for d in sorted(days):
        wd = date(year, month, d).strftime("%a")
        text_lines.append(f"  {wd}, {year}-{month:02d}-{d:02d} \u2014 {label_for(days[d])}")
    text_lines += ["", "Sent by Schedule Master."]
    text_body = "\n".join(text_lines) + "\n"

    # ---- html: the month as a rota ledger ----
    # Like the paper rota on the wall: one ruled line per week with the week number in the margin, seven day
    # columns, each working day marked with its shift's letter in the selected scheme's colours, and the
    # totals at the foot. Table layout and inline styles only, so it holds up in Outlook and Gmail.
    INK, INK2, INK3, RULE, PAPER, TEAL = "#221f1a", "#57524a", "#736e64", "#e4e0d6", "#f6f5f2", "#0f6b5c"
    SERIF = "Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif"
    MONO = "ui-monospace,Menlo,Consolas,'Courier New',monospace"
    total = calendar.monthrange(year, month)[1]
    hours = lambda t: f"{escape(str(t['start']))}&nbsp;&ndash;&nbsp;{escape(str(t['end']))}" if t.get("start") and t.get("end") else ""
    color_of = lambda v: colors[3] if v == "__leave__" else colors[type_order.index(v) % 3]

    marks = shift_marks(shift_types)

    def badge(v, size=12, w=34, h=26):
        bg, fg = color_of(v)
        return (f'<div style="margin:4px auto 0;width:{w}px;height:{h}px;line-height:{h}px;border-radius:{h // 2}px;'
                f'background:{bg};color:{fg};font-weight:bold;font-size:{size}px;text-align:center;font-family:{FONT}">'
                f'{escape(marks[v])}</div>')

    lead = date(year, month, 1).isoweekday() % 7
    cells = [None] * lead + list(range(1, total + 1))
    cells += [None] * (-len(cells) % 7)
    head = '<td style="width:64px"></td>' + "".join(
        f'<td align="center" style="padding:0 0 8px;font-size:11px;color:{INK3};font-family:{FONT}">{x}</td>' for x in DOWS)
    rows = [f"<tr>{head}</tr>"]
    for r in range(0, len(cells), 7):
        week = cells[r:r + 7]
        anchor = week[1] or next(d for d in week if d)        # the row's Monday: weeks are numbered Monday to Sunday
        wk = date(year, month, anchor).isocalendar()[1]
        tds = [f'<td valign="top" style="padding:8px 8px 8px 0;border-top:1px solid {RULE};font-family:{SERIF};'
               f'font-size:13px;color:{INK3};white-space:nowrap">Wk&nbsp;{wk}</td>']
        for i, d in enumerate(week):
            if d is None:
                tds.append(f'<td style="border-top:1px solid {RULE}"></td>')
                continue
            v = days.get(d)
            shade = "background:#faf9f6;" if i in (0, 6) else ""
            mark = badge(v) if v else (f'<div style="margin:4px auto 0;height:26px;line-height:26px;font-size:12px;'
                                       f'color:#c9c4b8;text-align:center;font-family:{FONT}">&middot;</div>')
            tds.append(f'<td align="center" valign="top" style="padding:8px 2px;border-top:1px solid {RULE};{shade}">'
                       f'<div style="font-size:12px;color:{INK};font-family:{FONT}">{d}</div>{mark}</td>')
        rows.append(f"<tr>{''.join(tds)}</tr>")

    key = []
    for t in shift_types:
        bg, fg = color_of(t["id"])
        n = counts.get(t["id"], 0)
        key.append(f'<tr><td style="padding:3px 0;width:30px"><div style="width:26px;height:18px;line-height:18px;text-align:center;'
                   f'border-radius:9px;background:{bg};color:{fg};font-weight:bold;font-size:11px;font-family:{FONT}">{escape(marks[t["id"]])}</div></td>'
                   f'<td style="padding:3px 10px;font-size:13px;color:{INK};font-family:{FONT}">{escape(t["name"])}</td>'
                   f'<td style="padding:3px 10px;font-family:{MONO};font-size:12px;color:{INK2}">{hours(t)}</td>'
                   f'<td align="right" style="padding:3px 0;font-size:13px;color:{INK};font-family:{FONT}"><b>{n}</b> shift{"" if n == 1 else "s"}</td></tr>')
    bg, fg = colors[3]
    key.append(f'<tr><td style="padding:3px 0;width:30px"><div style="width:26px;height:18px;line-height:18px;text-align:center;'
               f'border-radius:9px;background:{bg};color:{fg};font-weight:bold;font-size:11px;font-family:{FONT}">{marks["__leave__"]}</div></td>'
               f'<td style="padding:3px 10px;font-size:13px;color:{INK};font-family:{FONT}">Leave</td><td></td>'
               f'<td align="right" style="padding:3px 0;font-size:13px;color:{INK};font-family:{FONT}"><b>{n_leave}</b> day{"" if n_leave == 1 else "s"}</td></tr>')

    intro = (f"Hello {escape(first_name)}. Your marked days for {escape(month_label)} are below, one line per week."
             if days else f"Hello {escape(first_name)}. Nothing is planned for you in {escape(month_label)}.")
    html_body = (
        f'<!doctype html><html><body style="margin:0;padding:0;background:{PAPER}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}">'
        '<tr><td align="center" style="padding:24px 10px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;'
        f'background:#ffffff;border:1px solid {RULE};border-radius:4px">'
        f'<tr><td style="padding:28px 32px 0;font-family:{FONT}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td valign="bottom" style="font-family:{SERIF};font-size:30px;color:{INK};line-height:1.1">{escape(MONTHS[month - 1])}<br>'
        f'<span style="font-size:15px;color:{INK3}">{year} &middot; rota for {escape(name)}</span></td>'
        f'<td valign="bottom" align="right" style="font-size:12px;color:{TEAL};font-weight:bold;font-family:{FONT}">Schedule Master</td>'
        f'</tr></table><div style="border-top:2px solid {INK};margin-top:14px;font-size:0;line-height:0">&nbsp;</div></td></tr>'
        f'<tr><td style="padding:16px 32px 4px;font-size:14px;line-height:1.5;color:{INK2};font-family:{FONT}">{intro}</td></tr>'
        f'<tr><td style="padding:12px 32px 8px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="table-layout:fixed">{"".join(rows)}</table></td></tr>'
        f'<tr><td style="padding:12px 32px 20px"><div style="border-top:2px solid {INK};padding-top:12px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{"".join(key)}</table></div></td></tr>'
        f'<tr><td style="padding:12px 32px 22px;background:{PAPER};font-size:12px;line-height:1.5;color:{INK3};font-family:{FONT}">'
        'Only your own shifts are on this rota. The PDF and calendar file attached have the same dates. Sent by Schedule Master.</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return subject, text_body, html_body


# ---------- attachments: one-page PDF month calendar + .ics calendar file ----------
# Both are built with the standard library only (no reportlab/icalendar), in keeping with the rest of
# this file. The PDF uses the two base-14 fonts every PDF reader already has (Helvetica, Helvetica-Bold)
# so nothing needs embedding; text is WinAnsi (cp1252) encoded, so accented Latin names print correctly
# and characters outside cp1252 fall back to "?".

# Advance widths (1/1000 em) for ASCII 32..126, from the standard Adobe AFM metrics - used to centre and
# truncate text so a long name or shift label never spills out of its calendar cell.
_HELV_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
           556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
           722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
           278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
           556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELVB_W = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
            556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778,
            722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
            278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
            611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]


def _pdf_text_width(text, size, bold=False, font=None):
    import unicodedata
    if font == "mono":
        return len(text) * 0.6 * size              # Courier: every character is 600/1000 wide
    if font == "serif":
        return _pdf_text_width(text, size) * 0.92  # Times-Roman runs a little narrower than Helvetica
    table = _HELVB_W if bold else _HELV_W
    total = 0
    for ch in text:
        base = unicodedata.normalize("NFKD", ch)[:1] or ch  # accented letter -> width of its base letter
        o = ord(base)
        total += table[o - 32] if 32 <= o <= 126 else 556
    return total * size / 1000.0


def _pdf_fit(text, size, max_w, bold=False):
    """Truncate text with an ellipsis so it fits max_w points."""
    if _pdf_text_width(text, size, bold) <= max_w:
        return text
    while text and _pdf_text_width(text + "\u2026", size, bold) > max_w:
        text = text[:-1]
    return (text + "\u2026") if text else ""


def _pdf_str(text):
    """A PDF literal string in WinAnsi encoding, with ( ) \\ escaped and non-ASCII bytes as octal escapes."""
    raw = text.encode("cp1252", errors="replace")
    out = []
    for b in raw:
        if b in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(b))
        elif 32 <= b <= 126:
            out.append(chr(b))
        else:
            out.append("\\%03o" % b)
    return "(" + "".join(out) + ")"


def _pdf_rgb(hex_color):
    h = hex_color.lstrip("#")
    return "%.3f %.3f %.3f" % tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class _PdfPage:
    """Tiny content-stream builder. Coordinates are top-left based (y grows downward) like the HTML
    layout, and converted to PDF's bottom-left origin internally."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.ops = []

    def rect(self, x, y, w, h, fill=None, stroke=None, radius=0, line=0.8):
        y0 = self.h - y - h
        if radius:
            r, k = min(radius, w / 2, h / 2), 0.5523 * min(radius, w / 2, h / 2)
            p = [f"{x + r:.2f} {y0:.2f} m", f"{x + w - r:.2f} {y0:.2f} l",
                 f"{x + w - r + k:.2f} {y0:.2f} {x + w:.2f} {y0 + r - k:.2f} {x + w:.2f} {y0 + r:.2f} c",
                 f"{x + w:.2f} {y0 + h - r:.2f} l",
                 f"{x + w:.2f} {y0 + h - r + k:.2f} {x + w - r + k:.2f} {y0 + h:.2f} {x + w - r:.2f} {y0 + h:.2f} c",
                 f"{x + r:.2f} {y0 + h:.2f} l",
                 f"{x + r - k:.2f} {y0 + h:.2f} {x:.2f} {y0 + h - r + k:.2f} {x:.2f} {y0 + h - r:.2f} c",
                 f"{x:.2f} {y0 + r:.2f} l",
                 f"{x:.2f} {y0 + r - k:.2f} {x + r - k:.2f} {y0:.2f} {x + r:.2f} {y0:.2f} c", "h"]
            path = " ".join(p)
        else:
            path = f"{x:.2f} {y0:.2f} {w:.2f} {h:.2f} re"
        if fill:
            self.ops.append(f"{_pdf_rgb(fill)} rg")
        if stroke:
            self.ops.append(f"{_pdf_rgb(stroke)} RG {line:.2f} w")
        self.ops.append(path + (" B" if fill and stroke else (" f" if fill else " S")))

    def text(self, x, y, text, size, color="#17212b", bold=False, align="left", max_w=None, font=None):
        if max_w is not None and font is None:
            text = _pdf_fit(text, size, max_w, bold)
        if not text:
            return
        tw = _pdf_text_width(text, size, bold, font)
        if align == "center":
            x -= tw / 2
        elif align == "right":
            x -= tw
        fid = {"serif": "F3", "mono": "F4"}.get(font) or ("F2" if bold else "F1")
        self.ops.append(f"BT /{fid} {size:.2f} Tf {_pdf_rgb(color)} rg "
                        f"{x:.2f} {self.h - y - size * 0.8:.2f} Td {_pdf_str(text)} Tj ET")

    def to_pdf(self, title=""):
        import zlib
        stream = zlib.compress("\n".join(self.ops).encode("latin-1"))
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.w} {self.h}] /Contents 4 0 R "
             f"/Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 8 0 R /F4 9 0 R >> >> >>").encode("ascii"),
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            (f"<< /Title {_pdf_str(title)} /Producer (Schedule Master) >>").encode("latin-1"),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
        ]
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
        return bytes(out)


def build_shift_pdf(name, year, month, month_label, shift_types, shifts, leave, palette="classic"):
    """One-page landscape A4 PDF of this person's month as a rota ledger, like the email: the month in serif
    over a heavy rule, one ruled line per week with the week number in the margin, each working day marked
    with its shift (name and hours) in the colours of SCREEN_PALETTES[palette], and the totals at the foot."""
    colors = SCREEN_PALETTES.get(palette) or SCREEN_PALETTES["classic"]
    days, by_id = _person_days(name, year, month, shift_types, shifts, leave)
    type_order = [t["id"] for t in shift_types]
    counts = {tid: sum(1 for v in days.values() if v == tid) for tid in type_order}
    n_leave = sum(1 for v in days.values() if v == "__leave__")
    marks = shift_marks(shift_types)
    ink, ink2, ink3, rule, paper, teal = "#221f1a", "#57524a", "#736e64", "#d9d5cc", "#faf9f6", "#0f6b5c"
    color_of = lambda v: colors[3] if v == "__leave__" else colors[type_order.index(v) % 3]
    times_of = lambda t: f"{t['start']}-{t['end']}" if t.get("start") and t.get("end") else ""

    W, H, M = 842, 595, 32  # A4 landscape, points; M = page margin
    pg = _PdfPage(W, H)
    # header: the month in serif, whose rota it is, the app name in teal, a heavy rule
    pg.text(M, M, MONTHS[month - 1], 28, color=ink, font="serif")
    pg.text(M, M + 34, f"{year}  \u00b7  rota for {name}", 12, color=ink3, font="serif")
    pg.text(W - M, M + 36, "Schedule Master", 9.5, color=teal, bold=True, align="right")
    pg.rect(M, M + 54, W - 2 * M, 1.6, fill=ink)
    # the ledger
    margin_w, y = 46, M + 64
    col_w = (W - 2 * M - margin_w) / 7.0
    x0 = M + margin_w
    for i, dname in enumerate(DOWS):
        pg.text(x0 + i * col_w + col_w / 2, y, dname, 8.5, color=ink3, align="center")
    y += 14
    lead = date(year, month, 1).isoweekday() % 7
    total = calendar.monthrange(year, month)[1]
    cells = [None] * lead + list(range(1, total + 1))
    cells += [None] * (-len(cells) % 7)
    n_rows = len(cells) // 7
    key_rows = len(shift_types) + 1
    foot_h = 14 + key_rows * 15 + 22
    row_h = min(84, (H - M - foot_h - y) / n_rows)
    for r in range(n_rows):
        week = cells[r * 7:r * 7 + 7]
        cy = y + r * row_h
        for i in (0, 6):
            if week[i] is not None:
                pg.rect(x0 + i * col_w, cy, col_w, row_h, fill=paper)
        pg.rect(M, cy, W - 2 * M, 0.6, fill=rule)
        anchor = week[1] or next(d for d in week if d)       # the row's Monday: weeks run Monday to Sunday
        pg.text(M, cy + 7, f"Wk {date(year, month, anchor).isocalendar()[1]}", 10, color=ink3, font="serif")
        for i, d in enumerate(week):
            if d is None:
                continue
            cx = x0 + i * col_w
            pg.text(cx + col_w / 2, cy + 6, str(d), 10, color=ink, align="center")
            v = days.get(d)
            if v:
                bg, fg = color_of(v)
                ph = min(30, row_h - 26)
                py = cy + 21
                pg.rect(cx + 6, py, col_w - 12, ph, fill=bg, radius=ph / 2 if ph < 20 else 8)
                label = "Leave" if v == "__leave__" else by_id[v]["name"]
                hrs = "" if v == "__leave__" else times_of(by_id[v])
                if hrs and ph >= 26:
                    pg.text(cx + col_w / 2, py + 4.5, label, 9.5, color=fg, bold=True, align="center", max_w=col_w - 20)
                    pg.text(cx + col_w / 2, py + 16.5, hrs, 7.5, color=fg, align="center", font="mono")
                else:
                    pg.text(cx + col_w / 2, py + ph / 2 - 4.5, label, 9.5, color=fg, bold=True, align="center", max_w=col_w - 20)
            else:
                pg.text(cx + col_w / 2, cy + 30, "\u00b7", 12, color="#c9c4b8", align="center")
    # the foot: totals per shift type, then leave
    fy = y + n_rows * row_h + 8
    pg.rect(M, fy, W - 2 * M, 1.6, fill=ink)
    fy += 10
    for t in shift_types + [{"id": "__leave__", "name": "Leave"}]:
        v = t["id"]
        bg, fg = color_of(v)
        pg.rect(M, fy, 24, 13, fill=bg, radius=6.5)
        pg.text(M + 12, fy + 2.5, marks[v], 8, color=fg, bold=True, align="center")
        pg.text(M + 34, fy + 2, t["name"], 10, color=ink, max_w=200)
        if v != "__leave__":
            pg.text(M + 250, fy + 2.5, times_of(t), 9, color=ink2, font="mono")
        n = n_leave if v == "__leave__" else counts.get(v, 0)
        unit = ("day" if v == "__leave__" else "shift") + ("" if n == 1 else "s")
        pg.text(W - M, fy + 2, f"{n} {unit}", 10, color=ink, align="right")
        fy += 15
    pg.text(M, H - M - 4, f"Only your own shifts are on this rota. Sent by Schedule Master to {name}.", 8, color=ink3, max_w=W - 2 * M)
    return pg.to_pdf(title=f"{month_label} rota - {name}")


def _ics_escape(text):
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def _ics_fold(line):
    """RFC 5545 line folding: max 75 octets per line, continuation lines start with a space."""
    out, cur, cur_len = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        limit = 75 if not out else 74
        if cur_len + n > limit:
            out.append(cur)
            cur, cur_len = "", 0
        cur += ch
        cur_len += n
    out.append(cur)
    return "\r\n ".join(out)


def _hhmm(value):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def build_shift_ics(name, year, month, month_label, shift_types, shifts, leave):
    """An importable iCalendar (.ics) file: one timed event per shift (an end at/before the start means the
    shift runs past midnight into the next day) and one all-day event per leave day. Times are "floating"
    local wall-clock times - the same times shown in the app - so they appear at those hours on the
    recipient's calendar. UIDs are stable per person+date, so importing a re-sent month updates events
    rather than duplicating them (SEQUENCE increases with each send)."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    days, by_id = _person_days(name, year, month, shift_types, shifts, leave)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sequence = int(time.time())
    person_key = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Schedule Master//Shift schedule//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape(f'Shifts - {month_label}')}"]
    for d in sorted(days):
        v = days[d]
        day = date(year, month, d)
        lines += ["BEGIN:VEVENT", f"UID:{day:%Y%m%d}-{person_key}@schedulemaster",
                  f"DTSTAMP:{stamp}", f"SEQUENCE:{sequence}"]
        if v == "__leave__":
            lines += [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
                      "SUMMARY:Leave", "TRANSP:TRANSPARENT"]
        else:
            t = by_id[v]
            start, end = _hhmm(t.get("start")), _hhmm(t.get("end"))
            if start and end:
                s_dt = datetime(year, month, d, *start)
                e_dt = datetime(year, month, d, *end)
                if e_dt <= s_dt:
                    e_dt += timedelta(days=1)  # overnight shift, e.g. Night 19:00-07:00
                lines += [f"DTSTART:{s_dt:%Y%m%dT%H%M%S}", f"DTEND:{e_dt:%Y%m%dT%H%M%S}"]
            else:  # no usable times for this shift type - still put it on the right day
                lines += [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
            lines += [f"SUMMARY:{_ics_escape(t['name'] + ' shift')}",
                      f"DESCRIPTION:{_ics_escape(f'{name} - ' + t['name'] + ' shift, ' + month_label)}"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_ics_fold(line) for line in lines) + "\r\n").encode("utf-8")


def attachment_basename(name, year, month):
    """Shifts_YYYY-MM_Full Name - characters that are unsafe in file names are removed."""
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "", name).strip().strip(".") or "staff"
    safe = re.sub(r"\s+", " ", safe)[:80]
    return f"Shifts_{year}-{month:02d}_{safe}"


def open_smtp(cfg=None):
    """One SMTP connection, reused for every recipient in a batch rather than reconnecting each time.
    cfg: settings from read_mail_settings() (read here if not given)."""
    cfg = cfg or read_mail_settings()
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=ssl.create_default_context())
        server.ehlo()
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
    if cfg["username"] and cfg["password"]:
        server.login(cfg["username"], cfg["password"])
    return server


def send_via(server, to_addr, subject, text_body, html_body, attachments=None, cfg=None):
    """attachments: optional list of (filename, maintype, subtype, bytes). cfg: as for open_smtp()."""
    cfg = cfg or read_mail_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for filename, maintype, subtype, data in (attachments or []):
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    server.send_message(msg, from_addr=cfg["from_addr"], to_addrs=[to_addr])


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FOLDER, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        # This file changes often during development. Without this, browsers may keep serving a stale
        # cached copy of ScheduleMaster.html after it's been updated on disk, until the tab is hard-refreshed.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _authed(self):
        return valid_session(self._cookie_token())

    def _require_auth(self):
        if not self._authed():
            self._send_json(401, {"error": "not authenticated"})
            return False
        return True

    def _cookie_secure_attr(self):
        # only mark the cookie Secure when we know (via nginx's X-Forwarded-Proto) that this request
        # arrived over https - this http.server itself never terminates TLS directly, see install.sh
        return "; Secure" if (TRUST_PROXY and self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""

    def _session_cookie_header(self, token):
        return f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}{self._cookie_secure_attr()}"

    def _clear_session_cookie_header(self):
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{self._cookie_secure_attr()}"

    def do_GET(self):
        if self.path == "/" or self.path == "":
            # no index.html lives in this folder, so the base http.server would otherwise list the
            # directory (staff.json, login.json and friends included) instead of opening the app.
            self.path = "/" + HTML_FILE
        path = self.path.rstrip("/")
        if path == "/api/ping":
            self._send_json(200, {"ok": True, "app": "ScheduleMaster"})
            return
        if path == "/api/session":
            self._send_json(200, {"authenticated": self._authed()})
            return
        if path in ("/api/staff", "/api/records", "/api/emails") and not self._authed():
            self._send_json(401, {"error": "not authenticated"})
            return
        if path == "/api/staff":
            try:
                self._send_json(200, read_staff())
            except ValueError as e:
                self._send_json(400, {"error": f"staff.json is unreadable: {e}. It was not changed."})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/records":
            try:
                self._send_json(200, read_records())
            except ValueError as e:
                self._send_json(400, {"error": f"records.json is unreadable: {e}. It was not changed."})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/emails":
            try:
                self._send_json(200, read_emails())
            except ValueError as e:
                self._send_json(400, {"error": f"emails.json is unreadable: {e}. It was not changed."})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        self._serve_app_page_only(super().do_GET)

    def do_HEAD(self):
        # SimpleHTTPRequestHandler answers HEAD for any file too (headers only, but that still confirms
        # a file exists and its size) - apply the same one-file allowlist as GET.
        self._serve_app_page_only(super().do_HEAD)

    def _serve_app_page_only(self, serve):
        # The folder this server runs from also holds login.json (plaintext password), staff.json,
        # records.json, emails.json and this script. The static handler used to serve ANY file in it,
        # with no session check, so e.g. GET /login.json returned the password. Only the app page itself
        # is served statically now; its data goes through the authenticated /api/* routes above.
        req_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if req_path != "/" + HTML_FILE:
            if self.command == "HEAD":  # a HEAD response must not carry a body
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json(404, {"error": "not found"})
            return
        serve()

    def do_PUT(self):
        path = self.path.rstrip("/")
        if path in ("/api/staff", "/api/records", "/api/emails") and not self._require_auth():
            return
        if path == "/api/staff":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                data = json.loads(body.decode("utf-8"))
                result = write_staff(data)
                self._send_json(200, result)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/records":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                data = json.loads(body.decode("utf-8"))
                result = write_records(data)
                self._send_json(200, result)
            except RecordsConflict as e:
                self._send_json(409, {"error": str(e), "records": e.current["records"], "version": e.current["version"]})
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/emails":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                data = json.loads(body.decode("utf-8"))
                result = write_emails(data)
                self._send_json(200, result)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/api/login":
            ip = self._client_ip()
            if is_locked_out(ip):
                self._send_json(429, {"error": "too many attempts - wait a bit and try again"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"bad request body: {e}"})
                return
            username = str(req.get("username") or "").strip()
            password = str(req.get("password") or "")
            try:
                creds = read_login()
            except ValueError as e:
                self._send_json(500, {"error": f"login.json is unreadable: {e}"})
                return
            # constant-time comparison so response timing doesn't leak how much of the password matched
            user_ok = hmac.compare_digest(username.lower().encode("utf-8"), creds["username"].lower().encode("utf-8"))
            pass_ok = password_ok(creds, password)
            if user_ok and pass_ok:
                clear_login_failures(ip)
                token = create_session()
                self._send_json(200, {"ok": True}, extra_headers=[("Set-Cookie", self._session_cookie_header(token))])
            else:
                record_login_failure(ip)
                self._send_json(401, {"error": "invalid username or password"})
            return
        if path == "/api/logout":
            drop_session(self._cookie_token())
            self._send_json(200, {"ok": True}, extra_headers=[("Set-Cookie", self._clear_session_cookie_header())])
            return
        if path == "/api/mail":
            if not self._require_auth():
                return
            self._mail_page()
            return
        if path == "/api/admin":
            if not self._require_auth():
                return
            self._admin_check()
            return
        if path == "/api/send-shifts" and not self._require_auth():
            return
        if path == "/api/send-shifts":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"bad request body: {e}"})
                return
            try:
                year, month = int(req.get("year")), int(req.get("month")) + 1  # JS month is 0-based
            except (TypeError, ValueError):
                self._send_json(400, {"error": "bad year/month"})
                return
            if not (1 <= month <= 12 and 1 <= year <= 9999):
                self._send_json(400, {"error": "bad year/month"})
                return
            month_label = str(req.get("monthLabel") or f"{MONTHS[month - 1]} {year}")
            shift_types = req.get("shiftTypes") or []
            shifts = req.get("shifts") or {}
            leave = req.get("leave") or {}
            palette = str(req.get("palette") or "classic")
            if palette not in SCREEN_PALETTES:
                palette = "classic"  # unknown/older client value - fall back rather than fail the whole send
            try:
                email_data = read_emails()
                addresses = email_data["emails"]
                paused = set(email_data["paused"])
            except ValueError as e:
                self._send_json(400, {"error": f"emails.json is unreadable: {e}. Nothing was sent."})
                return
            if not addresses:
                self._send_json(200, {"sent": [], "skipped": [], "failed": []})
                return

            sent, skipped, failed = [], [], []
            to_send = []
            for name, addr in addresses.items():
                if name in paused:
                    skipped.append({"name": name, "reason": "sending turned off"})
                elif EMAIL_RE.match(addr):
                    to_send.append((name, addr))
                else:
                    skipped.append({"name": name, "reason": "invalid email address"})

            server = None
            mail_cfg = None
            if to_send:
                try:
                    mail_cfg = read_mail_settings()  # read once per batch, fresh each send - no restart needed
                except (ValueError, OSError) as e:
                    self._send_json(400, {"error": f"mail.json is unreadable: {e}. Nothing was sent."})
                    return
                try:
                    server = open_smtp(mail_cfg)
                except Exception as e:
                    self._send_json(502, {"error": f"could not connect to the mail relay "
                                                   f"({mail_cfg['host']}:{mail_cfg['port']}): {e}"})
                    return
            for name, addr in to_send:
                try:
                    subject, text_body, html_body = build_shift_message(name, year, month, month_label, shift_types,
                                                                        shifts, leave, palette)
                    base = attachment_basename(name, year, month)
                    attachments = [
                        (base + ".pdf", "application", "pdf",
                         build_shift_pdf(name, year, month, month_label, shift_types, shifts, leave, palette)),
                        (base + ".ics", "text", "calendar",
                         build_shift_ics(name, year, month, month_label, shift_types, shifts, leave)),
                    ]
                    try:
                        send_via(server, addr, subject, text_body, html_body, attachments, mail_cfg)
                    except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused, OSError):
                        server = open_smtp(mail_cfg)  # relay dropped the connection mid-batch - reconnect once and retry
                        send_via(server, addr, subject, text_body, html_body, attachments, mail_cfg)
                    sent.append(name)
                except Exception as e:
                    failed.append({"name": name, "error": str(e)})
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
            self._send_json(200, {"sent": sent, "skipped": skipped, "failed": failed})
            return
        self._send_json(404, {"error": "not found"})


def _mail_page_handler(self):
    """POST /api/mail - see the list of routes at the top of this file."""
    try:
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
        action = req.get("action") if isinstance(req, dict) else None
    except Exception as e:
        self._send_json(400, {"error": f"bad request body: {e}"})
        return
    token = self._cookie_token()
    if action == "lock":
        set_mail_open(token, False)
        self._send_json(200, {"ok": True})
        return
    if action == "unlock":
        ip = self._client_ip()
        if is_locked_out(ip):
            self._send_json(429, {"error": "Too many wrong passwords. Wait a bit and try again."})
            return
        password = req.get("password")
        if not isinstance(password, str) or not password:
            self._send_json(400, {"error": "Enter the admin password."})
            return
        try:
            creds = read_login()
        except ValueError as e:
            self._send_json(500, {"error": f"login.json is unreadable: {e}"})
            return
        if not password_ok(creds, password):
            record_login_failure(ip)
            self._send_json(403, {"error": "Wrong password."})
            return
        clear_login_failures(ip)
        set_mail_open(token, True)
    elif action in ("save", "test"):
        if not mail_open(token):
            self._send_json(403, {"error": "The mail settings are locked. Go back and enter the admin password again.",
                                  "locked": True})
            return
    else:
        self._send_json(400, {"error": "bad request"})
        return
    if action == "save":
        try:
            changes = clean_mail_form(req.get("mail"))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        try:
            write_mail_settings(changes)
        except OSError as e:
            self._send_json(500, {"error": f"Could not write mail.json: {e}"})
            return
        set_mail_open(token, True)
        print("Saved mail relay settings to mail.json")
    if action == "test":
        to = str(req.get("to") or "").strip()
        if not EMAIL_RE.match(to):
            self._send_json(400, {"error": "Enter a valid address to send the test to."})
            return
        try:
            cfg = read_mail_settings()
        except (ValueError, OSError) as e:
            self._send_json(400, {"error": f"mail.json is unreadable: {e}"})
            return
        try:
            server = open_smtp(cfg)
            try:
                send_via(server, to, "Schedule Master test email",
                         "This is a test email from Schedule Master.\n\nThe mail relay settings work: shifts sent from "
                         f"Schedule Master will come from {cfg['from_addr']} through {cfg['host']}:{cfg['port']}.\n",
                         "<p>This is a test email from Schedule Master.</p><p>The mail relay settings work: shifts sent "
                         f"from Schedule Master will come from {escape(cfg['from_addr'])} through "
                         f"{escape(cfg['host'])}:{cfg['port']}.</p>", None, cfg)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except Exception as e:
            self._send_json(502, {"error": f"The test email was not sent: {e or e.__class__.__name__}"})
            return
        set_mail_open(token, True)
        print(f"Sent a test email to {to}")
        self._send_json(200, {"ok": True})
        return
    try:
        cfg = read_mail_settings()
        self._send_json(200, {"mail": mail_form(cfg), "ready": True})
    except (ValueError, OSError) as e:
        cfg = _finish_security({"host": SMTP_HOST, "port": SMTP_PORT, "from_name": SMTP_FROM_NAME, "from_addr": SMTP_FROM_ADDR,
                                "username": None, "password": None, "use_tls": SMTP_USE_TLS, "security": SMTP_SECURITY})
        self._send_json(200, {"mail": mail_form(cfg), "ready": False,
                              "problem": f"mail.json is unreadable ({e}). Saving replaces it with the settings below."})


Handler._mail_page = _mail_page_handler


def _admin_check_handler(self):
    """POST /api/admin {"password"}: checks the admin password for a locked page of the app (the Algorithm
    tab). Same lockout as the sign-in; nothing is stored or opened on the server."""
    try:
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
        password = req.get("password") if isinstance(req, dict) else None
    except Exception as e:
        self._send_json(400, {"error": f"bad request body: {e}"})
        return
    ip = self._client_ip()
    if is_locked_out(ip):
        self._send_json(429, {"error": "Too many wrong passwords. Wait a bit and try again."})
        return
    if not isinstance(password, str) or not password:
        self._send_json(400, {"error": "Enter the admin password."})
        return
    try:
        creds = read_login()
    except ValueError as e:
        self._send_json(500, {"error": f"login.json is unreadable: {e}"})
        return
    if not password_ok(creds, password):
        record_login_failure(ip)
        self._send_json(403, {"error": "Wrong password."})
        return
    clear_login_failures(ip)
    self._send_json(200, {"ok": True})


Handler._admin_check = _admin_check_handler


def find_free_port(start):
    port = start
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    return start


def main():
    # SM_PORT set (by the systemd unit install.sh writes) means "run as a service": use that exact
    # port, no free-port scanning, no browser popup (there's usually no desktop on that machine).
    # Left unset - the old double-click-on-your-laptop behavior is unchanged.
    env_port = os.environ.get("SM_PORT")
    if env_port:
        port = int(env_port)
        open_browser = False
    else:
        port = find_free_port(PORT)
        open_browser = True
    server = http.server.ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/{HTML_FILE}"
    print(f"Schedule Master server running at {url}")
    print("Press Ctrl+C to stop.")
    if not os.path.exists(LOGIN_FILE):
        print("No login.json found - signing in with the default admin / admin for now.")
    else:
        try:
            read_login()            # turns a plain "password" into a hash right away
        except (ValueError, OSError) as e:
            print(f"WARNING: login.json is unreadable ({e}) - nobody can sign in until it is fixed.")
    try:
        _mc = read_mail_settings()
        print(f"Mail relay: {_mc['host']}:{_mc['port']} ({_mc['security']}) "
              f"({'from mail.json' if os.path.exists(MAIL_FILE) else 'defaults - no mail.json'})")
    except (ValueError, OSError) as e:
        print(f"WARNING: mail.json is unreadable ({e}) - sending email will fail until it is fixed.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Schedule Master server.")
        server.shutdown()


if __name__ == "__main__":
    main()
