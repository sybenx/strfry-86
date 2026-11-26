# Spam rules: first replay

Draft rules in `tools/spam_rules.py`, replayed 2026-11-25 against public
posts read from syb.lol, nos.lol and relay.primal.net (the `#airdrop`,
`#ethereum` and `#nostr` hashtag feeds, plus syb.lol's latest 6,000 notes).
Profiles came from syb.lol, purplepag.es and relay.damus.io; 1,034 of the 1,576
authors had one.

Nobody has flagged anything through Eighty Six yet, so the "spam" label here
is a stand-in: every post containing the `one.evmverse.cc` airdrop link
(357 posts from 308 keys). The rules never look at that domain. Everything
else (8,347 posts) is "other", unlabeled.

## Rules

| Rule | Tier | Fires when |
|---|---|---|
| `ref_link_burst` | reject | the post links a host that 10+ distinct keys linked within one hour, each link carrying a referral code (`?ref=`, `?invite=`, …) |
| `homoglyph_link` | reject | the post has a link and a mostly-Latin word with Cyrillic, Greek, Cherokee or similar look-alike letters mixed in |
| `stuffed_ref_link` | review | a referral link plus 5+ hashtags |
| `new_key_ref_link` | review | a referral link from a key with no profile |
| `stuffed_new_key` | review | 5+ hashtags from a key with no profile |

"Reject" rules are meant to block on their own. "Review" rules only queue a
post for a person.

## Results

| Set | Posts | Reject | Review only | Passed |
|---|---|---|---|---|
| spam (evmverse) | 357 | 355 | 0 | 2 |
| other | 8,347 | 979 | 183 | 7,185 |

- **The 2 spam posts that passed are not spam.** They are people writing
  about the campaign ("You can just mute one.evmverse.cc…"). The stand-in
  label was wrong for them.
- **All 979 rejects in "other" are two more airdrop campaigns**, found by the
  burst rule without being told about them: `ai.evmnest.cc` (501) and
  `gweiportal.top` (478). Same shape: fresh key, fresh `?ref=` code per post.
  There were no false rejects.
- **Of the 183 review-only posts:** 132 are recognizable scams (a French
  "leaked private key" lure, a multilingual "Pearl (PRL) snapshot"
  signature phish, a pump.fun promo code). The other 51 are automated
  accounts with no profile: crypto market recaps, stock-photo posters,
  riddle bots, plus one account (PREDYX) posting its own referral link
  with hashtags. Those 51 are judgment calls.

## What changed during the replay

The first draft flagged too much. Each change below came from looking at
what it caught wrongly:

- **"No profile" is a weak signal for this campaign.** 352 of the 357 evmverse
  posts come from keys that have a kind 0 profile.
- **Jumbled capitals was dropped.** It fired on CamelCase names ("RedBullTv",
  "MeshPunk") and random media URL ids.
- **Homoglyphs are now look-alike scripts only, in mostly-Latin words.**
  Latin next to Chinese ("ai乱真了") and a Latin slip in a Russian word
  ("идempотентность") no longer count.
- **A referral link plus hashtags dropped to review.** One account reusing
  its own code is promotion, not a campaign. The burst rule (many keys,
  each with a fresh code) carries the reject tier.

## Limits

- The corpus comes from hashtag feeds, which is where this spam lives, so
  "other" is spammier than an ordinary feed.
- The burst rule needs the last hour of traffic. At write time that means
  keeping a small rolling window per host in memory. Nothing in this branch
  does that yet: the rules are not wired into any write path.
- Profile coverage was partial (1,034 of 1,576), so some "no profile" keys
  may have one somewhere.
- The raw corpus is not committed. It is other people's posts, kept in a
  local scratch folder only.
