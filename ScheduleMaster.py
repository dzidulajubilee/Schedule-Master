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
                        "leave":{"Full Name":["YYYY-MM-DD", ...], ...},
                        "palette":"ocean"}. "palette" is whichever calendar color scheme is active
                        on screen when Send is clicked (see PALETTE_COLORS below) - it decides the
                        colors used in the attached PDF, the same way it decides what's on screen.
                        Sends one individual email per saved address (nobody sees anyone else's
                        schedule) through the SMTP relay configured below. Each email carries the
                        schedule as plain text plus two attachments: a one-page PDF (colored to
                        match the active scheme) and a .ics calendar file the person can import.
                        Returns {"sent":[names], "skipped":[{"name":...,"reason":...}],
                        "failed":[{"name":...,"error":...}]}.

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
Run this file (or double-click Start ScheduleMaster.bat), then a browser tab opens
automatically at ScheduleMaster.html. Default login is admin / admin — see login.json.
"""
import calendar
import http.cookies
import http.server
import json
import os
import re
import secrets
import smtplib
import socket
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

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

def _safe_filename_part(s):
    s = re.sub(r'[\\/:*?"<>|]+', "_", str(s)).strip(" ._")
    return s or "person"


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# ---------- calendar color schemes, mirrored from ScheduleMaster.html ----------
# The page itself already solves "which colors survive being put on paper": its @media print rule
# flattens every pastel, on-screen-only scheme (classic/ocean/sunset/forest/berry/slate) to one
# fixed, high-contrast set, because pale on-screen tints wash out once printed - while the "deep"
# family is already saturated enough to print exactly as chosen. An emailed PDF is paper too, so
# the same rule is reused here rather than inventing a second color story: whichever scheme is
# active in the browser decides which of these two cases applies, but a pastel scheme's own hue
# never appears in the PDF - only the deep family's hues do. chip1/chip2/chip3 line up with a shift
# type's position in shiftTypes (index % 3), exactly as the on-screen legend does.
_PRINT_SAFE = {"chip1": ("#0d3fb0", "#b9cdf7"), "chip2": ("#8a4400", "#f0c98a"),
               "chip3": ("#3a1f99", "#cabdf0"), "leave": ("#8f1710", "#f2b3ac")}
PALETTE_COLORS = {
    "classic": _PRINT_SAFE, "ocean": _PRINT_SAFE, "sunset": _PRINT_SAFE,
    "forest": _PRINT_SAFE, "berry": _PRINT_SAFE, "slate": _PRINT_SAFE,
    "deep": {"chip1": ("#eaf2ff", "#123a66"), "chip2": ("#fff2df", "#7a4416"),
             "chip3": ("#f5ecff", "#4a2170"), "leave": ("#ffffff", "#e8514f")},
    "deep-bold": {"chip1": ("#ffffff", "#3b76e8"), "chip2": ("#000000", "#e8960b"),
                  "chip3": ("#ffffff", "#16a34a"), "leave": ("#ffffff", "#e8514f")},
    "deep-emerald": {"chip1": ("#e9fff5", "#0f5c42"), "chip2": ("#ffe9f7", "#7a1a56"),
                      "chip3": ("#eef0ff", "#2c2f7a"), "leave": ("#ffffff", "#e8514f")},
    "deep-crimson": {"chip1": ("#ffeef0", "#8c1e2b"), "chip2": ("#fff5e6", "#7a4a0f"),
                      "chip3": ("#eaf2ff", "#1f3a5f"), "leave": ("#ffffff", "#e8514f")},
}


def palette_colors_for(palette_id):
    return PALETTE_COLORS.get(palette_id, PALETTE_COLORS["classic"])


def person_entries(name, shift_types, shifts, leave):
    """(date_str, weekday_abbr, label, color_key) rows for one person, sorted by date.
    color_key is "chip1"/"chip2"/"chip3" (shift_types index % 3, same rule as the on-screen
    legend) or "leave" - used to pick a color and, for the PDF, to look it up in PALETTE_COLORS."""
    index_by_id = {t["id"]: i for i, t in enumerate(shift_types)}
    by_id = {t["id"]: t for t in shift_types}
    entries = []
    for ds, day_shifts in shifts.items():
        shift_id = day_shifts.get(name)
        if not shift_id:
            continue
        t = by_id.get(shift_id)
        label = f"{t['name']} ({t['start']}\u2013{t['end']})" if t else shift_id
        color_key = f"chip{(index_by_id.get(shift_id, 0) % 3) + 1}"
        entries.append((ds, label, color_key, t))
    for ds in leave.get(name, []):
        entries.append((ds, "Leave", "leave", None))
    entries.sort(key=lambda e: e[0])
    out = []
    for ds, label, color_key, t in entries:
        y, m, d = (int(x) for x in ds.split("-"))
        weekday = date(y, m, d).strftime("%a")
        out.append((ds, weekday, label, color_key, t))
    return out


# ---------- minimal, dependency-free PDF writer ----------
# Just enough of the PDF spec (uncompressed content streams, core Helvetica font, a manual xref
# table) to lay out a colored one-page-or-more schedule - no reportlab or other third-party
# package needed, matching the "standard library only" promise at the top of this file.
_PDF_PAGE_W, _PDF_PAGE_H = 842, 595  # A4 landscape, points - room for 7 calendar columns
_PDF_MARGIN = 42


def _pdf_escape(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_text_bytes(s):
    # core fonts only support single-byte encodings; WinAnsiEncoding (~cp1252) covers the en/em
    # dashes this file uses. Anything further outside it (e.g. an unusual name) is dropped rather
    # than corrupting the PDF.
    return s.encode("cp1252", "replace")


def _hex_rgb01(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class _SimplePDF:
    def __init__(self):
        self.pages = []  # list of content-stream strings, one per page

    def new_page(self):
        self.pages.append([])
        return self.pages[-1]

    @staticmethod
    def _line(ops, cmd):
        ops.append(cmd)

    def rect(self, ops, x, y, w, h, hex_bg):
        r, g, b = _hex_rgb01(hex_bg)
        self._line(ops, f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def rect_stroke(self, ops, x, y, w, h, hex_line, width=0.6):
        r, g, b = _hex_rgb01(hex_line)
        self._line(ops, f"{width} w {r:.3f} {g:.3f} {b:.3f} RG {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    def text(self, ops, x, y, size, hex_ink, s, bold=False):
        r, g, b = _hex_rgb01(hex_ink)
        font = "/F2" if bold else "/F1"
        raw = _pdf_text_bytes(_pdf_escape(s)).decode("latin-1")
        self._line(ops, f"{r:.3f} {g:.3f} {b:.3f} rg BT {font} {size} Tf {x:.2f} {y:.2f} Td ({raw}) Tj ET")

    def text_centered(self, ops, cx, y, size, hex_ink, s, bold=False):
        """Core Helvetica isn't monospace, but a flat per-character average (a touch wider for
        the bold face) centers short labels - day names, pill captions - closely enough."""
        avg = size * (0.62 if bold else 0.56)
        self.text(ops, cx - (avg * len(s)) / 2, y, size, hex_ink, s, bold=bold)

    def build(self):
        n_pages = len(self.pages) or 1
        font_regular, font_bold = 3, 4
        first_page_obj = 5
        first_content_obj = first_page_obj + n_pages
        objects = {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [%s] /Count %d >>" % (
                " ".join(f"{first_page_obj + i} 0 R" for i in range(n_pages)), n_pages),
            font_regular: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            font_bold: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        }
        for i in range(n_pages):
            objects[first_page_obj + i] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PDF_PAGE_W} {_PDF_PAGE_H}] "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                f"/Contents {first_content_obj + i} 0 R >>"
            )
        for i, ops in enumerate(self.pages or [[]]):
            stream = "\n".join(ops)
            data = stream.encode("latin-1", "replace")
            objects[first_content_obj + i] = f"<< /Length {len(data)} >>\nstream\n{stream}\nendstream"
        out = bytearray(b"%PDF-1.4\n")
        offsets = {}
        max_obj = max(objects)
        for num in range(1, max_obj + 1):
            offsets[num] = len(out)
            out += f"{num} 0 obj\n{objects.get(num, '<< >>')}\nendobj\n".encode("latin-1", "replace")
        xref_at = len(out)
        out += f"xref\n0 {max_obj + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for num in range(1, max_obj + 1):
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF".encode()
        return bytes(out)


_PDF_BANNER = "#17324d"      # neutral chrome, not tied to any color scheme - like a folder cover
_PDF_BANNER_SUBTLE = "#dbe4ee"
_PDF_OFF_BG = "#faf7f0"
_PDF_GRID_LINE = "#dedad2"
_PDF_INK = "#221f1a"
_PDF_MUTED = "#726d63"
_WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def build_shift_pdf(name, year, month0, month_label, shift_types, shifts, leave, palette_id):
    """A one-page month grid, laid out the way Schedule Master's own on-screen/printed calendar
    is laid out (and the way a monthly shift calendar reads generally) - a banner with the month
    and the person's name, a shift-count summary, a legend, then a 7-column Sun-Sat grid with one
    cell per day. Colors still come from PALETTE_COLORS (the active on-screen scheme governs shift
    and leave colors, per the print-safe rule); the banner itself is a fixed neutral navy, since
    it's page chrome rather than a shift indicator."""
    colors = palette_colors_for(palette_id)
    month = month0 + 1
    by_id = {t["id"]: t for t in shift_types}
    index_by_id = {t["id"]: i for i, t in enumerate(shift_types)}
    days_in_month = calendar.monthrange(year, month)[1]
    first_dow = (date(year, month, 1).weekday() + 1) % 7  # Sun=0 .. Sat=6
    weeks = -(-(first_dow + days_in_month) // 7)  # ceil

    day_status = {}  # day -> ("shift", shift_id) | ("leave", None) | ("off", None)
    counts = {}      # shift_id -> count, in shift_types order
    for d in range(1, days_in_month + 1):
        ds = f"{year:04d}-{month:02d}-{d:02d}"
        shift_id = shifts.get(ds, {}).get(name)
        if shift_id and shift_id in by_id:
            day_status[d] = ("shift", shift_id)
            counts[shift_id] = counts.get(shift_id, 0) + 1
        elif ds in leave.get(name, []):
            day_status[d] = ("leave", None)
        else:
            day_status[d] = ("off", None)

    pdf = _SimplePDF()
    ops = pdf.new_page()

    # ---- banner ----
    banner_h = 68
    pdf.rect(ops, 0, _PDF_PAGE_H - banner_h, _PDF_PAGE_W, banner_h, _PDF_BANNER)
    pdf.text(ops, _PDF_MARGIN, _PDF_PAGE_H - 30, 19, "#ffffff", f"{month_label} Shift Calendar", bold=True)
    pdf.text(ops, _PDF_MARGIN, _PDF_PAGE_H - 50, 12, _PDF_BANNER_SUBTLE, name)

    # ---- summary line ----
    total_shifts = sum(counts.values())
    count_bits = [f"{counts[t['id']]} {t['name']}" for t in shift_types if counts.get(t["id"])]
    summary = f"{total_shifts} shift(s): " + ", ".join(count_bits) if count_bits else "No shifts scheduled"
    y = _PDF_PAGE_H - banner_h - 24
    pdf.text(ops, _PDF_MARGIN, y, 12.5, _PDF_INK, summary, bold=True)

    # ---- legend line ----
    y -= 18
    bits = []
    for t in shift_types:
        overnight = _overnight(t.get("start"), t.get("end"))
        bits.append(f"{t['name']} = {t.get('start', '?')}-{t.get('end', '?')}" + (" (next morning)" if overnight else ""))
    bits += ["Leave = day off", "Off = no shift"]
    pdf.text(ops, _PDF_MARGIN, y, 8.5, _PDF_MUTED, "     ".join(bits))

    # ---- grid ----
    grid_top = y - 20
    grid_bottom = _PDF_MARGIN
    grid_w = _PDF_PAGE_W - 2 * _PDF_MARGIN
    header_h = 22
    col_w = grid_w / 7
    row_h = (grid_top - grid_bottom - header_h) / weeks

    for c, wd in enumerate(_WEEKDAY_NAMES):
        x = _PDF_MARGIN + c * col_w
        pdf.rect(ops, x, grid_top - header_h, col_w, header_h, _PDF_BANNER)
        pdf.text_centered(ops, x + col_w / 2, grid_top - header_h + 7, 10, "#ffffff", wd, bold=True)

    d = 1
    for w in range(weeks):
        row_top = grid_top - header_h - w * row_h
        for c in range(7):
            x = _PDF_MARGIN + c * col_w
            cell_index = w * 7 + c
            in_month = first_dow <= cell_index < first_dow + days_in_month
            if in_month:
                status, shift_id = day_status[d]
                bg = _PDF_OFF_BG if status == "off" else "#ffffff"
                pdf.rect(ops, x, row_top - row_h, col_w, row_h, bg)
            pdf.rect_stroke(ops, x, row_top - row_h, col_w, row_h, _PDF_GRID_LINE)
            if not in_month:
                continue
            pdf.text(ops, x + 6, row_top - 13, 10, _PDF_INK, str(d), bold=True)
            if status == "off":
                pdf.text(ops, x + 6, row_top - row_h + 10, 9, _PDF_MUTED, "Off")
            else:
                if status == "leave":
                    ink, pbg, label, sub = *colors.get("leave", ("#ffffff", "#8f1710")), "LEAVE", None
                else:
                    key = f"chip{(index_by_id.get(shift_id, 0) % 3) + 1}"
                    ink, pbg = colors.get(key, (_PDF_INK, "#e5e5e5"))
                    t = by_id[shift_id]
                    label, sub = t["name"].upper(), f"{t.get('start', '')}-{t.get('end', '')}"
                pill_w = min(col_w - 8, max(col_w * 0.8, 6.2 * len(label) + 14))
                pill_x = x + (col_w - pill_w) / 2
                pill_y = row_top - row_h + (row_h * 0.42 if sub else row_h * 0.34)
                pdf.rect(ops, pill_x, pill_y, pill_w, 16, pbg)
                pdf.text_centered(ops, x + col_w / 2, pill_y + 5, 9, ink, label, bold=True)
                if sub:
                    pdf.text_centered(ops, x + col_w / 2, pill_y - 10, 7.5, _PDF_MUTED, sub)
            d += 1

    pdf.text(ops, _PDF_MARGIN, grid_bottom - 16, 8.5, _PDF_MUTED, "Sent by Schedule Master.")
    return pdf.build()


def _overnight(start, end):
    if not start or not end:
        return False
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    return (eh, em) <= (sh, sm)


def build_shift_ics(name, month_label, entries):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Schedule Master//EN", "CALSCALE:GREGORIAN"]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = re.sub(r"\W+", "", name) or "person"
    for ds, _weekday, _label, color_key, t in entries:
        y, m, d = (int(x) for x in ds.split("-"))
        if t and t.get("start") and t.get("end"):
            sh, sm = (int(x) for x in t["start"].split(":"))
            eh, em = (int(x) for x in t["end"].split(":"))
            start_dt = datetime(y, m, d, sh, sm)
            end_dt = datetime(y, m, d, eh, em)
            if end_dt <= start_dt:  # overnight shift, e.g. the default 19:00-07:00 "Night"
                end_dt += timedelta(days=1)
            dt_lines = [f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}", f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}"]
            summary = t["name"]
            uid_bit = t["id"]
        else:
            nd = date(y, m, d) + timedelta(days=1)
            dt_lines = [f"DTSTART;VALUE=DATE:{y:04d}{m:02d}{d:02d}",
                        f"DTEND;VALUE=DATE:{nd.year:04d}{nd.month:02d}{nd.day:02d}"]
            summary = "Leave"
            uid_bit = "leave"
        lines += ["BEGIN:VEVENT", f"UID:{ds}-{uid_bit}-{slug}@schedulemaster", f"DTSTAMP:{stamp}"]
        lines += dt_lines
        lines += [f"SUMMARY:{summary}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


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


def build_shift_email(name, month_label, entries):
    """Build (subject, plain-text body) for one person's slice of a month's shifts + leave."""
    subject = f"Your {month_label} shift schedule"
    if not entries:
        body = f"Hi {name},\n\nYou have no shifts or leave scheduled for {month_label}.\n"
        return subject, body
    lines = [f"Hi {name},", "", f"Your schedule for {month_label}:", ""]
    for ds, weekday, label, _color_key, _t in entries:
        lines.append(f"  {weekday}, {ds} \u2014 {label}")
    lines += ["", f"That's {len([e for e in entries if e[3] != 'leave'])} shift(s) this month.",
              "", "Your full schedule is also attached as a PDF, plus a calendar file (.ics)",
              "you can import into your phone or calendar app."]
    return subject, "\n".join(lines) + "\n"


def send_email(to_addr, subject, body, attachments=()):
    """attachments: iterable of (filename, mime_subtype, bytes) - e.g. ("shifts.pdf", "pdf", data)."""
    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for filename, subtype, data in attachments:
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_ADDR))
    msg["To"] = to_addr
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM_ADDR, [to_addr], msg.as_string())


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
        super().do_GET()

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
            if username.lower() == creds["username"].lower() and password == creds["password"]:
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
            month_label = str(req.get("monthLabel") or "this month")
            shift_types = req.get("shiftTypes") or []
            shifts = req.get("shifts") or {}
            leave = req.get("leave") or {}
            palette_id = str(req.get("palette") or "classic")
            today = date.today()
            try:
                year = int(req.get("year"))
            except (TypeError, ValueError):
                year = today.year
            try:
                month0 = int(req.get("month"))  # 0-indexed, matching the browser's Date convention
                assert 0 <= month0 <= 11
            except (TypeError, ValueError, AssertionError):
                month0 = today.month - 1
            try:
                addresses = read_emails()["emails"]
            except ValueError as e:
                self._send_json(400, {"error": f"emails.json is unreadable: {e}. Nothing was sent."})
                return
            if not addresses:
                self._send_json(200, {"sent": [], "skipped": [], "failed": []})
                return

            sent, skipped, failed = [], [], []
            for name, addr in addresses.items():
                if not EMAIL_RE.match(addr):
                    skipped.append({"name": name, "reason": "invalid email address"})
                    continue
                try:
                    entries = person_entries(name, shift_types, shifts, leave)
                    subject, mail_body = build_shift_email(name, month_label, entries)
                    pdf_bytes = build_shift_pdf(name, year, month0, month_label, shift_types, shifts, leave, palette_id)
                    ics_bytes = build_shift_ics(name, month_label, entries)
                    stem = f"Shifts_{year:04d}-{month0 + 1:02d}_{_safe_filename_part(name)}"
                    send_email(addr, subject, mail_body, attachments=[
                        (f"{stem}.pdf", "pdf", pdf_bytes),
                        (f"{stem}.ics", "ics", ics_bytes),
                    ])
                    sent.append(name)
                except Exception as e:
                    failed.append({"name": name, "error": str(e)})
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
