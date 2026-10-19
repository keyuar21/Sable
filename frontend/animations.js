/* ─────────────────────────────────────────────────────────────────────────────
   Sable — motion effects
   ─────────────────────────────────────────────────────────────────────────────
   The CSS layer already owns screen transitions, list stagger, press brighten,
   outcome pops, the pipeline flow and the sheet spring. This file adds only the
   motion CSS cannot express on its own:

     1. money that counts up instead of snapping
     2. a ripple that originates where the finger actually landed
     3. the balance card tilting toward the pointer

   Three rules hold throughout:
     - everything is transform / opacity / text only, so it stays on the
       compositor and never triggers layout
     - every effect is skipped when the OS asks for reduced motion, and the
       final state is rendered immediately instead
     - nothing here is required for the app to work; if this file fails to
       load, the UI is simply static
   ─────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  function calm() { return reduce.matches; }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }   // cubic out

  /* ── 1. Counting money ─────────────────────────────────────────────────────
     Amounts are written as plain text ("50,000", "1,250.50"). Rather than
     touch app.js at every call site, we watch the nodes that display money and
     tween between the old and new figure. Currency symbols and separators are
     preserved by only rewriting the digit runs. */
  var MONEY = [
    '#balance-value',
    '#confirm-amount',
    '#result-amount'
  ];

  function parseMoney(text) {
    var cleaned = String(text).replace(/[^0-9.]/g, '');
    if (!cleaned) return null;
    var n = parseFloat(cleaned);
    return isNaN(n) ? null : n;
  }

  function formatLike(sample, n) {
    var decimals = /\.\d\d$/.test(String(sample)) ? 2 : 0;
    var body = decimals
      ? n.toFixed(2)
      : Math.round(n).toString();
    var parts = body.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
  }

  /* The count-up is the one effect here that writes to the DOM it also watches,
     which is a feedback loop waiting to happen. Three things stop it:

       1. `shown` tracks the value we last RENDERED, re-parsed from the element
          after every write. Comparing against the float we computed would never
          match once formatLike() rounds it ("1,235" is not 1234.56), and the
          observer would read its own frame as an external change.
       2. `runId` supersedes an in-flight count, so exactly one loop ever writes.
       3. A circuit breaker disconnects the observer if it ever sees a storm of
          mutations, because a frozen main thread is far worse than a balance
          that does not count. */
  function watchMoney(el) {
    var shown = null;                 // null = we have never drawn this figure
    var firstRun = true;
    var runId = 0;
    var events = 0;
    var windowStart = 0;

    function animate(from, to, sample, id) {
      function put(v) {
        el.textContent = formatLike(sample, v);
        shown = parseMoney(el.textContent);   // what actually rendered
      }
      if (calm()) { put(to); return; }
      var start = performance.now();
      var dur = 720;
      (function step(now) {
        if (id !== runId) return;             // superseded by a newer figure
        var t = Math.min(1, (now - start) / dur);
        put(from + (to - from) * easeOut(t));
        if (t < 1) requestAnimationFrame(step);
      })(start);
    }

    var observer = new MutationObserver(function () {
      var external = parseMoney(el.textContent);
      if (external === null || external === shown) return;   // our own frame

      var now = performance.now();
      if (now - windowStart > 2000) { windowStart = now; events = 0; }
      if (++events > 40) { observer.disconnect(); return; }

      var from = shown;
      var sample = el.textContent;
      var id = ++runId;
      // The opening balance counts up from zero; later changes tween from the
      // previous figure, which is what makes a top-up feel like it landed.
      animate(firstRun ? 0 : (from === null ? to : from), external, sample, id);
      firstRun = false;
    });

    observer.observe(el, { childList: true, characterData: true, subtree: true });
  }

  function initMoney() {
    MONEY.forEach(function (sel) {
      var el = document.querySelector(sel);
      if (el) watchMoney(el);
    });
  }

  /* ── 2. Rip → ripple ───────────────────────────────────────────────────────
     Pointer-positioned so the feedback originates under the finger. One
     delegated listener covers every control, including ones rendered later. */
  var RIPPLE_TARGETS = '.action-btn, .primary-btn, .pay-now-btn, .nav-item, .merchant-chip, .result-btn, .chip';

  document.addEventListener('pointerdown', function (e) {
    if (calm() || e.button !== 0) return;
    var host = e.target.closest && e.target.closest(RIPPLE_TARGETS);
    if (!host) return;
    var rect = host.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    var size = Math.max(rect.width, rect.height) * 2.1;
    var span = document.createElement('span');
    span.className = 'ripple';
    span.style.width = span.style.height = size + 'px';
    span.style.left = (e.clientX - rect.left - size / 2) + 'px';
    span.style.top = (e.clientY - rect.top - size / 2) + 'px';
    host.appendChild(span);
    span.addEventListener('animationend', function () { span.remove(); });
    // belt and braces: if the animation never fires (tab hidden mid-press)
    // the node still cannot pile up
    setTimeout(function () { span.remove(); }, 900);
  }, { passive: true });

  /* ── 3. The balance card ───────────────────────────────────────────────────
     The one element per screen allowed to react to the pointer. Kept under 3°
     so it reads as depth rather than motion, and only on devices with a real
     pointer. */
  function initTilt() {
    var card = document.querySelector('.balance-card');
    if (!card || !finePointer.matches) return;
    var MAX = 3.2;

    card.addEventListener('pointermove', function (e) {
      if (calm()) return;
      var r = card.getBoundingClientRect();
      var px = (e.clientX - r.left) / r.width - 0.5;
      var py = (e.clientY - r.top) / r.height - 0.5;
      card.style.transition = 'none';
      card.style.transform =
        'perspective(900px) rotateY(' + (px * MAX * 2).toFixed(2) + 'deg) ' +
        'rotateX(' + (-py * MAX * 2).toFixed(2) + 'deg) translateY(-2px)';
    });

    card.addEventListener('pointerleave', function () {
      card.style.transition = '';
      card.style.transform = '';
    });
  }

  /* ── sign-in panel: pointer light ───────────────────────────────────────
     The left-hand brand panel carries a soft light that tracks the pointer.
     The panel is pointer-events:none itself, so listen on the document and
     only publish coordinates when the pointer is over the panel's half of
     the canvas. CSS owns all the rendering; this only writes two vars. */
  function initSideGlow() {
    var side = document.getElementById('sable-side');
    if (!side || calm()) return;
    var raf = 0, px = 0, py = 0;
    function apply() {
      raf = 0;
      side.style.setProperty('--gx', (px * 100).toFixed(2) + '%');
      side.style.setProperty('--gy', (py * 100).toFixed(2) + '%');
    }
    document.addEventListener('pointermove', function (e) {
      var r = side.getBoundingClientRect();
      if (e.clientX > r.right) return;                 // over the phone form
      if (!raf && e.clientX >= r.left) {
        px = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
        py = Math.min(1, Math.max(0, e.clientY / Math.max(1, window.innerHeight)));
        raf = requestAnimationFrame(apply);
      }
    });
  }

  /* ── boot ──────────────────────────────────────────────────────────────── */
  function init() {
    initMoney();
    initTilt();
    initSideGlow();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
