# Schedule Master

Schedule Master is a single-file HTML/JavaScript app for building and validating a monthly staff shift roster. It runs entirely in the browser — no build step, no backend required — and optionally talks to a small local server for shared `staff.json` storage.

Open `ScheduleMaster.html` directly in a browser (double-click it, or `xdg-open ScheduleMaster.html` / `firefox ScheduleMaster.html` from a terminal — it is a web page, not an executable script).

---

## 1. What it does

- Holds a **staff list**, a set of **shift types** (e.g. Day / Night) with weekday/weekend headcount rules, optional **keep-apart pairs**, and per-person **leave**.
- Can **Auto Plot** an entire month automatically, respecting all the coverage and rest rules below.
- Supports **Manual Plot** for one person across one or more days at a time, picked from a small click-to-select calendar, with the same rules enforced live.
- Shows a live **calendar**, a **workload & validation** table, and a **coverage check** panel.
- **Prints** a clean roster (and optionally the coverage check) to PDF/paper.
- Persists everything to the browser's `localStorage`; staff can also be exported/imported as `staff.json`, or synced with a tiny local server if one is running.

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
| `expShifts`, `restHrs`, `rotation`, `requireAll`, `weekendLeaveNoQuota` | Global planning settings |

### Shift type fields
- **Weekday target** — headcount Auto Plot aims for on a weekday.
- **Weekday min** — the lowest it may sag to on a weekday, only if **Flex** is on.
- **Weekend target** — headcount on a weekend; weekends are always strict, so this doubles as the weekend minimum.
- **Flex** — allows the weekday dip described above.
- **Keep Apart** — a keep-apart pair is blocked from sharing this shift even on a weekday (normally keep-apart only applies strictly on weekends).

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

---

## 4. The Auto Plot algorithm

Auto Plot's job: produce one full month's roster that satisfies the hard rules above and is as *fair* and *evenly rotated* as possible. It does this by **generating several candidate plans and scoring them**, rather than solving it as a single deterministic pass — the search space (who works what, when) is too large to solve exactly, so it uses a randomized construction + scoring + refinement approach (a form of stochastic local search).

### Step-by-step

1. **Pick rotation patterns to try.**
   - If rotation is "Auto": try `3 on/3 off`, `2 on/2 off`, and a `mix` of both (random per block).
   - If a pattern is fixed by the user, only that one is tried.

2. **Seed carryover from last month.** For every person, look at the last day of the *previous* month to see if they were mid-block or mid-rest, so a block that started in the last few days of last month correctly continues (or a rest period correctly continues) into this month.

3. **Build one candidate plan** (`buildOnePlan`), in three phases:
   - **Phase 1 — Minimum cover.** Walk the month day by day. Strict (non-flex) shift types are filled before flexible ones. For each shift type each day:
     - First, let anyone already mid-block continue their run.
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
    A[Auto Plot clicked] --> B[Pick rotation patterns to try:<br/>3on/3off, 2on/2off, mix — or the fixed one]
    B --> C[For each pattern: run N attempts]
    C --> D[Seed carryover from last day<br/>of previous month]
    D --> E[Phase 1: Minimum cover<br/>day by day, strict types first]
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
    M --> N[Report pattern, score,<br/>and any remaining gaps]
```

---

## 6. Persistence & data flow

- **Browser-only mode**: everything (staff, shift types, leave, shifts, settings) is saved to `localStorage` under the key `planthat_state`. "Save" downloads `staff.json`; "Reload" re-imports a `staff.json` file.
- **Server mode**: if a local server responding at `api/staff` is found on load, staff are loaded from and saved back to it directly (`GET`/`PUT api/staff`), so multiple people can share the same `staff.json` on disk.
- **Reset all local data** wipes shifts, leave, staff and settings from *this browser only* — it does not touch `staff.json` on disk.

---

## 7. Printing

Print produces one calendar page per month plus an optional coverage-check page, using a landscape layout that auto-shrinks (via CSS `zoom`, which reflows layout so measurements stay accurate) to fit one page, and stretches rows to fill any leftover vertical space. A separate, more saturated color palette is swapped in for print so shift colors don't wash out on paper.
