# strfry-86

Admin UI for a strfry Nostr relay. Python (server86.py) + vanilla HTML/JS pages. No frameworks, no build step. Locked style discipline: browser defaults + one shared `<style>` block.

See `WHY.md` for calibration data, rationale, and design history. Load it only when a rule's purpose is unclear.

## Constants (server86.py)

```python
RENDER_MAX          = 500   # list rows before truncation
FIGURE_HEAD_MAX     = 10    # ranked figure rows before tail summary
GAP_NOTICE_SHARE    = 0.02  # gap worth a sentence
KIND_ALARM_SHARE    = 0.005 # one unlisted kind this large → actionable stale
SUBSCRIBER_SCAN_LIMIT = 50000
RECIPIENT_SCAN_LIMIT  = 250000
```

## Rendering results (all pages)

Endpoints return complete data. Pages render a summary; the rest sits behind one click.

1. Every enumeration is capped (`RENDER_MAX` lists, `FIGURE_HEAD_MAX` ranked figures). Tail is summarised, not dropped: `+ 434 more kinds, 9,564 events`. Full data lives in a `<details>` labelled with its count.
2. Numeric results use a semantic `<table>` or `<dl>`, never a sentence or space-padded `<pre>`. Key-value → label/value/(share). Ranked counts → name/count.
3. Every percentage names its denominator (or the block header does).
4. Coverage is stated positively first: `98.0% covered`, then the gap.
5. Four panel states, none may wear another's clothes:

| state     | when                          | renders                                      |
|-----------|-------------------------------|----------------------------------------------|
| never run | no cache                      | cost line + button, visibly empty            |
| running   | job holds the lock            | progress, server figures only                 |
| result    | cache present, success        | age, then figures                            |
| failed    | started, no usable result     | why it failed + previous result at true age  |

A failed run never stamps `scanned_at` and never replaces the cache. Add `error: str|null` to every cache record (`warning` accompanies a rendered result; `error` replaces one). Precondition failures state the fix and where to make it.
6. Each panel has exactly one message slot (under status, above figures). Never echo warning/error into the result body.
7. `never run` always states the cheapest press that fills it (durations from this relay's measured `modes`, never a developer constant).

## Theme (dark mode)

```css
:root { color-scheme: light dark; }
#theme-btn { position: fixed; top: 0.5em; right: 0.5em; }
body { background: Canvas; color: CanvasText; max-width: 40em; margin: 0 auto; padding: 0 1em; }
li, pre { overflow-wrap: anywhere; }
pre { white-space: pre-wrap; }
@media (max-width: 600px) { body { font-size: 1.15em; } }
```

Toggle cycles auto → light → dark → auto (`s86WireThemeToggle` in common86.js). Storage key `strfry86_theme` (`"light"`/`"dark"`; absence = auto). Glyphs ☀/☾ only; auto shows the OS-resolved glyph via `matchMedia`. Tiny inline `<script>` at top of `<head>` applies stored override before first paint. Identical three ingredients on every page.

## Header & navigation (every page)

Fixed header, identical position and content on every page:

- Logo text `strfry-86`: clickable to `/`, rendered as plain text (no underline, inherits color — not a visible link).
- Search box: accepts npub, 64-hex, or domain. Routes to `/profile?npub=` or `/domain?d=`. Invalid input → status line only, no navigation. Domain never bans. Reachable by Modifier-K.
- Theme button (top-right, fixed).
- Consistent nav links to: Activity, Stats, Report, Authors, Userlist, Audit, Bans (logged-in only where required).

Header rules are part of the locked style block (reopened once for this enumerated set). Do not add further rules without explicit decision.

## Pages

### Activity (`/` — public landing)

Logged-out default. Shows a live activity feed of recent events: **kind number, timestamp, event id only**. No pubkeys, no content, no npubs. Logged-in may see the same feed plus admin chrome.

### Stats (`/stats`)

- Totals: all events, per-kind counts. Label kinds from https://nostrbook.dev/kinds/ (fallback to raw number).
- Surface only excess / actionable figures (gap ladder, large unlisted kinds, saturated scans).
- Live layer is a **delta on a cached walk baseline**: page states both (`baseline 2h ago, +1,412 since`). Subscription sees new events; deletions visible only via kind-5 or explicit purge. Auto-tick updates the live delta.
- Terminal box: live console log of the strfry process + input field. Allowlist only non-destructive verbs (`scan --count`, `info`, `sync --dry-run`, …). Everything else refused with reason. Never full passthrough.

### Report (`/report`)

Cached-scan panels (shared with authors). Two independent buttons in Database section: `refresh totals` (seconds) and `run full database walk` (minutes). Walk refreshes totals as byproduct; reverse is not true.

Totals panel: five-row table (all / gift wraps / non-gift-wrap / in allowlist / not in allowlist) + three-level gap verdict (see Allowlist). Walk panel: aggregate table, top-`FIGURE_HEAD_MAX` unlisted kinds, full histogram in `<details>`. Author-list pointer is one sentence + link that states the cheap press. Global lock visible: other buttons disable with `waiting on …`.

### Authors (`/authors`)

No public view. Logged-out: heading, nav, login only. Empty state renders the scan panel (`never run` + both cost lines + both buttons; `recent` is default radio) and one sentence naming contents and durations from `modes`. Progressive fill uses `—` for not-yet-known values; `0` only when measured.

### Userlist (`/userlist`)

Dedicated page for listing members as fast as possible with instant client-side filtering. Columns: name, nip-05, npub (truncatable), event count, gift-wrap p-tag events (labelled floor `≥ n` when from saturated recipient scan), count of reports by others (kind 1984). Populate known fields immediately; leave unknowns as `—` until filled. No blocking full scan required to show the page.

### Audit (`/audit`)

Server-side log of recent admin actions (the durable undo list). Filterable. Undo of an action is possible against the server log (not only this browser's localStorage). Safety property becomes: the only writes are undo of a recorded admin action, bounded by the audit log's retention and auth.

### Bans & record lines

A record line carries at most: ↩, Undo, expand arrow (multi-pubkey). No `<input>`, no reason field, no third-party control on any page. Bulk-reason control exists once, on `bans.html` only, bound to the checked set.

`report.html` has no list, no checkbox, no selection, and no control that acts on a set the page itself assembled. Its only write is undo of an already-recorded action.

## Allowlist audit

```
gap = count('{}') − count('{kinds: AUTHOR_SCAN_KINDS}') − count('{"kinds":[1059]}')
```

Three levels (only the third says stale):

- gap < GAP_NOTICE_SHARE → one positive coverage line.
- gap ≥ GAP_NOTICE_SHARE and no single unlisted kind ≥ KIND_ALARM_SHARE → notice naming the long-tail shape; explicitly declines to recommend an edit; may set `needs_walk: true`.
- any single unlisted kind ≥ KIND_ALARM_SHARE → actionable alarm naming the kind.

`/api/report/totals` returns `gap_level: "ok"|"notice"|"stale"` and can only emit `"stale"` when a sufficiently fresh walk record supplies composition. `test.sh` asserts the ladder.

## Critical safety rules

- **Subscriber saturation is a REFUSAL.** Exempt purge requires present + fresh + **not saturated** cache. Saturated list is a floor; using it as exemptions deletes real subscribers' DMs. Recipients saturating is safe (fewer → less deletion). Asymmetry is deliberate. Page states the refusal in words; emitted command must not contain a `#p` exemption list when saturated.
- Unset `relay_url` → `failed` state (no `scanned_at`, no cache replace, error names the fix on the admin page).
- Walk vs totals may differ by a few events (live writes during the multi-minute walk). State the time gap; never reconcile by adjusting either number.
- `expired_events` (NIP-40) is a walk-only size figure, never an alarm.

## Endpoints (deltas)

- `POST /api/subscribers`: return `counted` per kind + `saturated`. Saturation → refusal for exempt purge.
- `POST /api/report/totals`: exact index counts; `gap_level`; never `"stale"` without fresh walk composition.
- `POST /api/report/walk`: full `unlisted_kinds` + `unlisted_total` + `unlisted_kind_count` + `expired_events`.
- `GET /api/authors`: empty cache still idle; additionally `modes` with measured typical_seconds per mode on this relay.

## test.sh must cover

Gap ladder (notice vs stale vs no-walk). Failed ≠ result (scanned_at untouched). Saturation refuses exempt purge (no `#p` list). Caps hold. No record-line `<input>`. Report page has no page-assembled selection control. Empty authors page offers the cheap press with `recent` default.
