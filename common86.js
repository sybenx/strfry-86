// Shared client code for bans.html and authors.html: NIP-07 login (remembered
// per-browser in localStorage), NIP-98 signing, filter, select-all + live
// count, textContent-only rendering helpers, timestamps, record lines, the
// purge-command builder, and the command blocks. Duplicating any of this
// across the two pages is how a security-relevant fix lands in one file and
// not the other — so it all lives here instead.

var S86_ADMIN_KEY = 'strfry86_admin_pubkey';
var S86_RECORDS_KEY = 'strfry86_records';
var S86_DISMISSED_REPORTS_KEY = 'strfry86_dismissed_reports';
var S86_MAX_RECORDS_STORED = 200;
var S86_MAX_RECORDS_RENDERED = 20;
var S86_RECORDS_INLINE = 3;
var S86_MAX_DISMISSED = 1000;
var S86_REASON_UNDO_MAX = 50;

// --- localStorage helpers ---------------------------------------------------

function s86LoadStored(key) {
  try {
    var val = JSON.parse(localStorage.getItem(key));
    return Array.isArray(val) ? val : [];
  } catch (e) {
    return [];
  }
}

function s86SaveStored(key, arr, max) {
  try {
    localStorage.setItem(key, JSON.stringify(arr.slice(-max)));
  } catch (e) {
    // ignore quota/availability errors — this is UI state, not source of truth
  }
}

// --- generic DOM / formatting helpers ---------------------------------------

function s86El(tag, text) {
  var el = document.createElement(tag);
  if (text != null) {
    el.textContent = text;
  }
  return el;
}

function s86FormatDate(unixSeconds) {
  if (!unixSeconds) {
    return 'unknown date';
  }
  var iso = new Date(unixSeconds * 1000).toISOString();
  return iso.slice(0, 10) + '@' + iso.slice(11, 16) + ' UTC';
}

// npub is always the display text. When isAdmin and hex is known, the link
// goes to this relay's own profile page instead of njump — the admin has
// a strictly more useful place to look. Logged out (or hex unavailable),
// njump remains the only place to look, exactly as before.
function s86NpubLink(npub, hex, isAdmin) {
  var a = document.createElement('a');
  a.href = (isAdmin && hex) ? ('/profile?hex=' + hex) : ('https://njump.me/' + npub);
  a.target = '_blank';
  a.textContent = npub;
  return a;
}

function s86ErrMsg(result) {
  return (result.body && result.body.error) ? result.body.error : 'unknown error';
}

// --- minimal bech32 decode (BIP-173), decode-only ---------------------------
// The server already returns npub strings for display; this is only needed
// so the command-block pubkey input can accept an npub and turn it into the
// hex a `strfry` filter needs.

var S86_BECH32_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l';

function s86Bech32Polymod(values) {
  var generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
  var chk = 1;
  for (var i = 0; i < values.length; i++) {
    var top = chk >>> 25;
    chk = ((chk & 0x1ffffff) << 5) ^ values[i];
    for (var j = 0; j < 5; j++) {
      chk ^= ((top >>> j) & 1) ? generator[j] : 0;
    }
  }
  return chk >>> 0;
}

function s86Bech32HrpExpand(hrp) {
  var ret = [];
  for (var i = 0; i < hrp.length; i++) {
    ret.push(hrp.charCodeAt(i) >>> 5);
  }
  ret.push(0);
  for (var j = 0; j < hrp.length; j++) {
    ret.push(hrp.charCodeAt(j) & 31);
  }
  return ret;
}

function s86Bech32Decode(bech) {
  bech = bech.toLowerCase();
  var pos = bech.lastIndexOf('1');
  if (pos < 1 || pos + 7 > bech.length || bech.length > 90) {
    return null;
  }
  var data = [];
  for (var i = pos + 1; i < bech.length; i++) {
    var d = S86_BECH32_CHARSET.indexOf(bech[i]);
    if (d === -1) {
      return null;
    }
    data.push(d);
  }
  var hrp = bech.slice(0, pos);
  if (s86Bech32Polymod(s86Bech32HrpExpand(hrp).concat(data)) !== 1) {
    return null;
  }
  return { hrp: hrp, data: data.slice(0, -6) };
}

function s86ConvertBits(data, frombits, tobits) {
  var acc = 0, bits = 0, ret = [];
  var maxv = (1 << tobits) - 1;
  for (var i = 0; i < data.length; i++) {
    var value = data[i];
    if (value < 0 || (value >> frombits)) {
      return null;
    }
    acc = ((acc << frombits) | value) >>> 0;
    bits += frombits;
    while (bits >= tobits) {
      bits -= tobits;
      ret.push((acc >>> bits) & maxv);
    }
  }
  if (bits >= frombits || ((acc << (tobits - bits)) & maxv)) {
    return null;
  }
  return ret;
}

function s86NpubToHex(npub) {
  var decoded = s86Bech32Decode(npub);
  if (!decoded || decoded.hrp !== 'npub') {
    return null;
  }
  var bytes = s86ConvertBits(decoded.data, 5, 8);
  if (!bytes || bytes.length !== 32) {
    return null;
  }
  var hex = '';
  for (var i = 0; i < bytes.length; i++) {
    hex += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
  }
  return hex;
}

function s86PubkeyInputToHex(raw) {
  raw = (raw || '').trim();
  if (/^[0-9a-fA-F]{64}$/.test(raw)) {
    return raw.toLowerCase();
  }
  if (raw.indexOf('npub1') === 0) {
    return s86NpubToHex(raw);
  }
  return null;
}

// --- profile entry field -------------------------------------------------
// One <input type="search"> + "Go" button, present near-identically
// directly above the command generator on every admin page. A typed/pasted
// entry rather than a link on every row — that is what keeps npubs
// copyable plain text everywhere else instead of turning each one into a
// touch target. Invalid input sets a plain status line and navigates
// nowhere; Enter in the field submits it same as the button.
//
// A pubkey (npub or hex) wins first attempt — s86PubkeyInputToHex is
// checked before domain validation, so a bare 64-hex string is never
// mistaken for a hostname. Anything else is tried as a domain via the same
// s86ValidateDomainInput used by the command generator's "fetch domain"
// intent, so "asdf.com" typed here lands on that domain's own roster page
// instead of erroring.
function s86BuildProfileEntryField(statusEl) {
  var p = document.createElement('p');

  var input = document.createElement('input');
  input.type = 'search';
  input.placeholder = 'npub, hex, or domain';
  // size is an HTML attribute, not a CSS rule, so it doesn't touch the
  // locked per-page <style> block — plain enough for the placeholder to
  // read in full instead of clipping at the browser default ~20 chars.
  input.size = 40;

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = 'Go';

  function go() {
    var hex = s86PubkeyInputToHex(input.value);
    if (hex) {
      window.location.href = '/profile?hex=' + hex;
      return;
    }
    var domain = s86ValidateDomainInput(input.value);
    if (domain) {
      window.location.href = '/domain?d=' + encodeURIComponent(domain);
      return;
    }
    statusEl.textContent = 'enter a valid npub, hex pubkey, or domain';
  }

  btn.addEventListener('click', go);
  input.addEventListener('keydown', function (evt) {
    if (evt.key === 'Enter') {
      evt.preventDefault();
      go();
    }
  });

  p.appendChild(input);
  p.appendChild(document.createTextNode(' '));
  p.appendChild(btn);
  return p;
}

// --- login (NIP-07, remembered in localStorage) -----------------------------
// This grants nothing by itself: there are no sessions or tokens, every
// privileged action is still individually NIP-98 signed and verified
// server-side. A hand-edited localStorage value just produces a page full of
// buttons that all fail server-side. It only saves re-prompting the NIP-07
// extension on every page load / navigation between the two pages.

function s86GetStoredAdmin() {
  try {
    return localStorage.getItem(S86_ADMIN_KEY);
  } catch (e) {
    return null;
  }
}

function s86SetStoredAdmin(pubkey) {
  try {
    localStorage.setItem(S86_ADMIN_KEY, pubkey);
  } catch (e) {
    // ignore — worst case, re-prompts on next load
  }
}

function s86ClearStoredAdmin() {
  try {
    localStorage.removeItem(S86_ADMIN_KEY);
  } catch (e) {}
}

// getAdminPubkey() returns the server-declared admin hex once known (from
// /api/banned's `admin` field), or null before that fetch resolves.
function s86WireLogin(loginBtn, statusEl, getAdminPubkey, onLogin) {
  loginBtn.addEventListener('click', function () {
    if (s86GetStoredAdmin()) {
      s86ClearStoredAdmin();
      window.location.reload();
      return;
    }
    if (!window.nostr) {
      statusEl.textContent = 'a NIP-07 extension is required';
      return;
    }
    window.nostr.getPublicKey()
      .then(function (pk) {
        var admin = getAdminPubkey();
        if (pk !== admin) {
          statusEl.textContent = 'this key is not the admin';
          return;
        }
        s86SetStoredAdmin(pk);
        loginBtn.textContent = 'Logout';
        statusEl.textContent = 'logged in as admin';
        onLogin(pk);
      })
      .catch(function () {
        statusEl.textContent = 'a NIP-07 extension is required';
      });
  });
}

// Called once the admin pubkey is known (after the first /api/banned fetch)
// to silently restore a remembered login without re-prompting the extension.
function s86TryAutoLogin(adminPubkey, loginBtn, statusEl, onLogin) {
  var remembered = s86GetStoredAdmin();
  if (remembered && remembered === adminPubkey) {
    loginBtn.textContent = 'Logout';
    statusEl.textContent = 'logged in as admin';
    onLogin(remembered);
    return true;
  }
  return false;
}

// --- NIP-98 signing -----------------------------------------------------

function s86SignAndPost(endpoint, extraBody) {
  var url = window.location.origin + endpoint;
  var event = {
    kind: 27235,
    created_at: Math.floor(Date.now() / 1000),
    tags: [['u', url], ['method', 'POST']],
    content: ''
  };

  return window.nostr.signEvent(event)
    .then(function (signed) {
      var body = Object.assign({ auth: signed }, extraBody);
      return fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
    })
    .then(function (res) {
      return res.json().then(function (body) {
        return { ok: res.ok, body: body };
      });
    });
}

// --- filter -----------------------------------------------------------------
// Scoped to one list's direct children only — a collapsed <details> command
// block stays in the DOM and must never be filtered along with the list.

function s86ApplyFilter(filterInput, listEl) {
  var query = filterInput.value.trim().toLowerCase();
  var items = listEl.children;
  for (var i = 0; i < items.length; i++) {
    var match = !query || items[i].textContent.toLowerCase().indexOf(query) !== -1;
    items[i].style.display = match ? '' : 'none';
  }
}

function s86WireFilter(filterInput, listEl) {
  filterInput.addEventListener('input', function () {
    s86ApplyFilter(filterInput, listEl);
  });
}

// --- select-visible + live count ---------------------------------------
// Labelled "Select visible" everywhere, never "Select all" — the control
// has always been filter-scoped, so the old label was lying by omission
// on a control that can mass-unban.

function s86UpdateSelectedCount(listEl, checkboxClass, countEl, extraButtons) {
  var n = listEl.querySelectorAll('.' + checkboxClass + ':checked').length;
  var shown = 0;
  var items = listEl.children;
  for (var i = 0; i < items.length; i++) {
    if (items[i].style.display !== 'none') {
      shown++;
    }
  }
  countEl.textContent = n > 0 ? n + ' selected of ' + shown + ' shown' : '';
  (extraButtons || []).forEach(function (btn) {
    btn.disabled = n === 0;
  });
}

function s86WireSelectAll(selectAllEl, listEl, checkboxClass, countEl, extraButtons) {
  selectAllEl.addEventListener('change', function () {
    var boxes = listEl.querySelectorAll('.' + checkboxClass);
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].closest('li').style.display === 'none') {
        continue;
      }
      boxes[i].checked = selectAllEl.checked;
    }
    s86UpdateSelectedCount(listEl, checkboxClass, countEl, extraButtons);
  });
  listEl.addEventListener('change', function () {
    s86UpdateSelectedCount(listEl, checkboxClass, countEl, extraButtons);
  });
}

// --- sort row -------------------------------------------------------------
// A plain row of <button>s: "sort: <field> · <field> · …", the active one
// bold with a trailing arrow, a second click on the active field reversing
// direction. Shared because the rules are the same everywhere a list has
// more than one meaningful order: re-sort clears the checked set and resets
// select-visible (a selection carried across a re-order is the mass-unban
// failure mode in its purest form), and the caller must re-apply the filter
// after re-sorting — this helper only builds the control and returns the
// sorted array, it does not touch the DOM list itself.

function s86BuildSortRow(fields, initialField, initialDir, onChange) {
  var p = document.createElement('p');
  p.appendChild(document.createTextNode('sort: '));
  var state = { field: initialField, dir: initialDir };
  var buttons = {};

  function relabel() {
    fields.forEach(function (f) {
      var btn = buttons[f.key];
      btn.textContent = '';
      if (f.key === state.field) {
        var b = document.createElement('b');
        b.textContent = f.label + (state.dir === 'asc' ? ' ↑' : ' ↓');
        btn.appendChild(b);
      } else {
        btn.textContent = f.label;
      }
    });
  }

  fields.forEach(function (f, i) {
    if (i > 0) {
      p.appendChild(document.createTextNode(' · '));
    }
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.addEventListener('click', function () {
      if (state.field === f.key) {
        state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.field = f.key;
        state.dir = f.dir || 'desc';
      }
      relabel();
      onChange(state.field, state.dir);
    });
    buttons[f.key] = btn;
    p.appendChild(btn);
  });

  relabel();
  return p;
}

// cmpFn(a, b) returns ascending order; dir flips it when 'desc'.
function s86SortRows(rows, cmpFn, dir) {
  var sorted = rows.slice().sort(cmpFn);
  return dir === 'asc' ? sorted : sorted.reverse();
}

// --- render truncation ---------------------------------------------------
// No list renders more than `max` rows — applied AFTER sort and filter,
// never before, so filtering always searches the full result set.

function s86TruncateForRender(rows, max) {
  if (rows.length <= max) {
    return { shown: rows, truncatedCount: 0, totalCount: rows.length };
  }
  return { shown: rows.slice(0, max), truncatedCount: rows.length - max, totalCount: rows.length };
}

// --- copy purge command -------------------------------------------------

function s86BuildPurgeCommand(pubkeys) {
  return "strfry delete --filter '" + JSON.stringify({ authors: pubkeys }) + "'";
}

function s86WireCopyPurge(buttonEl, preEl, listEl, checkboxClass) {
  buttonEl.addEventListener('click', function () {
    var boxes = listEl.querySelectorAll('.' + checkboxClass + ':checked');
    if (boxes.length === 0) {
      return;
    }
    var pubkeys = [];
    for (var i = 0; i < boxes.length; i++) {
      pubkeys.push(boxes[i].value);
    }
    var cmd = s86BuildPurgeCommand(pubkeys);
    preEl.textContent = cmd;
    preEl.style.display = '';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(cmd).catch(function () {});
    }
  });
}

// --- command blocks -----------------------------------------------------
// Nothing here is executed and nothing here is fetched — every command is
// plain copyable text. Identical on every page.

// --- cached-scan panel ----------------------------------------------------
// Shared by report.html's four panels and by authors.html's own scan area
// (chrome only there — authors.html keeps its bespoke mode-radio UI, but
// renders its status line through s86ScanStatusText so the wording is
// identical everywhere a scan's progress is shown). heading / cost line /
// button / status line / result, in that fixed order. 'never run' is a
// visible state, not a hidden panel — the admin's first login shows the
// full inventory of what this page can tell them, empty, with the buttons
// that fill it.

var S86_JOB_LABELS = {
  authors: 'the author scan',
  recipients: 'the gift-wrap recipient scan',
  subscribers: 'the subscriber scan',
  'report-totals': 'the database totals refresh',
  'report-walk': 'the database walk'
};

function s86FormatRelativeAge(unixSeconds) {
  if (!unixSeconds) {
    return '';
  }
  var deltaSec = Math.max(0, Math.floor(Date.now() / 1000) - unixSeconds);
  if (deltaSec < 60) {
    return 'just now';
  }
  var mins = Math.floor(deltaSec / 60);
  if (mins < 60) {
    return mins + ' minute' + (mins === 1 ? '' : 's') + ' ago';
  }
  var hours = Math.floor(mins / 60);
  if (hours < 24) {
    return hours + ' hour' + (hours === 1 ? '' : 's') + ' ago';
  }
  var days = Math.floor(hours / 24);
  return days + ' day' + (days === 1 ? '' : 's') + ' ago';
}

// Absolute AND relative, always together: 'scanned 2026-07-26@14:02 UTC (3
// days ago)'. The absolute form is the locked project timestamp format;
// the relative form is what makes staleness legible at a glance on a page
// holding several results of several different ages.
function s86FormatScanAge(unixSeconds) {
  return 'scanned ' + s86FormatDate(unixSeconds) + ' (' + s86FormatRelativeAge(unixSeconds) + ')';
}

function s86FormatRate(rate) {
  return Math.max(0, Math.round(rate)).toLocaleString() + ' events/sec';
}

function s86FormatEta(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) {
    return 'estimating…';
  }
  if (seconds < 60) {
    return '<1 min left';
  }
  var mins = Math.round(seconds / 60);
  if (mins < 60) {
    return '~' + mins + ' min left';
  }
  var hours = Math.floor(mins / 60);
  var remMins = mins % 60;
  return '~' + hours + 'h' + (remMins ? ' ' + remMins + 'm' : '') + ' left';
}

function s86FormatDuration(seconds) {
  if (seconds == null) {
    return '';
  }
  var m = Math.floor(seconds / 60);
  var s = Math.round(seconds % 60);
  return m > 0 ? (m + 'm ' + (s < 10 ? '0' : '') + s + 's') : (s + 's');
}

// status: {status: 'idle'|'running', scanned_at, progress, total, rate,
// eta, blocked_by, warning}. The one function that renders a scan's
// progress line everywhere it appears, so 'scanning…' means the same
// thing on every page. No estimate is ever computed client-side beyond
// formatting the server's own numbers — the client may not invent a rate.
function s86ScanStatusText(status) {
  if (status.blocked_by) {
    return 'waiting on ' + (S86_JOB_LABELS[status.blocked_by] || status.blocked_by);
  }
  if (status.status === 'running') {
    var progress = status.progress || 0;
    if (status.total && status.rate) {
      var pct = Math.min(100, Math.floor(progress / status.total * 100));
      return 'scanning… ' + progress.toLocaleString() + ' of ' + status.total.toLocaleString()
        + ' events (' + pct + '%) — ' + s86FormatEta(status.eta) + ' at ' + s86FormatRate(status.rate);
    }
    if (status.total) {
      return 'scanning… ' + progress.toLocaleString() + ' of ' + status.total.toLocaleString() + ' events — estimating…';
    }
    return 'scanning… ' + progress.toLocaleString() + ' events read';
  }
  if (!status.scanned_at) {
    return 'never run';
  }
  var line = s86FormatScanAge(status.scanned_at);
  if (status.warning) {
    line += ' — ' + status.warning;
  }
  return line;
}

// opts: {
//   heading, costText (string shown before any run — describes the WORK,
//     never a duration), buttonLabel, onStart (function() -> Promise,
//     issues the signed POST),
//   renderResult(container, record) — record is never null when called,
//   lastRunNote(record) -> string|null, appended to the cost line once a
//     record exists ('last run: 9m 06s at 4,814 events/sec'). No duration
//     from a relay other than the operator's own is ever printed here.
//   extraNote(record) -> string|null — a staleness-consequence line.
// }
// Returns {el, applyStatus(jobStatus, record)}. The caller owns polling
// and auth; this only owns the DOM shape and the shared status wording.
function s86BuildScanPanel(opts) {
  var el = document.createElement('div');
  el.appendChild(s86El('h3', opts.heading));

  var costLine = s86El('p', opts.costText);
  el.appendChild(costLine);

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = opts.buttonLabel;
  var btnP = document.createElement('p');
  btnP.appendChild(btn);
  el.appendChild(btnP);

  var statusLine = s86El('p', 'never run');
  el.appendChild(statusLine);

  var resultEl = document.createElement('div');
  el.appendChild(resultEl);

  btn.addEventListener('click', function () {
    if (btn.disabled) {
      return;
    }
    btn.disabled = true;
    opts.onStart().catch(function () {
      statusLine.textContent = opts.buttonLabel + ' failed';
      btn.disabled = false;
    });
  });

  function applyStatus(jobStatus, record) {
    statusLine.textContent = s86ScanStatusText(jobStatus);
    btn.disabled = jobStatus.status === 'running' || !!jobStatus.blocked_by;

    resultEl.textContent = '';
    costLine.textContent = opts.costText;
    if (record) {
      if (opts.lastRunNote) {
        var note = opts.lastRunNote(record);
        if (note) {
          costLine.textContent = opts.costText + ' — ' + note;
        }
      }
      opts.renderResult(resultEl, record);
      if (opts.extraNote) {
        var extra = opts.extraNote(record);
        if (extra) {
          resultEl.appendChild(s86El('p', extra));
        }
      }
    }
  }

  return { el: el, applyStatus: applyStatus };
}

// --- generic status polling -----------------------------------------------
// fetchFn() -> Promise<data>; applyFn(data) renders it. Re-polls every 3s
// while data.status === 'running', stops otherwise. A failed fetch keeps
// polling rather than declaring failure — every one of these scans is
// server-side and unaffected by the browser's connection to it.
function s86PollStatus(fetchFn, applyFn, onError) {
  function poll() {
    fetchFn()
      .then(function (data) {
        applyFn(data);
        if (data.status === 'running') {
          setTimeout(poll, 3000);
        }
      })
      .catch(function () {
        if (onError) {
          onError();
        }
        setTimeout(poll, 3000);
      });
  }
  poll();
}

// --- command generator ---------------------------------------------------
// ONE <select> of intents, a FIELD SET that changes with the selection,
// ONE <pre> holding the rendered command, ONE copy button. Nothing here is
// executed, nothing is fetched by pressing anything in it, no output ever
// returns to the page — the terminal is where this project sends
// everything it refuses to do itself.
//
// The generator is now almost entirely destructive: every non-destructive
// intent it used to carry is a live button somewhere else (report.html's
// totals/walk panels, GET /api/subscribers, domain.html's own fetch). Each
// of those was a second implementation of a question the software already
// answered — a command block duplicating a working button is how the
// block goes stale without anyone noticing. 'Event kinds by author,
// lifetime' is the one survivor, because it is not a duplicate:
// /api/profile reports the kind tally over the most recent
// PROFILE_EVENT_LIMIT events, and this is the whole-history version.
//
// Fields are typed, and the type is validated before substitution. A
// pubkey is decoded and re-encoded to canonical hex before it reaches the
// <pre>; days is an integer; a kind is an integer, offered as a
// <datalist> for convenience but never restricted to it. Nothing here
// executes, but a tool that renders whatever it's handed teaches a habit
// that's wrong everywhere else in this project.

var S86_GIFTWRAP_PURGE_DEFAULT_DAYS = 90;
var S86_SUBSCRIBER_CACHE_STALE_SECONDS = 7 * 24 * 3600;
var S86_GIFTWRAP_PURGE_CHUNK_SIZE = 200;
var S86_STRFRY_CONFIG_FLAG = '--config /config/strfry.conf';
var S86_KNOWN_KINDS_LIST = [
  [0, 'profile'], [1, 'note'], [5, 'deletion'], [6, 'repost'], [7, 'reaction'],
  [1059, 'gift wrap'], [1984, 'report'], [9735, 'zap receipt'],
  [10002, 'relay list'], [10050, 'DM relay list'], [30023, 'long-form']
];

function s86ValidateDomainInput(raw) {
  raw = (raw || '').trim();
  if (!raw) {
    return null;
  }
  var value = raw.indexOf('://') === -1 ? 'http://' + raw : raw;
  try {
    var u = new URL(value);
    return u.hostname ? u.hostname.toLowerCase() : null;
  } catch (e) {
    return null;
  }
}

// Returns {ok, value}. Empty input is valid (defaults to
// S86_GIFTWRAP_PURGE_DEFAULT_DAYS); anything present that isn't a
// positive integer is rejected outright rather than silently coerced.
function s86ValidateDaysInput(raw) {
  raw = (raw || '').trim();
  if (!raw) {
    return { ok: true, value: S86_GIFTWRAP_PURGE_DEFAULT_DAYS };
  }
  if (!/^[0-9]+$/.test(raw)) {
    return { ok: false, value: null };
  }
  var n = parseInt(raw, 10);
  return n > 0 ? { ok: true, value: n } : { ok: false, value: null };
}

// Returns {ok, value}. Blank is valid and means 'no time bound' (value:
// null) — a distinct, louder state from a default, since 'delete all
// events by this author, all time' is not the same claim as 'the last 90
// days' and must never be reached by leaving a field empty by accident.
function s86ValidateOptionalDaysInput(raw) {
  raw = (raw || '').trim();
  if (!raw) {
    return { ok: true, value: null };
  }
  if (!/^[0-9]+$/.test(raw)) {
    return { ok: false, value: null };
  }
  var n = parseInt(raw, 10);
  return n > 0 ? { ok: true, value: n } : { ok: false, value: null };
}

// Returns {ok, value}. Blank is valid (value: null, no kind filter);
// present-but-non-numeric is rejected rather than silently ignored.
function s86ValidateOptionalKindInput(raw) {
  raw = (raw || '').trim();
  if (!raw) {
    return { ok: true, value: null };
  }
  if (!/^[0-9]+$/.test(raw)) {
    return { ok: false, value: null };
  }
  return { ok: true, value: parseInt(raw, 10) };
}

// A small json-lines tally, written in python3 rather than jq/awk because
// python3 is a hard requirement of this whole project (server86.py and
// plugin86.py both run on it inside the operator's own container), so it
// is guaranteed present — unlike jq, and unlike an awk one-liner naive
// quote-splitting would need, which breaks the moment `content` contains
// an internal quote.
function s86PyKindTallyScript() {
  return [
    'import sys, json',
    'total = 0',
    'kinds = {}',
    'for line in sys.stdin:',
    '    line = line.strip()',
    '    if not line:',
    '        continue',
    '    try:',
    '        e = json.loads(line)',
    '    except ValueError:',
    '        continue',
    '    total += 1',
    '    k = e.get("kind")',
    '    kinds[k] = kinds.get(k, 0) + 1',
    'print("total events:", total)',
    'print("kind histogram:")',
    'for k, c in sorted(kinds.items(), key=lambda kv: -kv[1]):',
    '    print("  kind", k, ":", c)',
  ].join('\n');
}

function s86RenderDeleteWithCountFirst(filterObj) {
  var filterJson = JSON.stringify(filterObj);
  return [
    "strfry " + S86_STRFRY_CONFIG_FLAG + " scan --count '" + filterJson + "'  # read this count BEFORE deleting",
    "strfry " + S86_STRFRY_CONFIG_FLAG + " delete --filter '" + filterJson + "'",
  ].join('\n');
}

// Both GET /api/recipients and GET /api/subscribers are unauthenticated
// public reads (same stance as GET /api/authors — only the scan-
// triggering POST costs a NIP-98 signature), so fetching them here on
// construction is harmless even for a not-yet-logged-in visitor; the
// generator itself stays hidden behind each page's own admin gate.
function s86WireGiftwrapPurgeSources(onUpdate) {
  var recipientsCache = null;
  var subscribersCache = null;

  function refresh(endpoint, setter) {
    return fetch(endpoint)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        setter(data);
        if (onUpdate) {
          onUpdate();
        }
        return data;
      });
  }
  function refreshRecipients() { return refresh('/api/recipients', function (d) { recipientsCache = d; }); }
  function refreshSubscribers() { return refresh('/api/subscribers', function (d) { subscribersCache = d; }); }

  function pollUntilIdle(refreshFn, cb) {
    refreshFn().then(function (data) {
      if (data.status === 'running') {
        setTimeout(function () { pollUntilIdle(refreshFn, cb); }, 3000);
      } else if (cb) {
        cb();
      }
    });
  }

  refreshRecipients();
  refreshSubscribers();

  // cb(errorMessageOrNull) — a failed POST (bad auth, network error) must
  // still re-enable the calling button and say WHY, rather than silently
  // doing nothing indistinguishable from a click that didn't register.
  function startScan(endpoint, refreshFn, cb) {
    s86SignAndPost(endpoint, {})
      .then(function (result) {
        if (!result.ok) {
          if (cb) cb('scan failed: ' + s86ErrMsg(result));
          return;
        }
        pollUntilIdle(refreshFn, function () { if (cb) cb(null); });
      })
      .catch(function () {
        if (cb) cb('scan failed');
      });
  }

  return {
    getRecipientsCache: function () { return recipientsCache; },
    getSubscribersCache: function () { return subscribersCache; },
    onScanRecipients: function (cb) { startScan('/api/recipients', refreshRecipients, cb); },
    onScanSubscribers: function (cb) { startScan('/api/subscribers', refreshSubscribers, cb); },
  };
}

var S86_COMMAND_INTENTS = [
  {
    key: 'delete_by_author',
    label: 'Delete all events by author',
    fields: [
      { key: 'pubkey', type: 'pubkey', label: 'pubkey (npub or hex): ' },
      { key: 'days', type: 'days_optional', label: 'older than (days, blank = all time): ' },
      { key: 'kind', type: 'kind_optional', label: 'kind (optional): ' }
    ]
  },
  {
    key: 'giftwrap_purge',
    label: 'Gift-wrap retention purge',
    fields: [
      { key: 'days', type: 'days', label: 'older than (days): ' },
      { key: 'exempt', type: 'checkbox', label: 'exempt subscribers' }
    ]
  },
  {
    key: 'kinds_by_author',
    label: 'Event kinds by author, lifetime',
    fields: [
      { key: 'pubkey', type: 'pubkey', label: 'pubkey (npub or hex): ' }
    ]
  }
];

function s86BuildCommandGenerator(options) {
  options = options || {};

  var container = document.createElement('div');
  var details = document.createElement('details');
  details.appendChild(s86El('summary', 'terminal commands (destructive)'));

  ['--config is mandatory or strfry reads the wrong database.',
   '--count returns a number without streaming event bodies, which is why counting is seconds and the histogram is minutes.',
   'scan reads and delete destroys while taking the SAME filter syntax, so any filter can and should be tested with scan --count before being run with delete.',
   'a filter is one shell argument in single quotes, so its inner quotes are never escaped.'
  ].forEach(function (line) { details.appendChild(s86El('p', line)); });

  var select = document.createElement('select');
  S86_COMMAND_INTENTS.forEach(function (intent) {
    var opt = document.createElement('option');
    opt.value = intent.key;
    opt.textContent = intent.label;
    select.appendChild(opt);
  });
  if (options.pubkey) {
    select.value = 'kinds_by_author';
  }
  var selectRow = document.createElement('p');
  selectRow.appendChild(select);
  details.appendChild(selectRow);

  var kindDatalistId = 's86-known-kinds-' + Math.random().toString(36).slice(2);
  var kindDatalist = document.createElement('datalist');
  kindDatalist.id = kindDatalistId;
  S86_KNOWN_KINDS_LIST.forEach(function (pair) {
    var opt = document.createElement('option');
    opt.value = String(pair[0]);
    opt.label = pair[1];
    kindDatalist.appendChild(opt);
  });
  details.appendChild(kindDatalist);

  var fieldsEl = document.createElement('div');
  details.appendChild(fieldsEl);

  var extraEl = document.createElement('div');
  details.appendChild(extraEl);

  var pre = document.createElement('pre');
  details.appendChild(pre);

  var copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.textContent = 'Copy';
  copyBtn.addEventListener('click', function () {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(pre.textContent).catch(function () {});
    }
  });
  details.appendChild(copyBtn);

  // Wired once, unconditionally — the two GETs are public reads (they
  // serve the same persisted caches report.html's own panels poll) and
  // this just keeps the generator's own data current; onUpdate re-renders
  // IF the purge intent happens to be the one currently selected, so a
  // fetch resolving shortly after the intent was picked doesn't get
  // stranded.
  var purgeSources = s86WireGiftwrapPurgeSources(function () {
    if (currentIntent().key === 'giftwrap_purge') {
      render();
    }
  });

  var fieldInputs = {};

  function currentIntent() {
    return S86_COMMAND_INTENTS.filter(function (i) { return i.key === select.value; })[0];
  }

  function buildFields(intent) {
    fieldsEl.textContent = '';
    fieldInputs = {};
    intent.fields.forEach(function (f) {
      var p = document.createElement('p');
      p.appendChild(s86El('span', f.label));

      var input = document.createElement('input');
      if (f.type === 'checkbox') {
        input.type = 'checkbox';
        input.checked = true;
      } else {
        input.type = 'text';
        if (f.type === 'kind_optional') {
          input.setAttribute('list', kindDatalistId);
          input.placeholder = 'e.g. 1';
        } else if (f.type === 'pubkey') {
          input.placeholder = 'npub or hex';
        } else if (f.type === 'days') {
          input.placeholder = String(S86_GIFTWRAP_PURGE_DEFAULT_DAYS);
        } else if (f.type === 'days_optional') {
          input.placeholder = 'blank = all time';
        }
      }
      input.addEventListener(f.type === 'checkbox' ? 'change' : 'input', render);
      fieldInputs[f.key] = input;
      p.appendChild(input);
      fieldsEl.appendChild(p);
    });
  }

  // Destructive intents render their equivalent scan --count line directly
  // above the delete command, on every field change, always — the count
  // and the command are one render or they are blank, never out of sync.

  function renderDeleteByAuthor() {
    var hex = s86PubkeyInputToHex(fieldInputs.pubkey.value);
    if (!hex) {
      pre.textContent = 'enter a pubkey above';
      return;
    }
    var daysCheck = s86ValidateOptionalDaysInput(fieldInputs.days.value);
    if (!daysCheck.ok) {
      pre.textContent = 'enter a positive whole number of days above, or leave it blank for all time';
      return;
    }
    var kindCheck = s86ValidateOptionalKindInput(fieldInputs.kind.value);
    if (!kindCheck.ok) {
      pre.textContent = 'enter a valid kind number above, or leave it blank';
      return;
    }
    var filter = { authors: [hex] };
    if (daysCheck.value != null) {
      // Resolved to a literal timestamp at render time, never a relative
      // expression — the copied command means the same thing an hour
      // later as it did in the clipboard, and slightly LESS deletion,
      // which is the correct direction to be wrong in.
      filter.until = Math.floor(Date.now() / 1000) - daysCheck.value * 86400;
    } else {
      extraEl.appendChild(s86El('p', 'no time bound — this deletes every matching event, all time'));
    }
    if (kindCheck.value != null) {
      filter.kinds = [kindCheck.value];
    }
    pre.textContent = s86RenderDeleteWithCountFirst(filter);
  }

  function renderGiftwrapPurge() {
    var daysCheck = s86ValidateDaysInput(fieldInputs.days.value);
    if (!daysCheck.ok) {
      pre.textContent = 'enter a positive whole number of days above';
      return;
    }
    var days = daysCheck.value;
    var cutoff = Math.floor(Date.now() / 1000) - days * 86400;
    var blanket = s86RenderDeleteWithCountFirst({ kinds: [1059], until: cutoff });

    if (!fieldInputs.exempt.checked) {
      pre.textContent = blanket;
      return;
    }

    var recipients = purgeSources.getRecipientsCache();
    var subscribers = purgeSources.getSubscribersCache();
    var subsFresh = subscribers && subscribers.scanned_at
      && (Math.floor(Date.now() / 1000) - subscribers.scanned_at) <= S86_SUBSCRIBER_CACHE_STALE_SECONDS;
    var recipientsAvailable = recipients && recipients.scanned_at;

    if (!recipientsAvailable || !subsFresh) {
      var reason = !recipients || !recipients.scanned_at
        ? 'no recipient scan has been run yet'
        : (!subscribers || !subscribers.scanned_at
          ? 'no subscriber scan has been run yet'
          : 'the subscriber scan is more than 7 days old');
      extraEl.appendChild(s86El('p', 'subscriber-exempt form unavailable: ' + reason
        + '. Run the missing scan(s) on the report page, or uncheck "exempt subscribers" for the blanket form below.'));
      pre.textContent = blanket;
      return;
    }

    var subscriberPubkeys = {};
    (subscribers.subscribers || []).forEach(function (s) { subscriberPubkeys[s.pubkey] = true; });
    var exempt = (recipients.recipients || [])
      .map(function (r) { return r.pubkey; })
      .filter(function (pk) { return !subscriberPubkeys[pk]; });

    extraEl.appendChild(s86El('p', 'subscriber-exempt form (excludes ' + Object.keys(subscriberPubkeys).length + ' DM-inbox subscriber(s)):'));

    var chunks = [];
    for (var i = 0; i < exempt.length; i += S86_GIFTWRAP_PURGE_CHUNK_SIZE) {
      chunks.push(exempt.slice(i, i + S86_GIFTWRAP_PURGE_CHUNK_SIZE));
    }
    var exemptCommands = chunks.length === 0
      ? [s86RenderDeleteWithCountFirst({ kinds: [1059], until: cutoff })]
      : chunks.map(function (chunk) {
        return s86RenderDeleteWithCountFirst({ kinds: [1059], until: cutoff, '#p': chunk });
      });

    pre.textContent = exemptCommands.join('\n\n')
      + '\n\n# blanket form below — deletes ALL gift wraps in the window, subscribers included:\n\n'
      + blanket;
  }

  function renderKindsByAuthor() {
    var hex = s86PubkeyInputToHex(fieldInputs.pubkey.value);
    pre.textContent = hex
      ? ("strfry " + S86_STRFRY_CONFIG_FLAG + " scan '" + JSON.stringify({ authors: [hex] }) + "' | python3 -c '\n" + s86PyKindTallyScript() + "\n'")
      : 'enter a pubkey above';
  }

  function render() {
    extraEl.textContent = '';
    var intent = currentIntent();
    if (intent.key === 'delete_by_author') {
      renderDeleteByAuthor();
    } else if (intent.key === 'giftwrap_purge') {
      renderGiftwrapPurge();
    } else if (intent.key === 'kinds_by_author') {
      renderKindsByAuthor();
    }
  }

  select.addEventListener('change', function () {
    buildFields(currentIntent());
    render();
  });

  buildFields(currentIntent());
  if (options.pubkey && fieldInputs.pubkey) {
    fieldInputs.pubkey.value = options.pubkey;
  }
  render();

  container.appendChild(details);
  return container;
}

// --- external name resolution (fallback relay chain -> POST /api/names) --
// Shared by bans.html (automatic, self-extinguishing set) and authors.html
// (button-triggered, bounded to visible rows) — see each page for when it
// fires. Moved here the moment a second page needed it, per the rule that
// duplicated client code is how a fix lands in one page and not the other.

// Tried in order; only a CONNECT failure advances to the next one (a
// completed query — EOSE or the 5s timeout — with zero results is not a
// failure and does not fall through).
var S86_NAME_RELAYS = [
  'wss://purplepag.es',
  'wss://indexer.coracle.social',
  'wss://user.kindpag.es',
  'wss://relay.nos.social',
  'wss://relay.ditto.pub',
  'wss://relay.noswhere.com'
];

// Collects raw, unparsed EVENT payloads for the caller to verify
// server-side — no page trusts or parses profile content itself.
// `connected` distinguishes "relay reachable, query completed" (EOSE or
// the 5s timeout) from "never connected" — only the latter triggers the
// damus.io fallback, per spec.
function s86QueryRelayForNames(url, pubkeys, done) {
  var settled = false;
  var connected = false;
  var events = [];
  var ws;

  function finish(ok) {
    if (settled) {
      return;
    }
    settled = true;
    try { ws.close(); } catch (e) {}
    done(events, ok);
  }

  try {
    ws = new WebSocket(url);
  } catch (e) {
    finish(false);
    return;
  }

  var timeout = setTimeout(function () { finish(connected); }, 5000);

  ws.addEventListener('open', function () {
    connected = true;
    ws.send(JSON.stringify(['REQ', 'strfry86names', { kinds: [0], authors: pubkeys }]));
  });

  ws.addEventListener('message', function (evt) {
    var msg;
    try { msg = JSON.parse(evt.data); } catch (e) { return; }
    if (!Array.isArray(msg)) {
      return;
    }
    if (msg[0] === 'EVENT' && msg[2]) {
      events.push(msg[2]);
    } else if (msg[0] === 'EOSE') {
      clearTimeout(timeout);
      finish(true);
    }
  });

  ws.addEventListener('error', function () { clearTimeout(timeout); finish(connected); });
  ws.addEventListener('close', function () { clearTimeout(timeout); finish(connected); });
}

// Query S86_NAME_RELAYS in order, falling through to the next one on a
// CONNECT failure only, then POST whatever came back (even nothing) to
// /api/names. callbacks: {onDone(ok), onError(msg)}. `onDone(false)` means
// none of the relays could be reached — the caller should stamp nothing
// client-side either and just let the pubkeys stay eligible for the next
// attempt.
function s86ResolveNamesExternally(pubkeys, callbacks) {
  function post(events) {
    s86SignAndPost('/api/names', { queried: pubkeys, events: events })
      .then(function (result) {
        if (!result.ok) {
          if (callbacks.onError) callbacks.onError('name lookup failed: ' + s86ErrMsg(result));
          if (callbacks.onDone) callbacks.onDone(false);
          return;
        }
        if (callbacks.onDone) callbacks.onDone(true);
      })
      .catch(function () {
        if (callbacks.onError) callbacks.onError('name lookup failed');
        if (callbacks.onDone) callbacks.onDone(false);
      });
  }

  function tryRelay(i) {
    if (i >= S86_NAME_RELAYS.length) {
      if (callbacks.onDone) callbacks.onDone(false);
      return;
    }
    s86QueryRelayForNames(S86_NAME_RELAYS[i], pubkeys, function (events, connected) {
      if (connected) {
        post(events);
        return;
      }
      tryRelay(i + 1);
    });
  }

  tryRelay(0);
}

// --- record lines -------------------------------------------------------
// A persistent log of admin actions. Element order within a line is fixed:
// the ↩ dismiss button LEFTMOST, then the label, then the Undo button, then
// the full npub as plain text LAST. The npub wraps onto its own line — that
// wrap is the design. This keeps ↩ and Undo apart with the label between
// them and leaves Undo flanked by inert text on both sides, rather than
// sitting adjacent to the next record's ↩ across the line break.
//
// Label shape: the display name in <b> when known, the nip05 as plain text
// after it when known, and neither when unknown — in which case the
// trailing npub is the only identifier, a perfectly acceptable resting
// state. The label supplies the separation (the locked CSS block permits no
// spacing rules), so it must never be empty.
//
// A ban or unban action covering 2+ pubkeys renders ONE grouped line instead
// of one per pubkey. Since which pubkey the trailing npub would name is
// ambiguous there, that slot is replaced by a ▸/▾ expand arrow — the one
// case where a record line has a third control — which toggles a nested
// per-pubkey detail list (name/nip05/npub) below the line. A bulk ban's
// single shared reason renders in the summary itself, since one reason
// input covers the whole batch.
//
// At most the 20 newest lines of all four kinds (ban, unban, reason,
// report) combined are ever rendered; older ones are dropped from view
// and, for stored records, deleted from localStorage outright.

// Returns {verb, name, nip05, suffix, npub, entries} — never a string, so
// the caller can build <b>/text nodes per the untrusted-string rule instead
// of merging attacker-influenced text into one opaque string. `entries` is
// only set for a 2+-pubkey ban/unban record, where it feeds the expand-arrow
// detail list in place of the (ambiguous, therefore omitted) single trailing
// npub.
function s86RecordLabelParts(record) {
  if (record.type === 'reason') {
    return {
      verb: 'set reason on ' + record.count + ' ban' + (record.count === 1 ? '' : 's')
        + (record.entries ? '' : ' — undo unavailable'),
      name: null, nip05: null, suffix: null, npub: null, entries: null
    };
  }
  var verb = record.type === 'ban' ? 'banned' : 'unbanned';
  if (record.entries.length === 1) {
    var e = record.entries[0];
    return { verb: verb, name: e.name || null, nip05: e.nip05 || null, suffix: null, npub: e.npub || e.pubkey, entries: null };
  }
  // A bulk ban has exactly one reason input for the whole batch, so every
  // entry carries the same value — show it once in the summary rather than
  // per row. Bulk unban has no analogous action-level reason: each entry's
  // `reason` there is the ORIGINAL ban reason, kept only so Undo can re-ban
  // with it, and belongs in the detail rows, not the summary.
  var suffix = null;
  if (record.type === 'ban') {
    var reason = record.entries[0] && record.entries[0].reason;
    if (reason) {
      suffix = ' — ' + reason;
    }
  }
  return { verb: verb + ' ' + record.entries.length + ' pubkeys', name: null, nip05: null, suffix: suffix, npub: null, entries: record.entries };
}

function s86AddRecord(record) {
  record.id = Date.now() + '-' + Math.random().toString(36).slice(2);
  var records = s86LoadStored(S86_RECORDS_KEY);
  records.push(record);
  s86SaveStored(S86_RECORDS_KEY, records, S86_MAX_RECORDS_STORED);
}

function s86RemoveStoredRecord(id) {
  var records = s86LoadStored(S86_RECORDS_KEY).filter(function (r) { return r.id !== id; });
  s86SaveStored(S86_RECORDS_KEY, records, S86_MAX_RECORDS_STORED);
}

function s86DismissReport(id) {
  var ids = s86LoadStored(S86_DISMISSED_REPORTS_KEY);
  if (ids.indexOf(id) === -1) {
    ids.push(id);
  }
  s86SaveStored(S86_DISMISSED_REPORTS_KEY, ids, S86_MAX_DISMISSED);
}

// Builds the label span from structured parts: createElement/textContent
// only, per the untrusted-string rule — name, nip05, and report type are
// all attacker-influenced.
function s86BuildLabelSpan(parts) {
  var label = document.createElement('span');
  label.appendChild(document.createTextNode(parts.verb));
  if (parts.name) {
    label.appendChild(document.createTextNode(' '));
    var b = document.createElement('b');
    b.textContent = parts.name;
    label.appendChild(b);
  }
  if (parts.nip05) {
    label.appendChild(document.createTextNode(' '));
    label.appendChild(document.createTextNode(parts.nip05));
  }
  if (parts.suffix) {
    label.appendChild(document.createTextNode(parts.suffix));
  }
  return label;
}

// One row per pubkey for a multi-entry record's expand-arrow detail list:
// name in <b> when known, nip05 as plain text after it, then the npub —
// the same shape and the same createElement/textContent-only rule as every
// other record line, since name/nip05 are attacker-influenced.
function s86BuildRecordDetailList(entries) {
  var ul = document.createElement('ul');
  entries.forEach(function (e) {
    var li = document.createElement('li');
    if (e.name) {
      var b = document.createElement('b');
      b.textContent = e.name;
      li.appendChild(b);
      li.appendChild(document.createTextNode(' '));
    }
    if (e.nip05) {
      li.appendChild(document.createTextNode(e.nip05 + ' '));
    }
    li.appendChild(document.createTextNode(e.npub || e.pubkey));
    ul.appendChild(li);
  });
  return ul;
}

// Inline "set reason" row, shared by record lines (bans.html, authors.html,
// domain.html — always called with currentReason: '', since that row is
// for typing a fresh reason and prefilling it from history invited setting
// the same one back) and profile.html's ban-status editor (which passes
// the pubkey's actual current reason, since there the row IS the "edit the
// reason on file" control). One text input and one button, disabled while
// the input is empty — same disable rule as the bulk-reason row on
// bans.html, since an empty reason submitted from a row with no checkbox
// concept would otherwise silently no-op. Posts /api/reason in 'replace'
// mode for exactly the pubkeys this call concerns, then records a normal
// 'reason' undo entry — reusing the existing mechanism rather than
// inventing a second way to undo a reason change.
function s86BuildReasonEditRow(pubkeys, currentReason, callbacks) {
  var p = document.createElement('p');

  var input = document.createElement('input');
  input.type = 'text';
  input.value = currentReason || '';
  input.placeholder = 'reason — shown publicly';
  input.maxLength = 500;

  // A row that started prefilled (profile.html's ban-status editor) is
  // changing a reason already on file; a blank-start row (every record-line
  // caller) is setting one for the first time — the button says which.
  var wasPrefilled = !!(currentReason && currentReason.trim().length > 0);
  var verb = wasPrefilled ? 'Change reason' : 'Set reason';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = verb;
  var label = verb.toLowerCase() + ' on ' + (pubkeys.length === 1 ? 'this ban' : pubkeys.length + ' bans');
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.disabled = input.value.trim().length === 0;

  input.addEventListener('input', function () {
    btn.disabled = input.value.trim().length === 0;
  });

  btn.addEventListener('click', function () {
    btn.disabled = true;
    s86SignAndPost('/api/reason', { pubkeys: pubkeys, reason: input.value, mode: 'replace' })
      .then(function (result) {
        if (!result.ok) {
          btn.disabled = false;
          if (callbacks.onError) callbacks.onError('set reason failed: ' + s86ErrMsg(result));
          return;
        }
        var updated = result.body.updated;
        if (updated.length > 0) {
          s86AddRecord({
            type: 'reason',
            at: Date.now(),
            count: updated.length,
            entries: updated.length <= S86_REASON_UNDO_MAX
              ? updated.map(function (u) { return { pubkey: u.pubkey, old_reason: u.old_reason }; })
              : null
          });
        }
        if (callbacks.onChanged) callbacks.onChanged();
      })
      .catch(function () {
        btn.disabled = false;
        if (callbacks.onError) callbacks.onError('set reason failed');
      });
  });

  p.appendChild(input);
  p.appendChild(document.createTextNode(' '));
  p.appendChild(btn);
  return p;
}

// parts: {verb, name, nip05, suffix, npub, entries} — see
// s86RecordLabelParts. npub is omitted (no trailing element at all) when
// null. `entries` (2+ pubkeys) replaces that omitted npub with a third
// control, a ▸/▾ expand arrow revealing a detail list — the one exception
// to a record line's normal two-touch-target rule.
//
// onUndo is optional — a null/undefined onUndo (the reason-record "undo
// unavailable" case, once its snapshot exceeds S86_REASON_UNDO_MAX) omits
// the Undo button entirely rather than rendering one that can't work. A
// truncated undo is worse than none.
//
// reasonEdit is optional: {pubkeys, currentReason, callbacks}. When present
// (ban and report records only — never unban or reason records), renders
// the inline reason row from s86BuildReasonEditRow as a second row below
// the line, exactly like the expand-arrow detail list, so it never disturbs
// the main line's fixed dismiss/label/Undo/npub ordering.
function s86BuildRecordLine(parts, onUndo, onDismiss, reasonEdit) {
  var wrap = document.createElement('div');
  var line = document.createElement('p');

  var dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.textContent = '↩';
  dismissBtn.title = 'dismiss';
  dismissBtn.setAttribute('aria-label', 'dismiss');
  dismissBtn.addEventListener('click', onDismiss);
  line.appendChild(dismissBtn);
  line.appendChild(document.createTextNode(' '));

  line.appendChild(s86BuildLabelSpan(parts));

  if (onUndo) {
    line.appendChild(document.createTextNode(' '));
    var undoBtn = document.createElement('button');
    undoBtn.type = 'button';
    undoBtn.textContent = 'Undo';
    undoBtn.title = 'undo';
    undoBtn.setAttribute('aria-label', 'undo');
    undoBtn.addEventListener('click', function () { onUndo(undoBtn); });
    line.appendChild(undoBtn);
  }

  if (parts.npub) {
    line.appendChild(document.createTextNode(' '));
    line.appendChild(document.createTextNode(parts.npub));
  }

  wrap.appendChild(line);

  if (parts.entries && parts.entries.length > 1) {
    var expandBtn = document.createElement('button');
    expandBtn.type = 'button';
    expandBtn.textContent = '▸';
    expandBtn.title = 'show pubkeys';
    expandBtn.setAttribute('aria-label', 'show pubkeys');
    line.appendChild(document.createTextNode(' '));
    line.appendChild(expandBtn);

    var detail = s86BuildRecordDetailList(parts.entries);
    detail.style.display = 'none';
    expandBtn.addEventListener('click', function () {
      var expanded = detail.style.display !== 'none';
      detail.style.display = expanded ? 'none' : '';
      expandBtn.textContent = expanded ? '▸' : '▾';
      expandBtn.title = expanded ? 'show pubkeys' : 'hide pubkeys';
      expandBtn.setAttribute('aria-label', expandBtn.title);
    });
    wrap.appendChild(detail);
  }

  if (reasonEdit) {
    wrap.appendChild(s86BuildReasonEditRow(reasonEdit.pubkeys, reasonEdit.currentReason, reasonEdit.callbacks));
  }

  return wrap;
}

// A reason-record undo restores each pubkey's PRIOR reason, which can
// differ from one pubkey to the next within the same bulk edit — one
// /api/reason call only ever sets one reason string, so this groups the
// snapshot by old_reason and issues one 'replace' POST per distinct
// value, sequentially (never in parallel, so a failed group doesn't race
// a later one signing over it).
function s86UndoReasonRecord(record, btn, callbacks) {
  var groups = {};
  var order = [];
  record.entries.forEach(function (e) {
    var key = e.old_reason || '';
    if (!groups[key]) {
      groups[key] = [];
      order.push(key);
    }
    groups[key].push(e.pubkey);
  });

  var chain = Promise.resolve();
  var failed = false;
  order.forEach(function (reason) {
    chain = chain.then(function () {
      if (failed) {
        return;
      }
      return s86SignAndPost('/api/reason', { pubkeys: groups[reason], reason: reason, mode: 'replace' })
        .then(function (result) {
          if (!result.ok) {
            failed = true;
            if (callbacks.onError) callbacks.onError('undo failed: ' + s86ErrMsg(result));
          }
        });
    });
  });

  chain
    .then(function () {
      btn.disabled = false;
      if (failed) {
        return;
      }
      s86RemoveStoredRecord(record.id);
      if (callbacks.onChanged) callbacks.onChanged();
    })
    .catch(function () {
      btn.disabled = false;
      if (callbacks.onError) callbacks.onError('undo failed');
    });
}

function s86UndoStoredRecord(record, btn, callbacks) {
  btn.disabled = true;

  if (record.type === 'reason') {
    s86UndoReasonRecord(record, btn, callbacks);
    return;
  }

  var req;
  if (record.type === 'ban') {
    req = s86SignAndPost('/api/unban', { pubkeys: record.entries.map(function (e) { return e.pubkey; }) });
  } else {
    req = s86SignAndPost('/api/ban', {
      entries: record.entries.map(function (e) { return { pubkey: e.pubkey, reason: e.reason || '' }; })
    });
  }
  req
    .then(function (result) {
      if (!result.ok) {
        btn.disabled = false;
        if (callbacks.onError) callbacks.onError('undo failed: ' + s86ErrMsg(result));
        return;
      }
      s86RemoveStoredRecord(record.id);
      if (callbacks.onChanged) callbacks.onChanged();
    })
    .catch(function () {
      btn.disabled = false;
      if (callbacks.onError) callbacks.onError('undo failed');
    });
}

function s86UndoReportBan(pubkey, btn, callbacks) {
  btn.disabled = true;
  s86SignAndPost('/api/unban', { pubkeys: [pubkey] })
    .then(function (result) {
      if (!result.ok) {
        btn.disabled = false;
        if (callbacks.onError) callbacks.onError('undo failed: ' + s86ErrMsg(result));
        return;
      }
      if (callbacks.onChanged) callbacks.onChanged();
    })
    .catch(function () {
      btn.disabled = false;
      if (callbacks.onError) callbacks.onError('undo failed');
    });
}

// container: element to render into. bannedList: the current /api/banned
// `banned` array (report records are derived from it, never stored).
// callbacks: {onChanged, onError}.
function s86RenderRecords(container, isAdmin, bannedList, callbacks) {
  container.textContent = '';
  if (!isAdmin) {
    return;
  }

  var stored = s86LoadStored(S86_RECORDS_KEY);
  var dismissed = s86LoadStored(S86_DISMISSED_REPORTS_KEY);

  var lines = [];
  stored.forEach(function (r) {
    // A reason record whose snapshot exceeded S86_REASON_UNDO_MAX stores
    // entries: null deliberately (see s86UndoReasonRecord) — it still
    // renders, just without an Undo button, so it needs its own bare
    // "has a record at all" check rather than the entries-array check
    // every other stored kind uses.
    if (!r) {
      return;
    }
    if (r.type === 'reason') {
      if (typeof r.count !== 'number' || r.count <= 0) {
        return;
      }
    } else if (!Array.isArray(r.entries) || r.entries.length === 0) {
      return;
    }
    lines.push({ sortAt: r.at || 0, kind: 'stored', ref: r });
  });
  (bannedList || []).forEach(function (ban) {
    if (!ban.report_event_id) {
      return;
    }
    var rid = ban.report_event_id + ':' + ban.pubkey;
    if (dismissed.indexOf(rid) !== -1) {
      return;
    }
    lines.push({ sortAt: (ban.banned_at || 0) * 1000, kind: 'report', ref: ban, id: rid });
  });

  lines.sort(function (a, b) { return b.sortAt - a.sortAt; });

  var rendered = lines.slice(0, S86_MAX_RECORDS_RENDERED);
  var overflow = lines.slice(S86_MAX_RECORDS_RENDERED);

  var overflowStoredIds = {};
  overflow.forEach(function (l) {
    if (l.kind === 'stored') {
      overflowStoredIds[l.ref.id] = true;
    }
  });
  if (Object.keys(overflowStoredIds).length > 0) {
    stored = stored.filter(function (r) { return !overflowStoredIds[r.id]; });
    s86SaveStored(S86_RECORDS_KEY, stored, S86_MAX_RECORDS_STORED);
  }

  var lineEls = rendered.map(function (l) {
    if (l.kind === 'stored') {
      var record = l.ref;
      var canUndo = !(record.type === 'reason' && !record.entries);
      // currentReason is always blank here, never the reason the ban was
      // made with — this row is for typing a NEW reason, and prefilling
      // it from history invited setting the same reason back rather than
      // deliberately choosing one.
      var reasonEdit = record.type === 'ban' ? {
        pubkeys: record.entries.map(function (e) { return e.pubkey; }),
        currentReason: '',
        callbacks: callbacks
      } : null;
      return s86BuildRecordLine(
        s86RecordLabelParts(record),
        canUndo ? function (btn) { s86UndoStoredRecord(record, btn, callbacks); } : null,
        function () {
          s86RemoveStoredRecord(record.id);
          s86RenderRecords(container, isAdmin, bannedList, callbacks);
        },
        reasonEdit
      );
    } else {
      var ban = l.ref;
      var parts = {
        verb: 'reported',
        name: ban.name || null,
        nip05: ban.nip05 || null,
        suffix: ban.report_type ? ' — ' + ban.report_type : null,
        npub: ban.npub,
        entries: null
      };
      return s86BuildRecordLine(
        parts,
        function (btn) { s86UndoReportBan(ban.pubkey, btn, callbacks); },
        function () {
          s86DismissReport(l.id);
          s86RenderRecords(container, isAdmin, bannedList, callbacks);
        },
        // Blank for the same reason as the ban-record row above — this
        // is for entering a fresh reason, not editing the one on file.
        { pubkeys: [ban.pubkey], currentReason: '', callbacks: callbacks }
      );
    }
  });

  // Newest S86_RECORDS_INLINE lines sit outside any disclosure, always
  // visible — the rest go behind a <details> so a busy admin isn't
  // greeted by twenty lines of history above the controls they came for.
  // Nothing here changes what's stored or the S86_MAX_RECORDS_RENDERED
  // cap above; this only changes how the already-capped set is drawn.
  lineEls.slice(0, S86_RECORDS_INLINE).forEach(function (el) {
    container.appendChild(el);
  });

  var older = lineEls.slice(S86_RECORDS_INLINE);
  if (older.length > 0) {
    var details = document.createElement('details');
    var summary = document.createElement('summary');
    summary.textContent = 'recent activity (' + older.length + ' older) ▸';
    details.appendChild(summary);
    older.forEach(function (el) { details.appendChild(el); });
    details.addEventListener('toggle', function () {
      summary.textContent = 'recent activity (' + older.length + ' older) ' + (details.open ? '▾' : '▸');
    });
    container.appendChild(details);
  }
}
