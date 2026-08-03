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
var S86_FIGURE_HEAD_MAX = 10;   // ranked figure rows shown before the tail is summarised — mirrors FIGURE_HEAD_MAX in server86.py
var S86_RENDER_MAX = 500;       // list rows rendered before truncation — mirrors RENDER_MAX in server86.py
var S86_THEME_KEY = 'strfry86_theme';
// Flash undo: one temporary ban/unban line after the action, then it
// disappears. Permanent undo lives on /audit only.
var S86_FLASH_UNDO_MS = 45000;
var _s86FlashUndoTimer = null;

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

// --- theme toggle ------------------------------------------------------------
// This toggle writes exactly ONE property, `color-scheme` on <html>, and it
// is still the only thing in this file that knows anything about appearance.
// The pages used to be unstyled browser-default HTML, so that one property
// picked between the browser's OWN tested light and dark palettes. They are
// painted now, but the pivot did not move: every colour in the style block is
// a `light-dark(light, dark)` token, and `light-dark()` resolves against the
// same `color-scheme` value written here. So there is still one switch, still
// two palettes, and still no colour chosen in JavaScript — a page that sets a
// colour from script is a bug, because it will not follow this toggle.
//
// 'auto' (the default, nothing stored) leaves `color-scheme: light dark`
// from the page's own <style> in effect, which follows the OS/browser dark
// mode signal. Picking 'light' or 'dark' overrides that by setting the
// property directly on the root element, which wins over the stylesheet
// rule regardless of source order. The SAME override is applied by a tiny
// inline <script> at the top of <head>, before first paint, so a stored
// preference never flashes the wrong theme on load; this function only
// needs to handle the toggle button itself.
function s86CurrentTheme() {
  try {
    var t = localStorage.getItem(S86_THEME_KEY);
    return (t === 'light' || t === 'dark') ? t : 'auto';
  } catch (e) {
    return 'auto';
  }
}

function s86ApplyTheme(pref) {
  document.documentElement.style.colorScheme = (pref === 'light' || pref === 'dark') ? pref : 'light dark';
}

// Auto has no icon of its own — it shows whichever of the two icons
// matches what the OS is CURRENTLY resolving to, via matchMedia, so the
// glyph is always an honest description of what's on screen right now
// rather than a third symbol the operator has to learn.
function s86OsPrefersDark() {
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

// buttonEl: a <button> already in the DOM, at the right end of the shared
// header (part of the flex layout in header#s86, not position:fixed).
// Cycles auto -> light
// -> dark -> auto on each press and persists the choice. The glyph (☀/☾)
// always names the CURRENT resolved appearance and the hover text (title,
// doubling as aria-label per this project's icon-button convention) names
// both the current state and what the next click does, so the icon alone
// never has to be memorized.
function s86WireThemeToggle(buttonEl) {
  function render() {
    var pref = s86CurrentTheme();
    var resolvedDark = pref === 'dark' || (pref === 'auto' && s86OsPrefersDark());
    var next = pref === 'auto' ? 'light' : (pref === 'light' ? 'dark' : 'auto');
    buttonEl.textContent = resolvedDark ? '☾' : '☀';
    var label = 'theme: ' + (pref === 'auto' ? ('auto, ' + (resolvedDark ? 'dark' : 'light')) : pref)
      + ' — click for ' + next;
    buttonEl.title = label;
    buttonEl.setAttribute('aria-label', label);
  }
  buttonEl.addEventListener('click', function () {
    var order = ['auto', 'light', 'dark'];
    var next = order[(order.indexOf(s86CurrentTheme()) + 1) % order.length];
    try {
      if (next === 'auto') {
        localStorage.removeItem(S86_THEME_KEY);
      } else {
        localStorage.setItem(S86_THEME_KEY, next);
      }
    } catch (e) {
      // ignore quota/availability errors — this is UI state, not source of truth
    }
    s86ApplyTheme(next);
    render();
  });
  // While on auto, the glyph tracks a LIVE OS change (no reload needed) —
  // the same signal color-scheme: light dark already reacts to in CSS.
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (s86CurrentTheme() === 'auto') {
        render();
      }
    });
  }
  render();
}

// --- generic DOM / formatting helpers ---------------------------------------

// className is optional and purely presentational — 'mono' for hex/npub/id
// cells, 'badge' for kind chips, 'muted' for secondary text. Callers that
// pass two arguments behave exactly as before.
function s86El(tag, text, className) {
  var el = document.createElement(tag);
  if (text != null) {
    el.textContent = text;
  }
  if (className) {
    el.className = className;
  }
  return el;
}

// A status pill with a leading dot: live (green) or its off state (muted).
// Used wherever a page has a continuously-updating source, so "this number is
// moving" is visible without reading the age line under it.
function s86BuildPill(label, isLive) {
  var pill = s86El('span', null, isLive ? 'pill live' : 'pill');
  pill.appendChild(s86El('span', null, 'dot'));
  pill.appendChild(document.createTextNode(label));
  return pill;
}

// An outcome chip: accepted (green), blocked (accent), unknown (muted dash).
// `null` is NOT "accepted" — it means nothing judged this event, which the
// fallback feed cannot distinguish from an accept (rule 9).
function s86BuildResult(result, title) {
  if (!result) {
    return s86El('span', '—', 'muted');
  }
  var chip = s86El('span', result, result === 'blocked' ? 'chip bad' : 'chip good');
  if (title) {
    chip.title = title;
  }
  return chip;
}

// Identity dot for a pubkey. Deliberately NOT an avatar: rendering a profile
// picture means fetching an arbitrary URL from an arbitrary host for every
// row on the page, which is exactly the external traffic this project avoids.
// The hue is derived from the pubkey itself, so the same key is the same
// colour on every page and in every session without anything being stored.
function s86BuildIdentityDot(pubkeyHex) {
  var dot = s86El('span', null, 'idot');
  if (typeof pubkeyHex === 'string' && pubkeyHex.length >= 6) {
    // Folded over the WHOLE key, not a prefix: keys that share a prefix are
    // exactly the pair a colour most needs to tell apart.
    var h = 0;
    for (var i = 0; i < pubkeyHex.length; i++) {
      h = (h * 31 + pubkeyHex.charCodeAt(i)) % 360;
    }
    dot.style.setProperty('--h', String(h));
  } else {
    dot.classList.add('unknown');
  }
  return dot;
}

// Copy plain text to the clipboard. Prefer the async Clipboard API when the
// page is a secure context; otherwise (and on API failure) fall back to a
// short-lived <textarea> + document.execCommand('copy'). The fallback is
// what makes copy work on plain-http admin UIs — navigator.clipboard is
// simply absent there, and the old writeText-only path failed silently.
// Returns true when the write was initiated (async) or succeeded (sync).
function s86CopyText(text) {
  if (text == null || text === '') {
    return false;
  }
  var canAsync = !!(window.isSecureContext
    && navigator.clipboard
    && typeof navigator.clipboard.writeText === 'function');
  if (canAsync) {
    navigator.clipboard.writeText(text).catch(function () {
      s86CopyTextFallback(text);
    });
    return true;
  }
  return s86CopyTextFallback(text);
}

function s86CopyTextFallback(text) {
  try {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    // Keep it in-viewport but invisible — some browsers refuse to copy from
    // display:none / off-screen-far elements during a user gesture.
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '0';
    ta.style.width = '1px';
    ta.style.height = '1px';
    ta.style.padding = '0';
    ta.style.border = 'none';
    ta.style.outline = 'none';
    ta.style.boxShadow = 'none';
    ta.style.background = 'transparent';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    var ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return !!ok;
  } catch (e) {
    return false;
  }
}

// Name cell as the design draws it: identity dot, then the best label this
// row has — display name, else nip-05, else a truncated npub — never a raw
// 64-hex key, which is unreadable at row height.
//
// linkIt (admin): the label navigates to this relay's profile page.
// Otherwise, if an npub is known, a click copies it — public viewers have no
// profile page to open, and the full npub is what they need to paste elsewhere.
// Column label for phone card layout (table.cards). Cells without a label —
// checkbox picks and trailing Ban/Undo actions — omit this and the card CSS
// does not draw a label strip. Figure tables never use cards or data-label.
function s86DataLabel(td, label) {
  if (td && label) {
    td.setAttribute('data-label', label);
  }
  return td;
}

// Identity cell for a list row. Optional dataLabel (default "User") is the
// phone-card column name; pass false to suppress it.
function s86BuildIdentityCell(row, linkIt, dataLabel) {
  var td = s86El('td', null, 'identity');
  // Flex lives on this inner row, never on the <td> — display:flex on a
  // table-cell breaks border alignment with the rest of the row.
  var idrow = s86El('span', null, 'idrow');
  idrow.appendChild(s86BuildIdentityDot(row.pubkey));
  var label = row.name || row.nip05 || (row.npub ? s86ShortKey(row.npub) : '—');
  if (linkIt && row.npub && row.pubkey) {
    var a = s86NpubLink(row.npub, row.pubkey, true);
    a.textContent = label;
    a.title = row.npub;
    idrow.appendChild(a);
  } else {
    var span = s86El('span', label);
    if (row.npub) {
      var idleTitle = row.npub + ' — click to copy';
      span.title = idleTitle;
      span.style.cursor = 'pointer';
      span.setAttribute('role', 'button');
      span.tabIndex = 0;
      var setTitles = function (t) {
        span.title = t;
        td.title = t;
      };
      var flashTitle = function (ok) {
        setTitles(ok ? 'copied' : 'copy failed');
        setTimeout(function () {
          setTitles(idleTitle);
        }, 1200);
      };
      var copyNpub = function (e) {
        if (e) {
          e.preventDefault();
        }
        // Secure-context path is async; flash "copied" optimistically — the
        // fallback inside s86CopyText recovers if writeText rejects.
        // Non-secure path is sync and reports real success/failure.
        var ok = s86CopyText(row.npub);
        flashTitle(ok);
      };
      // Whole cell is the hit target — the truncated label is easy to miss
      // between the identity dot and the ellipsis edge.
      td.style.cursor = 'pointer';
      td.title = idleTitle;
      td.addEventListener('click', copyNpub);
      span.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          copyNpub(e);
        }
      });
    }
    idrow.appendChild(span);
  }
  td.appendChild(idrow);
  if (dataLabel !== false) {
    s86DataLabel(td, dataLabel || 'User');
  }
  return td;
}

// One truncation rule for every key-shaped string on every page.
function s86ShortKey(key) {
  if (typeof key !== 'string' || key.length < 20) {
    return key || '—';
  }
  return key.slice(0, 10) + '…' + key.slice(-6);
}

// Metric tiles. A <dl> rather than a grid of <div>s because the rendering
// rules allow exactly two shapes for a numeric result, <table> or <dl>, and
// a tile IS a term and its definition. tiles: [{label, value, note, tone}].
function s86BuildTiles(tiles) {
  var dl = s86El('dl', null, 'tiles');
  tiles.forEach(function (t) {
    var cell = s86El('div', null, 'tile');
    cell.appendChild(s86El('dt', t.label));
    cell.appendChild(s86El('dd', t.value == null ? '—' : String(t.value)));
    if (t.note) {
      cell.appendChild(s86El('p', t.note, t.tone ? ('note ' + t.tone) : 'note'));
    }
    dl.appendChild(cell);
  });
  return dl;
}

// Header row for a data table. Labels are the design's words; every caller
// passes them in the design's order. `growIndex` names the one column that
// should absorb the leftover width (the free-text column, always).
// Prefer s86BuildSortableTableHead for list tables the operator can re-order.
function s86BuildTableHead(labels, growIndex) {
  var thead = document.createElement('thead');
  var tr = document.createElement('tr');
  labels.forEach(function (label, i) {
    tr.appendChild(s86El('th', label, i === growIndex ? 'grow' : null));
  });
  thead.appendChild(tr);
  return thead;
}

// Domain half of a NIP-05 address (`alice@example.com` → `example.com`).
// Used for sort and search so "example.com" finds and groups every local-part
// on that domain rather than ordering by the name before the @.
function s86Nip05Domain(nip05) {
  if (typeof nip05 !== 'string' || !nip05) {
    return '';
  }
  var at = nip05.lastIndexOf('@');
  return at === -1 ? nip05.toLowerCase() : nip05.slice(at + 1).toLowerCase();
}

// Clickable column headers for list tables. `columns` is
// [{label, key?, dir?, grow?}, …] — omit `key` for non-sortable cells
// (checkbox, Ban/Undo). Active column shows ↑/↓ and aria-sort; a second
// click on the active field flips direction. `state` is {field, dir};
// `onSort(field, dir)` re-renders. A real <button> inside the <th> keeps
// keyboard and screen-reader behaviour honest.
function s86BuildSortableTableHead(columns, state, onSort) {
  var thead = document.createElement('thead');
  var tr = document.createElement('tr');
  (columns || []).forEach(function (col) {
    var th = document.createElement('th');
    if (col.grow) {
      th.className = 'grow';
    }
    if (!col.key) {
      th.textContent = col.label || '';
      tr.appendChild(th);
      return;
    }
    th.classList.add('sortable');
    var active = !!(state && state.field === col.key);
    th.setAttribute('aria-sort', active
      ? (state.dir === 'asc' ? 'ascending' : 'descending')
      : 'none');
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'th-sort';
    btn.textContent = col.label + (active ? (state.dir === 'asc' ? ' ↑' : ' ↓') : '');
    btn.addEventListener('click', function () {
      var nextDir = active
        ? (state.dir === 'asc' ? 'desc' : 'asc')
        : (col.dir || 'desc');
      onSort(col.key, nextDir);
    });
    th.appendChild(btn);
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  return thead;
}

function s86FormatBytes(n) {
  if (n == null) {
    return null;
  }
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = 0;
  var v = Number(n);
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + ' ' + units[i];
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
function s86NavigateSearchInput(raw, statusEl) {
  var hex = s86PubkeyInputToHex(raw);
  if (hex) {
    window.location.href = '/profile?hex=' + hex;
    return true;
  }
  var domain = s86ValidateDomainInput(raw);
  if (domain) {
    window.location.href = '/domain?d=' + encodeURIComponent(domain);
    return true;
  }
  if (statusEl) {
    statusEl.textContent = 'enter a valid npub, hex pubkey, or domain';
  }
  return false;
}

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
    s86NavigateSearchInput(input.value, statusEl);
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

// --- page chrome (header + nav + search + theme) -------------------------
// Fixed ingredients on every page per CLAUDE.md "Header — identical on
// every page, same positions, always". Logo is clickable plain text (no
// underline, inherits color). Search accepts npub / 64-hex / domain;
// invalid input sets the status line only. Ctrl-K / Cmd-K focuses search.
// Nav order is fixed. adminOnly tabs (Users, Audit, Settings) are blank
// until login, so they stay out of the nav until the admin is signed in.
// [label, path, adminOnly]
var S86_NAV_LINKS = [
  ['Live Feed', '/', false],
  ['Stats & Console', '/stats', false],
  ['Users', '/users', true],
  ['Banlist', '/bans', false],
  ['Audit Log', '/audit', true],
  ['Settings', '/settings', true]
];

function s86SetNavAdminVisible(visible) {
  var nav = document.querySelector('header#s86 nav');
  if (!nav) {
    return;
  }
  var links = nav.querySelectorAll('a[data-admin-only="1"]');
  for (var i = 0; i < links.length; i++) {
    links[i].hidden = !visible;
  }
}

function s86BuildPageChrome(statusEl) {
  var header = document.createElement('header');
  header.id = 's86';

  // Two rows, same ingredients in the same order on every page: the brand row
  // carries the wordmark and the two account controls, the bar below it
  // carries the nav tabs and the search box. The current page's tab is the
  // only thing that differs between pages, and it differs by attribute
  // (aria-current) rather than by markup, so the header still renders
  // identically everywhere the CSS is identical.
  var brand = document.createElement('div');
  brand.className = 'brand';
  header.appendChild(brand);

  var logo = document.createElement('a');
  logo.className = 'logo';
  logo.href = '/';
  logo.textContent = 'strfry-86';
  brand.appendChild(logo);

  brand.appendChild(s86El('span', 'Relay sidecar — admin console', 'tagline'));
  brand.appendChild(s86El('span', null, 'spacer'));

  var loginBtn = document.createElement('button');
  loginBtn.type = 'button';
  loginBtn.id = 'login-btn';
  loginBtn.textContent = 'Login with extension';
  brand.appendChild(loginBtn);

  var themeBtn = document.createElement('button');
  themeBtn.type = 'button';
  themeBtn.id = 'theme-btn';
  brand.appendChild(themeBtn);
  s86WireThemeToggle(themeBtn);

  var bar = document.createElement('div');
  bar.className = 'bar';
  header.appendChild(bar);

  var here = window.location.pathname;
  var nav = document.createElement('nav');
  S86_NAV_LINKS.forEach(function (pair) {
    var a = document.createElement('a');
    a.href = pair[1];
    a.textContent = pair[0];
    if (pair[2]) {
      a.setAttribute('data-admin-only', '1');
      a.hidden = true;
    }
    if (here === pair[1]) {
      a.setAttribute('aria-current', 'page');
    }
    nav.appendChild(a);
  });
  bar.appendChild(nav);

  var searchInput = document.createElement('input');
  searchInput.type = 'search';
  searchInput.id = 'q';
  searchInput.placeholder = 'npub, hex, or domain';
  searchInput.setAttribute('aria-label', 'search npub, hex, or domain');
  searchInput.addEventListener('keydown', function (evt) {
    if (evt.key === 'Enter') {
      evt.preventDefault();
      s86NavigateSearchInput(searchInput.value, statusEl);
    }
  });
  bar.appendChild(searchInput);

  document.addEventListener('keydown', function (evt) {
    if (!(evt.metaKey || evt.ctrlKey)) {
      return;
    }
    if (evt.key !== 'k' && evt.key !== 'K') {
      return;
    }
    evt.preventDefault();
    searchInput.focus();
    searchInput.select();
  });

  if (document.body.firstChild) {
    document.body.insertBefore(header, document.body.firstChild);
  } else {
    document.body.appendChild(header);
  }

  return { loginBtn: loginBtn, themeBtn: themeBtn, searchInput: searchInput, header: header };
}

// --- live layer (shared SSE connection) -------------------------------------
// GET /api/live is ONE server-side strfry poll loop fanned out to every
// connected / and /stats tab (CLAUDE.md "Endpoints"). onMessage(data)
// fires for every `event: live` frame, including the very first one, which
// carries the server's current state (ring buffer + delta counters) — never
// this browser's own history, per the "no history fetch" rule. EventSource
// reconnects on its own after a drop; onDisconnected fires once per drop so
// the page can say so, and the next onMessage clears it back out.
function s86ConnectLive(onMessage, onDisconnected) {
  if (typeof EventSource === 'undefined') {
    if (onDisconnected) {
      onDisconnected();
    }
    return null;
  }
  var es = new EventSource('/api/live');
  es.addEventListener('live', function (evt) {
    try {
      onMessage(JSON.parse(evt.data));
    } catch (e) {
      // malformed frame — wait for the next one rather than throwing
    }
  });
  es.onerror = function () {
    if (onDisconnected) {
      onDisconnected();
    }
  };
  return es;
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
        s86SetNavAdminVisible(true);
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
    s86SetNavAdminVisible(true);
    onLogin(remembered);
    return true;
  }
  return false;
}

// --- NIP-98 signing -----------------------------------------------------
// Auth is carried in the JSON body (not an Authorization header). The
// signed event therefore cannot cover the raw wire body (which includes
// auth itself). Instead both sides hash the non-auth object with sorted
// keys (NIP-98 `payload` tag) and bind `u` to window.location.origin so a
// phishing host's signature is useless on this relay.

function s86CanonicalJson(value) {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return '[' + value.map(s86CanonicalJson).join(',') + ']';
  }
  var keys = Object.keys(value).sort();
  var parts = [];
  for (var i = 0; i < keys.length; i++) {
    parts.push(JSON.stringify(keys[i]) + ':' + s86CanonicalJson(value[keys[i]]));
  }
  return '{' + parts.join(',') + '}';
}

function s86Sha256Hex(text) {
  var bytes = new TextEncoder().encode(text);
  return crypto.subtle.digest('SHA-256', bytes).then(function (buf) {
    var arr = new Uint8Array(buf);
    var hex = '';
    for (var i = 0; i < arr.length; i++) {
      hex += arr[i].toString(16).padStart(2, '0');
    }
    return hex;
  });
}

function s86SignAndPost(endpoint, extraBody) {
  // Auth always wins: never let a caller-supplied `auth` field clobber the
  // event this function is about to sign.
  var data = Object.assign({}, extraBody || {});
  delete data.auth;
  var url = window.location.origin + endpoint;

  // Wrapped in Promise.resolve().then(...) so a synchronous throw here (no
  // window.nostr, or no crypto.subtle because the page isn't in a secure
  // context — plain http:// on anything but localhost) becomes a rejection
  // instead of an exception that skips every caller's own .then/.catch.
  return Promise.resolve()
    .then(function () {
      if (!window.crypto || !window.crypto.subtle) {
        throw new Error('signing needs a secure context (https:// or localhost)');
      }
      if (!window.nostr) {
        throw new Error('a NIP-07 extension is required');
      }
      return s86Sha256Hex(s86CanonicalJson(data));
    })
    .then(function (payloadHex) {
      var event = {
        kind: 27235,
        created_at: Math.floor(Date.now() / 1000),
        tags: [
          ['u', url],
          ['method', 'POST'],
          ['payload', payloadHex]
        ],
        content: ''
      };
      return window.nostr.signEvent(event);
    })
    .then(function (signed) {
      var body = Object.assign({}, data, { auth: signed });
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
// There is no shared filter helper any more. It hid non-matching rows with
// display:none, which meant "shown" and "rendered" were different sets and
// every other helper had to keep asking which was which. Every page now
// filters the DATA and re-renders, so what is in the DOM is exactly what
// matches. s86VisibleRowCount below still honours display:none, because a
// page is allowed to hide a row for other reasons.

// --- select-visible + live count ---------------------------------------
// Labelled "Select visible" everywhere, never "Select all" — the control
// has always been filter-scoped, so the old label was lying by omission
// on a control that can mass-unban.

// A selectable row is an <li> on the pages that render lists and a <tr> on
// the pages that render tables. Both shapes go through these two helpers, so
// "select visible" means the same thing — and skips hidden rows the same way
// — regardless of which element the page happened to use.
var S86_ROW_SELECTOR = 'li, tbody > tr';

function s86RowOf(el) {
  return el.closest(S86_ROW_SELECTOR);
}

function s86VisibleRowCount(listEl) {
  var rows = listEl.querySelectorAll(S86_ROW_SELECTOR);
  var shown = 0;
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].style.display !== 'none') {
      shown++;
    }
  }
  return shown;
}

function s86UpdateSelectedCount(listEl, checkboxClass, countEl, extraButtons) {
  var n = listEl.querySelectorAll('.' + checkboxClass + ':checked').length;
  var shown = s86VisibleRowCount(listEl);
  countEl.textContent = n > 0 ? n + ' selected of ' + shown + ' shown' : '';
  (extraButtons || []).forEach(function (btn) {
    btn.disabled = n === 0;
  });
}

function s86WireSelectAll(selectAllEl, listEl, checkboxClass, countEl, extraButtons) {
  selectAllEl.addEventListener('change', function () {
    var boxes = listEl.querySelectorAll('.' + checkboxClass);
    for (var i = 0; i < boxes.length; i++) {
      var row = s86RowOf(boxes[i]);
      if (row && row.style.display === 'none') {
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

// --- figure tables --------------------------------------------------------
// CLAUDE.md 'Rendering results': a numeric result is a semantic <table>,
// never a sentence and never a space-padded <pre>. Browser-default
// rendering only — no new CSS.

// rows: [[label, value, shareText|null], ...]. shareText names its own
// denominator ('64.7% of all') so percentages never collide in one
// sentence when they refer to different totals.
function s86BuildKeyValueTable(rows) {
  var table = document.createElement('table');
  rows.forEach(function (r) {
    var tr = document.createElement('tr');
    var th = document.createElement('th');
    th.textContent = r[0];
    tr.appendChild(th);
    var td = document.createElement('td');
    td.textContent = typeof r[1] === 'number' ? r[1].toLocaleString() : (r[1] || '');
    tr.appendChild(td);
    var td2 = document.createElement('td');
    td2.textContent = r[2] || '';
    tr.appendChild(td2);
    table.appendChild(tr);
  });
  return table;
}

// Ranked two-column <table> of (label, count), capped at
// S86_FIGURE_HEAD_MAX with a one-line tail summary naming the omitted
// count AND its total — never a bare 'more' — plus the full list behind a
// <details>, one row per line, never a comma-separated run.
// rows: [[label, count], ...], already sorted descending by count.
function s86BuildRankedFigureTable(rows, opts) {
  opts = opts || {};
  var wrap = document.createElement('div');
  if (rows.length === 0) {
    if (opts.emptyText) {
      wrap.appendChild(s86El('p', opts.emptyText));
    }
    return wrap;
  }

  var unit = opts.unit || 'rows';

  function rowTr(r) {
    var tr = document.createElement('tr');
    var th = document.createElement('th');
    th.textContent = r[0];
    tr.appendChild(th);
    var td = document.createElement('td');
    td.textContent = r[1].toLocaleString();
    tr.appendChild(td);
    var td2 = document.createElement('td');
    td2.textContent = r[2] || '';
    tr.appendChild(td2);
    return tr;
  }

  var head = rows.slice(0, S86_FIGURE_HEAD_MAX);
  var tail = rows.slice(S86_FIGURE_HEAD_MAX);

  var headTable = document.createElement('table');
  head.forEach(function (r) { headTable.appendChild(rowTr(r)); });

  if (tail.length > 0) {
    // A server-supplied total (e.g. unlisted_total) avoids summing every
    // one of potentially hundreds of tail rows just to report their sum —
    // the head is only S86_FIGURE_HEAD_MAX rows, so subtracting is cheap
    // where re-summing the tail would not be.
    var headTotal = head.reduce(function (sum, r) { return sum + r[1]; }, 0);
    var tailTotal = opts.grandTotal != null ? (opts.grandTotal - headTotal)
      : tail.reduce(function (sum, r) { return sum + r[1]; }, 0);
    var tailTr = document.createElement('tr');
    var tailTd = document.createElement('td');
    tailTd.colSpan = 3;
    tailTd.textContent = '+ ' + tail.length.toLocaleString() + ' more ' + unit + ', ' + tailTotal.toLocaleString() + (opts.itemNoun || ' events');
    tailTr.appendChild(tailTd);
    headTable.appendChild(tailTr);
  }
  wrap.appendChild(headTable);

  if (tail.length > 0) {
    var details = document.createElement('details');
    var summary = document.createElement('summary');
    summary.textContent = 'all ' + rows.length.toLocaleString() + ' ' + unit;
    details.appendChild(summary);
    var fullTable = document.createElement('table');
    rows.forEach(function (r) { fullTable.appendChild(rowTr(r)); });
    details.appendChild(fullTable);
    wrap.appendChild(details);
  }

  return wrap;
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
    s86CopyText(cmd);
  });
}

// --- command blocks -----------------------------------------------------
// Nothing here is executed and nothing here is fetched — every command is
// plain copyable text. Identical on every page.
//
// One command per box, one Copy button per box — a box holding two or more
// shell commands taught operators to copy the whole block (comments, blank
// lines and all) and edit it down by hand before pasting. The slight
// background is inline rather than a rule in the locked <style> block: it
// marks only these boxes, not every <pre> on the page (the curl hint,
// figures, etc.), and it is mixed from the same system colors the theme
// section already uses (CanvasText/Canvas) so it never needs a light/dark
// override of its own.
//
// A chunked exempt-purge command embeds up to S86_GIFTWRAP_PURGE_CHUNK_SIZE
// 64-hex pubkeys in one filter — tens of thousands of characters that wrap
// into a wall of hex nobody reads, burying the boxes around it. Past
// S86_COMMAND_DISPLAY_MAX the box shows only the head (which is always
// where the operative part of the command lives — "scan --count" vs
// "delete --filter" never sits inside the truncated tail) plus a notice
// stating exactly how much is hidden. The notice is never a footnote: it is
// bold, and it says outright that Copy still copies the whole command, so
// truncation is never mistaken for the command itself.
var S86_COMMAND_DISPLAY_MAX = 400;

function s86AppendCommandBox(container, text) {
  var box = document.createElement('div');
  box.style.background = 'color-mix(in srgb, CanvasText 8%, Canvas)';
  box.style.borderRadius = '0.3em';
  box.style.padding = '0.5em 0.6em';
  box.style.margin = '0.4em 0';

  var isTruncated = text.length > S86_COMMAND_DISPLAY_MAX;

  var pre = document.createElement('pre');
  pre.style.margin = '0 0 0.4em 0';
  pre.style.background = 'transparent';
  pre.textContent = isTruncated ? text.slice(0, S86_COMMAND_DISPLAY_MAX) : text;
  box.appendChild(pre);

  if (isTruncated) {
    var hiddenChars = text.length - S86_COMMAND_DISPLAY_MAX;
    var notice = s86El('strong', '⚠ truncated for display — ' + hiddenChars.toLocaleString()
      + ' more characters not shown. Copy still copies the command in full.');
    var noticeP = document.createElement('p');
    noticeP.style.margin = '0 0 0.4em 0';
    noticeP.appendChild(notice);
    box.appendChild(noticeP);

    var details = document.createElement('details');
    details.appendChild(s86El('summary', 'show full command'));
    var fullPre = document.createElement('pre');
    fullPre.style.margin = '0.4em 0 0 0';
    fullPre.style.background = 'transparent';
    fullPre.textContent = text;
    details.appendChild(fullPre);
    box.appendChild(details);
  }

  var copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.textContent = 'Copy';
  copyBtn.addEventListener('click', function () {
    s86CopyText(text);
  });
  box.appendChild(copyBtn);

  container.appendChild(box);
  return box;
}

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
  'report-walk': 'the database walk',
  'giftwrap-purge': 'the gift-wrap purge',
  'ephemeral-purge': 'the ephemeral-kinds purge',
  'db-compact': 'database compact'
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
// Covers ONLY the running/blocked wording — the idle states (never run /
// result / failed) are rendered by s86BuildScanPanel's applyStatus below,
// since those need the CACHED RECORD (scanned_at/warning/error), not just
// the job-status dict, to tell a fresh result apart from a failed attempt
// still showing a stale one.
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
  if (status.error) {
    return 'could not run — ' + status.error;
  }
  if (!status.scanned_at) {
    return 'never run';
  }
  return s86FormatScanAge(status.scanned_at);
}

// opts: {
//   heading, costText (string shown before any run — describes the WORK,
//     never a duration), buttonLabel, onStart (function() -> Promise,
//     issues the signed POST),
//   renderResult(container, record) — called only when a usable result
//     exists (a successful run, OR a previous successful run being shown
//     underneath a failed attempt).
//   lastRunNote(record) -> string|null, appended to the cost line once a
//     record exists ('last run: 9m 06s at 4,814 events/sec'). No duration
//     from a relay other than the operator's own is ever printed here.
//   extraNote(record) -> string|null — a staleness-consequence line.
// }
// Returns {el, applyStatus(jobStatus, record)}. The caller owns polling
// and auth; this only owns the DOM shape and the shared status wording.
//
// Four panel states (CLAUDE.md 'Rendering results', rule 7), and each has
// exactly one slot for message text (rule 8):
//   never run — no cache: the cost line and the button, visibly empty.
//   running   — the job holds the lock: the progress line only.
//   result    — cache present, run succeeded: the age, then the figures.
//   failed    — run started, produced no usable result: why it failed,
//               and the PREVIOUS result (if any) at its TRUE age — never
//               rendered as though it were this attempt's output.
// `error` and `warning` are distinct and never both rendered as the same
// line: `error` replaces a result that is NOT rendered as fresh; `warning`
// rides alongside a result that IS.
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

  var msgLine = s86El('p', '');
  el.appendChild(msgLine);

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
    var running = jobStatus.status === 'running';
    var blocked = !!jobStatus.blocked_by;
    btn.disabled = running || blocked;

    resultEl.textContent = '';
    msgLine.textContent = '';
    costLine.textContent = opts.costText;

    if (running || blocked) {
      statusLine.textContent = s86ScanStatusText(jobStatus);
      return;
    }

    record = record || {};
    var hasResult = record.scanned_at != null;
    var hasError = !!record.error;

    if (hasError) {
      statusLine.textContent = 'could not run — ' + record.error;
      if (hasResult) {
        costLine.textContent = opts.costText + ' — last successful run '
          + s86FormatDate(record.scanned_at) + ' (' + s86FormatRelativeAge(record.scanned_at) + ')';
        if (opts.renderResult) {
          opts.renderResult(resultEl, record);
        }
      }
      return;
    }

    if (!hasResult) {
      statusLine.textContent = 'never run';
      return;
    }

    statusLine.textContent = s86FormatScanAge(record.scanned_at);
    if (opts.lastRunNote) {
      var note = opts.lastRunNote(record);
      if (note) {
        costLine.textContent = opts.costText + ' — ' + note;
      }
    }
    if (record.warning) {
      msgLine.textContent = record.warning;
    }
    if (opts.renderResult) {
      opts.renderResult(resultEl, record);
    }
    if (opts.extraNote) {
      var extra = opts.extraNote(record);
      if (extra) {
        resultEl.appendChild(s86El('p', extra));
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
        // Keep polling while THIS job runs, and also while it is blocked by
        // another one. A blocked job's own status reads `idle`, so stopping
        // on status alone leaves the panel frozen on "waiting on X…" forever:
        // nothing ever fetches again to notice that X has finished.
        if (data.status === 'running' || data.blocked_by) {
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
// and one command box per command — never one box holding several
// commands, since that taught operators to copy-and-hand-edit a blob
// instead of copying exactly what they meant to run. Nothing here is
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
// A guess, nothing more — the Docker image's mount point, true only for that
// one deployment shape. s86ResolveStrfryConfFlag() below overwrites this with
// server86's own resolved path (the same one /api/settings shows on the
// Settings page) as soon as an admin session can ask for it; every command
// generated before that answer lands is built from this placeholder, so a
// source build or a relocated conf sends the operator a command that reads
// the wrong database — or none — with no hint that it's wrong.
var S86_STRFRY_CONFIG_FLAG = '--config /config/strfry.conf';

// Admin-only (the path is filesystem detail GET /api/metrics deliberately
// withholds from the public). Call once a page knows it is logged in as
// admin; commands are rendered lazily off field input, so this only needs
// to win the race against the operator actually typing something, not
// against page load.
function s86ResolveStrfryConfFlag() {
  return s86SignAndPost('/api/strfry-conf-path', {})
    .then(function (result) {
      if (result.ok && result.body && result.body.exists && result.body.path) {
        S86_STRFRY_CONFIG_FLAG = '--config ' + result.body.path;
      }
    })
    .catch(function () {});
}
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

// Stream-sum authored events for one pubkey. Reports JSONL line bytes
// (serialized event body as strfry prints it) and content-field UTF-8
// bytes. That is the best per-author size strfry can give from the
// outside — LMDB page overhead is shared and not attributable per key,
// so freeable disk after a delete is a different, larger question.
function s86PyStorageByAuthorScript() {
  return [
    'import sys, json',
    'n = 0',
    'raw = 0',
    'content = 0',
    'kinds = {}',
    'for line in sys.stdin:',
    '    line = line.strip()',
    '    if not line:',
    '        continue',
    '    raw += len(line.encode("utf-8"))',
    '    n += 1',
    '    try:',
    '        e = json.loads(line)',
    '    except ValueError:',
    '        continue',
    '    k = e.get("kind")',
    '    kinds[k] = kinds.get(k, 0) + 1',
    '    c = e.get("content") or ""',
    '    if isinstance(c, str):',
    '        content += len(c.encode("utf-8"))',
    'def fmt(b):',
    '    for u, d in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):',
    '        if b >= d:',
    '            return "%.2f %s" % (b / float(d), u)',
    '    return "%d B" % b',
    'print("events:", n)',
    'print("jsonl bytes:", raw, "(" + fmt(raw) + ")")',
    'print("content field bytes:", content, "(" + fmt(content) + ")")',
    'print("mean jsonl bytes/event:", (raw // n) if n else 0)',
    'print("kind histogram:")',
    'for k, c in sorted(kinds.items(), key=lambda kv: -kv[1]):',
    '    print("  kind", k, ":", c)',
    'print("note: authored-event payload size only; LMDB page overhead is extra")',
  ].join('\n');
}

// Returns [countCommand, deleteCommand] — two commands, meant to be
// copied and run one at a time (count first), never as one pasted blob.
function s86RenderDeleteWithCountFirst(filterObj) {
  var filterJson = JSON.stringify(filterObj);
  return [
    "strfry " + S86_STRFRY_CONFIG_FLAG + " scan --count '" + filterJson + "'",
    "strfry " + S86_STRFRY_CONFIG_FLAG + " delete --filter '" + filterJson + "'",
  ];
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
  },
  {
    key: 'storage_by_author',
    label: 'Storage used by author',
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

  var commandsEl = document.createElement('div');
  details.appendChild(commandsEl);

  // A message (an error, a prompt to fill in a field) is plain text with
  // no box and no copy button — only an actual command gets those.
  function setMessage(text) {
    commandsEl.textContent = '';
    commandsEl.appendChild(s86El('p', text));
  }

  // `items` is an array of strings (one box, no label) or
  // {label, command} (a label line above its own box) — every command in
  // its own box with its own Copy button, never merged into one blob.
  function setCommands(items) {
    commandsEl.textContent = '';
    items.forEach(function (item) {
      if (typeof item === 'string') {
        s86AppendCommandBox(commandsEl, item);
      } else {
        commandsEl.appendChild(s86El('p', item.label));
        s86AppendCommandBox(commandsEl, item.command);
      }
    });
  }

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

  // [countCommand, deleteCommand] -> [{label, command}, {label, command}],
  // the pair every destructive intent below emits as its two boxes.
  function countThenDeleteItems(filterObj) {
    var pair = s86RenderDeleteWithCountFirst(filterObj);
    return [
      { label: 'read this count BEFORE deleting:', command: pair[0] },
      { label: 'then, if the count looks right:', command: pair[1] }
    ];
  }

  function renderDeleteByAuthor() {
    var hex = s86PubkeyInputToHex(fieldInputs.pubkey.value);
    if (!hex) {
      setMessage('enter a pubkey above');
      return;
    }
    var daysCheck = s86ValidateOptionalDaysInput(fieldInputs.days.value);
    if (!daysCheck.ok) {
      setMessage('enter a positive whole number of days above, or leave it blank for all time');
      return;
    }
    var kindCheck = s86ValidateOptionalKindInput(fieldInputs.kind.value);
    if (!kindCheck.ok) {
      setMessage('enter a valid kind number above, or leave it blank');
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
    setCommands(countThenDeleteItems(filter));
  }

  function renderGiftwrapPurge() {
    var daysCheck = s86ValidateDaysInput(fieldInputs.days.value);
    if (!daysCheck.ok) {
      setMessage('enter a positive whole number of days above');
      return;
    }
    var days = daysCheck.value;
    var cutoff = Math.floor(Date.now() / 1000) - days * 86400;
    var blanketItems = countThenDeleteItems({ kinds: [1059], until: cutoff });

    if (!fieldInputs.exempt.checked) {
      setCommands(blanketItems);
      return;
    }

    var recipients = purgeSources.getRecipientsCache();
    var subscribers = purgeSources.getSubscribersCache();
    var subsFresh = subscribers && subscribers.scanned_at
      && (Math.floor(Date.now() / 1000) - subscribers.scanned_at) <= S86_SUBSCRIBER_CACHE_STALE_SECONDS;
    var subsSaturated = !!(subscribers && subscribers.saturated);
    var recipientsAvailable = recipients && recipients.scanned_at;

    // Saturation is a REFUSAL here, not a label, and is checked separately
    // from staleness: a saturated subscriber scan is a FLOOR on who
    // subscribes, so missing subscribers means missing exemptions means
    // MORE deletion — the opposite of a saturated recipient scan, which
    // only ever deletes less. A floor cannot be used as an exemption list.
    if (!recipientsAvailable || !subsFresh || subsSaturated) {
      var reason = !recipients || !recipients.scanned_at
        ? 'no recipient scan has been run yet'
        : (!subscribers || !subscribers.scanned_at
          ? 'no subscriber scan has been run yet'
          : (!subsFresh
            ? 'the subscriber scan is more than 7 days old'
            : 'the subscriber scan hit its ' + (subscribers.scan_limit ? subscribers.scan_limit.toLocaleString() + '-event ' : '')
              + 'cap — a floor cannot be used as an exemption list'));
      extraEl.appendChild(s86El('p', 'subscriber-exempt form unavailable: ' + reason
        + '. Run the missing scan(s) on the report page, or uncheck "exempt subscribers" for the blanket form below.'));
      setCommands(blanketItems);
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
    var items = [];
    if (chunks.length === 0) {
      items = countThenDeleteItems({ kinds: [1059], until: cutoff });
    } else {
      chunks.forEach(function (chunk, i) {
        var pairItems = countThenDeleteItems({ kinds: [1059], until: cutoff, '#p': chunk });
        pairItems[0].label = 'chunk ' + (i + 1) + ' of ' + chunks.length + ' (' + chunk.length + ' pubkeys) — read this count BEFORE deleting:';
        pairItems[1].label = 'chunk ' + (i + 1) + ' of ' + chunks.length + ' — then, if the count looks right:';
        items = items.concat(pairItems);
      });
    }
    items.push({ label: 'blanket form below — deletes ALL gift wraps in the window, subscribers included:', command: blanketItems[0].command });
    items.push({ label: blanketItems[1].label, command: blanketItems[1].command });
    setCommands(items);
  }

  function renderKindsByAuthor() {
    var hex = s86PubkeyInputToHex(fieldInputs.pubkey.value);
    if (!hex) {
      setMessage('enter a pubkey above');
      return;
    }
    setCommands(["strfry " + S86_STRFRY_CONFIG_FLAG + " scan '" + JSON.stringify({ authors: [hex] }) + "' | python3 -c '\n" + s86PyKindTallyScript() + "\n'"]);
  }

  // Storage for one npub: strfry has no per-author disk metric. We can
  // (1) count authored events from the index in seconds, (2) stream those
  // events and sum JSONL/content bytes for a real payload figure, and
  // (3) count gift wraps addressed TO them (kind 1059 + #p) — those
  // bodies sit under the *sender's* author key, so size would be a
  // different, heavier scan and is left as a count only.
  function renderStorageByAuthor() {
    var hex = s86PubkeyInputToHex(fieldInputs.pubkey.value);
    if (!hex) {
      setMessage('enter a pubkey above');
      return;
    }
    var authorFilter = JSON.stringify({ authors: [hex] });
    var gwFilter = JSON.stringify({ kinds: [1059], '#p': [hex] });
    extraEl.appendChild(s86El('p',
      'Authored payload size is measured by streaming every event this '
      + 'pubkey published. Freeable LMDB disk after a delete is larger '
      + '(page overhead) and is not attributable per author. Gift wraps '
      + 'they received are counted separately — the encrypted bodies are '
      + 'stored under the senders, not this key.',
      'muted'));
    setCommands([
      {
        label: 'fast authored-event count (index, seconds):',
        command: "strfry " + S86_STRFRY_CONFIG_FLAG + " scan --count '" + authorFilter + "'"
      },
      {
        label: 'authored payload size — streams every event (minutes if large):',
        command: "strfry " + S86_STRFRY_CONFIG_FLAG + " scan '" + authorFilter + "' | python3 -c '\n"
          + s86PyStorageByAuthorScript() + "\n'"
      },
      {
        label: 'gift wraps addressed TO this pubkey (count only):',
        command: "strfry " + S86_STRFRY_CONFIG_FLAG + " scan --count '" + gwFilter + "'"
      }
    ]);
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
    } else if (intent.key === 'storage_by_author') {
      renderStorageByAuthor();
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

// Returns {verb, name, nip05, suffix, npub, hex, entries} — never a string,
// so the caller can build <b>/text nodes per the untrusted-string rule
// instead of merging attacker-influenced text into one opaque string.
// `entries` is only set for a 2+-pubkey ban/unban record, where it feeds the
// expand-arrow detail list in place of the (ambiguous, therefore omitted)
// single trailing npub. `hex` is the pubkey behind that single trailing
// npub, so the caller can link it to this relay's own profile page.
function s86RecordLabelParts(record) {
  if (record.type === 'reason') {
    return {
      verb: 'set reason on ' + record.count + ' ban' + (record.count === 1 ? '' : 's')
        + (record.entries ? '' : ' — undo unavailable'),
      name: null, nip05: null, suffix: null, npub: null, hex: null, entries: null
    };
  }
  var verb = record.type === 'ban' ? 'banned' : 'unbanned';
  if (record.entries.length === 1) {
    var e = record.entries[0];
    return { verb: verb, name: e.name || null, nip05: e.nip05 || null, suffix: null, npub: e.npub || e.pubkey, hex: e.pubkey, entries: null };
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
  return { verb: verb + ' ' + record.entries.length + ' pubkeys', name: null, nip05: null, suffix: suffix, npub: null, hex: null, entries: record.entries };
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
// name in <b> when known, nip05 as plain text after it, then the npub,
// linked to this relay's own profile page — same as the single-entry
// record line's trailing npub. This list only ever renders inside
// s86RenderRecords' admin-gated container, so isAdmin is always true here.
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
    li.appendChild(s86NpubLink(e.npub || e.pubkey, e.pubkey, true));
    ul.appendChild(li);
  });
  return ul;
}

// Inline "set reason" row. Used ONLY by profile.html's ban-status editor,
// which passes the pubkey's actual current reason — there the row IS the
// "edit the reason on file" control for that one named pubkey, not a
// control acting on a set the page assembled. (It used to also render
// inside every record line on every page; that leaked a bulk
// public-publishing control onto pages with no checked set for it to act
// on — see the note on s86BuildRecordLine.) One text input and one
// button, disabled while the input is empty — same disable rule as the
// bulk-reason row on bans.html, since an empty reason submitted would
// otherwise silently no-op. Posts /api/reason in 'replace' mode for
// exactly the pubkeys this call concerns, then records a normal 'reason'
// undo entry — reusing the existing mechanism rather than inventing a
// second way to undo a reason change.
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

// parts: {verb, name, nip05, suffix, npub, hex, entries} — see
// s86RecordLabelParts. npub is omitted (no trailing element at all) when
// null; when present it renders via s86NpubLink (isAdmin is always true
// here — this whole component only renders inside s86RenderRecords' own
// isAdmin gate), linking to this relay's profile page. `entries` (2+
// pubkeys) replaces that omitted npub with a third control, a ▸/▾ expand
// arrow revealing a detail list — the one exception to a record line's
// normal two-touch-target rule.
//
// onUndo is optional — a null/undefined onUndo (the reason-record "undo
// unavailable" case, once its snapshot exceeds S86_REASON_UNDO_MAX) omits
// the Undo button entirely rather than rendering one that can't work. A
// truncated undo is worse than none.
//
// A record line carries at most three controls: ↩ (dismiss), Undo, and
// (multi-pubkey records only) the expand arrow — no input, no reason
// field, no third-party control, on any page. A per-line reason-edit row
// used to render here too; it leaked a bulk public-publishing control onto
// every record line on every page, including report.html, which has no
// checked set for it to act on. The bulk-reason control lives exactly
// once, on bans.html, bound to the checked set — see bulk-reason-row
// there — and is not part of this component.
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
    line.appendChild(s86NpubLink(parts.npub, parts.hex, true));
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

// Clear any flash-undo line and cancel its auto-dismiss timer.
function s86ClearFlashUndo(container) {
  if (_s86FlashUndoTimer != null) {
    clearTimeout(_s86FlashUndoTimer);
    _s86FlashUndoTimer = null;
  }
  if (container) {
    container.textContent = '';
  }
}

// One temporary Undo line right after a ban or unban. Disappears after
// S86_FLASH_UNDO_MS, on dismiss (↩), or after a successful Undo. Permanent
// history and undo live on /audit only — never a standing list here.
// callbacks: {onChanged, onError}.
function s86ShowFlashUndo(container, record, callbacks) {
  if (!container || !record) {
    return;
  }
  if (record.type !== 'ban' && record.type !== 'unban') {
    return;
  }
  if (!Array.isArray(record.entries) || record.entries.length === 0) {
    return;
  }
  s86ClearFlashUndo(container);
  if (!record.id) {
    record.id = Date.now() + '-' + Math.random().toString(36).slice(2);
  }
  callbacks = callbacks || {};
  var wrapped = {
    onChanged: function () {
      s86ClearFlashUndo(container);
      if (callbacks.onChanged) {
        callbacks.onChanged();
      }
    },
    onError: callbacks.onError
  };
  // No dismiss-to-history: ↩ just clears the flash. Full undo later is Audit.
  var line = s86BuildRecordLine(
    s86RecordLabelParts(record),
    function (btn) { s86UndoStoredRecord(record, btn, wrapped); },
    function () { s86ClearFlashUndo(container); }
  );
  container.appendChild(line);
  _s86FlashUndoTimer = setTimeout(function () {
    _s86FlashUndoTimer = null;
    if (container) {
      container.textContent = '';
    }
  }, S86_FLASH_UNDO_MS);
}

// Legacy name kept so pages that "refresh records" on load stay valid: that
// refresh must clear any flash, never rebuild a history list.
function s86RenderRecords(container, isAdmin, bannedList, callbacks) {
  s86ClearFlashUndo(container);
}
