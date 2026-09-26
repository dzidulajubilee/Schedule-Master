#!/usr/bin/env python3
"""
Schedule Master local server.
Serves ScheduleMaster.html and its companion files from this folder, and exposes:
  GET  /api/staff    -> reads staff.json ({"staff": [...]})
  PUT  /api/staff    -> writes staff.json (body: {"staff": [...]})
  GET  /api/records  -> reads records.json ({"records": [...]})
  PUT  /api/records  -> writes records.json (body: {"records": [...]}) -- full replace,
                        same pattern as /api/staff; the client sends the whole array back
                        each time (e.g. after adding or deleting one record).
  GET  /api/emails   -> reads emails.json ({"emails": {"Full Name": "addr@example.com", ...}})
  PUT  /api/emails   -> writes emails.json (body: {"emails": {...}}) -- full replace, same
                        pattern as /api/staff.
  POST /api/send-shifts -> emails each person in emails.json their own dates and shift times
                        for one month. Body: {"year":2026,"month":8,"monthLabel":"September 2026",
                        "shiftTypes":[{"id":...,"name":...,"start":...,"end":...}, ...],
                        "shifts":{"YYYY-MM-DD":{"Full Name":"shiftTypeId", ...}, ...},
                        "leave":{"Full Name":["YYYY-MM-DD", ...], ...}, "palette":"classic"}. Sends one individual
                        email per saved address (nobody sees anyone else's schedule) through the
                        SMTP relay configured below - plain text + HTML body, plus two attachments named
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

  Every other /api/* route above requires a valid session cookie (401 if missing/expired) — the
  static page itself (ScheduleMaster.html, this file's assets) is not gated, so the browser can
  always load the app shell and show the login screen; only the data behind it is protected.

No third-party packages required (standard library only).
Run this file (python3 ScheduleMaster.py), then a browser tab opens
automatically at ScheduleMaster.html. Default login is admin / admin — see login.json.
"""
import calendar
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
# Defaults assume a Postfix instance on this same machine, listening on 25, accepting mail from
# localhost with no login (the common setup for an internal relay). Point SMTP_HOST at your VM's
# address if Postfix runs elsewhere on the network instead.
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25
SMTP_FROM_NAME = "Schedule Master"
SMTP_FROM_ADDR = "scheduler@localhost"
# If your relay requires STARTTLS and/or a login, fill these in and see send_email() below —
# the auth/TLS lines are already there, just commented out.
SMTP_USERNAME = None
SMTP_PASSWORD = None
SMTP_USE_TLS = False

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


def read_records():
    if not os.path.exists(RECORDS_FILE):
        return {"records": []}
    with open(RECORDS_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"records": []}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it, file is left untouched
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError('records.json must look like {"records": [...]}')
    return data


def write_records(data):
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError('Expected {"records": [...]}')
    # full replace, same as staff: the client already merged its add/delete into the array
    # it sends. Atomic write: temp file then replace, so a crash mid-write can't corrupt records.json
    tmp = RECORDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"records": data["records"]}, f, indent=2)
    os.replace(tmp, RECORDS_FILE)
    return {"records": data["records"]}


def read_emails():
    if not os.path.exists(EMAILS_FILE):
        return {"emails": {}}
    with open(EMAILS_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"emails": {}}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it, file is left untouched
    if not isinstance(data, dict) or not isinstance(data.get("emails"), dict):
        raise ValueError('emails.json must look like {"emails": {"Full Name": "addr@example.com"}}')
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
    # atomic write: temp file then replace, so a crash mid-write can't corrupt emails.json
    tmp = EMAILS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"emails": cleaned}, f, indent=2)
    os.replace(tmp, EMAILS_FILE)
    return {"emails": cleaned}


def read_login():
    if not os.path.exists(LOGIN_FILE):
        return {"username": "admin", "password": "admin"}
    with open(LOGIN_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"username": "admin", "password": "admin"}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it
    if not isinstance(data, dict) or not isinstance(data.get("password"), str) or not data["password"]:
        raise ValueError('login.json must look like {"username": "admin", "password": "your password"}')
    return {"username": str(data.get("username") or "admin"), "password": data["password"]}


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

    # ---- html calendar grid + dated list (mirrors the on-screen calendar's color coding) ----
    lead = date(year, month, 1).isoweekday() % 7  # 0=Sun offset into the first week
    total = calendar.monthrange(year, month)[1]
    cells = [None] * lead + list(range(1, total + 1))
    cells += [None] * (-len(cells) % 7)
    mon3 = MONTHS[month - 1][:3]
    rows = []
    for r in range(0, len(cells), 7):
        tds = []
        for i, d in enumerate(cells[r:r + 7]):
            if d is None:
                tds.append('<td style="border:0;background:#ffffff">&nbsp;</td>')
                continue
            weekend = i in (0, 6)
            v = days.get(d)
            if v == "__leave__":
                bg, fg = colors[3]
                body = (f'<div style="background:{bg};color:{fg};font-weight:bold;font-size:11px;'
                        f'padding:5px 6px;border-radius:5px;margin-top:6px">Leave</div>')
            elif v:
                idx = type_order.index(v) % 3
                bg, fg = colors[idx]
                body = (f'<div style="background:{bg};color:{fg};font-weight:bold;font-size:11px;'
                        f'padding:5px 6px;border-radius:5px;margin-top:6px">{escape(by_id[v]["name"])}</div>')
            else:
                body = '<div style="color:#8793a0;font-size:11px;margin-top:6px">Off</div>'
            tds.append(f'<td valign="top" height="60" style="border:1px solid #d8e0e8;border-radius:6px;'
                       f'padding:6px;background:{"#fcfaf4" if weekend else "#ffffff"};font-family:{FONT}">'
                       f'<div style="font-weight:bold;font-size:11px;color:#17212b">{DOWS[i]}, {mon3} {d}</div>{body}</td>')
        rows.append(f"<tr>{''.join(tds)}</tr>")
    head = "".join(f'<td align="center" style="background:#263746;color:#ffffff;font-weight:bold;'
                   f'font-size:12px;padding:8px 2px;border-radius:5px;font-family:{FONT}">{x}</td>' for x in DOWS)
    # legend as filled pills (not colored text): a "deep" scheme's text color is light, so plain colored
    # text would be unreadable on the white email background
    pill = ('<span style="display:inline-block;background:{bg};color:{fg};font-weight:bold;padding:2px 8px;'
            'border-radius:9px;margin:2px 0">{label}</span>')
    key_parts = [pill.format(bg=colors[i % 3][0], fg=colors[i % 3][1], label=escape(t["name"])) +
                 f' ({escape(str(t["start"]))}\u2013{escape(str(t["end"]))})' for i, t in enumerate(shift_types)]
    key_parts.append(pill.format(bg=colors[3][0], fg=colors[3][1], label="Leave"))
    key = " &middot; ".join(key_parts) + " &middot; Grey = Off"
    items = "".join(
        f'<tr><td style="padding:3px 0;color:#17212b;font-size:13px;font-family:{FONT}">'
        f'<b>{date(year, month, d).strftime("%a")} {mon3} {d}</b> \u2013 {escape(label_for(days[d]))}</td></tr>'
        for d in sorted(days))
    listing = (f'<tr><td style="padding:0 20px 12px"><div style="font-weight:bold;font-size:14px;'
               f'color:#17212b;margin-bottom:4px;font-family:{FONT}">Your dates</div>'
               f'<table role="presentation" cellpadding="0" cellspacing="0">'
               f'{items or f"<tr><td style=\"font-size:13px;color:#687684\">No shifts this month.</td></tr>"}'
               f'</table></td></tr>')
    html_body = (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f4f6f8">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8">'
        '<tr><td align="center" style="padding:16px 8px">'
        f'<table role="presentation" width="720" cellpadding="0" cellspacing="0" style="width:100%;'
        f'max-width:720px;background:#ffffff;border:1px solid #d8e0e8;border-radius:10px;font-family:{FONT}">'
        '<tr><td style="background:#17324d;color:#ffffff;padding:16px 20px;border-radius:10px 10px 0 0">'
        f'<div style="font-size:20px;font-weight:bold">{escape(month_label)} Shift Calendar</div>'
        f'<div style="font-size:12px;opacity:.85;margin-top:4px">{escape(name)}</div></td></tr>'
        f'<tr><td style="padding:16px 20px 6px;color:#17212b;font-size:14px">Hello {escape(first_name)}, '
        f'here are your shifts for {escape(month_label)}.<br><b>{escape(summary_line)}</b></td></tr>'
        f'<tr><td style="padding:4px 20px 10px;color:#687684;font-size:12px">{key}</td></tr>'
        '<tr><td style="padding:0 14px 16px"><table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="4" style="table-layout:fixed;border-collapse:separate"><tr>{head}</tr>{"".join(rows)}</table></td></tr>'
        f'{listing}'
        '<tr><td style="padding:0 20px 16px;color:#8793a0;font-size:11px">Sent by Schedule Master.</td></tr>'
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


def _pdf_text_width(text, size, bold=False):
    import unicodedata
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

    def text(self, x, y, text, size, color="#17212b", bold=False, align="left", max_w=None):
        if max_w is not None:
            text = _pdf_fit(text, size, max_w, bold)
        if not text:
            return
        tw = _pdf_text_width(text, size, bold)
        if align == "center":
            x -= tw / 2
        elif align == "right":
            x -= tw
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size:.2f} Tf {_pdf_rgb(color)} rg "
                        f"{x:.2f} {self.h - y - size * 0.8:.2f} Td {_pdf_str(text)} Tj ET")

    def to_pdf(self, title=""):
        import zlib
        stream = zlib.compress("\n".join(self.ops).encode("latin-1"))
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.w} {self.h}] /Contents 4 0 R "
             f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>").encode("ascii"),
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            (f"<< /Title {_pdf_str(title)} /Producer (Schedule Master) >>").encode("latin-1"),
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
    """One-page landscape A4 PDF: this person's month as a Sun-Sat grid (same layout as the on-screen /
    printed calendar), with a shift-count summary and legend, in the exact colors of SCREEN_PALETTES[palette]."""
    colors = SCREEN_PALETTES.get(palette) or SCREEN_PALETTES["classic"]
    days, by_id = _person_days(name, year, month, shift_types, shifts, leave)
    type_order = [t["id"] for t in shift_types]
    counts = {tid: sum(1 for v in days.values() if v == tid) for tid in type_order}
    n_leave = sum(1 for v in days.values() if v == "__leave__")
    n_shifts = sum(counts.values())
    breakdown = ", ".join(f"{counts[tid]} {by_id[tid]['name']}" for tid in type_order if counts[tid]) or "0"
    summary = f"{n_shifts} shift(s): {breakdown}" + (f", {n_leave} leave day(s)" if n_leave else "") + "."

    W, H, M = 842, 595, 28  # A4 landscape, points; M = page margin
    pg = _PdfPage(W, H)
    # header band
    pg.rect(M, M, W - 2 * M, 50, fill="#17324d", radius=8)
    pg.text(M + 16, M + 10, f"{month_label} Shift Calendar", 18, color="#ffffff", bold=True, max_w=W - 2 * M - 32)
    pg.text(M + 16, M + 33, name, 10.5, color="#dbe4ee", max_w=W - 2 * M - 32)
    # summary + legend
    y = M + 62
    pg.text(M, y, summary, 11, bold=True, max_w=W - 2 * M)
    y += 18
    x = M
    legend = [(t["name"] + (f"  {t['start']}\u2013{t['end']}" if t.get("start") and t.get("end") else ""),
               colors[i % 3]) for i, t in enumerate(shift_types)]
    legend.append(("Leave", colors[3]))
    for label, (bg, fg) in legend:
        wlab = min(_pdf_text_width(label, 9, True) + 16, 200)
        if x + wlab > W - M:
            break
        pg.rect(x, y, wlab, 15, fill=bg, radius=7)
        pg.text(x + wlab / 2, y + 3.5, label, 9, color=fg, bold=True, align="center", max_w=wlab - 10)
        x += wlab + 6
    pg.text(x + 2, y + 3.5, "Off = no shift", 9, color="#687684")
    y += 24
    # weekday header pills (matches the print stylesheet's dark weekday header)
    gap = 4
    col_w = (W - 2 * M - 6 * gap) / 7.0
    for i, dname in enumerate(DOWS):
        cx = M + i * (col_w + gap)
        pg.rect(cx, y, col_w, 18, fill="#1e293b", radius=4)
        pg.text(cx + col_w / 2, y + 4.5, dname, 9.5, color="#ffffff", bold=True, align="center")
    y += 18 + gap
    # month grid
    lead = date(year, month, 1).isoweekday() % 7
    total = calendar.monthrange(year, month)[1]
    cells = [None] * lead + list(range(1, total + 1))
    cells += [None] * (-len(cells) % 7)
    n_rows = len(cells) // 7
    footer_h = 16
    row_h = min(78, (H - M - footer_h - y - (n_rows - 1) * gap) / n_rows)
    mon3 = MONTHS[month - 1][:3]
    for r in range(n_rows):
        for i in range(7):
            d = cells[r * 7 + i]
            if d is None:
                continue
            cx, cy = M + i * (col_w + gap), y + r * (row_h + gap)
            weekend = i in (0, 6)
            pg.rect(cx, cy, col_w, row_h, fill="#fcfaf4" if weekend else "#ffffff", stroke="#8a8578", radius=5, line=0.6)
            pg.text(cx + 6, cy + 5, f"{mon3} {d}", 9, color="#17212b", bold=True)
            v = days.get(d)
            chip_y, chip_h = cy + 20, min(30, row_h - 26)
            if v == "__leave__":
                bg, fg = colors[3]
                pg.rect(cx + 5, chip_y, col_w - 10, chip_h, fill=bg, stroke="#000000", radius=4, line=0.3)
                pg.text(cx + col_w / 2, chip_y + chip_h / 2 - 5, "Leave", 10, color=fg, bold=True, align="center")
            elif v:
                t = by_id[v]
                bg, fg = colors[type_order.index(v) % 3]
                pg.rect(cx + 5, chip_y, col_w - 10, chip_h, fill=bg, stroke="#000000", radius=4, line=0.3)
                times = f"{t['start']}\u2013{t['end']}" if t.get("start") and t.get("end") else ""
                if times and chip_h >= 24:
                    pg.text(cx + col_w / 2, chip_y + 4, t["name"], 10, color=fg, bold=True, align="center", max_w=col_w - 16)
                    pg.text(cx + col_w / 2, chip_y + 16, times, 8, color=fg, align="center", max_w=col_w - 16)
                else:
                    pg.text(cx + col_w / 2, chip_y + chip_h / 2 - 5, t["name"], 10, color=fg, bold=True,
                            align="center", max_w=col_w - 16)
            else:
                pg.text(cx + col_w / 2, chip_y + chip_h / 2 - 4, "Off", 9, color="#8793a0", align="center")
    pg.text(M, H - M - 10, f"Sent by Schedule Master \u00b7 {name} \u00b7 {month_label}", 8, color="#8793a0", max_w=W - 2 * M)
    return pg.to_pdf(title=f"{month_label} shifts - {name}")


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


def open_smtp():
    """One SMTP connection, reused for every recipient in a batch rather than reconnecting each time."""
    server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
    server.ehlo()
    if SMTP_USE_TLS:
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    if SMTP_USERNAME and SMTP_PASSWORD:
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
    return server


def send_via(server, to_addr, subject, text_body, html_body, attachments=None):
    """attachments: optional list of (filename, maintype, subtype, bytes)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_ADDR))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for filename, maintype, subtype, data in (attachments or []):
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    server.send_message(msg, from_addr=SMTP_FROM_ADDR, to_addrs=[to_addr])


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
            pass_ok = hmac.compare_digest(password.encode("utf-8"), creds["password"].encode("utf-8"))
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
                addresses = read_emails()["emails"]
            except ValueError as e:
                self._send_json(400, {"error": f"emails.json is unreadable: {e}. Nothing was sent."})
                return
            if not addresses:
                self._send_json(200, {"sent": [], "skipped": [], "failed": []})
                return

            sent, skipped, failed = [], [], []
            to_send = []
            for name, addr in addresses.items():
                if EMAIL_RE.match(addr):
                    to_send.append((name, addr))
                else:
                    skipped.append({"name": name, "reason": "invalid email address"})

            server = None
            if to_send:
                try:
                    server = open_smtp()
                except Exception as e:
                    self._send_json(502, {"error": f"could not connect to the mail relay: {e}"})
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
                        send_via(server, addr, subject, text_body, html_body, attachments)
                    except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused, OSError):
                        server = open_smtp()  # relay dropped the connection mid-batch - reconnect once and retry
                        send_via(server, addr, subject, text_body, html_body, attachments)
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
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Schedule Master server.")
        server.shutdown()


if __name__ == "__main__":
    main()
