# Schedule Master

Schedule Master is a single-file HTML/JavaScript app for building and validating a monthly staff shift roster, paired with a small Python server (`ScheduleMaster.py`, standard library only — no packages to install) that adds a sign-in screen, shared `staff.json`/`records.json`/`emails.json` storage, and the ability to email each person their own shifts. It can be run two ways:

- **Personal / local use** — double-click or run `ScheduleMaster.py` on your own machine; it opens a browser tab pointed at itself. See *How to run* below.
- **Shared / production use** — deploy it on a small Linux VM with `install.sh`, which puts it behind nginx and https, running as a systemd service so the whole team can reach it. See *Deploying with install.sh* (§11) below.

Either way, the app itself always talks to the same `ScheduleMaster.py` server — there's no truly offline, server-less mode anymore (see *Login & access* below).

---

## How to run (personal / local use)

1. Make sure Python 3 is installed: `python3 --version`.
2. Open a terminal *in the folder* containing `ScheduleMaster.py`, `ScheduleMaster.html`, and `staff.json` (they all need to sit together).
3. Make the server script executable — you only need to do this once:
   ```
   chmod +x ScheduleMaster.py
   ```
4. Run it:
   ```
   ./ScheduleMaster.py
   ```
5. A terminal message confirms the server is running and a browser tab opens automatically at `http://127.0.0.1:8743/ScheduleMaster.html` (or the next free port, if 8743 is taken). A sign-in screen appears first (see *Login & access*) — the default is **admin / admin** until you set your own. Once signed in, staff, saved records, and saved email addresses are read from and written directly to `staff.json` / `records.json` / `emails.json` in that folder.
6. To stop the server, go back to the terminal and press `Ctrl+C`.

Next time, just repeat steps 4 and 5 — step 3 only needs doing once per copy of the files.

**Windows note:** `chmod`/`./` are Unix-only. On Windows, run `python ScheduleMaster.py` instead (or double-click the file if `.py` files are associated with Python) — everything else above is the same.

**Don't open `ScheduleMaster.html` directly** (double-clicking it, or dragging it into a browser) — the page only works when loaded *from* the server's own address (`http://127.0.0.1:...`), not opened as a local `file://` page: without the server behind it there's nothing to sign in against. Always start from the terminal as above.

---

## Login & access

Every visit starts at a sign-in screen; there's no way to reach the app without it.

- **Default credentials** are `admin` / `admin` until you create `login.json` next to `ScheduleMaster.py`:
  ```json
  { "username": "admin", "password": "your new password" }
  ```
  This is read fresh on every sign-in attempt — no restart needed after changing it.
- **Sessions** last 30 days (a cookie in the browser), so you're not asked again on every visit.
- **Lockout**: 5 wrong attempts in a row from the same address locks it out for 15 minutes.
- Signing out (the **Log out** button) clears the session immediately.
- This gate covers the whole app and every `/api/*` route except `/api/ping` and `/api/session` themselves (which is how the page knows whether to show the sign-in screen in the first place).

---

## 1. What it does

- Holds a **staff list**, a set of **shift types** (e.g. Day / Night) with weekday/weekend headcount rules, optional **keep-apart pairs**, and per-person **leave**.
- Can **Auto Plot** an entire month automatically, respecting all the coverage and rest rules below — optionally forced to open the month with up to 4 named **compulsory starters** (see §9).
- Supports **Manual Plot** for one person across one or more days at a time, picked from a small click-to-select calendar, with the same rules enforced live.
- Shows a live **calendar**, a **workload & validation** table, and a **coverage check** panel.
- **Prints** a clean roster (and optionally the coverage check) to PDF/paper.
- Lets you **save a labeled snapshot** of the current month — staff, shift types, keep-apart pairs, leave, that month's shifts, and settings — as a standalone record you can look back on or delete later, kept separate from the live data (see §8).
- Lets you pick one of **10 calendar color schemes** (a row of swatches next to the calendar) — purely cosmetic, but it's what colors the on-screen calendar, the printed page, *and* the emailed PDF (see *Color schemes* and §10).
- Can **email each person their own shifts** for the month — nobody sees anyone else's — with a PDF and an importable calendar (`.ics`) file attached (see §10).
- Sits behind a **sign-in screen** (see *Login & access*) once `ScheduleMaster.py` is running.
- Persists staff, records, saved email addresses, and login credentials to `staff.json` / `records.json` / `emails.json` / `login.json` on the server; the current month's shifts, leave, and settings live only in the browser's `localStorage` (see §6).

---

## 2. Data model

Everything lives in a single in-memory state object, `S`, saved to `localStorage` on every change:

| Field | Meaning |
|---|---|
| `staff` | Array of staff names |
| `shiftTypes` | Array of `{id, name, start, end, wdTarget, wdMin, weTarget, flex, ka}` |
| `keepApart` | Pairs of names that should never share a shift |
| `leave` | `{ personName: ["YYYY-MM-DD", ...] }` |
| `shifts` | `{ "YYYY-MM-DD": { personName: shiftTypeId } }` |
| `palette` | Selected calendar color scheme id (default `"classic"`) — see *Color schemes* |
| `expShifts`, `restHrs`, `rotation`, `requireAll`, `weekendLeaveNoQuota` | Global planning settings |

### Shift type fields
- **Weekday target** — headcount Auto Plot aims for on a weekday.
- **Weekday min** — the lowest it may sag to on a weekday, only if **Flex** is on.
- **Weekend target** — headcount on a weekend; weekends are always strict, so this doubles as the weekend minimum.
- **Flex** — allows the weekday dip described above.
- **Keep Apart** — a keep-apart pair is blocked from sharing this shift even on a weekday (normally keep-apart only applies strictly on weekends).

---

## Color schemes

A row of swatch buttons next to the calendar switches between 10 color schemes: **Classic** (the default), **Ocean**, **Sunset**, **Forest**, **Berry**, **Slate**, **Deep**, **Deep Bold**, **Deep Emerald**, and **Deep Crimson**. The choice is purely cosmetic — it never affects rules or planning — and is saved per browser (in `S.palette`, `localStorage`), not shared between people.

- Whichever shift type sits at position 1, 4, 7… in the shift-types list gets the scheme's first color; position 2, 5, 8… gets the second; position 3, 6, 9… gets the third. Leave always gets the scheme's own dedicated color, separate from the three shift-type colors.
- **On screen**, all 10 schemes show their own distinct pastel or bold tones.
- **On paper** — both the printed calendar (§7) and the emailed PDF (§10) — the six pastel schemes (Classic/Ocean/Sunset/Forest/Berry/Slate) are all flattened to one fixed, high-contrast set instead of their on-screen pastel tones, because pale tints wash out badly once printed. The four "deep" schemes are already saturated enough to survive printing, so they print and PDF exactly as chosen on screen. Practically: pick any of the first six for a nice look on a monitor, or one of the "deep" four if you specifically want the printed/emailed version to look distinct from the others.

---

## 3. Rules the system enforces

1. **Minimum cover** — every shift type must meet its minimum headcount (weekday-min-if-flex, or target-if-not-flex; weekend target) every day.
2. **No 24-hour flip** — a person can't work a Day shift then a Night shift (or vice versa) on adjacent days; once on a shift type, they stay on it until their block ends.
3. **Minimum rest** — the gap between the end of one shift and the start of the next (for the same person) must be at least `restHrs` hours.
4. **Run length** — nobody works more than 3 consecutive calendar days on a shift type before a break (used both as a soft rotation pattern and a hard penalty).
5. **Keep-apart pairs** — never share a shift on a weekend, or on any shift flagged Keep Apart; sharing an ordinary weekday shift is allowed but flagged, and Manual Plot asks for confirmation.
6. **Quota** — each person's total shifts for the month should not exceed `expShifts` minus their leave days that month (weekend leave days are excluded from the deduction if "Weekend leave doesn't reduce expected shifts" is on).
7. **Shift-type variety** — if "Every person needs every shift type" is on, someone who worked at all that month should get at least one of each shift type, not just one.
8. **Leave blocks shifts** — a person on leave can never be placed that day; saving leave immediately clears any shift already on those dates.
9. **Weekend leave auto-fill** — if leave spans a weekend (leave either side of Saturday/Sunday), the weekend itself is automatically added as leave too.
10. **Compulsory starters** — up to 4 named people can be forced onto a chosen shift on day 1 of the month; Auto Plot seeds them in before minimum cover runs, so they count toward it automatically, then they're treated as ordinary people for the rest of the month (see §9).

---

## 4. The Auto Plot algorithm

Auto Plot's job: produce one full month's roster that satisfies the hard rules above and is as *fair* and *evenly rotated* as possible. It does this by **generating several candidate plans and scoring them**, rather than solving it as a single deterministic pass — the search space (who works what, when) is too large to solve exactly, so it uses a randomized construction + scoring + refinement approach (a form of stochastic local search).

### Step-by-step

1. **Pick rotation patterns to try.**
   - If rotation is "Auto": try `3 on/3 off`, `2 on/2 off`, and a `mix` of both (random per block).
   - If a pattern is fixed by the user, only that one is tried.

2. **Seed carryover from last month.** For every person, look at the last day of the *previous* month to see if they were mid-block or mid-rest, so a block that started in the last few days of last month correctly continues (or a rest period correctly continues) into this month.

3. **Build one candidate plan** (`buildOnePlan`), in four phases:
   - **Phase 0 — Compulsory starters.** Anyone set in §9 (and not on leave day 1) is force-placed on their chosen shift on day 1, then seeded a normal rotation-length block from there — the same mechanism used to continue a carried-over run. This runs before Phase 1, so they count toward day 1's minimum cover automatically.
   - **Phase 1 — Minimum cover.** Walk the month day by day. Strict (non-flex) shift types are filled before flexible ones. For each shift type each day:
     - First, let anyone already mid-block (including a Phase 0 compulsory starter) continue their run.
     - Then pick more people from those who are free, under quota, resting-eligible, and keep-apart-safe — preferring whoever has the most spare quota (with a small randomized tie-break, and a slight bias toward whichever shift type they've had less of so far) — until the day's minimum is met.
   - **Phase 2 — Top up spare quota.** For anyone still under their personal quota, try to place additional full rotation-length blocks (preferring a slot with a full rest gap on both sides, falling back to a 1-day gap only if nothing fully-rested exists), shrinking the block size if a person's remaining quota is smaller than one full block.
   - **Phase 3 — Even things out.** Repeatedly find whoever is most *over* their fair share and whoever has the most *room*, and move a single flexible-shift day between them — but only at the edge of a block or a lone day, never splitting a block in half, and only if it doesn't break rest, keep-apart, or run-length rules.

4. **Score the plan** (`scorePlan`) — lower is better. Heavy penalties for:
   - Any day/shift under its **minimum** (very large penalty — this should basically never happen if staffing is adequate).
   - Under **target** but above minimum (medium penalty).
   - Over target (small penalty, discourages needless over-staffing).
   - A hard keep-apart clash (weekend, or a Keep Apart–flagged shift).
   - A soft keep-apart clash on an ordinary weekday (smaller penalty).
   - A run longer than 3 consecutive days, or a shift-type flip between two consecutive worked days.
   - Missing a required shift type for someone who worked that month (if "every person needs every shift type" is on).
   - **Unevenness**: the spread (max − min) and variance of "quota left unused" across staff — this is weighted heavily so the optimizer actively favors an even spread of total shifts.
   - A secondary, smaller penalty for an uneven personal split between shift types (e.g. mostly Day vs mostly Night) — a tie-breaker under the total-quota fairness above.

5. **Repeat and pick the best.** For each rotation pattern, build several candidate plans (attempts) and keep the lowest-scoring one. Compare the best of each pattern, then run more attempts on the overall best pattern to refine it further.

6. **Commit.** The winning plan replaces the month's shifts. The status line reports the pattern used, its score, and how many minimum-coverage/keep-apart issues (if any) remain unresolved — a non-zero count means staffing is too tight to fully satisfy every rule.

### Manual Plot
Manual Plot places one person on one or more days at once. Pick a person, click days on the mini calendar to select them (days on leave are grayed out and can't be selected; days that already have a shift show it as a small colored tag), choose a shift type (or "Off" to clear), then apply.

Each selected day runs the same live checks Auto Plot's scoring penalizes for: leave conflicts, shift-type flip vs. the day before/after, minimum rest, keep-apart, and quota — so a manual placement never silently breaks a rule Auto Plot would have avoided:
- **Hard blocks** (leave, a shift-type flip, insufficient rest, a hard keep-apart clash, or being over quota) are skipped for that day and listed in the status message; every other selected day is still applied.
- **Soft keep-apart clashes** (sharing an ordinary weekday shift with a keep-apart partner) are batched into a single confirmation dialog covering all affected days, rather than one prompt per day.
- "Select all" selects every day in the month that isn't a leave day for the chosen person; "Clear selection" deselects everything. The selection is also cleared automatically after applying, or when you switch person, month, or year.

---

## 5. Flow diagram

### Overall app flow

The diagram below picks up *after* the sign-in screen (see *Login & access*) — since that screen itself needs the server to check your session against, the "no" branch below is now mostly a leftover safety net for a server hiccup mid-session rather than a real day-to-day path.

```mermaid
flowchart TD
    subgraph Startup["Startup"]
        A[Load app] --> B{Local server<br/>reachable?}
        B -- yes --> C[Load staff.json<br/>from server]
        B -- no --> D[Load state from localStorage<br/>browser-only mode]
    end

    C --> E
    D --> E

    E[Render UI: Staff, Shift types,<br/>Keep apart, Leave, Calendar]
    E --> F{User action}

    subgraph Editing["Editing & Auto Plot"]
        F -- Add/edit staff<br/>or shift types --> R1[Update state]
        F -- Save leave --> R2[Update leave, clear any<br/>shifts on those dates]
        F -- Auto Plot --> R3[Run Auto Plot algorithm]
    end

    subgraph ManualPlot["Manual Plot"]
        direction TB
        F -- Manual Plot --> MP1[Select 1+ days on mini<br/>calendar, pick shift type]
        MP1 --> MP2[canPlaceShift checks each<br/>selected day: leave / flip /<br/>rest / keep-apart / quota]
        MP2 -- day blocked --> MPX[Skip that day,<br/>report reason]
        MP2 -- soft clash --> MP3[One batched confirm<br/>for all soft clashes]
        MP2 -- ok --> MP4[Place shift<br/>on that day]
        MP3 --> MP4
    end

    R1 --> E
    R2 --> E
    R3 --> E
    MPX --> E
    MP4 --> E

    E --> K[Workload & validation table]
    E --> L[Coverage check panel]

    subgraph Output["Output & persistence"]
        F -- Print --> M[Print / export PDF]
        F -- Save --> N[Persist to localStorage<br/>and/or push staff.json<br/>to server]
    end

    style Startup fill:none
    style Editing fill:none
    style ManualPlot fill:none
    style Output fill:none
```

### Auto Plot algorithm

```mermaid
flowchart TD
    A[Auto Plot clicked] --> A0{Compulsory starters<br/>valid? §9}
    A0 -- no --> A0X[Stop — report the<br/>conflict, plan nothing]
    A0 -- yes --> B[Pick rotation patterns to try:<br/>3on/3off, 2on/2off, mix — or the fixed one]
    B --> C[For each pattern: run N attempts]
    C --> D[Seed carryover from last day<br/>of previous month]
    D --> D2[Phase 0: force compulsory<br/>starters onto day 1<br/>leave conflicts skipped]
    D2 --> E[Phase 1: Minimum cover<br/>day by day, strict types first]
    E --> F[Phase 2: Top up spare quota<br/>in full/partial blocks, rested slots preferred]
    F --> G[Phase 3: Even out totals<br/>move single edge/lone days only]
    G --> H[Score the plan<br/>lower = better]
    H --> I{More attempts<br/>for this pattern?}
    I -- yes --> D
    I -- no --> J[Keep best plan for this pattern]
    J --> K{More patterns<br/>to try?}
    K -- yes --> C
    K -- no --> L[Refine overall best pattern<br/>with extra attempts]
    L --> M[Commit winning plan<br/>to this month's shifts]
    M --> N[Report pattern, score,<br/>starters used, leave skips,<br/>and any remaining gaps]
```

---

## 6. Persistence & data flow

Signing in requires `ScheduleMaster.py` to be running (see *Login & access*), so in practice there's no longer a fully offline, server-less mode — but not everything lives on the server:

- **Server-backed** (shared across everyone who signs in, read/written straight to disk next to `ScheduleMaster.py`): `staff.json` (`GET`/`PUT api/staff`), `records.json` (`GET`/`PUT api/records`, §8), `emails.json` (`GET`/`PUT api/emails`, §10), and `login.json` (credentials only, §*Login & access*).
- **Browser-only** (this device/browser only, never sent to the server except when you explicitly send shifts by email): the current month's `shifts`, `leave`, planning settings, and the selected color `palette` — saved to `localStorage` under the key `schedulemaster_state` (auto-migrated once from the old key `planthat_state` if found).
- **Reset all local data** wipes shifts, leave, and settings from *this browser only* — it does not touch `staff.json`, `records.json`, `emails.json`, or `login.json` on disk.

---

## 7. Printing

Print produces one calendar page per month plus an optional coverage-check page, using a landscape layout that auto-shrinks (via CSS `zoom`, which reflows layout so measurements stay accurate) to fit one page, and stretches rows to fill any leftover vertical space. As covered in *Color schemes* above, a separate, more saturated set of colors is swapped in for print for the six pastel schemes so shift colors don't wash out on paper — the "deep" schemes print as chosen. The emailed PDF (§10) follows this exact same rule.

---

## 8. Saved records

The **Saved records** panel lets you keep a permanent, labeled history of past rosters — entirely separate from the live `S` state everything else in the app reads and writes. Saving, viewing, and deleting a record never changes what's currently plotted, and plotting or editing the live roster never changes a saved record.

- **Save**: typing a label (optional — it defaults to "Month Year") and clicking "Save current month as a record" takes a full snapshot of that month — staff, shift types, keep-apart pairs, leave, that month's shifts, and the global settings (expected shifts, min rest, rotation, and the two checkboxes) — and adds it to the saved list.
- **View**: each record shows a one-line summary (staff/shift-type/placed-shift counts) plus a "View" button that expands the complete saved snapshot as raw JSON, so you can always refer back to exactly what was saved.
- **Delete**: each record has its own "Delete" button (with a confirmation prompt); deleting one only removes that record.
- **Storage**: records live in their own file, `records.json`, kept apart from `staff.json`, read from and written to it directly (`GET`/`PUT api/records`) since signing in requires the server to be reachable in the first place (see §6). If the server can't be reached after signing in, they fall back to `localStorage` under `schedulemaster_records` (auto-migrated once from the old key `planthat_records` if found), with "Export records.json" / "Import records.json" buttons to move them to/from a file by hand, mirroring how staff are exported/imported.

---

## 9. Compulsory starters

The **Compulsory starters** panel lets you name up to 4 people who must already be working a chosen shift on **day 1** of the month, before Auto Plot runs — useful when you know a specific opening lineup is required regardless of what Auto Plot would otherwise pick.

- **Scope: one-off, not a standing rule.** The slots apply only to the month shown when you set them. Switching month or year (via the calendar's own selectors) clears all 4 slots back to empty — they do **not** carry forward to the next month automatically.
- **Setting it up**: each of the up to 4 slots is a person + a shift type. Leaving a slot's person or shift empty just means that slot isn't active yet.
- **Validation, live in the panel**:
  - The same person picked in two slots → blocked (duplicate), shown as a warning under the slots.
  - Two people who are a keep-apart pair, both set to start on the *same* shift → blocked, same as anywhere else a keep-apart clash would happen.
  - These two are hard errors: Auto Plot refuses to run at all until they're fixed, and the panel tells you exactly what to change.
- **Leave on day 1**: not a hard error. If a compulsory starter is on leave on the 1st, Auto Plot skips forcing that person for day 1 and places them normally instead (leave always blocks a placement, for anyone) — and says so afterwards in the status line, e.g. *"X was on leave on the 1st — placed normally instead of forced."*
- **How Auto Plot uses it**: this runs as a new **Phase 0**, before minimum cover (see §4) — each active compulsory starter is force-placed on their chosen shift on day 1, then seeded a normal rotation-length block from there, so they count toward day 1's minimum headcount automatically rather than needing a separate carve-out. After day 1, they're treated as an ordinary person: quota, rest, keep-apart, and the fairness balancing in Phase 3 all still apply to them normally.
- **Carryover overrides**: if forcing someone onto day 1 would normally have meant more rest, or continuing a different shift type from the end of last month, the compulsory-starter rule wins — they're placed as requested — but the status line notes the override, e.g. *"X was mid-block on Night — starting them on Day instead overrides that."*

---

## 10. Sending shifts by email

The **Email** panel (§8 on screen) lets you save one address per person and, with one click, send everyone their own dates and shift times for the month currently shown — nobody sees anyone else's schedule.

- **Saved addresses** live in `emails.json` on the server (`GET`/`PUT api/emails`), separate from `staff.json` and `records.json`. Saving and sending both require `ScheduleMaster.py` to be running — the panel disables the Send button (with an explanatory tooltip) if it can't reach the server.
- **Send this month's shifts to everyone** asks for confirmation (naming how many emails will go out and which color scheme they'll be colored with), then sends one individual email per saved address through the SMTP relay configured in `ScheduleMaster.py` (`SMTP_HOST`/`SMTP_PORT`/etc. near the top of the file — defaults to a local Postfix relay on the same machine, no login required).
- **Each email contains three things:**
  1. A plain-text list of that person's dates and shifts for the month.
  2. A one-page **PDF month calendar** for that person — a Sun–Sat grid (the same layout as the on-screen/printed calendar, not a plain list), with a shift-count summary and legend, colored using whichever calendar scheme was active in the browser when you clicked Send — see *Color schemes* above for exactly how a scheme's colors carry over.
  3. A **calendar file (`.ics`)** the recipient can import into their phone or calendar app — one event per shift (correctly spanning midnight for overnight shifts like the default Night shift) plus an all-day event for each day of leave.
- **Both attachments are named after the recipient**, not a generic filename — `Shifts_YYYY-MM_Full Name.pdf` / `.ics` (e.g. `Shifts_2026-09_Dzidula Gati.pdf`).
- **After sending**, the panel reports how many emails were sent, and lists anyone **skipped** (no valid-looking email address on file) or **failed** (the relay rejected or couldn't be reached), each with a reason.
- The PDF is generated with a small dependency-free writer built into `ScheduleMaster.py` itself — no `reportlab` or other third-party package required, consistent with the rest of the app.

---

## 11. Deploying with install.sh (production / shared use)

For a team to share one always-on instance instead of everyone running it on their own laptop, `install.sh` sets Schedule Master up as a proper service on a Linux VM (Debian/Ubuntu or RHEL/Rocky/Alma/Fedora):

```
sudo bash install.sh                          # install/upgrade, https on this VM's own address(es)
sudo bash install.sh sched.lan 10.0.0.5        # also answer to these extra names/IPs (added to the cert)
sudo bash install.sh --allow any               # let addresses outside your private network reach it too
sudo bash install.sh --allow 41.66.0.0/16      # or just allow one specific extra range
sudo bash install.sh --uninstall               # remove the service and nginx site (your data stays put)
```

What it sets up:

- The app in `/opt/schedulemaster`, run by systemd as its own unprivileged user — starts at boot, restarts if it ever stops.
- **nginx** in front of it on ports 80/443 (80 redirects to https); the app itself only listens on `127.0.0.1:8766` and is never reachable directly.
- A **self-signed https certificate** for the VM's own address(es) — browsers show a one-time trust warning, which you can make permanent by installing the certificate (`/etc/ssl/schedulemaster/schedulemaster.crt`) as trusted, or clear for good with a real certificate of your own.
- By default nginx only accepts connections from private/loopback addresses (RFC1918 + localhost); `--allow` widens that, and the choice is remembered on future re-runs.
- Extra brute-force protection at the nginx layer on `/api/login` (rate-limited), on top of the app's own 5-attempt lockout (see *Login & access*).

Re-running the installer to upgrade is safe — `staff.json`/`records.json`/`emails.json`/`login.json` are never overwritten — but it **does** replace `ScheduleMaster.py` with the new version, which would undo any hand-edited `SMTP_*` mail-relay settings; keep a copy of those if you customize them, and re-apply them (then `sudo systemctl restart schedulemaster`) after upgrading.

Useful commands once installed:

```
systemctl status schedulemaster        # is it running (it starts by itself at boot)
journalctl -u schedulemaster -f        # live log (logins, emails sent)
systemctl restart schedulemaster       # restart after replacing ScheduleMaster.py/.html by hand
```
