# WHY.md — strfry-86 rationale

Not loaded as agent context. This file exists so CLAUDE.md can be rules-only and still
survive a future minimalist. If you are about to delete or "simplify" a rule in CLAUDE.md,
find its section here first. Section numbers are referenced from CLAUDE.md.

## §1 Rendering caps and the thresholds

`FIGURE_HEAD_MAX` is 10, not 20: the measured top 20 unlisted kinds are 48.9% of the gap
and 0.98% of non-gift-wrap — the second ten buy attention and carry no decision. The tail
is summarised, never dropped; the full set sits behind a `<details>` that costs nothing
unopened, where 444 items as prose cost the reader the two lines above it. The standard is
Shneiderman's: overview first, zoom and filter, then details on demand.

`KIND_ALARM_SHARE = 0.005` is calibrated on the real miss this audit was built to catch:
kind 2003 at 258,290 events, **27.9% of non-gift-wrap** — more numerous than every kind-1
note on the relay, its authors entirely invisible — fifty-five times the threshold. The
largest unlisted kind on the healthy relay is 0.079%, sixty-three times under it. No
plausible relay makes that threshold ambiguous; if one appears, look at the walk, do not
move the constant.

The single-threshold design it replaced fired permanently: gap 2.008%, one tick over 2%,
spread across 444 kinds, largest 0.079%, 165 kinds holding ≤2 events. Adding the top 20 by
hand moves coverage 98.0%→99.0% and leaves 424 kinds uncovered. That is Nostr's kind long
tail, not drift, and it only lengthens — the old design read STALE forever on every relay.
An alarm nobody can act on trains the operator to ignore the one they could (Grafana's
alerting discipline: remove alerts that don't lead to action; fewer high-quality alerts).

## §2 The `pre` rule and the no-table history

`pre { white-space:pre-wrap }` exists for the ONE legitimate `<pre>`: shell commands in
the generator. A `strfry delete --filter …` with an enumerated `#p` list ran off the right
edge of a phone with no wrap — a destructive command the operator could not read the end
of. `pre-wrap` keeps the newlines and copy behaviour while wrapping. Figures never use
`<pre>`: space-padded columns are presentational, read by a screen reader as one blob, and
shear when a value grows a digit. The old "no table element" note applied only to sortable
lists (sorting re-appends `<li>`); a static figure table never sorts, so semantic
`<table>`/`<dl>` with browser defaults IS the no-CSS rule, applied to data.

## §3 Theme: why Canvas/CanvasText is load-bearing

Found by testing the toggle, not by reasoning. With no author `background` declared,
changing `color-scheme` after first paint recomputed text color on click but did NOT
repaint the background — black text on black until the next navigation. Declaring
`background:Canvas; color:CanvasText` moves the paint into ordinary style recomputation,
which repaints on every toggle press. Remove the pair and the toggle becomes a readability
trap. The anti-flash `<head>` script is duplicated per page deliberately: a stored
dark-preference painting light while `common86.js` loads would flash on every navigation,
which is worse than the duplicated lines. Absence of the storage key IS the auto state —
never write `"auto"`, so there is exactly one representation. The glyph shows what is on
screen (via `matchMedia`), never a third auto icon the operator must decode.

## §4 Four panel states and the failed/result distinction

The live page rendered `scanned … (just now)` above "relay.info.url is not set — cannot
match". Nothing ran, nothing could, and the panel stamped a time. The dangerous empty
state is the one that looks like a result. Hence: a run producing no usable result never
stamps `scanned_at` and never replaces the cache; `error` replaces a result where
`warning` accompanies one; the previous result renders at its TRUE age; a precondition
failure names its fix and WHERE it lives (`relay_url` is in `config.json` via the field on
`/report` — strfry.conf never reads it). One message slot because the same warning printed
twice reads as two problems. Empty states name the cheapest press (`recent` ≈ 3s) because
"never run" alone is a dead end — the user complaint about the unpopulated author list was
an empty-state complaint, not a data complaint.

## §5 Saturation asymmetry — the most important paragraph in the project

The worst available outcome is a `strfry delete` that removes the gift wraps of people
this relay is the DM inbox for. A saturated subscriber scan produces exactly that: at
`SUBSCRIBER_SCAN_LIMIT` the list is a FLOOR, missing exemptions are missing subscribers,
and the emitted exempt purge deletes their DMs while looking like a complete answer with a
fresh timestamp. So saturation there is a REFUSAL, not a label. Recipients are the
mirror image: a saturated recipient list means a shorter `#p` list means LESS deletion —
wrong in the harmless direction, which is why it may stay permanently saturated (14.6% of
gift wraps, a 47-day window) and why raising `RECIPIENT_SCAN_LIMIT` to "fix" the label is
forbidden without re-reading this section: the same word means opposite things on the two
scans. Kinds 10050/10002 are replaceable, bounded by users not traffic, so 50,000 should
hold — but "should hold" is what `REPORT_SCAN_LIMIT` said too, hence the `counted` field:
two index counts turn saturation from inference into fact.

This is also why the stats console allowlists verbs. A free command box makes every guard
above decorative and the test.sh saturation assertion untestable — one typed `delete`
bypasses the only interlock standing between a floor and a purge. Read-only verbs give the
live-debugging value without reopening the hole.

## §6 Live layer as delta, never as truth

A relay subscription sees only new events. It cannot see the 2.6M-event backfill and it
cannot see strfry's internal NIP-40 expiry (expiration is not a single-letter tag; no
filter can express it — only the walk can count it, which is why `expired_events` exists
and why it is a size figure, never an alarm: there is no filter-expressible purge for it).
So live figures are deltas over a cached baseline, both shown with their ages. The walk
and totals disagreeing by 6 events over 8m37s is the same lesson: two measurements at two
times differ, correctly; reconciling them by adjusting either forges data.

## §7 Deleted figures and honest measurement blocks

The old measurement block held kind `20001:387` in a 20,000-event window beside
`20001:224` DB-wide — a window count cannot exceed a database count, so one was from
another relay, another date, or a typo, and the "known open item" about 20001 rested on
it. Resolved on current data: 224 DB-wide is 0.024%, two orders under `KIND_ALARM_SHARE`;
20001 is closed, out of the allowlist. The stale figure is deleted rather than corrected
because its provenance is unknown, and an unsourced number in that block is what caused
the confusion. Standing rules that survive: every figure states DB-wide vs window; no
figure anywhere comes from a constant or the developer's relay; every panel states its own
age in absolute and relative form. A recent-window sample can never substitute for a
whole-database count — kind 2003 and 30166 appeared in no 20,000-event sample.

## §8 Record lines, report-page safety, and the audit page

The live report page grew a `reason — shown publicly` input and `Set reason` button inside
every record line — a bulk public-publishing control, per-line, on the page specified as
the safest in the project, bound to a checked set that page does not have. Hence the hard
cap: three controls, no inputs, on any page, in any state. The report page's old rule ("no
ban control anywhere in its DOM") was false — `Undo` POSTs `/api/unban` — and a rule known
to be false in one place stops constraining the next place; the honest rule (no control
acting on a page-assembled set) is the one that survives contact. Moving undo authority to
the server-side audit log replaces "recorded in this browser" with a bound that still
holds with multiple admins and multiple sessions: undo acts only on pubkeys named on the
record itself, and every action — including console commands — leaves a record.

## §9 Public feed vs the no-leak rule

Logged-out pages render nothing that could start work or leak a list. The activity feed is
public only because its logged-out rendering carries no pubkeys, no content, and no
profile links — kind, label, time, truncated id. That is metadata about relay liveness,
not about people. The moment a logged-out row links to a profile, the no-leak rule is
gone; test.sh asserts the logged-out DOM contains no npub for exactly that reason.
