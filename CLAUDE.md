# CLAUDE.md — strfry-86

Admin UI for a strfry Nostr relay. Rules only; every "do not simplify" argument lives in
`WHY.md` (never loaded as context). A rule here without its WHY entry is still a rule.

## Constants — `server86.py`, one block, every bound auditable in one place

```python
RENDER_MAX             = 500     # list rows rendered before truncation
FIGURE_HEAD_MAX        = 10      # ranked figure rows before tail summary
GAP_NOTICE_SHARE       = 0.02    # allowlist gap worth a sentence, never an alarm
KIND_ALARM_SHARE       = 0.005   # ONE unlisted kind this big = the actionable alarm
SUBSCRIBER_SCAN_LIMIT  = 50000   # saturation here REFUSES the exempt purge (WHY.md §5)
RECIPIENT_SCAN_LIMIT   = 250000  # permanently saturated; safe direction only (WHY.md §5)
CONSOLE_VERBS          = ("scan", "info", "export")  # --count/read-only; all else refused
```

## Rendering results — project-wide; every page and future page inherits this

WHY: endpoints return complete data; uncapped rendering buried the one number that matters
under 444 rows of long tail. Overview first, details on demand.

1. Every enumeration is capped (`RENDER_MAX` lists, `FIGURE_HEAD_MAX` figures). Tail is
   summarised with its total (`+ 434 more kinds, 9,564 events`), full data behind a
   `<details>` labelled with its count, one row per line, never comma-separated prose.
2. Numeric results render as semantic `<table>` (or `<dl>`; pick one project-wide), never
   in a sentence, never space-padded `<pre>`. `<pre>` is for shell commands only.
3. Every percentage names its denominator in its own row or block header.
4. Coverage stated positively first, gap second.
5. Tables handle their own width; shorten labels, never force horizontal scroll at 40em.
6. Four panel states — none may wear another's clothes:
   `never run` (cost line + button, visibly empty) · `running` (server figures only) ·
   `result` (age abs+rel, then figures) · `failed` (why + fix + PREVIOUS result at TRUE age).
   A failed run never stamps `scanned_at`, never replaces cache, never renders as an age.
   Cache records carry `error` (replaces a result) distinct from `warning` (accompanies one).
7. One message slot per panel, under the status line. Never echo a message twice.
8. Empty states name the cheapest press that fills them and its measured cost.
9. Progressive fill renders `—` for not-yet-known; `0` only when measured. Blank = never.

## Style block — locked, enumerated, reopened once for the header (do not grow it again)

```css
body { max-width:40em; margin:0 auto; padding:0 1em; }
li { overflow-wrap:anywhere; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; }   /* commands only — WHY.md §2 */
@media (max-width:600px){ body{ font-size:1.15em; } }
:root { color-scheme: light dark; }
body { background:Canvas; color:CanvasText; }           /* required — WHY.md §3 */
header#s86 { position:sticky; top:0; background:Canvas; display:flex;
             gap:1em; align-items:center; padding:.5em 0; }
header#s86 a.logo { text-decoration:none; color:inherit; font-weight:bold; }
header#s86 input#q { flex:1; min-width:8em; }
```

Duplicated character-for-character in every page's `<style>`. A page differing is a bug.

## Header — identical on every page, same positions, always

- `strfry-86` text logo, left: an `<a href="/home">` styled as plain text via `a.logo`.
- Search box `#q`, center, grows: accepts npub / 64-hex / domain; routes to
  `/profile?npub=` or `/domain?d=` by what was typed. Invalid input sets a plain status
  line and navigates nowhere. A domain typed here navigates; it NEVER bans.
  Focused by Ctrl-K / Cmd-K (`keydown`, `preventDefault`, in `common86.js`, once).
- Theme button, right, inside the header (not `position:fixed`). Cycle auto→light→dark→auto;
  glyphs ☀/☾ only, auto shows the OS-resolved glyph via `matchMedia`, live `change`
  listener; `title`+`aria-label` state current AND next. Store `strfry86_theme` =
  `"light"|"dark"`; absence IS auto. Anti-flash inline `<script>` at top of `<head>`
  applies a stored override before first paint — duplicated per page, deliberately.
- Nav links after the logo, same order everywhere: home · stats · userlist · report ·
  authors · bans · audit.

## Pages

### `/home` — activity feed (public landing page)
Logged out this IS the site. It renders kind, human kind label, timestamp, truncated event
id — **no pubkeys, no content, no links to profiles** while logged out; the no-leak rule
holds. Logged in, rows add author npub linked to `/profile`. Fed by the live layer,
newest first, capped at `RENDER_MAX`, no history fetch.

### `/stats`
- Totals table + per-kind table, kinds labelled from the bundled nostrbook kind map
  (`kinds86.json`, vendored, never fetched at runtime), unknown kinds shown as `kind N`.
- **Baseline + delta model**: figures = last walk/totals cache PLUS live deltas since.
  Header line: `baseline <abs> (<rel>) · +N events, −M deletes since`. Never present a
  session-relative number as a database total. Kind-5 deletes decrement; strfry-internal
  expiry does NOT appear live and is only corrected by the next walk — say so on the page.
- Surfaces only excess worth noticing: the gap ladder verdict, saturation notices, expired
  events. Everything else is a figure, not an alarm.
- **Console box**: live tail of strfry stderr/stdout (server-sent events, ring buffer,
  `RENDER_MAX` lines). Input runs `strfry <verb> …` only when the first token is in
  `CONSOLE_VERBS` and no write flag is present; anything else renders the refusal and the
  reason, executes nothing. The console NEVER runs `delete` — purges go through the
  guarded forms only (WHY.md §5). Global job lock applies to console commands too.

### `/userlist`
One dedicated page: list every known member as fast as possible, filter instantly.
Columns: name · nip-05 · npub (truncated, full on hover/copy) · event count ·
gift-wrap p-tag count · reports by others (kind 1984).
- Rows appear immediately from cache; unknown cells render `—` and fill in as endpoints
  answer (rule 9). Filter is client-side over ALL rows, instant, no debounce fetch.
- Gift-wrap column is a FLOOR while the recipient scan saturates: render `≥ n` and label
  the column header `(floor — scan capped)`. Never render a saturated count bare.
- Report counts come from `POST /api/reports` (index count of kind 1984 per p-tag, cached).
- Render cap `RENDER_MAX`; filter matches against the full set.

### `/report`
Shared cached-scan panel (also used by `/authors`): heading `<h3>` · cost line ·
last-run line (THIS relay's measured duration) · button · status line · message slot ·
figures table. `<hr>` between panels, `<h2>` for sections. Two buttons in § Database:
`refresh totals` (seconds, exact) and `run full database walk` (minutes); walk refreshes
totals as a byproduct, never the reverse. Totals table = five rows with per-row
denominators; walk panel = aggregate table (incl. split author counts and `expired
events`, rendered only when non-null), then top-`FIGURE_HEAD_MAX` unlisted kinds with
shares, then `<details>` full histogram. When walk and totals timestamps differ, state
`figures taken <n> min apart` — never reconcile by adjusting either. Author-list pointer
is one sentence + link naming the cheap press. Global lock: while any job runs, every
other button disables and says what it is waiting on. Undo history here is ONE line:
`last action: <summary> — full log → /audit`.

`report.html` has no list, no checkbox, no selection, and no control acting on a set the
page assembled. Its only write is the undo of an action recorded in the audit log,
against pubkeys named on the record itself.

### `/audit`
Server-side log of every admin action (bans, unbans, purges, reason edits, console
commands), newest first, capped at `RENDER_MAX`, full set behind filter. Each record:
absolute+relative time, action, pubkeys/args, and `Undo` where the inverse exists.
Every other page shows at most ONE undo line at top linking here. The audit log is the
authority for undo — localStorage undo state is dead; do not resurrect it.

### `/authors`
Logged out: heading, header, login button only — no list, no control, no filter.
Logged in, empty: full scan panel (`never run`, both cost lines, both buttons) above one
sentence naming contents and the cheap press; durations from `modes` in `GET /api/authors`,
measured on THIS relay, never a developer-relay number. `recent` is the default radio.

### Record lines — every page
At most three controls: `↩`, `Undo`, expand arrow. No `<input>`, no reason field, ever.
The bulk-reason row is `bans.html`-only, exists once, bound to the checked set.

### Allowlist audit — two questions, two thresholds
`gap = count{} − count{AUTHOR_SCAN_KINDS} − count{1059}` — exact, from the index.
Ladder: gap < `GAP_NOTICE_SHARE` → one coverage line. Gap ≥ notice with no single kind ≥
`KIND_ALARM_SHARE` → notice naming the shape, explicitly declining to recommend an edit,
`needs_walk:true` when composition is unknown. Any single kind ≥ `KIND_ALARM_SHARE` →
stale alarm naming the kind. Totals alone can never return `stale`; it needs a walk no
older than itself. `test.sh` fails only on the single-kind alarm.

## Endpoints (deltas that must hold)

- `POST /api/report/totals`: exact index counts, no `saturated` field; returns
  `gap_level` (+ `gap_alarm_kind`/`gap_alarm_events` at stale).
- `POST /api/report/walk`: returns `unlisted_kinds` in full plus `unlisted_total`,
  `unlisted_kind_count`, `expired_events` (null in `_empty_report_walk`). Expired counted
  before the 1059 branch, one time snapshot per walk.
- `POST /api/subscribers`: saturation is a REFUSAL of the exempt purge, not a label —
  a saturated list is a floor, and a floor cannot be an exemption list. Returns
  `counted:{"10050":n,"10002":n}` beside `saturated`. Recipients saturating is safe;
  subscribers saturating is not; the asymmetry is deliberate (WHY.md §5).
  Unset `relay_url` = `failed` per rule 6; fix lives in `config.json` via `/api/relay-url`
  on `/report` — never `strfry.conf`.
- `GET /api/authors`: empty response starts nothing; carries `modes` with measured
  durations (null until a run exists).
- `GET /api/audit` (paged, newest first) and `POST /api/undo` back `/audit`.
- Live layer: one server-side strfry subscription fanned out over SSE to `/home` and
  `/stats`; deltas only, no history replay, reconnect resets deltas and re-reads baseline.

## test.sh — every assertion covers a silent failure

Gap ladder (2.008%/0.079% → notice, never "stale"; 27.9% kind → stale, named; no walk →
never stale). Failed ≠ result (`relay_url` unset: `scanned_at` unchanged, cache
byte-identical, no timestamp for the failed run). Saturated subscriber cache → blanket
purge form only; emitted command has NO `#p` list. Caps hold (444 keys → head + tail line
whose total equals omitted sum; 12,481 rows → `RENDER_MAX` shown, all filtered). No record
line contains an `<input>`. `report.html` DOM has no checkbox and no set-bound handler.
Empty authors page: both buttons, `recent` checked, duration sourced from `modes`.
Console: a `delete` verb is refused and nothing executes. Userlist: saturated gift-wrap
cell renders `≥`. `/home` logged-out DOM contains no npub and no profile link.

## Reference (2026-07-30, this relay; every figure states DB-wide vs window)

2,642,995 events; 1,709,910 kind-1059 (64.7% DB-wide); non-gift-wrap 933,085; in
allowlist 914,357 (98.0%); gap 18,728 by index / 18,734 by walk (6-event drift over the
8m37s walk — correct, never reconcile). 444 unlisted kinds, largest kind 43 at 735
(0.079%). Walk ~5,113 ev/s. Recipient scan saturated at 250,000 of 1,709,910 (14.6%),
safe direction only. Kind 20001: 224 DB-wide (0.024%) — not allowlist material; the
conflicting 387 figure is deleted, unsourced (WHY.md §7).