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

function s86BuildCommandBlock() {
  var details = document.createElement('details');
  details.appendChild(s86El('summary', 'terminal commands'));

  var inputP = document.createElement('p');
  inputP.appendChild(document.createTextNode('pubkey for the commands below (npub or hex): '));
  var input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'npub or hex';
  inputP.appendChild(input);
  details.appendChild(inputP);

  var totalPre = s86El('pre', "strfry scan --count '{}'  # total events on this relay");
  details.appendChild(totalPre);

  var countPre = document.createElement('pre');
  var purgePre = document.createElement('pre');
  details.appendChild(countPre);
  details.appendChild(purgePre);

  function render() {
    var hex = s86PubkeyInputToHex(input.value);
    if (hex) {
      countPre.textContent = "strfry scan --count '" + JSON.stringify({ authors: [hex] }) + "'  # events by this pubkey";
      purgePre.textContent = "strfry delete --filter '" + JSON.stringify({ authors: [hex] }) + "'  # delete this pubkey's events (irreversible)";
    } else {
      countPre.textContent = "strfry scan --count '{\"authors\":[\"<hex>\"]}'  # events by this pubkey — enter a pubkey above";
      purgePre.textContent = "strfry delete --filter '{\"authors\":[\"<hex>\"]}'  # delete this pubkey's events — enter a pubkey above";
    }
  }
  input.addEventListener('input', render);
  render();

  return details;
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
// the ↩ dismiss button LEFTMOST, then the label, then Undo RIGHTMOST — the
// two buttons are not equally consequential (↩ only touches localStorage,
// Undo changes server state), so the dangerous one sits isolated at the end
// and the harmless one absorbs mis-taps. The label supplies the separation
// (the locked CSS block permits no spacing rules), so it must never be empty.
//
// At most the 20 newest lines of all three kinds combined are ever rendered;
// older ones are dropped from view and, for stored ban/unban records, deleted
// from localStorage outright.

function s86RecordLabel(record) {
  var verb = record.type === 'ban' ? 'banned' : 'unbanned';
  if (record.entries.length === 1) {
    var e = record.entries[0];
    return verb + ' ' + (e.name ? e.name + ' ' : '') + (e.npub || e.pubkey);
  }
  return verb + ' ' + record.entries.length + ' pubkeys';
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

function s86BuildRecordLine(labelText, onUndo, onDismiss) {
  var line = document.createElement('p');

  var dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.textContent = '↩';
  dismissBtn.addEventListener('click', onDismiss);
  line.appendChild(dismissBtn);
  line.appendChild(document.createTextNode(' '));

  var label = document.createElement('span');
  label.textContent = labelText;
  line.appendChild(label);
  line.appendChild(document.createTextNode(' '));

  var undoBtn = document.createElement('button');
  undoBtn.type = 'button';
  undoBtn.textContent = 'Undo';
  undoBtn.addEventListener('click', function () { onUndo(undoBtn); });
  line.appendChild(undoBtn);

  return line;
}

function s86UndoStoredRecord(record, btn, callbacks) {
  btn.disabled = true;
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
    if (!r || !Array.isArray(r.entries) || r.entries.length === 0) {
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
      var line = s86BuildRecordLine(
        s86RecordLabel(record),
        function (btn) { s86UndoStoredRecord(record, btn, callbacks); },
        function () {
          s86RemoveStoredRecord(record.id);
          s86RenderRecords(container, isAdmin, bannedList, callbacks);
        }
      );
      container.appendChild(line);
    } else {
      var ban = l.ref;
      var labelText = 'reported ' + (ban.name ? ban.name + ' ' : '') + ban.npub
        + (ban.report_type ? ' — ' + ban.report_type : '');
      var line = s86BuildRecordLine(
        labelText,
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
