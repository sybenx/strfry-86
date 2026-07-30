# CLAUDE.md revisions — result rendering, the gap alarm, and three safety holes

Replacement text for the affected sections. Each part says where it goes and what it
replaces. Nothing outside these parts changes.

Written after reading a live `/report` page against the doc. The presentation problems and
the correctness problems turned out to be the same problem: the doc specifies *what* every
endpoint returns and never specifies *how much of it reaches the page*, so every renderer
defaulted to "all of it, in a sentence."

---

## Part 0 — What the live page revealed

Ranked by consequence, not by how bad they look.

1. **The `allowlist gap` alarm is a permanent false positive**, and it is the one number
   the doc says pays for the whole report page. Measured: gap 18,728 events = 2.008% of
   non-gift-wrap, one tick over the 2% threshold, so the page reads `AUTHOR_SCAN_KINDS is
   stale. The full author list is missing authors.` The walk says otherwise: those 18,728
   events are spread over **444 distinct kinds**, the largest of which (kind 43) is **735
   events — 0.079% of non-gift-wrap**. 165 of those kinds have two events or fewer, 218
   events between them. Adding the entire top 20 by hand would move coverage from 98.0% to
   99.0% and still leave 424 kinds uncovered. There is no action here. The threshold was
   calibrated on kind 2003 at 258,290 events — **27.9%** of non-gift-wrap — and a threshold
   that fires identically at 27.9% and at 0.079% is not measuring the thing it was built to
   measure. Grafana's alerting guidance is the relevant discipline here:
   <cite index="24-1">remove alerts that don't lead to action, tune thresholds that fire too often without providing useful signal, and favour fewer high-quality actionable alerts over many low-value ones</cite>.
   Fixed in Part 3 by splitting the size question from the actionability question.

2. **A saturated subscriber scan can generate a purge command that deletes real
   subscribers' DMs.** The doc guards the exempt purge form against *absence* and *staleness*
   and never against *saturation*. `SUBSCRIBER_SCAN_LIMIT` is 50,000; when it saturates the
   subscriber list is a floor, missing exemptions are missing subscribers, and the emitted
   `delete` removes the gift wraps of people this relay is the DM inbox for. That is the
   outcome the doc names as the worst available. Recipients saturating is safe (fewer named
   recipients = less deletion); subscribers saturating is not. The asymmetry has to be
   written down or someone will "fix" the wrong side of it. Fixed in Part 5.

3. **A scan that could not run at all rendered as a fresh result.** The subscribers panel
   reads `scanned 2026-07-30@00:03 UTC (just now)` above `relay.info.url is not set in
   strfry.conf — cannot match subscriber relay lists`. Nothing was scanned and nothing can
   be until the operator edits strfry.conf, but the panel stamped a time and claimed a
   result. The doc has three panel states and needs four; the missing one is the dangerous
   one. This is the standard failure mode:
   <cite index="37-1">the most dangerous case is when the screen looks empty but the real problem is a failure</cite>,
   and stable state handling requires explicit conditions for not-yet-fetched, fetched-with-zero-results,
   and fetch-failure rather than collapsing them into one treatment. Fixed in Part 2.

4. **The same warning printed twice**, once appended to the status line and once as the
   result body. Fixed in Part 2 by giving each panel exactly one slot for message text.

5. **`unlisted_kinds` rendered as 444 comma-separated pairs in a single run of prose.**
   This is the mess the report page is mostly made of. It is not a rendering slip — the doc
   told it to: *"renders `unlisted_kinds` as a to-do list, ranked by count."* Ranked, yes;
   capped, never specified. `RENDER_MAX` exists for exactly this and was written to apply
   only to author lists. Fixed in Part 2 by making the cap project-wide.

6. **The bulk-reason control leaked onto the report page, three times.** A `reason — shown
   publicly` input and a `Set reason` button render inside every record line. That control is
   specified as `bans.html`-only, bound to the checked set, existing once, below the live
   count. On `/report` there is no checked set, so those buttons act on nothing or on
   something unstated, and they are a bulk *public-publishing* control on the page the doc
   calls structurally the safest document in the project. Fixed in Part 6.

7. **`full author list — never run`** with no indication that a 3-second press would fill
   it. The user-visible complaint ("the authors list is unpopulated until a full scan is
   done") is really an empty-state complaint: `recent` mode takes about three seconds and
   neither page says so at the moment it matters. NN/g's guidance for empty regions in
   complex applications applies directly —
   <cite index="42-1">tell the user what could be displayed and how to populate the area, and provide direct pathways to the task that populates it</cite>.
   Fixed in Part 6.

8. **Two figures in the doc's own measurement block cannot both be true.** The composition
   sample lists kind `20001:387` in a 20,000-event recent window; the whole-database walk
   lists kind `20001: 224`. A window count cannot exceed the database count. One figure is
   from a different relay, a different date, or a transcription error, and the "known open
   item" about 20001 has been resting on it. Fixed in Part 7.

9. **The walk histogram and the index counts disagree by 6 events** (18,734 vs 18,728).
   Correct and unavoidable: the walk streams for 8m37s on a live relay and the `--count`
   ran at one instant. Undocumented, though, so an operator finding it has no way to know
   whether it is drift or a bug. Fixed in Part 5.

Two things the page got right and that the revisions preserve: every panel states its own
age in absolute and relative form, and no figure anywhere came from a constant or the
developer's relay.

---

## Part 1 — Constants to add

Merge into the constants block in `server86.py`. Same block, same argument for it: every
bound and every rendering cap auditable by reading one place.

```python
# --- rendering caps: no result reaches a page uncapped -------------------
RENDER_MAX             = 500     # list rows rendered client-side before truncation
FIGURE_HEAD_MAX        = 10      # ranked rows shown before the tail is summarised
# --- allowlist audit: size and actionability are separate questions ------
GAP_NOTICE_SHARE       = 0.02    # gap worth a sentence, never an alarm on its own
KIND_ALARM_SHARE       = 0.005   # ONE unlisted kind this big is the actionable alarm
```

`FIGURE_HEAD_MAX` is 10 rather than 20 because the measured top 20 unlisted kinds are
48.9% of the gap and 0.98% of non-gift-wrap events — the second ten buy attention and
carry no decision. The remainder is not discarded; it is summarised and available behind a
`<details>`, per Part 2.

`KIND_ALARM_SHARE` is 0.005 because kind 2003, the miss this whole audit was built to
catch, was 27.9% of non-gift-wrap events — fifty-five times the threshold — while the
largest kind on the measured relay today is 0.079%, sixty-three times under it. There is no
plausible relay where that threshold is ambiguous. If one ever appears, the fix is to look
at the walk, not to move the constant.

---

## Part 2 — New project-wide section: **Rendering results**

Insert as a new top-level section immediately before `## Client pages`. This is a
constraints section like the environment constraints, not page-specific guidance, and every
page and every future page inherits it.

### Rendering results

The endpoints return complete data. The pages render a *summary* of it and put the rest
behind one click. This is not a preference and it is not about taste — the whole-database
walk returns a 444-key histogram, and the doc's instruction to render it "ranked by count"
without a cap produced a page where the single most important number on it (the gap alarm)
sits above a wall of 444 numbers, the largest of which is 0.079% of anything. The standard
here is Shneiderman's, and it is old enough to be uncontroversial:
<cite index="8-1">overview first, zoom and filter, then details on demand</cite>.
Deferring the rest is progressive disclosure in its plainest form —
<cite index="19-1">initially show only the most important options, offer the larger set on request</cite>
— and `<details>` is the whole mechanism, already in use on every page.

**1. Every enumeration is capped, and the tail is summarised rather than dropped.**
`RENDER_MAX` for list rows, `FIGURE_HEAD_MAX` for ranked figure rows. Beyond the cap,
render one line stating what is not shown *and its total*, so the tail is a known quantity
rather than an absence: `+ 434 more kinds, 9,564 events between them`. The complete data
goes inside a `<details>` labelled with its own count — `▸ all 444 unlisted kinds` — one
row per line, never a comma-separated run. A `<details>` an operator never opens costs
nothing; a 444-item run of prose costs them the ability to read the two lines above it.

**2. Numeric results render as a semantic `<table>` or `<dl>`, never in a sentence and
never in a space-padded `<pre>`.** `2,642,995 events, 1,709,910 gift wraps (64.7%), 914,357
in AUTHOR_SCAN_KINDS` is three figures with two different denominators and no way to scan
them vertically. The correct element is the one that matches the data's shape, per web.dev:
a `<table>` is right when data is <cite index="47-1">presented, compared, or
cross-referenced</cite>, which is exactly what a per-kind histogram or a totals breakdown
is. A `<pre>` with space-padded columns is NOT the answer — it is presentational, not
structural; a screen reader reads it as one undifferentiated blob (the run-on sentence in a
different font), and the columns shear the instant a value grows a digit. `<pre>` is
reserved for genuinely preformatted text — shell commands in the generator — and nothing
else.

  - **Key-value figures** (the totals breakdown, a profile's counts) → a two- or
    three-column `<table>`: label, value, and an optional share column so each percentage
    sits beside its own denominator instead of several denominators colliding in one
    sentence. A `<dl>` is the stricter-semantics alternative for pure key-value pairs and is
    equally acceptable; the project should pick one and use it everywhere.
  - **Ranked counts** (`unlisted_kinds`, `kinds`, recipient and author tallies where shown
    as figures rather than actionable lists) → a two-column `<table>` of name and count,
    numeric column consistent, capped per rule 1.
  - **This needs ZERO new CSS and is the reason it fits the locked style block.**
    Browser-default `<table>` and `<dl>` rendering is precisely the "browser defaults
    throughout" the project already mandates. Semantic HTML with default styling is not a
    liberty against the no-CSS rule — it is the rule, applied to data instead of to text.
    The existing "no table element" note applies ONLY to the sortable lists, where sorting
    re-appends `<li>` nodes; a static figure table never sorts and never re-appends, so that
    note does not reach it.

**3. Every percentage names its denominator, or sits in a block whose header does.** Mixed
denominators in one sentence is how `64.7%` (of all events) ended up adjacent to `2.0%` (of
non-gift-wrap events) with nothing distinguishing them.

**4. Coverage is stated positively, the gap secondarily.** `98.0% of non-gift-wrap events
are covered` then `18,728 not covered (2.0%)`. Identical information; the first reads as a
state and the second reads as a defect, and only one of those is true at 98%.

**5. A `<table>` handles its own width; abbreviate labels rather than let a cell force
horizontal scroll on mobile.** Browser-default tables reflow and wrap cell contents, so the
`<pre>` overflow problem does not arise for figures. Keep label text short enough that the
table fits a 40em column without a horizontal scrollbar on a phone — the constraint is on
the label wording, not on a character count, because the table, unlike `<pre>`, degrades
gracefully. `FIGURE_LINE_WIDTH` is therefore dropped from the constants in Part 1; it was a
`<pre>`-era measure with nothing left to bound.

**6. The locked `<style>` block gains one rule, and only one.**

```css
body { max-width: 40em; margin: 0 auto; padding: 0 1em; }
li { overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 600px) { body { font-size: 1.15em; } }
```

The `pre` rule is REQUIRED and is not a new liberty. It exists for the ONE remaining
legitimate use of `<pre>` — shell commands in the generator, which are genuinely
preformatted text. The generator renders `strfry delete --filter '{"kinds":[1059],...,"#p":
[...]}'` with an enumerated recipient list, which today runs off the right edge of a phone
with no wrap and no scroll — a destructive command the operator cannot read the end of.
`pre-wrap` preserves the newlines and the copy behaviour that make `<pre>` the right element
for a command while letting long lines wrap like everything else on the page. Figures do NOT
use `<pre>` (see rule 2), so this rule governs commands alone. Do not remove it in the name
of minimalism, and do not add a second rule on the strength of this one.

**7. Four panel states, and none may wear another's clothes.**

| state | when | renders |
|---|---|---|
| `never run` | no cache | the cost line and the button, visibly empty — never hidden |
| `running` | job holds the lock | the progress line, server-supplied figures only |
| `result` | cache present, run succeeded | the age, then the figures |
| `failed` | run started and produced no usable result | why it failed, and the PREVIOUS result at its TRUE age |

The fourth state is the one the live page lacked, which is why a scan that could not run
reported itself as having run just now. Rules for it:

- **A run that produced no usable result does not stamp `scanned_at` and does not replace
  the cache.** This is already the rule for a deadline hit; it now covers every failure,
  including a precondition that was never satisfiable (`relay.info.url` unset, strfry binary
  not found, `--count` returning non-numeric output). Add `error: <string|null>` to every
  cache record, distinct from `warning`: `warning` accompanies a result that IS rendered,
  `error` replaces one that is not.
- **A failed run never renders as an age.** `could not run — relay.info.url is not set in
  strfry.conf`, then, if a previous result exists, `last successful run 2026-07-22@09:14 UTC
  (7 days ago)`. An operator must never have to work out that a timestamp describes a
  failure.
- **A precondition failure states the fix**, since it is the operator's to make:
  `set relay.info.url in /config/strfry.conf and re-run`. Naming the file and the key is the
  whole difference between a diagnostic and a shrug.

**8. Each panel has exactly one slot for message text**, directly under the status line and
above the figures. No renderer may echo `warning` or `error` into the result body. A message
printed twice reads as two problems.

**9. `never run` is a visible state everywhere, and it states the cost of the press.**
Unchanged from the existing report-page rule, promoted to project-wide, plus one addition:
the empty state names the cheapest press that fills it, not merely the fact that nothing has
run. `never run — a recent scan takes about 3 seconds` is an invitation; `never run` alone is
a dead end.

---

## Part 3 — Replaces `### Auditing the allowlist — now a number on a page, and still a release step`

### Auditing the allowlist — two questions, two thresholds

`AUTHOR_SCAN_KINDS` is hand-maintained and WILL drift as Nostr adds kinds, and its failure
mode is silent: authors simply do not appear, and the page keeps calling the list complete.
So the gap is computed, not assumed:

```
gap = scan --count '{}'  −  scan --count '{kinds: AUTHOR_SCAN_KINDS}'  −  scan --count '{"kinds":[1059]}'
```

All three terms are index counts answered in seconds. This is exactly
`POST /api/report/totals`, which is why that endpoint exists: the check runs on every
operator's relay whenever they press a button, instead of only in a `test.sh` invocation
this document admits has never happened against real data.

**The gap SIZE and the gap ACTIONABILITY are different questions, and conflating them
produced a permanent false alarm.** The original single threshold — 2% of non-gift-wrap
events means stale — was calibrated on a real miss: kind 2003 at 258,290 events, 27.9% of
non-gift-wrap, more numerous than every kind-1 note on the relay, its authors entirely
invisible. That is what staleness looks like, and no threshold that also fires on the
measured reality below can detect it usefully.

Measured on the reference relay after the allowlist was fixed: gap 18,734 events, 2.008% of
non-gift-wrap — **one tick over the threshold** — distributed across **444 distinct kinds**,
largest 735 events (0.079%), with 165 of those kinds holding two events or fewer. Adding
the entire top 20 by hand moves coverage from 98.0% to 99.0% and leaves 424 kinds
uncovered. The page said `AUTHOR_SCAN_KINDS is stale. The full author list is missing
authors.` There was nothing to do. **This is Nostr's kind long tail, not drift, and it will
only lengthen** — which means the single-threshold design would read STALE forever on every
relay, on a number the rest of this document claims justifies the report page's existence.
An alarm nobody can act on trains the operator to ignore the one they could.

So, three levels, and only the third says stale:

- **`gap < GAP_NOTICE_SHARE`** — one line, no alarm:
  `allowlist covers 99.6% of non-gift-wrap events`.
- **`gap ≥ GAP_NOTICE_SHARE`, no single unlisted kind ≥ `KIND_ALARM_SHARE`** — a notice
  that names the shape and explicitly declines to recommend an edit:
  `allowlist covers 98.0% of non-gift-wrap events. The remaining 2.0% is spread across 444
  kinds; the largest is kind 43 at 735 events (0.08%). This is the long tail, not staleness —
  no single kind is worth adding.`
- **any single unlisted kind ≥ `KIND_ALARM_SHARE`** — the actionable alarm, which names the
  kind, because that is the whole point of having it:
  `kind 2003 holds 258,290 events — 27.9% of non-gift-wrap — and is NOT in
  AUTHOR_SCAN_KINDS. The full author list is missing its authors. Add it.`

**The totals endpoint cannot reach level three on its own.** An index count knows the gap's
size and nothing about its composition, because Nostr filters have neither negation nor
group-by. So `/api/report/totals` computes levels one and two, and at level two says what
level three would need: `run the database walk to see whether this is one missing kind or
the long tail`. When a walk record exists and is not older than the totals record, the page
computes the level from `unlisted_kinds` directly. When the walk is older than the totals,
say so rather than combining them — a composition from a month ago and a size from a minute
ago are not one measurement.

**A recent-window sample cannot substitute for either.** A kind used heavily two years ago
and since abandoned holds its events forever and appears in no recent scan; the
20,000-event sample that seeded the original list showed neither 2003 nor 30166 at
meaningful volume. Only a whole-database count can find them.

`test.sh` keeps its assertion, retargeted: it fails when any SINGLE unlisted kind exceeds
`KIND_ALARM_SHARE`, and reports the total gap as information without failing on it. The old
assertion, run today against the reference relay, fails on a correct allowlist.

---

## Part 4 — Replaces the panel-rendering bullets in `### report.html — GET /report`

Page order (items 1–7) is unchanged. Logged-out behaviour is unchanged. Replace everything
from **"The cached-scan panel is the page, repeated"** through **"The global lock is
visible"** with the following.

- **The cached-scan panel** is the page, repeated, and is shared code used by
  `authors.html` too. Every panel renders the same six things in the same order:

  ```
  heading            — what this is                            <h3>
  cost line          — what the button does, before it is pressed
  last-run line      — measured duration and rate from THIS relay, when one exists
  [button]
  status line        — never run | scanning… | scanned <abs> (<rel>) | could not run
  message slot       — warning or error, at most one, or nothing
  figures            — a semantic <table> of label/value rows (rule 2), or nothing
  ```

  Panel states, the `<pre>` figure rule, the caps, the single message slot, and the four
  states are all governed by **Rendering results** and are not restated per panel. `<h3>`
  for panel headings and `<h2>` for the two `§` sections, with an `<hr>` between panels:
  the live page ran every panel together as undifferentiated paragraphs, and semantic
  headings fix that with no CSS.

- **Two buttons in `§ Database`, not one.** Independent persistence implies independent
  triggers, and a single button that takes three seconds on Tuesday and nine minutes on
  Wednesday is how an operator learns to be afraid of a button. `refresh totals` is the
  seconds-long exact one; `run full database walk` is the long one. The walk runs the same
  `--count` first for its denominator, so pressing it refreshes the totals as a byproduct —
  the reverse is not true and the panels must not imply it is.

- **The totals panel** renders a five-row `<table>` and one verdict line:

  ```html
  <table>
    <tr><th>all events</th>        <td>2,642,995</td><td></td></tr>
    <tr><th>gift wraps</th>        <td>1,709,910</td><td>64.7% of all</td></tr>
    <tr><th>non-gift-wrap</th>     <td>933,085</td>  <td></td></tr>
    <tr><th>— in allowlist</th>    <td>914,357</td>  <td>98.0%</td></tr>
    <tr><th>— not in allowlist</th><td>18,728</td>   <td>2.0%</td></tr>
  </table>
  ```

  The share column names each denominator in the row itself — `64.7% of all` against the
  whole database, the two allowlist rows against non-gift-wrap — so two denominators never
  collide in one sentence, which was the specific defect in `... gift wraps (64.7%), 914,357
  in AUTHOR_SCAN_KINDS`. The verdict line below the table is the three-level ladder from
  Part 3, and this remains the only place in the running software where that check surfaces.

- **The walk panel** renders an aggregate `<table>`, then a ranked `<table>` of the top
  unlisted kinds, then the full histogram behind a `<details>`:

  ```html
  <table>
    <tr><th>events walked</th>     <td>2,642,995</td><td></td></tr>
    <tr><th>distinct authors</th>  <td>1,758,919</td><td></td></tr>
    <tr><th>— measured</th>        <td>49,009</td>   <td></td></tr>
    <tr><th>— gift wraps</th>      <td>1,709,910</td><td>one key per message</td></tr>
    <tr><th>kinds seen</th>        <td>486</td>      <td></td></tr>
    <tr><th>unlisted kinds</th>    <td>444</td>      <td>18,734 events</td></tr>
  </table>
  ```

  `distinct authors` is split into its two components because one is measured and the other
  is a consequence of NIP-17 — gift wraps are signed by a fresh key per message by
  specification, so their author count IS their event count — and presenting them as one
  number hides that. Then a `<table>` of the `FIGURE_HEAD_MAX` largest unlisted kinds,
  ranked, well-known kinds named as the composition line names them, each with its share of
  non-gift-wrap so the reader sees for themselves that no row is worth acting on:

  ```html
  <table>
    <tr><th>kind 43</th>   <td>735</td><td>0.08%</td></tr>
    <tr><th>kind 10011</th><td>598</td><td>0.06%</td></tr>
    <!-- … through FIGURE_HEAD_MAX rows … -->
    <tr><td colspan="3">+ 434 more kinds, 9,564 events</td></tr>
  </table>
  ```

  followed by `▸ all 444 unlisted kinds`, the full histogram as a `<table>` inside the
  `<details>`, one row per kind — never a comma-separated run.

  **`unlisted_kinds` is read as a to-do list only when a row clears `KIND_ALARM_SHARE`.**
  The doc previously called the whole histogram a to-do list; on a healthy relay it is a
  census of Nostr, and 444 rows presented as tasks is 444 tasks nobody will do. The head
  exists so an operator can confirm the shape, not so they can work through it.

  **The walk histogram and the totals counts will disagree slightly, and that is correct.**
  The walk streams for minutes on a live relay while the `--count` was taken at one instant;
  the measured drift was 6 events over 8m37s. When both records are present and their
  `scanned_at` differ, the panel says `figures taken <n> minutes apart; events written in
  between account for small differences` rather than leaving an operator to wonder which
  number is broken. Never reconcile them by adjusting either.

- **The author-list pointer** is one sentence and a link, and it states the cheap press:
  `full author list — 19,004 authors, scanned 2 days ago → /authors`, or
  `full author list — never run; a recent scan takes about 3 seconds → /authors`. It reads
  `GET /api/authors` for the two numbers and renders nothing else from it. This is the
  boundary that keeps the page safe, and it is stated here so that a future commit adding
  "just the top ten" has to argue with a sentence rather than with a habit.

- **The global lock is visible.** While any job runs, every other button on the page
  disables and says `waiting on the database walk`. Four buttons that silently do nothing
  while a fifth job runs is indistinguishable from four broken buttons.

---

## Part 5 — Endpoint amendments

### `POST /api/subscribers` — add after "Matching is host-only"

**Saturation on this endpoint is a REFUSAL, not a label.** The exempt purge form requires
the subscriber cache to be present, under 7 days old, **and not saturated**. When
`SUBSCRIBER_SCAN_LIMIT` is reached the returned list is a FLOOR: subscribers exist that
were not read, and a `delete` built from it destroys the gift wraps of people this relay is
the DM inbox for. That is the single worst outcome this document is trying to prevent, and
the existing guards do not catch it — an absent cache and a stale cache are both refused,
while a saturated one looks like a complete answer with a fresh timestamp.

The asymmetry with recipients is deliberate and must not be "fixed":

- **Recipients saturating is safe.** Fewer named recipients means a shorter `#p` list means
  less deletion. Wrong in the harmless direction.
- **Subscribers saturating is unsafe.** Fewer named subscribers means fewer exemptions means
  more deletion, of exactly the events that must not be deleted.

Kind 10050 and 10002 are replaceable events, so their totals are bounded by distinct users
rather than by traffic, and 50,000 should hold for a long time — but "should hold" is what
`REPORT_SCAN_LIMIT` said too. Run `scan --count` on each kind before the read and return
`counted: {"10050": <int>, "10002": <int>}` alongside `saturated`; two index counts cost
nothing and turn saturation from an inference into a fact. The page states it in words:
`subscriber scan hit its 50,000-event cap — the exempt purge command is unavailable until
the cap is raised, because a floor cannot be used as an exemption list`.

An unset `relay.info.url` is a `failed` run under Part 2, not a result: no `scanned_at`,
no cache replacement, `error: "relay.info.url is not set in /config/strfry.conf"`.

### `POST /api/report/totals` — replace the `gap_share` paragraph

`gap_events = total_events − allowlist_events − giftwrap_events` and
`gap_share = gap_events / (total_events − giftwrap_events)`. **These numbers are EXACT.**
They come from the index, there is no window, no `limit`, and therefore no `saturated`
field to render — the one place in this project where a count is simply a count.

Add `gap_level: "ok" | "notice" | "stale"` computed per Part 3. The endpoint can return
`"ok"` and `"notice"` from its own three counts; it returns `"stale"` only when a walk
record is present, no older than this totals record, and contains a kind at or above
`KIND_ALARM_SHARE` — in which case it also returns `gap_alarm_kind` and
`gap_alarm_events`. Without walk data it never returns `"stale"`, and `"notice"` carries
`needs_walk: true` so the page's wording defers instead of guessing. `gap_share` alone is
not a verdict and the page must not turn it into one.

### `POST /api/report/walk` — add to the `unlisted_kinds` bullet

`unlisted_kinds` is returned in full and rendered capped, per **Rendering results**. It is a
census, not a task list; the head confirms the shape and `KIND_ALARM_SHARE` is what makes any
row a task. Return `unlisted_total` (the summed event count) and `unlisted_kind_count`
alongside it so the page's tail summary needs no client-side arithmetic over 444 keys.

### `GET /api/authors` — add

When the cache is empty the response is unchanged (`{"status": "idle", "scanned_at": null,
"authors": []}`) and still starts nothing, forever. It additionally carries
`modes: {"recent": {"events": 20000, "typical_seconds": 3}, "full": {...}}` — the constants
already in `AUTHOR_SCAN_MODES`, plus the measured duration of the last successful run of
each mode ON THIS RELAY, null until one exists. This is what lets an empty state name the
cost of the cheapest press without any page inventing a number.

---

## Part 6 — Client-page amendments

### Record lines — add to the existing rules

**A record line carries at most three controls: `↩`, `Undo`, and (for multi-pubkey records)
the expand arrow. It carries no input, no reason field, and no third-party control, on any
page.** The live report page rendered a `reason — shown publicly` input and a `Set reason`
button inside every record line — a bulk public-publishing control, duplicated per line, on
a page specified to hold no ban or unban control at all. The bulk-reason row is
`bans.html`-only, exists exactly once, is bound to the checked set, and is not part of the
record-line component. A record line has no checked set; a reason control there acts on
nothing or on something unstated, and both are worse than absent.

**And the report page's own rule needs restating honestly.** It currently reads "no
checkbox, no ban control, no unban control anywhere in its DOM," which the record lines
already contradict: `Undo` on a ban record POSTs `/api/unban`. The accurate rule, which is
the one worth keeping:

> `report.html` has no list, no checkbox, no selection, and no control that acts on a set
> the page itself assembled. The only write it can perform is the undo of an action already
> recorded in this browser, against pubkeys named on the record itself.

That is a real safety property and it survives contact with the record lines. The previous
wording did not, and a rule known to be false in one place stops constraining the next
place.

### `authors.html` — replace the logged-out/empty bullet

- **Logged out**: heading, navigation links, and the login button only. No list, no scan
  control, no filter — nothing that could start work or leak a list. There is no public view
  of this page.

- **Logged in with no scan result** is an empty state, not a blank page, and it is the state
  every new operator meets first. Render the scan panel exactly as the report page does —
  `never run`, both cost lines, both buttons live — above one plain sentence naming what the
  list will contain and which press is cheap: `no scan yet. A recent scan reads the newest
  20,000 events and takes about three seconds; the full author list takes about two minutes
  and you can close the tab while it runs.` Durations come from `modes` in
  `GET /api/authors` — measured on this relay when a run exists, the mode's documented shape
  when it does not. Never a number from the developer's relay.

- **`recent` is the DEFAULT radio selection.** The three-second press is the one that turns
  an empty page into a working one, and defaulting to the two-minute press is how an
  operator's first experience of this page becomes a wait they did not choose.

### Every page — the profile entry field

The live pages label this field `npub, hex, or domain` and route a domain to
`/domain?d=<domain>`. The doc says npub or 64-hex. The drift is an improvement and the doc
should adopt it rather than the code reverting: one field, three accepted forms, routing to
`/profile?npub=` or `/domain?d=` by what was typed. Invalid input sets a plain status line
and navigates nowhere. **A domain typed here navigates; it never bans** — that distinction
is the reason `domain.html` exists and the reason the manual ban form still refuses domains.

---

## Part 7 — Reference measurements

Replace the reference-relay figures with the current ones and mark the block with its date.
Every figure quoted anywhere in this document states whether it is DB-wide or from a window.

Reference relay, **2026-07-30**: **2,642,995 events, of which 1,709,910 are kind 1059
(64.7% DB-wide)**. Non-gift-wrap: 933,085. In `AUTHOR_SCAN_KINDS`: 914,357 (98.0%).
Unlisted: 18,728 by index arithmetic, 18,734 by walk histogram across 444 kinds — the
6-event difference is events written during the 8m37s walk, per Part 4.

| Measurement | Result |
|---|---|
| `scan --count` ×3 (totals) | 10s |
| Unfiltered `scan '{}'` walk | 2,642,995 events in **8m 37s** (~5,113/s) |
| Distinct non-gift-wrap authors | 49,009 |
| Largest unlisted kind | kind 43, 735 events (0.079% of non-gift-wrap) |
| Unlisted kinds holding ≤2 events | 165 kinds, 218 events total |
| Recipient scan at `RECIPIENT_SCAN_LIMIT` | 250,000 events, 1,861 distinct recipients, SATURATED |

**The recipient scan is saturating at 14.6% of gift wraps** (250,000 of 1,709,910) and its
window is 47 days of a much longer history. It stays saturated, and that is acceptable
*only* because of the direction of the error: a shorter recipient list deletes less. State
it on the panel in those terms rather than as a bare `SATURATED`, and never raise the limit
to "fix" it without re-reading the subscriber rule in Part 5, where the same word means the
opposite thing.

**Two figures in the previous measurement block could not both be true.** The composition
sample listed kind `20001:387` in a 20,000-event recent window while the whole-database walk
lists kind `20001: 224`; a window count cannot exceed a database count. The "known open
item" about kind 20001 rested on the larger figure. Resolved on the current data: 224 events
DB-wide is 0.024% of non-gift-wrap, two orders of magnitude below `KIND_ALARM_SHARE`, so
**20001 does not belong in `AUTHOR_SCAN_KINDS`** and the open item is closed. The stale
figure is deleted rather than corrected — its provenance is unknown, and an unsourced number
in this block is what caused the confusion.

Retain the composition block, the 1.000 gift-wrap singleton ratio and its three
consequences, and the `~1,500,000 non-giftwrap ceiling` paragraph unchanged — none of them
is affected, and the ceiling paragraph is still the right thing for a future maintainer to
read before raising `AUTHOR_SCAN_DEADLINE`.

---

## Part 8 — `test.sh` additions

Add to the existing assertions. Every one of these covers a failure that is silent by
nature, which is the bar the rest of the suite already meets.

- **The gap ladder.** A synthetic totals record at 2.008% with a walk whose largest unlisted
  kind is 0.079% yields `gap_level: "notice"` and `needs_walk: false`, and the rendered
  wording contains neither "stale" nor a recommendation to add a kind. The same totals with a
  walk containing one kind at 27.9% yields `"stale"` and names that kind. Totals with no
  walk record never yields `"stale"`. This is the assertion that would have caught the false
  alarm before it shipped.
- **`failed` is not `result`.** A subscriber scan with `relay.info.url` unset leaves
  `scanned_at` unchanged, leaves the previous cache byte-identical, sets `error`, and the
  rendered status line contains no timestamp attributable to the failed run. Same for a
  missing strfry binary and for `--count` returning non-numeric output.
- **Saturation refuses the exempt purge.** With a present, fresh, but SATURATED subscriber
  cache, the gift-wrap purge intent renders the blanket form only and states why. Assert the
  emitted command does not contain a `#p` exemption list — this is the one test standing
  between a saturated scan and a command that deletes subscribers' DMs.
- **Caps hold.** A 444-key `unlisted_kinds` renders `FIGURE_HEAD_MAX` rows plus one tail
  line whose stated event total equals the sum of the omitted keys, and the `<details>`
  contains all 444. A 12,481-row author list renders `RENDER_MAX` rows and filters against
  all 12,481.
- **No record line contains an `<input>`**, on any page, in any state.
- **`report.html` contains no control bound to a page-assembled set** — assert the rendered
  DOM has no `<input type="checkbox">` and no button whose handler reads a selection.
- **The empty authors page offers the cheap press**: with an empty cache, the rendered page
  contains both mode buttons, `recent` is the checked radio, and the empty-state sentence
  names a duration sourced from `modes` rather than a literal in the HTML.

---

## Part 9 — New project-wide section: **Theme (dark mode)**

Insert as a new top-level section immediately after **Rendering results** (Part 2) — the
two are the project's only client-wide visual constraints, one governing what reaches the
page and the other governing how the page is lit while displaying it.

### Theme (dark mode)

**No page in this project chooses a color, and the theme toggle does not change that.**
Every page is unstyled browser-default HTML — the whole locked-`<style>`-block discipline
elsewhere in this document exists to keep it that way. The mechanism here is
`color-scheme`, a CSS property that does not set colors itself; it tells the browser which
of *its own* tested light/dark palettes to paint the page's background, text, form
controls, and links with. We never pick a hex value, so we never get a contrast pair
wrong — readability is the browser's own guarantee, the same one it gives its own UI, not
something computed in this project.

```css
:root { color-scheme: light dark; }
#theme-btn { position: fixed; top: 0.5em; right: 0.5em; }
body { background: Canvas; color: CanvasText; }
```

`color-scheme: light dark` on `:root` is what makes **auto** (the default, nothing
overridden) follow the OS/browser dark-mode signal with zero script involvement — this
alone is the entire feature for an operator who never touches the toggle.

**The explicit `background: Canvas; color: CanvasText` on `body` is required, not
decorative, and was found by testing the toggle rather than by reasoning about the spec.**
Relying on the browser's implicit unset-background painting (no author `background`
declared at all) does not reliably repaint when `color-scheme` is changed *after* the page
has already rendered — text color recomputed on a click in testing, the page background
did not, leaving black text on a black background until the next full navigation. Declaring
`Canvas`/`CanvasText` explicitly moves that paint out of the implicit fast path and into
ordinary style recomputation, which does repaint correctly on every toggle press. Do not
remove these two declarations in the name of minimalism; they are what make the toggle safe
to ship rather than a readability trap on a click.

**The toggle** is one `<button id="theme-btn">` on every page, pinned to the top-right
corner of the viewport by the `position: fixed` rule above (it lives in the DOM next to
`Login with extension`; the fixed rule is what actually places it, so it stays put through
scrolling and reads as chrome rather than page content). Cycling is **auto → light → dark →
auto**, wired by one shared function (`s86WireThemeToggle` in `common86.js`) so the click
behavior, the glyph/label wording, and the storage key (`strfry86_theme` in `localStorage`,
values `"light"` / `"dark"`, absent means auto) exist in exactly one place. Choosing
`light` or `dark` sets `document.documentElement.style.colorScheme` directly, which wins
over the stylesheet rule regardless of source order and persists across reloads until
cycled back to auto, at which point the stored key is removed rather than written as
`"auto"` — absence IS the auto state, so there is only ever one way to represent it.

**The button shows exactly two glyphs, ☀ and ☾, never a third for auto.** Auto has no icon
of its own; it shows whichever glyph matches what the OS is resolving to *right now*, read
via `matchMedia('(prefers-color-scheme: dark)')` — the glyph is always an honest
description of what's on screen, not a state the operator has to decode. A `change`
listener on that same media query updates the glyph live while on auto, without a reload,
matching the same signal `color-scheme: light dark` already reacts to in CSS. Hover text
(`title`, duplicated onto `aria-label` per this project's existing icon-button convention —
see the record-line dismiss/undo/expand buttons) always states BOTH the current state and
what the next click does, e.g. `theme: auto, dark — click for light`, so the icon is never
the only source of truth for what pressing it will do.

**A tiny inline `<script>` at the top of `<head>`, before `<title>` and before the
`<style>` block, applies a stored override immediately**: it reads `strfry86_theme` and, if
it names a real override, sets `colorScheme` on the root element right there — before
`common86.js` loads, before the body parses, before first paint. This is the one thing that
has to run early rather than through the shared script tag at the bottom of `<body>`, for
the same reason the shared script can't run first: a page that fetched a stale
dark-preferring page then painted light for a moment while `common86.js` loaded would flash,
and a flash on every navigation is worse than the two extra lines duplicated across five
files. Duplicating it is deliberate, not an oversight — see the duplicated `<style>` blocks
elsewhere in this project for the precedent.

**Nothing here is a fifth thing to keep in sync.** The toggle, the anti-flash script, and
the `:root`/`body` rules are the same three ingredients on every page, character-for-character;
a page missing any one of them is a bug, not a variant.