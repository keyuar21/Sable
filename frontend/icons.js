/* ─────────────────────────────────────────────────────────────────────────────
   icons.js — one icon family for the whole app.

   The UI used to paint emoji (🍔 🍕 🛵 🏦 ✅ …) as its structural icons. Emoji are
   font-dependent, render differently on every OS, cannot be themed with design
   tokens, and sit at inconsistent optical weights next to the real SVG icons the
   app already had (the fingerprint, the status bar). This file replaces them
   with a single stroked SVG family.

   Usage
     • markup : <svg class="icon" aria-hidden="true"><use href="#ic-mic"/></svg>
     • script : el.innerHTML = iconFor('🎤')  +  ' Confirm with your voice'
                (iconFor() accepts either a key — 'mic' — or the emoji it replaced)

   Style contract: 24×24 grid, 1.7px stroke, round caps/joins, currentColor only,
   so an icon always matches the text colour and size of its container.
   ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  // key -> inner markup. Kept to simple primitives so nothing depends on a
  // half-remembered library path: every shape is reviewable at a glance.
  var PATHS = {
    // ── voice ─────────────────────────────────────────────────────────────────
    mic:
      '<path d="M12 3.2a3.1 3.1 0 0 1 3.1 3.1v5.4a3.1 3.1 0 0 1-6.2 0V6.3A3.1 3.1 0 0 1 12 3.2Z"/>' +
      '<path d="M5.6 11.2a6.4 6.4 0 0 0 12.8 0"/>' +
      '<path d="M12 17.6V21"/><path d="M8.6 21h6.8"/>',
    waveform:
      '<path d="M4 10v4"/><path d="M8 7v10"/><path d="M12 4v16"/>' +
      '<path d="M16 7v10"/><path d="M20 10v4"/>',
    // ── payment / money ───────────────────────────────────────────────────────
    card:
      '<rect x="2.6" y="5.2" width="18.8" height="13.6" rx="2.6"/>' +
      '<path d="M2.6 9.8h18.8"/><path d="M6.2 14.6h3.6"/>',
    sendMoney:
      '<circle cx="12" cy="12" r="8.4"/>' +
      '<path d="M12 8.2v7.6"/><path d="m8.8 12.2 3.2 3.4 3.2-3.4"/>',
    shuffle:
      '<path d="M3.8 6.4h3.4l9.4 11.6h3.6"/>' +
      '<path d="M3.8 18h3.4l2.9-3.6"/>' +
      '<path d="m13.6 7.6 2.9-1.2h3.7"/>' +
      '<path d="m17.8 3.6 2.6 2.6-2.6 2.6"/>' +
      '<path d="m17.8 15.6 2.6 2.6-2.6 2.6"/>',
    bank:
      '<path d="M3.4 9.6 12 4.2l8.6 5.4"/>' +
      '<path d="M5.6 10.4v8.2"/><path d="M9.9 10.4v8.2"/>' +
      '<path d="M14.1 10.4v8.2"/><path d="M18.4 10.4v8.2"/>' +
      '<path d="M3 20.4h18"/>',
    store:
      '<path d="M4.4 9.8V20h15.2V9.8"/>' +
      '<path d="M3 4.6h18l-.9 4.1a2.7 2.7 0 0 1-5.3 0 2.7 2.7 0 0 1-5.2 0 2.7 2.7 0 0 1-5.3 0Z"/>' +
      '<path d="M9.6 20v-4.8h4.8V20"/>',
    // ── merchant categories ───────────────────────────────────────────────────
    burger:
      '<path d="M4.4 9.6c0-3.2 3.5-5.4 7.6-5.4s7.6 2.2 7.6 5.4"/>' +
      '<path d="M3.6 13h16.8"/>' +
      '<rect x="3.6" y="15.8" width="16.8" height="4.6" rx="2.3"/>',
    pizza:
      '<path d="M12 2.8 3.2 20.2a18.6 18.6 0 0 0 17.6 0Z"/>' +
      '<circle cx="10.4" cy="13.4" r="1.1"/><circle cx="13.8" cy="16.6" r="1.1"/>' +
      '<circle cx="13.6" cy="10.6" r="1.1"/>',
    scooter:
      '<circle cx="5.4" cy="17.6" r="2.8"/><circle cx="18.6" cy="17.6" r="2.8"/>' +
      '<path d="M8.4 17.6h7.2l-1.9-8.8h-2.6"/>' +
      '<path d="M15.6 8.8h2.6l1.4 5.6"/><path d="M11.1 8.8H8.2"/>',
    cart:
      '<circle cx="9.4" cy="20" r="1.4"/><circle cx="17.6" cy="20" r="1.4"/>' +
      '<path d="M2.6 3.6h2.3l2.3 11.1a1.9 1.9 0 0 0 1.9 1.5h8.7a1.9 1.9 0 0 0 1.9-1.5L21.4 7H6.2"/>',
    package:
      '<path d="M12 3 3.6 7.5v9L12 21l8.4-4.5v-9Z"/>' +
      '<path d="m3.6 7.5 8.4 4.5 8.4-4.5"/>' +
      '<path d="M12 12v9"/>',
    fuel:
      '<path d="M5 21V5.2A2.2 2.2 0 0 1 7.2 3h4.6A2.2 2.2 0 0 1 14 5.2V21"/>' +
      '<path d="M4 21h11"/><path d="M5 10.4h9"/>' +
      '<path d="M14 8.2h2.3a2 2 0 0 1 2 2v6.6a1.6 1.6 0 0 0 3.2 0v-5.4l-2-3.2"/>',
    // ── utilities ─────────────────────────────────────────────────────────────
    camera:
      '<path d="M3.2 8.8A2.6 2.6 0 0 1 5.8 6.2h1.3a1 1 0 0 0 .85-.47l.9-1.4a1 1 0 0 1 .85-.47h4.6a1 1 0 0 1 .85.47l.9 1.4a1 1 0 0 0 .85.47h1.3a2.6 2.6 0 0 1 2.6 2.6v7.6a2.6 2.6 0 0 1-2.6 2.6H5.8a2.6 2.6 0 0 1-2.6-2.6Z"/>' +
      '<circle cx="12" cy="12.6" r="3.2"/>',
    receipt:
      '<path d="M6 3.2h12v17.6l-3-1.6-3 1.6-3-1.6-3 1.6Z"/>' +
      '<path d="M9.4 8.2h5.2"/><path d="M9.4 12h5.2"/>',
    smartphone:
      '<rect x="6.6" y="2.6" width="10.8" height="18.8" rx="2.6"/>' +
      '<path d="M10.8 18.4h2.4"/>',
    bulb:
      '<path d="M9.4 18.2h5.2"/><path d="M10.4 21h3.2"/>' +
      '<path d="M12 3.2a5.8 5.8 0 0 0-3.4 10.5c.6.45.95 1.15.95 1.9v.6h4.9v-.6c0-.75.35-1.45.95-1.9A5.8 5.8 0 0 0 12 3.2Z"/>',
    signal:
      '<path d="M4 20v-3.6"/><path d="M9.3 20v-7.2"/>' +
      '<path d="M14.7 20V9.6"/><path d="M20 20V4.6"/>',
    radio:
      '<circle cx="12" cy="12" r="2.1"/>' +
      '<path d="M7.9 16.1a5.8 5.8 0 0 1 0-8.2"/>' +
      '<path d="M16.1 7.9a5.8 5.8 0 0 1 0 8.2"/>' +
      '<path d="M5 19a9.9 9.9 0 0 1 0-14"/>' +
      '<path d="M19 4.9a9.9 9.9 0 0 1 0 14.1"/>',
    refresh:
      '<path d="M20.4 12a8.4 8.4 0 1 1-2.5-6"/>' +
      '<path d="M20.4 4.4v5.2h-5.2"/>',
    chevronRight:
      '<path d="m9.8 5.8 6.2 6.2-6.2 6.2"/>',
    backspace:
      '<path d="M9.4 5.4h9.2a2.3 2.3 0 0 1 2.3 2.3v8.6a2.3 2.3 0 0 1-2.3 2.3H9.4a2.3 2.3 0 0 1-1.75-.8L3.4 12.9a1.45 1.45 0 0 1 0-1.8l4.25-4.9A2.3 2.3 0 0 1 9.4 5.4Z"/>' +
      '<path d="m12.7 9.7 4.8 4.6"/><path d="m17.5 9.7-4.8 4.6"/>',
    power:
      '<path d="M12 3.6v7.8"/>' +
      '<path d="M7.4 6.9a6.6 6.6 0 1 0 9.2 0"/>',
    trash:
      '<path d="M4 6.8h16"/><path d="M9.2 6.8V4.9a1.5 1.5 0 0 1 1.5-1.5h2.6a1.5 1.5 0 0 1 1.5 1.5v1.9"/>' +
      '<path d="m6.2 6.8.95 12.9a2 2 0 0 0 2 1.85h5.7a2 2 0 0 0 2-1.85L17.8 6.8"/>' +
      '<path d="M10.4 10.8v6.4"/><path d="M13.6 10.8v6.4"/>',
    hourglass:
      '<path d="M7 3h10"/><path d="M7 21h10"/>' +
      '<path d="M8.2 3v3.4c0 2.4 3.8 3.5 3.8 5.6s-3.8 3.2-3.8 5.6V21"/>' +
      '<path d="M15.8 3v3.4c0 2.4-3.8 3.5-3.8 5.6s3.8 3.2 3.8 5.6V21"/>',
    clock:
      '<circle cx="12" cy="13" r="7.4"/>' +
      '<path d="M12 9.4V13l2.6 2.1"/>' +
      '<path d="m5.4 4.6 2.4-2"/><path d="m18.6 4.6-2.4-2"/>',
    inbox:
      '<path d="m3.6 13.6 2.5-8h11.8l2.5 8"/>' +
      '<path d="M3.6 13.6h4.7l1 2.5h5.4l1-2.5h4.7v4.2a2.2 2.2 0 0 1-2.2 2.2H5.8a2.2 2.2 0 0 1-2.2-2.2Z"/>',
    bolt:
      '<path d="M13.4 2.6 5.6 13.4h5.1L9.9 21.4l7.8-10.8h-5.1Z"/>',
    // ── status ────────────────────────────────────────────────────────────────
    check: '<path d="m4.8 12.6 4.7 5.2L19.2 6.6"/>',
    checkCircle:
      '<circle cx="12" cy="12" r="8.6"/><path d="m8.2 12.4 2.7 3 5-6"/>',
    xCircle:
      '<circle cx="12" cy="12" r="8.6"/><path d="m9.2 9.2 5.6 5.6"/><path d="m14.8 9.2-5.6 5.6"/>',
    x: '<path d="m6.2 6.2 11.6 11.6"/><path d="m17.8 6.2-11.6 11.6"/>',
    alert:
      '<path d="M12 4.2 2.6 20.4h18.8Z"/><path d="M12 10v4.2"/><path d="M12 17.4h.01"/>',
    shield:
      '<path d="M12 3.2 5 6v5.6c0 4.2 2.9 7.6 7 9.2 4.1-1.6 7-5 7-9.2V6Z"/>' +
      '<path d="m9.2 12.2 2.2 2.4 3.6-4.4"/>',
    // ── auth ──────────────────────────────────────────────────────────────────
    lock:
      '<rect x="4.6" y="10" width="14.8" height="10.4" rx="2.6"/>' +
      '<path d="M8.2 10V7.4a3.8 3.8 0 0 1 7.6 0V10"/>',
    lockKey:
      '<rect x="4.6" y="10" width="14.8" height="10.4" rx="2.6"/>' +
      '<path d="M8.2 10V7.4a3.8 3.8 0 0 1 7.6 0V10"/>' +
      '<circle cx="12" cy="14.6" r="1.4"/><path d="M12 16v1.9"/>',
    tap:
      '<path d="M9.6 11.6V5.6a1.8 1.8 0 0 1 3.6 0v6"/>' +
      '<path d="M13.2 11.6V9.9a1.6 1.6 0 0 1 3.2 0v1.7"/>' +
      '<path d="M16.4 11.6v-1a1.6 1.6 0 0 1 3.2 0v5.5a5.6 5.6 0 0 1-5.6 5.6h-1.5a5 5 0 0 1-3.7-1.6l-3.9-4.4a1.7 1.7 0 0 1 2.4-2.4l1.7 1.6"/>',
    undo:
      '<path d="M9.2 13.8 4.6 9.2l4.6-4.6"/><path d="M4.6 9.2h9.2a5.4 5.4 0 0 1 0 10.8h-2.6"/>',
    party:
      '<path d="M3.4 20.6 9 6.2l8.8 8.8Z"/>' +
      '<path d="M14.4 3.4h.01"/><path d="M18.6 6.6h.01"/><path d="M20.4 11.2h.01"/>' +
      '<path d="M16.4 3.6v1.8"/><path d="M19.8 8.4h1.8"/>'
  };

  // The emoji this file replaces. Keeping the mapping means existing data
  // (data-icon="🍔", icon: '🛵', getMerchantIcon()) keeps working untouched.
  var FROM_EMOJI = {
    '🎤': 'mic', '🎙': 'mic', '🎙️': 'mic',
    '💳': 'card', '💸': 'sendMoney', '🔀': 'shuffle',
    '🏦': 'bank', '🏪': 'store',
    '🍔': 'burger', '🍕': 'pizza', '🛵': 'scooter',
    '🛒': 'cart', '📦': 'package', '⛽': 'fuel',
    '📷': 'camera', '📋': 'receipt', '📱': 'smartphone', '💡': 'bulb',
    '📶': 'signal', '📡': 'radio', '🔄': 'refresh', '🗑': 'trash', '🗑️': 'trash',
    '⌛': 'hourglass', '⏳': 'hourglass', '⏰': 'clock', '📭': 'inbox', '📬': 'inbox',
    '⚡': 'bolt',
    '✅': 'checkCircle', '❌': 'xCircle', '⚠': 'alert', '⚠️': 'alert',
    '✓': 'check', '✔': 'check', '✔️': 'check',
    '🔒': 'lock', '🔐': 'lockKey', '🔓': 'lock',
    '👆': 'tap', '↩': 'undo', '↩️': 'undo', '🎉': 'party',
    '✕': 'x', '✖': 'x', '✖️': 'x', '❌️': 'xCircle',
    // not emoji, but platform-font glyphs used as controls
    '⏻': 'power', '⌫': 'backspace', '›': 'chevronRight'
  };

  var NAME = 'icon';   // CSS hook: .icon

  function keyFor(value) {
    if (!value) return null;
    var v = String(value).trim();
    if (PATHS[v]) return v;                 // already a key
    if (FROM_EMOJI[v]) return FROM_EMOJI[v];
    // "🎤 Record sample" style — the caller usually splits this, but be lenient.
    for (var i = 0; i < v.length; i++) {
      if (FROM_EMOJI[v[i]]) return FROM_EMOJI[v[i]];
    }
    return null;
  }

  function svgMarkup(key, extraClass) {
    return '<svg class="' + NAME + (extraClass ? ' ' + extraClass : '') +
           '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
           '<use href="#ic-' + key + '"></use></svg>';
  }

  /**
   * iconFor(value[, extraClass]) -> HTML string.
   * Accepts a sprite key ('mic'), an emoji ('🎤'), or anything else; unknown
   * values are returned unchanged and HTML-escaped, so callers can pass user
   * text through the same path without injecting markup.
   */
  function iconFor(value, extraClass) {
    var key = keyFor(value);
    if (!key) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    return svgMarkup(key, extraClass);
  }

  function buildSprite() {
    if (document.getElementById('ic-sprite')) return;
    var parts = ['<svg id="ic-sprite" class="icon-sprite" aria-hidden="true" ' +
      'xmlns="http://www.w3.org/2000/svg" style="position:absolute;width:0;height:0;overflow:hidden">'];
    Object.keys(PATHS).forEach(function (key) {
      parts.push('<symbol id="ic-' + key + '" viewBox="0 0 24 24" fill="none" ' +
        'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" ' +
        'stroke-linejoin="round">' + PATHS[key] + '</symbol>');
    });
    parts.push('</svg>');
    var host = document.createElement('div');
    host.innerHTML = parts.join('');
    document.body.insertBefore(host.firstChild, document.body.firstChild);
  }

  window.iconFor = iconFor;
  window.iconKey = keyFor;
  window.ICON_PATHS = PATHS;

  // This script is loaded at the end of <body>, so the sprite can go in
  // immediately — that way every <use href="#ic-…"> in the markup, and every
  // iconFor() call from app.js's DOMContentLoaded handler, already resolves.
  if (document.body) {
    buildSprite();
  } else {
    document.addEventListener('DOMContentLoaded', buildSprite);
  }
})();
