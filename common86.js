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

function s86NpubLink(npub) {
  var a = document.createElement('a');
  a.href = 'https://njump.me/' + npub;
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
// One <input type="search"> + "View profile" button, present near-
// identically directly above the command generator on every admin page.
// A typed/pasted entry rather than a link on every row — that is what
// keeps npubs copyable plain text everywhere else instead of turning each
// one into a touch target. Invalid input sets a plain status line and
// navigates nowhere; Enter in the field submits it same as the button.
function s86BuildProfileEntryField(statusEl) {
  var p = document.createElement('p');

  var input = document.createElement('input');
  input.type = 'search';
  input.placeholder = 'npub or hex — view a pubkey\'s profile';

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = 'View profile';

  function go() {
    var hex = s86PubkeyInputToHex(input.value);
    if (!hex) {
      statusEl.textContent = 'enter a valid npub or hex pubkey';
      return;
    }
    window.location.href = '/profile?hex=' + hex;
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
// plain copyable text. Identical on both pages.

// --- command generator ---------------------------------------------------
// ONE <select> of intents, ONE input that appears only when the selected
// intent needs one, ONE <pre> holding the rendered command, ONE copy
// button. Replaces a wall of static <pre>s nobody read. Nothing here is
// executed, nothing is fetched by pressing anything in it, no output ever
// returns to the page — the terminal is where this project sends
// everything it refuses to do itself. The one exception is the gift-wrap
// purge intent's own "scan recipients"/"scan subscribers" buttons, which
// are the SAME admin-triggered async scans Phase 2 already built — a
// legitimate server action feeding parameters INTO a rendered command,
// never the rendered command itself.
//
// A pubkey is decoded and re-encoded to canonical hex before it reaches
// the <pre>; a domain is hostname-validated; days is a plain positive
// integer. Nothing here executes, but a tool that renders whatever it's
// handed teaches a habit that's wrong everywhere else in this project.

var S86_GIFTWRAP_PURGE_DEFAULT_DAYS = 90;
var S86_SUBSCRIBER_CACHE_STALE_SECONDS = 7 * 24 * 3600;
var S86_GIFTWRAP_PURGE_CHUNK_SIZE = 200;
var S86_STRFRY_CONFIG_FLAG = '--config /config/strfry.conf';

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

// A small json-lines tally, written in python3 rather than jq/awk because
// python3 is a hard requirement of this whole project (server86.py and
// plugin86.py both run on it inside the operator's own container), so it
// is guaranteed present — unlike jq, and unlike an awk one-liner naive
// quote-splitting would need, which breaks the moment `content` contains
// an internal quote.
function s86PyKindTallyScript(withAuthorsAndGiftwrapShare) {
  var lines = [
    'import sys, json',
    'total = 0',
    'kinds = {}',
    withAuthorsAndGiftwrapShare ? 'authors = set()' : null,
    withAuthorsAndGiftwrapShare ? 'giftwraps = 0' : null,
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
    withAuthorsAndGiftwrapShare ? '    authors.add(e.get("pubkey"))' : null,
    withAuthorsAndGiftwrapShare ? '    if k == 1059:' : null,
    withAuthorsAndGiftwrapShare ? '        giftwraps += 1' : null,
    withAuthorsAndGiftwrapShare ? 'print("total events:", total)' : null,
    withAuthorsAndGiftwrapShare ? 'print("distinct authors:", len(authors))' : null,
    withAuthorsAndGiftwrapShare ? 'print("gift-wrap share: %.1f%%" % (giftwraps / total * 100 if total else 0))' : null,
    'print("kind histogram:")',
    'for k, c in sorted(kinds.items(), key=lambda kv: -kv[1]):',
    '    print("  kind", k, ":", c)',
  ].filter(function (l) { return l !== null; });
  return lines.join('\n');
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

function s86BuildCommandGenerator(options) {
  options = options || {};

  var container = document.createElement('div');
  var details = document.createElement('details');
  details.appendChild(s86El('summary', 'terminal commands'));

  ['--config is mandatory or strfry reads the wrong database.',
   '--count returns a number without streaming event bodies, which is why counting is seconds and the histogram is minutes.',
   'scan reads and delete destroys while taking the SAME filter syntax, so any filter can and should be tested with scan --count before being run with delete.',
   'a filter is one shell argument in single quotes, so its inner quotes are never escaped.'
  ].forEach(function (line) { details.appendChild(s86El('p', line)); });

  var select = document.createElement('select');
  var intents = [
    { key: 'count_all', label: 'Count all events', input: null },
    { key: 'whole_db_report', label: 'Whole-database report: total, kind histogram, author count, gift-wrap share (~9 min)', input: null },
    { key: 'kinds_by_author', label: 'Event kinds by author', input: 'pubkey' },
    { key: 'delete_by_author', label: 'Delete all events by author', input: 'pubkey' },
    { key: 'giftwrap_purge', label: 'Gift-wrap retention purge', input: 'days' },
    { key: 'dm_inbox_list', label: 'Who lists this relay as their DM inbox', input: null },
    { key: 'fetch_domain', label: "Fetch a domain's nostr.json", input: 'domain' },
  ];
  intents.forEach(function (intent) {
    var opt = document.createElement('option');
    opt.value = intent.key;
    opt.textContent = intent.label;
    select.appendChild(opt);
  });
  if (options.pubkey) {
    select.value = 'kinds_by_author';
  } else if (options.domain) {
    select.value = 'fetch_domain';
  }
  var selectRow = document.createElement('p');
  selectRow.appendChild(select);
  details.appendChild(selectRow);

  var inputRow = document.createElement('p');
  var inputLabel = document.createElement('span');
  var input = document.createElement('input');
  input.type = 'text';
  inputRow.appendChild(inputLabel);
  inputRow.appendChild(input);
  details.appendChild(inputRow);

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

  // Wired once, unconditionally — the two GETs are public reads and this
  // just keeps the generator's own data current; onUpdate re-renders IF
  // the purge intent happens to be the one currently selected, so a fetch
  // resolving shortly after the intent was picked doesn't get stranded.
  var purgeSources = s86WireGiftwrapPurgeSources(function () {
    if (currentIntent().key === 'giftwrap_purge') {
      renderGiftwrapPurge();
    }
  });

  function currentIntent() {
    return intents.filter(function (i) { return i.key === select.value; })[0];
  }

  function renderGiftwrapPurge() {
    var daysCheck = s86ValidateDaysInput(input.value);
    extraEl.textContent = '';
    if (!daysCheck.ok) {
      pre.textContent = 'enter a positive whole number of days above';
      return;
    }
    var days = daysCheck.value;
    var cutoff = Math.floor(Date.now() / 1000) - days * 86400;
    var blanket = s86RenderDeleteWithCountFirst({ kinds: [1059], until: cutoff });

    var recipients = purgeSources.getRecipientsCache();
    var subscribers = purgeSources.getSubscribersCache();
    var subsFresh = subscribers && subscribers.scanned_at
      && (Math.floor(Date.now() / 1000) - subscribers.scanned_at) <= S86_SUBSCRIBER_CACHE_STALE_SECONDS;
    var recipientsAvailable = recipients && recipients.scanned_at;

    var notice = document.createElement('p');
    if (!recipientsAvailable || !subsFresh) {
      var reason = !recipients || !recipients.scanned_at
        ? 'no recipient scan has been run yet'
        : (!subscribers || !subscribers.scanned_at
          ? 'no subscriber scan has been run yet'
          : 'the subscriber scan is more than 7 days old');
      notice.textContent = 'subscriber-exempt form unavailable: ' + reason + '. Showing the blanket form only — it purges EVERY subscriber\'s gift wraps too.';
      extraEl.appendChild(notice);

      var scanErrorLine = document.createElement('p');
      extraEl.appendChild(scanErrorLine);

      if (!recipients || !recipients.scanned_at) {
        var rBtn = document.createElement('button');
        rBtn.type = 'button';
        rBtn.textContent = 'scan recipients now (a few minutes)';
        rBtn.addEventListener('click', function () {
          rBtn.disabled = true;
          purgeSources.onScanRecipients(function (err) {
            if (err) {
              scanErrorLine.textContent = err;
              rBtn.disabled = false;
              return;
            }
            renderGiftwrapPurge();
          });
        });
        extraEl.appendChild(rBtn);
      }
      if (!subscribers || !subscribers.scanned_at || !subsFresh) {
        var sBtn = document.createElement('button');
        sBtn.type = 'button';
        sBtn.textContent = 'scan subscribers now';
        sBtn.addEventListener('click', function () {
          sBtn.disabled = true;
          purgeSources.onScanSubscribers(function (err) {
            if (err) {
              scanErrorLine.textContent = err;
              sBtn.disabled = false;
              return;
            }
            renderGiftwrapPurge();
          });
        });
        extraEl.appendChild(sBtn);
      }

      pre.textContent = blanket;
      return;
    }

    var subscriberPubkeys = {};
    (subscribers.subscribers || []).forEach(function (s) { subscriberPubkeys[s.pubkey] = true; });
    var exempt = (recipients.recipients || [])
      .map(function (r) { return r.pubkey; })
      .filter(function (pk) { return !subscriberPubkeys[pk]; });

    notice.textContent = 'subscriber-exempt form (excludes ' + Object.keys(subscriberPubkeys).length + ' DM-inbox subscriber(s)):';
    extraEl.appendChild(notice);

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

  function render() {
    var intent = currentIntent();
    inputRow.style.display = intent.input ? '' : 'none';
    extraEl.textContent = '';
    input.placeholder = '';

    if (intent.input === 'pubkey') {
      inputLabel.textContent = 'pubkey (npub or hex): ';
      input.placeholder = 'npub or hex';
    } else if (intent.input === 'domain') {
      inputLabel.textContent = 'domain: ';
      input.placeholder = 'example.com';
    } else if (intent.input === 'days') {
      inputLabel.textContent = 'days (default ' + S86_GIFTWRAP_PURGE_DEFAULT_DAYS + '): ';
      input.placeholder = String(S86_GIFTWRAP_PURGE_DEFAULT_DAYS);
    }

    if (intent.key === 'count_all') {
      pre.textContent = "strfry " + S86_STRFRY_CONFIG_FLAG + " scan --count '{}'";
    } else if (intent.key === 'whole_db_report') {
      pre.textContent = "strfry " + S86_STRFRY_CONFIG_FLAG + " scan '{}' | python3 -c '\n" + s86PyKindTallyScript(true) + "\n'";
    } else if (intent.key === 'kinds_by_author') {
      var hex = s86PubkeyInputToHex(input.value);
      if (!hex) {
        pre.textContent = 'enter a pubkey above';
      } else {
        pre.textContent = "strfry " + S86_STRFRY_CONFIG_FLAG + " scan '" + JSON.stringify({ authors: [hex] }) + "' | python3 -c '\n" + s86PyKindTallyScript(false) + "\n'";
      }
    } else if (intent.key === 'delete_by_author') {
      var hex2 = s86PubkeyInputToHex(input.value);
      pre.textContent = hex2 ? s86RenderDeleteWithCountFirst({ authors: [hex2] }) : 'enter a pubkey above';
    } else if (intent.key === 'giftwrap_purge') {
      renderGiftwrapPurge();
    } else if (intent.key === 'dm_inbox_list') {
      pre.textContent = "strfry " + S86_STRFRY_CONFIG_FLAG + " scan '" + JSON.stringify({ kinds: [10050] }) + "' | python3 -c '\n"
        + 'import sys, json\n'
        + 'for line in sys.stdin:\n'
        + '    line = line.strip()\n'
        + '    if not line:\n'
        + '        continue\n'
        + '    try:\n'
        + '        e = json.loads(line)\n'
        + '    except ValueError:\n'
        + '        continue\n'
        + '    for tag in e.get("tags", []):\n'
        + '        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "relay":\n'
        + '            print(e.get("pubkey"), tag[1])\n'
        + "'  # pubkey, relay-tag pairs — check manually against this relay's own URL";
    } else if (intent.key === 'fetch_domain') {
      var domain = s86ValidateDomainInput(input.value);
      pre.textContent = domain ? ('curl -s https://' + domain + '/.well-known/nostr.json') : 'enter a domain above';
    }
  }

  select.addEventListener('change', render);
  input.addEventListener('input', render);

  if (options.pubkey) {
    input.value = options.pubkey;
  } else if (options.domain) {
    input.value = options.domain;
  }
  render();

  container.appendChild(details);
  return container;
}

// --- external name resolution (wss://purplepag.es -> POST /api/names) ---
// Shared by bans.html (automatic, self-extinguishing set) and authors.html
// (button-triggered, bounded to visible rows) — see each page for when it
// fires. Moved here the moment a second page needed it, per the rule that
// duplicated client code is how a fix lands in one page and not the other.

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

// Query purplepag.es, falling back to relay.damus.io on a CONNECT failure
// only, then POST whatever came back (even nothing) to /api/names.
// callbacks: {onDone(ok), onError(msg)}. `onDone(false)` means neither
// relay could be reached — the caller should stamp nothing client-side
// either and just let the pubkeys stay eligible for the next attempt.
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

  s86QueryRelayForNames('wss://purplepag.es', pubkeys, function (events, connected) {
    if (connected) {
      post(events);
      return;
    }
    s86QueryRelayForNames('wss://relay.damus.io', pubkeys, function (events2, connected2) {
      if (connected2) {
        post(events2);
      } else if (callbacks.onDone) {
        callbacks.onDone(false);
      }
    });
  });
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
function s86BuildRecordLine(parts, onUndo, onDismiss) {
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

  rendered.forEach(function (l) {
    if (l.kind === 'stored') {
      var record = l.ref;
      var canUndo = !(record.type === 'reason' && !record.entries);
      var line = s86BuildRecordLine(
        s86RecordLabelParts(record),
        canUndo ? function (btn) { s86UndoStoredRecord(record, btn, callbacks); } : null,
        function () {
          s86RemoveStoredRecord(record.id);
          s86RenderRecords(container, isAdmin, bannedList, callbacks);
        }
      );
      container.appendChild(line);
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
      var line = s86BuildRecordLine(
        parts,
        function (btn) { s86UndoReportBan(ban.pubkey, btn, callbacks); },
        function () {
          s86DismissReport(l.id);
          s86RenderRecords(container, isAdmin, bannedList, callbacks);
        }
      );
      container.appendChild(line);
    }
  });
}
