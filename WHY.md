# WHY.md — rationales for CLAUDE.md rules

Load only when a rule's purpose is unclear. Not part of the agent context by default.

## Rendering & panel states

The original doc specified *what* every endpoint returns and never *how much* reaches the page, so every renderer defaulted to "all of it, in a sentence." A 444-key histogram rendered as comma-separated prose buried the single most important number (the gap). Shneiderman overview-first + progressive disclosure (`<details>`) is the fix. Semantic `<table>`/`<dl>` matches data shape; space-padded `<pre>` is presentational, shears on digit growth, and is opaque to screen readers. Reserve `<pre>` for genuine shell commands (with `pre-wrap` so long delete filters remain readable on mobile).

Four states exist because a scan that could not run (unset `relay_url`) previously stamped "just now" and looked like a successful empty result — the most dangerous failure mode. `error` vs `warning` and "never stamp scanned_at on failure" close it. Single message slot prevents the same warning appearing twice.

## Allowlist thresholds

Calibrated on the real miss that justified the feature: kind 2003 at 258 290 events (27.9 % of non-gift-wrap). A single 2 % threshold also fired on the healthy long tail (18 728 events = 2.008 % spread across 444 kinds, largest 735 = 0.079 %). That produced a permanent false positive that trained operators to ignore the page. Splitting size (`GAP_NOTICE_SHARE`) from actionability (`KIND_ALARM_SHARE` = 0.5 %) makes only a concentrated missing kind alarm. Index counts alone cannot see composition, so totals never emits `"stale"` without a fresh walk.

## Subscriber vs recipient saturation

Recipients saturating → shorter `#p` list → less deletion (safe direction). Subscribers saturating → fewer exemptions → more deletion of exactly the DMs this relay holds for its users (the worst outcome the project exists to prevent). Existing absence/staleness guards did not catch a fresh-but-saturated cache. Refusal + explicit wording + test that the emitted command contains no `#p` list is the only hard barrier.

## Live tracking

A pure subscription sees only new events after connect; it cannot reconstruct the 2.6 M-event baseline nor observe strfry's own expiration/purge (only kind-5 deletes). Hence live is a delta on top of a cached walk, and the page must state both numbers. "Auto-tick with deletions" is therefore only partial.

## Userlist columns

Gift-wrap p-tag counts come from the permanently saturated recipient scan (250 k of 1.7 M). The number is a floor for almost every user; labelling `≥ n` is honest. Report counts (kind 1984) are a new data source; progressive fill with `—` (never blank, never premature 0) preserves the four-state rule that an unknown must not look like a measured zero.

## Audit page & safety property

Original property ("only undo of an action already recorded in this browser") cannot survive a cross-session audit log. Server-side log + rewritten bound ("undo of a recorded admin action, bounded by audit retention and auth") is the honest replacement when more than one admin exists.

## Style block

Pinned search, consistent nav, and a logo that is clickable but does not look like a link require a small, enumerated addition to the previously locked four-rule block. Re-opening once for a fixed header ruleset is preferable to either (a) violating the lock or (b) shipping a non-compliant header. Theme `Canvas`/`CanvasText` pair is required for correct repaint on toggle after first paint; do not remove.

## Terminal allowlist

Full command passthrough would make every saturation/absence/staleness guard decorative. Allowlisting non-destructive verbs keeps the live log useful while preserving the safety model and keeping the saturation test meaningful.

## Activity feed public surface

Hardest prior client rule was "logged out = heading + nav + login, nothing that could leak a list." A public landing feed is still useful if it shows only kind + timestamp + event id. No pubkeys, no content.

## Compression choice

Rules-only CLAUDE.md (~180 lines) + separate WHY.md is the only way to stay under the ~200-line adherence budget while retaining the calibration arguments that stop a future minimalist from undoing KIND_ALARM_SHARE, Canvas/CanvasText, pre-wrap, or the subscriber asymmetry.