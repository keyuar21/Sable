/* ─────────────────────────────────────────────────────────────────────────────
   Voice UPI Simulator — Main Application Logic
   ───────────────────────────────────────────────────────────────────────────── */

'use strict';

// ── API base URLs ─────────────────────────────────────────────────────────────
// Read from config.js, which is mounted from the Helm ConfigMap in a container
// and ships with localhost defaults for local development. Falling back per key
// means a partial config still works.
const RUNTIME = (typeof window !== 'undefined' && window.__UPI_CONFIG__) || {};
const API = {
  psp:       RUNTIME.psp       || 'http://localhost:5001',
  switch:    RUNTIME.switch    || 'http://localhost:5002',
  payerBank: RUNTIME.payerBank || 'http://localhost:5003',
  payeeBank: RUNTIME.payeeBank || 'http://localhost:5004',
  auth:      RUNTIME.auth      || 'http://localhost:5005',
  recharge:  RUNTIME.recharge  || 'http://localhost:5007',
};

// ── App state ─────────────────────────────────────────────────────────────────
const state = {
  user: { vpa: 'you@mockbank', name: 'Keyur Arbhelonde', balance: 50000 },
  loggedIn: false,
  voiceEnrolled: false,
  payment: { payeeVpa: null, payeeName: null, payeeIcon: null, amount: null, txnId: null },
  pin: '',
  pollInterval: null,
  settlementInterval: null,
  settlementCountdown: 30,
  localTxns: [],           // local mirror of transactions for history
  recognition: null,
  voiceEnroll: { step: 0, samples: [], recording: false },
  voiceConfirmBusy: false,
  pendingRecharge: null,   // set while a recharge payment is in flight
};

// Start periodic tasks after a successful login
function startAfterLogin() {
  refreshBalance();

  // Settlement countdown every 2s
  updateSettlementCountdown();
  setInterval(async () => {
    await updateSettlementCountdown();
    if (document.getElementById('screen-settlement').classList.contains('active')) {
      loadSettlements();
    }
  }, 2_000);

  console.log('%c🎤 Voice UPI Simulator', 'color:#4D8EFF;font-size:18px;font-weight:700');
  console.log('%cAll API endpoints:', 'color:#666');
  console.table({
    PSP:       `${API.psp}/health`,
    Switch:    `${API.switch}/health`,
    PayerBank: `${API.payerBank}/health`,
    PayeeBank: `${API.payeeBank}/health`,
  });
}

// Per-transaction cap, mirrored from the PSP's own limit.
const MAX_TXN_AMOUNT = 100000;

// ── Merchant directory ────────────────────────────────────────────────────────
const MERCHANTS = {
  'sharma store': { vpa: 'sharmastore@mockbank2', name: 'Sharma Store', icon: '🛒' },
  'sharma':       { vpa: 'sharmastore@mockbank2', name: 'Sharma Store', icon: '🛒' },
  'blinkit':      { vpa: 'blinkit@mockbank2',     name: 'Blinkit',      icon: '🛵' },
  'swiggy':       { vpa: 'swiggy@mockbank2',      name: 'Swiggy',       icon: '🍔' },
  'zomato':       { vpa: 'zomato@mockbank2',      name: 'Zomato',       icon: '🍕' },
  'amazon':       { vpa: 'amazon@mockbank2',      name: 'Amazon',       icon: '📦' },
  'petrol':       { vpa: 'petrolpump@mockbank2',  name: 'HP Petrol Pump', icon: '⛽' },
  'hp':           { vpa: 'petrolpump@mockbank2',  name: 'HP Petrol Pump', icon: '⛽' },
  'petrolpump':   { vpa: 'petrolpump@mockbank2',  name: 'HP Petrol Pump', icon: '⛽' },
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ── Device binding ────────────────────────────────────────────────────────────
// This browser stands in for the handset. Only accounts that have successfully
// authenticated here are remembered, and only those are offered at sign-in —
// so one user's device never reveals another user's VPA.
const DEVICE_ACCOUNTS_KEY = 'upiSim.deviceAccounts.v1';

/** 10 local digits from whatever was typed, or null if it is not a mobile. */
function normaliseMobile(raw) {
  const d = String(raw || '').replace(/\D/g, '');
  if (d.length === 10) return d;
  if (d.length === 12 && d.startsWith('91')) return d.slice(2);
  if (d.length === 11 && d.startsWith('0')) return d.slice(1);
  return null;
}

/** The number this device was set up with, if we know it. */
function myMobile() {
  const acct = getDeviceAccounts().find(a => a.vpa === state.user.vpa) || getDeviceAccounts()[0];
  // Some VPAs are the mobile number itself (9820098200@ybl), so fall back to that.
  return acct?.mobile || normaliseMobile((state.user.vpa || '').split('@')[0]);
}

function getDeviceAccounts() {
  try {
    const raw = JSON.parse(localStorage.getItem(DEVICE_ACCOUNTS_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter(a => a && typeof a.vpa === 'string') : [];
  } catch {
    return [];
  }
}

function rememberDeviceAccount(vpa, holderName, mobile) {
  if (!vpa) return;
  const existing = getDeviceAccounts().find(a => a.vpa === vpa);
  const accounts = getDeviceAccounts().filter(a => a.vpa !== vpa);
  accounts.unshift({
    vpa,
    holder_name: holderName || vpa,
    // The number given at bank-linking time. Kept so recharge can offer
    // "my number" without asking again. An existing value is not overwritten
    // by a later sign-in that has no number to hand.
    mobile: normaliseMobile(mobile) || existing?.mobile || null,
  });
  try {
    localStorage.setItem(DEVICE_ACCOUNTS_KEY, JSON.stringify(accounts));
  } catch (e) {
    console.warn('Could not persist device account binding:', e);
  }
}

function forgetDeviceAccount(vpa) {
  try {
    localStorage.setItem(
      DEVICE_ACCOUNTS_KEY,
      JSON.stringify(getDeviceAccounts().filter(a => a.vpa !== vpa))
    );
  } catch { /* storage unavailable */ }
}

// ── Screen navigation ─────────────────────────────────────────────────────────

// Screens reachable before anyone has signed in.
const PUBLIC_SCREENS = new Set(['login', 'onboarding']);
// Reachable once the PIN/biometric check passed but voice ID is not yet set up.
const PENDING_SCREENS = new Set(['voice-enroll']);

// The session is only fully unlocked after BOTH gates: credential + voice ID.
function isUnlocked() {
  return state.loggedIn && state.voiceEnrolled;
}

// Nav chrome is hidden *and* inert until the session is unlocked, so the
// bottom nav / sidebar cannot be used to walk straight into the app.
function syncAuthChrome() {
  const unlocked = isUnlocked();
  document.querySelector('.bottom-nav')?.classList.toggle('locked', !unlocked);
  document.getElementById('header-avatar')?.classList.toggle('hidden', !state.loggedIn);
}

function showScreen(name) {
  // Route guard: never render a protected screen for an unauthenticated session.
  if (!PUBLIC_SCREENS.has(name)) {
    if (!state.loggedIn) {
      name = 'login';
    } else if (!state.voiceEnrolled && !PENDING_SCREENS.has(name)) {
      name = 'voice-enroll';
    }
  }

  // Leaving the scanner must release the camera, or the indicator light stays
  // on and the stream keeps running in the background.
  if (name !== 'qr' && typeof stopQrScan === 'function') stopQrScan();

  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const screen = document.getElementById(`screen-${name}`);
  if (screen) screen.classList.add('active');

  // Update bottom nav active state
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const navMap = { home: 'nav-home', history: 'nav-history', settlement: 'nav-settlement', voice: 'nav-voice' };
  const navId = navMap[name];
  if (navId) document.getElementById(navId)?.classList.add('active');

  syncAuthChrome();
}

// Drop the session and return to the sign-in screen.
function signOut() {
  state.loggedIn = false;
  state.voiceEnrolled = false;
  state.localTxns = [];
  showScreen('login');
}

// ── Live clock ────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  const h = now.getHours().toString().padStart(2, '0');
  const m = now.getMinutes().toString().padStart(2, '0');
  document.getElementById('status-time').textContent = `${h}:${m}`;
}
updateClock();
setInterval(updateClock, 10_000);

// ── Balance fetch ─────────────────────────────────────────────────────────────
async function refreshBalance(showSpin = false) {
  const btn = document.getElementById('btn-refresh-balance');
  if (showSpin) btn.classList.add('spinning');

  try {
    const res = await fetch(`${API.payerBank}/balance/${state.user.vpa}`);
    if (res.ok) {
      const data = await res.json();
      state.user.balance = data.balance;
      document.getElementById('balance-value').textContent = formatAmount(data.balance);
    }
  } catch { /* services may not be up yet */ }

  setTimeout(() => btn.classList.remove('spinning'), 600);
}

// ── Intent parsing (rule-based) ───────────────────────────────────────────────
function parseIntent(text) {
  if (!text) return null;
  text = text.toLowerCase().trim();

  // Amount: supports "500", "1,000", "1.5k", "2 lakh".
  //
  // The comma-grouped alternative MUST require at least one group (+ not *).
  // With * it matches a bare three-digit prefix and, because alternation takes
  // the first branch that matches, "20000" was read as "200" and "2500" as
  // "250" — every amount over three digits was silently truncated.
  let amount = null;
  const amtMatch = text.match(/(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:k|thousand|lakh|lac|rupees?|rs\.?)?/);
  if (amtMatch) {
    const raw = amtMatch[1].replace(/,/g, '');
    amount = parseFloat(raw);
    if (/[\d.]\s*(k\b|thousand)/.test(text)) amount *= 1000;
    if (/[\d.]\s*(lakh|lac)\b/.test(text)) amount *= 100000;
  }

  if (!amount || amount <= 0) return null;
  // Over the per-transaction cap: report it rather than failing as "not
  // understood", so the user learns why instead of repeating themselves.
  if (amount > MAX_TXN_AMOUNT) {
    return { error: 'limit', amount: Math.round(amount * 100) / 100 };
  }

  // Merchant matching
  let merchant = null;
  for (const [keyword, info] of Object.entries(MERCHANTS)) {
    if (text.includes(keyword)) { merchant = info; break; }
  }

  if (!merchant) return null;

  return { amount: Math.round(amount * 100) / 100, merchant };
}

// ── Voice input ───────────────────────────────────────────────────────────────
function startVoiceInput() {
  if (!isUnlocked()) return showScreen('login');
  showScreen('voice');
  const waveform     = document.getElementById('waveform');
  const micBtn       = document.getElementById('voice-mic-btn');
  const statusText   = document.getElementById('voice-status-text');
  const transcript   = document.getElementById('voice-transcript');
  const hint         = document.getElementById('voice-hint');

  waveform.classList.remove('idle');
  micBtn.classList.remove('idle');
  micBtn.classList.add('listening');
  statusText.textContent = 'Listening…';
  hint.textContent = 'Try: "Pay 500 to Swiggy" or "Send 250 to Zomato"';
  transcript.textContent = '…';

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  if (!SpeechRecognition) {
    waveform.classList.add('idle');
    micBtn.classList.remove('listening');
    micBtn.classList.add('idle');
    statusText.textContent = 'Voice not supported';
    hint.innerHTML = 'Web Speech API not available in this browser.<br>Use the text box below or try Chrome/Edge.';
    document.getElementById('voice-text-input').focus();
    return;
  }

  if (state.recognition) { try { state.recognition.stop(); } catch {} }

  const rec = new SpeechRecognition();
  rec.lang = 'en-IN';
  rec.continuous = false;
  rec.interimResults = true;
  state.recognition = rec;

  rec.onresult = (event) => {
    const text = Array.from(event.results).map(r => r[0].transcript).join('');
    transcript.textContent = `"${text}"`;

    if (event.results[event.results.length - 1].isFinal) {
      const intent = parseIntent(text);
      if (intent && !intent.error) {
        stopVoiceInput();
        showConfirm(intent.merchant, intent.amount);
      } else if (intent && intent.error === 'limit') {
        statusText.textContent = 'Amount too high';
        hint.textContent = `₹${formatAmount(intent.amount)} is over the ₹${formatAmount(MAX_TXN_AMOUNT)} per-payment limit.`;
        waveform.classList.add('idle');
        micBtn.classList.remove('listening');
        micBtn.classList.add('idle');
      }
    }
  };

  rec.onerror = (e) => {
    console.warn('Speech error:', e.error);
    statusText.textContent = `Mic error: ${e.error}`;
    waveform.classList.add('idle');
    micBtn.classList.remove('listening');
    micBtn.classList.add('idle');
  };

  rec.onend = () => {
    waveform.classList.add('idle');
    micBtn.classList.remove('listening');
  };

  rec.start();
}

function stopVoiceInput() {
  if (state.recognition) { try { state.recognition.stop(); } catch {} state.recognition = null; }
  document.getElementById('waveform').classList.add('idle');
  document.getElementById('voice-mic-btn').classList.remove('listening');
}

// ── Text input fallback ───────────────────────────────────────────────────────
function handleTextInput() {
  const input = document.getElementById('voice-text-input');
  const text  = input.value.trim();
  if (!text) return;

  document.getElementById('voice-transcript').textContent = `"${text}"`;
  const intent = parseIntent(text);

  if (intent && !intent.error) {
    input.value = '';
    stopVoiceInput();
    showConfirm(intent.merchant, intent.amount);
  } else if (intent && intent.error === 'limit') {
    document.getElementById('voice-status-text').textContent = 'Amount too high';
    document.getElementById('voice-hint').textContent =
      `₹${formatAmount(intent.amount)} is over the ₹${formatAmount(MAX_TXN_AMOUNT)} per-payment limit.`;
    input.style.borderColor = 'var(--red)';
    setTimeout(() => { input.style.borderColor = ''; }, 1500);
  } else {
    document.getElementById('voice-status-text').textContent = 'Could not understand';
    document.getElementById('voice-hint').textContent = 'Try: "Pay 500 to Swiggy"';
    input.style.borderColor = 'var(--red)';
    setTimeout(() => { input.style.borderColor = ''; }, 1500);
  }
}

// ── Confirm screen + AI spoken prompt ─────────────────────────────────────────
function showConfirm(merchant, amount) {
  state.payment.payeeVpa   = merchant.vpa;
  state.payment.payeeName  = merchant.name;
  state.payment.payeeIcon  = merchant.icon;
  state.payment.amount     = amount;

  document.getElementById('confirm-merchant-icon').textContent = merchant.icon;
  document.getElementById('confirm-merchant-name').textContent = merchant.name;
  document.getElementById('confirm-merchant-vpa').textContent  = merchant.vpa;
  document.getElementById('confirm-amount').textContent        = formatAmount(amount);
  document.getElementById('confirm-from').textContent          = state.user.vpa;
  document.getElementById('confirm-to').textContent            = merchant.vpa;

  const aiEl = document.getElementById('voice-confirm-ai');
  if (aiEl) aiEl.textContent = `AI: “Payment of ₹${formatAmount(amount)} to ${merchant.name}. Want to proceed?”`;
  const tr = document.getElementById('voice-confirm-transcript');
  if (tr) tr.textContent = '';
  document.getElementById('confirm-waveform')?.classList.add('idle');
  const hint = document.getElementById('voice-confirm-hint');
  if (hint) {
    hint.innerHTML = 'Tap below — AI will ask you, then you speak a <em>fresh random code</em> in your live voice';
  }

  const btn = document.getElementById('btn-pay-now');
  if (btn) {
    btn.disabled = false;
    btn.textContent = '🎤 Confirm with your voice';
  }

  showScreen('confirm');

  if (typeof speakPrompt === 'function') {
    speakPrompt(
      `Payment of ${formatAmount(amount)} rupees to ${merchant.name}. Tap confirm with your voice when ready.`
    );
  }
}

/** Voice authorises the payment: one-time code + live speaker match. The
    spoken code is a random nonce — the UPI PIN is never spoken. */
async function authorizePaymentWithVoice() {
  if (state.voiceConfirmBusy) return;
  if (!state.voiceEnrolled) {
    startVoiceEnrollment();
    return;
  }

  state.voiceConfirmBusy = true;
  stopSpeaking();

  const btn = document.getElementById('btn-pay-now');
  const wave = document.getElementById('confirm-waveform');
  const aiEl = document.getElementById('voice-confirm-ai');
  const trEl = document.getElementById('voice-confirm-transcript');
  const hint = document.getElementById('voice-confirm-hint');

  try {
    const challenge = await requestVoiceChallenge(state.user.vpa, 'pay');
    const codeSpoken = challenge.digits.split('').join(' ');

    if (btn) { btn.disabled = true; btn.textContent = 'Listening…'; }
    wave?.classList.remove('idle');
    if (aiEl) {
      aiEl.textContent =
        `AI: Pay ₹${formatAmount(state.payment.amount)} to ${state.payment.payeeName}? ` +
        `Say “Yes, make payment. Code ${codeSpoken}”`;
    }
    if (hint) {
      hint.innerHTML = `Say <em>“Yes, make payment. Code ${codeSpoken}”</em> — live voice only (recordings fail)`;
    }
    if (trEl) trEl.textContent = '…';

    await speakPrompt(
      `Payment of ${formatAmount(state.payment.amount)} rupees to ${state.payment.payeeName}. ` +
      `Say yes make payment, then code ${codeSpoken}`
    );

    window.__onVoiceTranscript = (t) => { if (trEl) trEl.textContent = `"${t}"`; };

    const { blob, transcript } = await recordVoiceSample(5000, { withTranscript: true });
    if (trEl && transcript) trEl.textContent = `"${transcript}"`;

    if (transcript && isNegativeSpeech(transcript)) {
      wave?.classList.add('idle');
      showFailed('Payment cancelled by voice command', null, 'auth');
      return;
    }

    if (btn) btn.textContent = 'Anti-spoof + Voice ID…';
    if (hint) hint.textContent = 'Checking liveness (AASIST) and speaker match (Resemblyzer)…';

    const result = await verifyVoiceBiometric(state.user.vpa, blob, {
      challengeId: challenge.challenge_id,
      spokenText: transcript || '',
    });
    if (trEl) {
      trEl.textContent = `Live voice matched (${Math.round((result.score || 0) * 100)}%)`;
    }
    await submitPayment({ step_up_token: result.step_up_token });
  } catch (e) {
    wave?.classList.add('idle');
    const reason = e.voiceMismatch
      ? `Voice / anti-spoof failed — payment blocked. ${e.message || ''}`
      : (e.message || 'Voice authorization failed');
    showFailed(reason, null, 'auth');
  } finally {
    state.voiceConfirmBusy = false;
    window.__onVoiceTranscript = null;
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🎤 Confirm with your voice';
    }
    wave?.classList.add('idle');
  }
}

const ENROLL_SAMPLES = 5;   // more samples -> a tighter voiceprint and a better threshold

// ── Voice enrollment (mandatory after login) ──────────────────────────────────
async function startVoiceEnrollment() {
  state.voiceEnroll = {
    step: 0,
    samples: [],
    transcripts: [],
    recording: false,
    challengeId: null,
    digits: null,
    phrase: ENROLL_BASE_PHRASE,
  };
  showScreen('voice-enroll');
  document.getElementById('voice-enroll-error').textContent = '';
  document.getElementById('voice-enroll-status').textContent = 'Fetching anti-replay challenge…';

  try {
    const challenge = await requestVoiceChallenge(state.user.vpa, 'enroll');
    state.voiceEnroll.challengeId = challenge.challenge_id;
    state.voiceEnroll.digits = challenge.digits;
    state.voiceEnroll.phrase = challenge.phrase;
    updateEnrollUI();
    await speakPrompt(
      `Voice ID setup. Say: my voice is my key, then code ${challenge.digits.split('').join(' ')}`
    );
  } catch (e) {
    document.getElementById('voice-enroll-error').textContent = e.message || String(e);
    updateEnrollUI();
  }
}

function updateEnrollUI() {
  const step = state.voiceEnroll.step;
  const digits = state.voiceEnroll.digits;
  const code = digits ? digits.split('').join(' ') : '····';
  const phrase = state.voiceEnroll.phrase || `${ENROLL_BASE_PHRASE}. Code ${code}`;
  document.getElementById('voice-enroll-phrase').textContent = `“${phrase}”`;
  document.getElementById('voice-enroll-status').textContent =
    step < ENROLL_SAMPLES
      ? `Sample ${step + 1} of ${ENROLL_SAMPLES} — say the phrase + code ${code} (live mic only)`
      : 'Uploading voiceprint + anti-spoof checks…';

  document.querySelectorAll('.voice-enroll-step').forEach((el) => {
    const i = Number(el.dataset.step);
    el.classList.toggle('active', i === step);
    el.classList.toggle('done', i < step);
  });

  const btn = document.getElementById('btn-voice-enroll-record');
  if (btn) {
    btn.disabled = state.voiceEnroll.recording || step >= ENROLL_SAMPLES || !state.voiceEnroll.challengeId;
    btn.textContent = step >= ENROLL_SAMPLES ? 'Saving…' : `🎤 Record sample ${step + 1} of ${ENROLL_SAMPLES}`;
  }
}

async function recordEnrollSample() {
  if (state.voiceEnroll.recording || state.voiceEnroll.step >= ENROLL_SAMPLES) return;
  if (!state.voiceEnroll.challengeId) {
    document.getElementById('voice-enroll-error').textContent = 'Challenge not ready — wait a moment.';
    return;
  }
  if (typeof recordVoiceSample !== 'function') {
    document.getElementById('voice-enroll-error').textContent = 'Voice recorder unavailable in this browser.';
    return;
  }

  state.voiceEnroll.recording = true;
  updateEnrollUI();
  stopSpeaking();

  const wave = document.getElementById('enroll-waveform');
  wave?.classList.remove('idle');
  document.getElementById('voice-enroll-status').textContent = 'Listening… speak phrase + code now';

  window.__onVoiceTranscript = (t) => {
    document.getElementById('voice-enroll-status').textContent = `Heard: “${t}”`;
  };

  try {
    const { blob, transcript } = await recordVoiceSample(5000, { withTranscript: true });
    state.voiceEnroll.samples.push(blob);
    state.voiceEnroll.transcripts.push(transcript || '');
    state.voiceEnroll.step += 1;
    wave?.classList.add('idle');

    if (state.voiceEnroll.step >= ENROLL_SAMPLES) {
      document.getElementById('voice-enroll-status').textContent = 'Creating voice biometric (Resemblyzer + AASIST)…';
      try {
        await enrollVoiceBiometric(
          state.user.vpa,
          state.voiceEnroll.samples,
          state.user.name || '',
          {
            challengeId: state.voiceEnroll.challengeId,
            spokenTexts: state.voiceEnroll.transcripts,
          }
        );
        state.voiceEnrolled = true;
        document.getElementById('voice-enroll-status').textContent =
          'Voice ID enrolled with anti-spoof. Payments need your live voice + fresh code.';
        await speakPrompt('Voice ID set up successfully. Only your live voice can authorize payments.');
        setTimeout(() => {
          startAfterLogin();
          showScreen('home');
        }, 900);
      } catch (enrollErr) {
        state.voiceEnroll = {
          step: 0, samples: [], transcripts: [], recording: false,
          challengeId: null, digits: null, phrase: ENROLL_BASE_PHRASE,
        };
        document.getElementById('voice-enroll-error').textContent = enrollErr.message || String(enrollErr);
        // Fetch a fresh challenge after failure
        try {
          const challenge = await requestVoiceChallenge(state.user.vpa, 'enroll');
          state.voiceEnroll.challengeId = challenge.challenge_id;
          state.voiceEnroll.digits = challenge.digits;
          state.voiceEnroll.phrase = challenge.phrase;
        } catch (_) {}
        updateEnrollUI();
      }
    } else {
      updateEnrollUI();
      document.getElementById('voice-enroll-status').textContent = 'Good. Same phrase + code again.';
    }
  } catch (e) {
    wave?.classList.add('idle');
    document.getElementById('voice-enroll-error').textContent = e.message || String(e);
  } finally {
    window.__onVoiceTranscript = null;
    state.voiceEnroll.recording = false;
    if (state.voiceEnroll.step < ENROLL_SAMPLES) updateEnrollUI();
  }
}

async function ensureVoiceEnrolledOrSetup(vpa) {
  try {
    const status = await fetchVoiceStatus(vpa);
    state.voiceEnrolled = !!status.enrolled;
    if (!status.enrolled) {
      startVoiceEnrollment();
      return false;
    }
    return true;
  } catch (e) {
    console.warn('Voice status check failed:', e);
    // If auth is down, still force enrollment attempt
    startVoiceEnrollment();
    return false;
  }
}

// Merchant chip quick-pay
function handleMerchantChipClick(el) {
  const vpa  = el.dataset.vpa;
  const name = el.dataset.name;
  const icon = el.dataset.icon;
  const amt  = parseFloat(prompt(`Pay to ${name}\nEnter amount (₹):`, '500') || '0');
  if (!amt || amt <= 0) return;
  showConfirm({ vpa, name, icon }, amt);
}

// ── Mobile recharge ───────────────────────────────────────────────────────────
//
// The operator is detected server-side via Veriphone — the API key stays on the
// recharge service and never reaches this file. Plans come from that operator's
// catalogue, and paying for one reuses the ordinary UPI payment flow.

const rc = { phone: null, operator: null, operatorName: null, plans: [], pickerOpen: false };
let rcDebounce = null;

function openRecharge() {
  if (!isUnlocked()) return showScreen('login');
  showScreen('recharge');

  // Show the "my number" shortcut only when we actually know the number —
  // an empty card that does nothing is worse than no card.
  const mine = myMobile();
  const card = document.getElementById('rc-mine');
  const or = document.getElementById('rc-or');
  const otherLabel = document.getElementById('rc-other-label');

  if (mine) {
    document.getElementById('rc-mine-value').textContent =
      `+91 ${mine.slice(0, 5)} ${mine.slice(5)}`;
    card.classList.remove('hidden');
    or.classList.remove('hidden');
    otherLabel.textContent = 'Another number';
  } else {
    card.classList.add('hidden');
    or.classList.add('hidden');
    otherLabel.textContent = 'Mobile number';
    document.getElementById('rc-phone').focus();
  }

  document.getElementById('rc-phone').value = '';
  setRcDetect('');
  document.getElementById('rc-current').classList.add('hidden');
  document.getElementById('rc-plans-wrap').classList.add('hidden');
}

/** One tap: fill in this device's own number and look it up. */
function rechargeMyNumber() {
  const mine = myMobile();
  if (!mine) return;
  document.getElementById('rc-phone').value = mine;
  detectOperator(mine);
}

function setRcDetect(html, cls = '') {
  const el = document.getElementById('rc-detect');
  el.className = `rc-detect ${cls}`;
  el.innerHTML = html;
}

/** Look up the operator, then load its plans and any active subscription. */
async function detectOperator(raw) {
  const digits = (raw || '').replace(/\D/g, '');
  document.getElementById('rc-current').classList.add('hidden');
  document.getElementById('rc-plans-wrap').classList.add('hidden');

  if (digits.length < 10) {
    setRcDetect(digits.length ? 'Enter all 10 digits' : '');
    return;
  }

  setRcDetect('<span class="rc-spin"></span> Checking operator…', 'muted');
  try {
    const res = await fetch(`${API.recharge}/carrier?phone=${encodeURIComponent(digits)}`);
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'Lookup failed');

    if (!d.valid || !d.operator) {
      setRcDetect(
        `Could not identify the operator${d.phone_type && d.phone_type !== 'mobile'
          ? ` — that looks like a ${d.phone_type.replace('_', ' ')} number` : ''}. Pick one below.`,
        'warn');
      rc.phone = d.phone;
      showOperatorPicker(true);
      return;
    }

    rc.phone = d.phone;
    setRcDetect(
      `<span class="rc-op-badge op-${d.operator}">${escapeHtml(d.operator_name)}</span>` +
      `<span class="rc-op-note">${d.source === 'cache' ? 'from this device' : escapeHtml(d.note || '')}</span>`,
      'ok');
    await Promise.all([loadPlans(d.operator), loadSubscription(d.phone)]);
  } catch (e) {
    setRcDetect(`Operator lookup unavailable — ${escapeHtml(e.message)}. Pick one below.`, 'warn');
    rc.phone = `+91${digits}`;
    showOperatorPicker(true);
  }
}

async function showOperatorPicker(open) {
  const el = document.getElementById('rc-op-picker');
  rc.pickerOpen = open;
  el.classList.toggle('hidden', !open);
  document.getElementById('rc-plans-wrap').classList.remove('hidden');
  if (!el.dataset.loaded) {
    try {
      const { operators } = await fetch(`${API.recharge}/operators`).then(r => r.json());
      el.innerHTML = operators.map(o =>
        `<button class="rc-op-chip op-${o.key}" data-op="${o.key}">${escapeHtml(o.name)}</button>`).join('');
      el.querySelectorAll('.rc-op-chip').forEach(b =>
        b.addEventListener('click', () => {
          showOperatorPicker(false);
          setRcDetect(`<span class="rc-op-badge op-${b.dataset.op}">${escapeHtml(b.textContent)}</span>` +
                      `<span class="rc-op-note">chosen manually</span>`, 'ok');
          loadPlans(b.dataset.op);
        }));
      el.dataset.loaded = '1';
    } catch { el.innerHTML = '<div class="rc-empty">Operator list unavailable</div>'; }
  }
}

async function loadPlans(operator) {
  const wrap = document.getElementById('rc-plans-wrap');
  const list = document.getElementById('rc-plans');
  wrap.classList.remove('hidden');
  list.innerHTML = '<div class="rc-empty">Loading plans…</div>';
  try {
    const d = await fetch(`${API.recharge}/plans/${operator}`).then(r => r.json());
    rc.operator = operator;
    rc.operatorName = d.operator_name;
    rc.plans = d.plans;
    document.getElementById('rc-plans-title').textContent = `${d.operator_name} plans`;
    document.getElementById('rc-disclaimer').textContent = d.disclaimer;
    list.innerHTML = d.plans.map(p => `
      <button class="rc-plan" data-plan="${p.id}">
        <div class="rc-plan-price">₹${p.price}</div>
        <div class="rc-plan-body">
          <div class="rc-plan-top">
            <span class="rc-plan-data">${escapeHtml(p.data)}</span>
            ${p.popular ? '<span class="rc-plan-tag">POPULAR</span>' : ''}
          </div>
          <div class="rc-plan-meta">${p.validity_days} days · ${escapeHtml(p.calls)} calls · ${escapeHtml(p.sms)}</div>
          ${p.extras.length ? `<div class="rc-plan-extras">${p.extras.map(escapeHtml).join(' · ')}</div>` : ''}
        </div>
        <span class="rc-plan-go">›</span>
      </button>`).join('');
    list.querySelectorAll('.rc-plan').forEach(b =>
      b.addEventListener('click', () => payForPlan(b.dataset.plan)));
  } catch (e) {
    list.innerHTML = `<div class="rc-empty">Could not load plans — ${escapeHtml(e.message)}</div>`;
  }
}

async function loadSubscription(phone) {
  const el = document.getElementById('rc-current');
  try {
    const d = await fetch(`${API.recharge}/subscription/${encodeURIComponent(phone)}`).then(r => r.json());
    if (!d.active) { el.classList.add('hidden'); return; }
    const urgent = d.expired || d.expiring_soon;
    el.className = `rc-current ${urgent ? 'urgent' : ''}`;
    el.innerHTML = `
      <div class="rc-current-head">
        <span>Current plan</span>
        <span class="rc-current-pill">${d.expired ? 'EXPIRED'
          : d.days_left === 0 ? 'EXPIRES TODAY' : `${d.days_left} DAYS LEFT`}</span>
      </div>
      <div class="rc-current-main">₹${d.price} · ${escapeHtml(d.plan.data || '')}</div>
      <div class="rc-current-sub">
        ${escapeHtml(d.operator_name)} · ${d.expired ? 'expired on' : 'valid until'}
        ${new Date(d.expires_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}
      </div>`;
  } catch { el.classList.add('hidden'); }
}

/** Pay for a plan through the normal UPI flow, then activate it on success. */
function payForPlan(planId) {
  const plan = rc.plans.find(p => p.id === planId);
  if (!plan || !rc.phone) return;
  const local = rc.phone.replace('+91', '');
  state.pendingRecharge = { phone: rc.phone, operator: rc.operator, plan_id: plan.id };
  showConfirm({
    vpa: `${rc.operator}@billpay`,
    name: `${rc.operatorName} · ${local}`,
    icon: '📱',
  }, plan.price);
}

/** Called after a payment succeeds; starts the plan's validity. */
async function activatePendingRecharge(txnId) {
  const pending = state.pendingRecharge;
  if (!pending) return;
  state.pendingRecharge = null;
  try {
    await fetch(`${API.recharge}/subscription/activate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...pending, txn_id: txnId }),
    });
  } catch (e) {
    console.warn('Recharge activation failed:', e);
  }
}

// ── QR scanning ───────────────────────────────────────────────────────────────
//
// The old "Scan QR" button picked a random merchant and asked for an amount in
// a prompt() — it never looked at a camera. This reads real UPI QR codes using
// the browser's built-in BarcodeDetector, so no library is needed; Chrome and
// Edge ship it. Anything else falls back to typing the UPI ID by hand.

let qrStream = null;
let qrLoopId = null;

function setQrStatus(msg, isError = false) {
  const el = document.getElementById('qr-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? 'var(--red)' : '';
}

/** Build a payee from a bare VPA, reusing the known merchant icon when we have one. */
function payeeFromVpa(vpa, nameHint = '') {
  const handle = vpa.split('@')[0].toLowerCase();
  for (const [key, info] of Object.entries(MERCHANTS)) {
    if (info.vpa === vpa || handle === key.replace(/\s+/g, '')) return info;
  }
  return {
    vpa,
    name: nameHint || handle.charAt(0).toUpperCase() + handle.slice(1),
    icon: getMerchantIcon(vpa),
  };
}

/**
 * Parse a scanned UPI QR payload.
 * Real-world format: upi://pay?pa=<vpa>&pn=<name>&am=<amount>&cu=INR
 * A static QR carries no amount, so the user is asked for one.
 */
function parseUpiQr(raw) {
  if (!raw) return null;
  const text = raw.trim();
  try {
    if (/^upi:\/\//i.test(text)) {
      const q = new URLSearchParams(text.slice(text.indexOf('?') + 1));
      const pa = (q.get('pa') || '').trim().toLowerCase();
      if (!pa.includes('@')) return null;
      const am = parseFloat(q.get('am') || '');
      return { vpa: pa, name: q.get('pn') || '', amount: Number.isFinite(am) && am > 0 ? am : null };
    }
  } catch { /* fall through to the bare-VPA case */ }
  // Some QR codes carry just the VPA.
  if (/^[a-z0-9._-]+@[a-z0-9.-]+$/i.test(text)) {
    return { vpa: text.toLowerCase(), name: '', amount: null };
  }
  return null;
}

async function startQrScan() {
  if (!isUnlocked()) return showScreen('login');
  showScreen('qr');
  document.getElementById('qr-manual-vpa').value = '';
  document.getElementById('qr-manual-amt').value = '';

  const video = document.getElementById('qr-video');
  const placeholder = document.getElementById('qr-placeholder');
  const placeholderText = document.getElementById('qr-placeholder-text');

  if (!('BarcodeDetector' in window)) {
    placeholderText.textContent = 'This browser cannot scan QR codes — enter the UPI ID below';
    setQrStatus('Scanning needs Chrome or Edge', true);
    return;
  }

  try {
    qrStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment' },
    });
  } catch (e) {
    placeholderText.textContent =
      e && e.name === 'NotAllowedError'
        ? 'Camera permission denied — enter the UPI ID below'
        : 'No camera available — enter the UPI ID below';
    setQrStatus('Camera unavailable', true);
    return;
  }

  video.srcObject = qrStream;
  await video.play().catch(() => {});
  placeholder.classList.add('hidden');
  setQrStatus('Point the camera at any UPI QR code');

  const detector = new window.BarcodeDetector({ formats: ['qr_code'] });
  let busy = false;

  const tick = async () => {
    if (!qrStream) return;
    if (!busy && video.readyState >= 2) {
      busy = true;
      try {
        const codes = await detector.detect(video);
        if (codes && codes.length) {
          const parsed = parseUpiQr(codes[0].rawValue);
          if (parsed) {
            const payee = payeeFromVpa(parsed.vpa, parsed.name);
            stopQrScan();
            if (parsed.amount) {
              showConfirm(payee, parsed.amount);           // dynamic QR
            } else {
              // Static QR: it names the payee but not the amount.
              document.getElementById('qr-manual-vpa').value = parsed.vpa;
              showScreen('qr');
              setQrStatus(`Found ${payee.name} — enter the amount below`);
              document.getElementById('qr-manual-amt').focus();
            }
            return;
          }
          setQrStatus('That QR is not a UPI code', true);
        }
      } catch { /* a dropped frame is not worth reporting */ }
      busy = false;
    }
    qrLoopId = requestAnimationFrame(tick);
  };
  qrLoopId = requestAnimationFrame(tick);
}

function stopQrScan() {
  if (qrLoopId) { cancelAnimationFrame(qrLoopId); qrLoopId = null; }
  if (qrStream) {
    qrStream.getTracks().forEach(t => t.stop());   // release the camera light
    qrStream = null;
  }
  const video = document.getElementById('qr-video');
  if (video) video.srcObject = null;
  document.getElementById('qr-placeholder')?.classList.remove('hidden');
}

// ── PIN screen ────────────────────────────────────────────────────────────────
function showPin() {
  state.pin = '';
  updatePinDots();
  document.getElementById('pin-error').textContent = '';
  document.getElementById('pin-amount-label').textContent =
    `₹${formatAmount(state.payment.amount)} to ${state.payment.payeeName}`;
  showScreen('pin');
}

function updatePinDots() {
  for (let i = 0; i < 4; i++) {
    const dot = document.getElementById(`pin-dot-${i}`);
    dot.classList.toggle('filled', i < state.pin.length);
  }
}

function handlePinKey(val) {
  if (state.pin.length >= 4) return;
  state.pin += val;
  updatePinDots();
  if (state.pin.length === 4) {
    // Small delay so user sees 4 dots before processing begins
    setTimeout(submitPayment, 200);
  }
}

function handlePinBackspace() {
  state.pin = state.pin.slice(0, -1);
  updatePinDots();
  document.getElementById('pin-error').textContent = '';
}

// ── Submit payment ────────────────────────────────────────────────────────────
async function submitPayment(opts = {}) {
  showScreen('processing');

  const txnId = null;   // PSP will generate it
  document.getElementById('processing-txn-id').textContent = 'Initiating…';
  document.getElementById('processing-status-text').textContent = 'Connecting to PSP…';
  document.getElementById('hop-log-entries').innerHTML = '';

  // Reset all hops to pending
  setHopState('psp', 'completed');
  setHopState('switch', 'pending');
  setHopState('payer_bank', 'pending');
  setHopState('payee_bank', 'pending');
  setLineState('switch', '');
  setLineState('payer', '');
  setLineState('payee', '');

  let txnIdResult;

  try {
    const body = {
      payer_vpa: state.user.vpa,
      payee_vpa: state.payment.payeeVpa,
      amount:    state.payment.amount,
    };
    if (opts.step_up_token) body.step_up_token = opts.step_up_token;
    else body.pin = state.pin;

    const res = await fetch(`${API.psp}/initiate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json();
      showFailed(err.detail || 'PSP rejected the request', null, 'psp');
      return;
    }

    const data = await res.json();
    txnIdResult = data.txn_id;
    state.payment.txnId = txnIdResult;
    document.getElementById('processing-txn-id').textContent = `TXN: ${txnIdResult}`;

  } catch (e) {
    showFailed('Cannot connect to PSP service. Is it running on port 5001?', null, 'psp');
    return;
  }

  // Poll for status
  pollTxnStatus(txnIdResult);
}

// ── Status polling ────────────────────────────────────────────────────────────
let lastLogLength = 0;

async function pollTxnStatus(txnId) {
  lastLogLength = 0;
  clearInterval(state.pollInterval);

  state.pollInterval = setInterval(async () => {
    try {
      const res = await fetch(`${API.psp}/txn/${txnId}`);
      if (!res.ok) return;

      const data = await res.json();
      updateProcessingUI(data);

      const terminal = ['success', 'settled', 'failed', 'timeout', 'partial_failure', 'reversed'];
      if (terminal.includes(data.status)) {
        clearInterval(state.pollInterval);

        if (data.status === 'success' || data.status === 'settled') {
          await refreshBalance();
          setTimeout(() => showSuccess(data), 600);
        } else {
          // A reversal means the money is already back — refresh so the
          // balance the user sees reflects the refund.
          if (data.status === 'reversed') await refreshBalance();
          setTimeout(() => showFailed(data.error || 'Unknown error', txnId, data.error_stage), 400);
        }
      }
    } catch { /* keep polling */ }
  }, 400);

  // Safety timeout — stop polling after 30s
  setTimeout(() => clearInterval(state.pollInterval), 30_000);
}

function updateProcessingUI(data) {
  // Status text
  const statusMessages = {
    initiated:       'PSP received — connecting to NPCI Switch…',
    routing:         'NPCI Switch routing request…',
    debiting:        'Payer Bank: verifying PIN & checking balance…',
    crediting:       'Payee Bank: processing credit…',
    success:         'All hops confirmed ✓ — awaiting settlement',
    settled:         'Transaction settled ✓',
    failed:          'Transaction failed',
    timeout:         'Service timed out — checking status…',
    reversed:        'Payment failed — amount refunded ✓',
    partial_failure: 'Failed, and the refund did not go through — under investigation',
  };
  document.getElementById('processing-status-text').textContent =
    statusMessages[data.status] || `Status: ${data.status}`;

  // Hop states
  const hops = data.hops || {};
  setHopState('psp',        hops.psp        || 'completed');
  setHopState('switch',     hops.switch      || 'pending');
  setHopState('payer_bank', hops.payer_bank  || 'pending');
  setHopState('payee_bank', hops.payee_bank  || 'pending');

  // Line states
  setLineState('switch', hops.switch && hops.switch !== 'pending' ? (hops.switch === 'completed' ? 'completed' : 'active') : '');
  setLineState('payer',  hops.payer_bank && hops.payer_bank !== 'pending' ? (hops.payer_bank === 'completed' ? 'completed' : 'active') : '');
  setLineState('payee',  hops.payee_bank && hops.payee_bank !== 'pending' ? (hops.payee_bank === 'completed' ? 'completed' : 'active') : '');

  // Live log entries
  const entries = data.hop_log || [];
  if (entries.length > lastLogLength) {
    const container = document.getElementById('hop-log-entries');
    for (let i = lastLogLength; i < entries.length; i++) {
      container.appendChild(buildLogEntry(entries[i]));
    }
    lastLogLength = entries.length;
    // Auto-scroll
    const logEl = document.getElementById('hop-log');
    logEl.scrollTop = logEl.scrollHeight;
  }
}

function setHopState(hopId, state) {
  const nodeMap = {
    psp:        'hop-psp',
    switch:     'hop-switch',
    payer_bank: 'hop-payer-bank',
    payee_bank: 'hop-payee-bank',
  };
  const el = document.getElementById(nodeMap[hopId]);
  if (!el) return;
  el.className = `hop-node ${state}`;
}

function setLineState(lineId, state) {
  const el = document.getElementById(`line-${lineId}`);
  if (!el) return;
  el.className = `hop-line ${state}`;
}

function buildLogEntry(entry) {
  const div = document.createElement('div');
  div.className = 'hop-log-entry';

  const badge = document.createElement('span');
  badge.className = `hop-log-badge ${entry.hop}`;
  badge.textContent = entry.hop.replace('_', ' ').toUpperCase();

  const text = document.createElement('span');
  text.className = 'hop-log-text';
  text.textContent = entry.event;

  const time = document.createElement('span');
  time.className = 'hop-log-time';
  const d = new Date(entry.ts + 'Z');
  time.textContent = d.toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  div.append(badge, text, time);
  return div;
}

// ── Success screen ────────────────────────────────────────────────────────────
function showSuccess(data) {
  // A recharge only becomes active once the money has actually moved.
  activatePendingRecharge(data.txn_id);

  document.getElementById('success-amount').textContent  = `₹${formatAmount(data.amount)} sent`;
  document.getElementById('success-to').textContent      = `to ${state.payment.payeeName}`;
  document.getElementById('success-txn-id').textContent  = data.txn_id;
  document.getElementById('success-new-balance').textContent =
    data.new_balance !== null ? `₹${formatAmount(data.new_balance)}` : `₹${formatAmount(state.user.balance)}`;
  document.getElementById('success-time').textContent    = new Date().toLocaleString('en-IN');

  // Store in local history
  state.localTxns.unshift({
    txn_id:     data.txn_id,
    payer_vpa:  state.user.vpa,
    payee_vpa:  state.payment.payeeVpa,
    payee_name: state.payment.payeeName,
    payee_icon: state.payment.payeeIcon,
    amount:     data.amount,
    status:     data.status,
    timestamp:  new Date().toISOString(),
  });

  updateHomeTxnList();
  spawnConfetti();
  showScreen('success');
}

// ── Failed screen ─────────────────────────────────────────────────────────────
function clearPendingRecharge() { state.pendingRecharge = null; }

function showFailed(reason, txnId, stage) {
  document.getElementById('failed-amount').textContent  = `₹${formatAmount(state.payment.amount)}`;
  document.getElementById('failed-to').textContent      = `to ${state.payment.payeeName}`;
  document.getElementById('error-reason').textContent   = reason || 'Unknown error';
  document.getElementById('failed-txn-id').textContent  = txnId  || '—';
  document.getElementById('failed-stage').textContent   = stage  || '—';

  state.localTxns.unshift({
    txn_id:     txnId,
    payer_vpa:  state.user.vpa,
    payee_vpa:  state.payment.payeeVpa,
    payee_name: state.payment.payeeName,
    payee_icon: state.payment.payeeIcon,
    amount:     state.payment.amount,
    status:     'failed',
    failure_reason: reason,
    timestamp:  new Date().toISOString(),
  });

  updateHomeTxnList();
  clearPendingRecharge();
  showScreen('failed');
}

// ── History ───────────────────────────────────────────────────────────────────
async function loadHistory(filter = 'all') {
  const container = document.getElementById('history-txn-list');
  container.innerHTML = '<div class="txn-empty"><div class="txn-empty-icon">⏳</div>Loading…</div>';

  // Always filter to the currently logged-in user's VPA
  const vpa = state.user.vpa;

  try {
    const url = `${API.psp}/transactions${vpa ? '?payer_vpa=' + encodeURIComponent(vpa) : ''}`;
    const res = await fetch(url);
    let txns = res.ok ? await res.json() : [];

    // Fallback: filter local mirror to current user
    if (!txns.length) txns = state.localTxns.filter(t => !vpa || t.payer_vpa === vpa || t.payee_vpa === vpa);

    // Status filter
    if (filter !== 'all') txns = txns.filter(t => t.status === filter
      || (filter === 'success' && t.status === 'settled')
      || (filter === 'failed' && ['reversed', 'partial_failure'].includes(t.status)));

    container.innerHTML = txns.length ? '' : `
      <div class="txn-empty">
        <div class="txn-empty-icon">📭</div>
        No ${filter !== 'all' ? filter : ''} transactions yet.
      </div>`;

    txns.forEach(t => container.appendChild(buildTxnItem(t)));
  } catch {
    const txns = state.localTxns
      .filter(t => !vpa || t.payer_vpa === vpa || t.payee_vpa === vpa)
      .filter(t => filter === 'all' || t.status === filter);
    container.innerHTML = txns.length ? '' :
      '<div class="txn-empty"><div class="txn-empty-icon">⚡</div>Services offline. Local history shown.</div>';
    txns.forEach(t => container.appendChild(buildTxnItem(t)));
  }
}

function buildTxnItem(t) {
  const div = document.createElement('div');
  div.className = 'txn-item';

  const icon = document.createElement('div');
  icon.className = `txn-icon ${['success','settled','debited','credited'].includes(t.status) ? 'debit' : ['failed','reversed'].includes(t.status) ? 'debit' : 'pending'}`;
  icon.textContent = t.payee_icon || getMerchantIcon(t.payee_vpa || t.payee_name) || '💳';

  const info = document.createElement('div');
  info.className = 'txn-info';
  info.innerHTML = `
    <div class="txn-name">${t.payee_name || t.payee_vpa || 'Unknown'}</div>
    <div class="txn-meta">${formatTxnStatus(t.status)} · ${formatRelativeTime(t.timestamp)}</div>
  `;

  const amount = document.createElement('div');
  const isSuccess = ['success','settled','debited'].includes(t.status);
  amount.className = `txn-amount ${isSuccess ? 'debit' : 'credit'}`;
  amount.textContent = `${isSuccess ? '−' : '+'}₹${formatAmount(t.amount)}`;

  div.append(icon, info, amount);
  return div;
}

function updateHomeTxnList() {
  const container = document.getElementById('home-txn-list');
  // Only show the current user's own transactions
  const vpa = state.user.vpa;
  const recent = state.localTxns
    .filter(t => !vpa || t.payer_vpa === vpa || t.payee_vpa === vpa)
    .slice(0, 5);

  if (!recent.length) {
    container.innerHTML = '<div class="txn-empty"><div class="txn-empty-icon">💳</div>No transactions yet. Try Voice Pay!</div>';
    return;
  }

  container.innerHTML = '';
  recent.forEach(t => container.appendChild(buildTxnItem(t)));
}

// ── Settlement ────────────────────────────────────────────────────────────────
async function loadSettlements() {
  const container = document.getElementById('settlement-list');

  try {
    const res = await fetch(`${API.psp}/settlement/history`);
    if (!res.ok) throw new Error();
    const data = await res.json();

    document.getElementById('settlement-cycle-count').textContent = `Cycle #${data.total_cycles}`;

    if (!data.cycles.length) {
      container.innerHTML = '<div class="txn-empty"><div class="txn-empty-icon">⏳</div>Waiting for first settlement cycle (every 30s)…</div>';
      return;
    }

    container.innerHTML = '';
    data.cycles.forEach(c => container.appendChild(buildCycleCard(c)));
  } catch {
    container.innerHTML = '<div class="txn-empty"><div class="txn-empty-icon">⚡</div>Settlement service offline.</div>';
  }
}

function buildCycleCard(c) {
  const div = document.createElement('div');
  div.className = 'settlement-cycle-card';

  const status = c.status === 'SETTLED' ? 'settled' : c.status === 'EMPTY_CYCLE' ? 'empty' : 'error';
  const label  = c.status === 'SETTLED' ? 'SETTLED' : c.status === 'EMPTY_CYCLE' ? 'EMPTY' : 'ERROR';

  div.innerHTML = `
    <div class="cycle-header">
      <span class="cycle-id">${c.cycle_id}</span>
      <span class="cycle-badge ${status}">${label}</span>
    </div>
    <div class="cycle-net">Net obligation: ₹${formatAmount(c.net_obligation || 0)}</div>
    <div class="cycle-meta">
      ${new Date(c.timestamp + 'Z').toLocaleString('en-IN')} · 
      ${c.debit_count || 0} debit txns · ${c.credit_count || 0} credit txns
    </div>
    ${c.status === 'SETTLED' ? `
    <div class="cycle-bar">
      <div class="cycle-bar-item">
        <div class="cycle-bar-val" style="color:var(--red)">₹${formatAmount(c.total_debit_settled || 0)}</div>
        <div class="cycle-bar-key">Debits settled</div>
      </div>
      <div class="cycle-bar-item">
        <div class="cycle-bar-val" style="color:var(--green)">₹${formatAmount(c.total_credit_settled || 0)}</div>
        <div class="cycle-bar-key">Credits settled</div>
      </div>
      <div class="cycle-bar-item">
        <div class="cycle-bar-val" style="color:var(--amber)">₹${formatAmount(c.net_obligation || 0)}</div>
        <div class="cycle-bar-key">Bank A → Bank B</div>
      </div>
    </div>` : ''}
  `;

  return div;
}

// ── Settlement countdown ──────────────────────────────────────────────────────
async function updateSettlementCountdown() {
  try {
    const res  = await fetch(`${API.psp}/settlement/next`);
    if (!res.ok) return;
    const data = await res.json();
    const secs = data.next_in_secs;

    const fmt = secs > 60
      ? `${Math.floor(secs / 60)}m ${secs % 60}s`
      : `${secs}s`;

    document.getElementById('settlement-countdown').textContent     = fmt;
    document.getElementById('settlement-hero-timer').textContent    = fmt;
    document.getElementById('settlement-mini-sub').textContent      = `Cycle #${data.cycle_number} — every ${data.interval_secs}s`;
  } catch { /* services not up yet */ }
}

// ── System reset ──────────────────────────────────────────────────────────────
async function resetSystem() {
  if (!confirm('Reset all balances to ₹50,000 and clear all transactions?')) return;

  try {
    await fetch(`${API.psp}/reset`, { method: 'POST' });
  } catch { /* ignore if offline */ }

  state.localTxns = [];
  state.payment   = { payeeVpa: null, payeeName: null, payeeIcon: null, amount: null, txnId: null };
  state.pin       = '';

  document.getElementById('home-txn-list').innerHTML =
    '<div class="txn-empty"><div class="txn-empty-icon">💳</div>No transactions yet. Try Voice Pay!</div>';

  await refreshBalance();
  showScreen('home');
  alert('✅ System reset! All balances restored.');
}

// ── Confetti ──────────────────────────────────────────────────────────────────
function spawnConfetti() {
  const container = document.getElementById('success-confetti-area');
  const colors = ['var(--green)', 'var(--blue)', 'var(--amber)', 'var(--purple)', '#FF6B6B'];

  for (let i = 0; i < 30; i++) {
    setTimeout(() => {
      const el = document.createElement('div');
      el.className = 'confetti-particle';
      el.style.cssText = `
        left: ${20 + Math.random() * 60}%;
        background: ${colors[Math.floor(Math.random() * colors.length)]};
        animation-delay: ${Math.random() * 0.4}s;
        animation-duration: ${1 + Math.random() * 0.8}s;
        transform: rotate(${Math.random() * 360}deg);
        border-radius: ${Math.random() > 0.5 ? '50%' : '2px'};
      `;
      container.appendChild(el);
      setTimeout(() => el.remove(), 2500);
    }, i * 30);
  }
}

// ── Utility ───────────────────────────────────────────────────────────────────
function formatAmount(n) {
  if (n === null || n === undefined) return '—';
  return parseFloat(n).toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

function formatRelativeTime(ts) {
  if (!ts) return '—';
  const diff = (Date.now() - new Date(ts + (ts.includes('Z') ? '' : 'Z')).getTime()) / 1000;
  if (diff < 60)   return 'Just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(ts).toLocaleDateString('en-IN');
}

function formatTxnStatus(s) {
  const map = {
    success:         '✅ Success',
    settled:         '✅ Settled',
    debited:         '⏳ Debited (pending settlement)',
    credited:        '⏳ Credited (pending settlement)',
    failed:          '❌ Failed',
    timeout:         '⏰ Timed out',
    initiated:       '🔄 Initiated',
    reversed:        '↩️ Refunded',
    partial_failure: '⚠️ Stuck — needs recon',
  };
  return map[s] || s;
}

function getMerchantIcon(identifier) {
  if (!identifier) return null;
  const lower = identifier.toLowerCase();
  if (lower.includes('swiggy'))    return '🍔';
  if (lower.includes('zomato'))    return '🍕';
  if (lower.includes('blinkit'))   return '🛵';
  if (lower.includes('sharma'))    return '🛒';
  if (lower.includes('amazon'))    return '📦';
  if (lower.includes('petrol'))    return '⛽';
  return '💳';
}

// ── Initialise event listeners ────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // Balance refresh
  document.getElementById('btn-refresh-balance').addEventListener('click', () => refreshBalance(true));

  // Quick-action buttons
  document.getElementById('btn-voice-pay').addEventListener('click', startVoiceInput);
  document.getElementById('btn-voice-pay-link').addEventListener('click', startVoiceInput);
  document.getElementById('btn-history-nav').addEventListener('click', () => {
    showScreen('history'); loadHistory();
  });
  document.getElementById('btn-reset-demo').addEventListener('click', resetSystem);
  document.getElementById('btn-qr-demo').addEventListener('click', () => startQrScan());

  // Recharge
  document.getElementById('btn-recharge').addEventListener('click', () => openRecharge());
  document.getElementById('btn-recharge-back').addEventListener('click', () => showScreen('home'));
  document.getElementById('rc-switch-op').addEventListener('click', () => showOperatorPicker(!rc.pickerOpen));
  document.getElementById('rc-mine').addEventListener('click', rechargeMyNumber);
  document.getElementById('rc-phone').addEventListener('input', (e) => {
    // Debounced: a lookup costs a credit, so do not fire on every keystroke.
    clearTimeout(rcDebounce);
    const v = e.target.value;
    rcDebounce = setTimeout(() => detectOperator(v), 450);
  });
  document.getElementById('btn-qr-back').addEventListener('click', () => {
    stopQrScan();
    showScreen('home');
  });
  document.getElementById('btn-qr-manual-go').addEventListener('click', () => {
    const vpa = (document.getElementById('qr-manual-vpa').value || '').trim().toLowerCase();
    const amt = parseFloat((document.getElementById('qr-manual-amt').value || '').replace(/[^\d.]/g, ''));
    if (!vpa.includes('@')) return setQrStatus('Enter a full UPI ID, e.g. merchant@bank', true);
    if (!amt || amt <= 0) return setQrStatus('Enter an amount greater than zero', true);
    if (amt > MAX_TXN_AMOUNT) return setQrStatus(`Over the ₹${formatAmount(MAX_TXN_AMOUNT)} limit`, true);
    stopQrScan();
    showConfirm(payeeFromVpa(vpa), amt);
  });

  // Merchant chips
  document.querySelectorAll('.merchant-chip').forEach(el => {
    el.addEventListener('click', () => handleMerchantChipClick(el));
  });

  // Voice screen
  document.getElementById('btn-voice-close').addEventListener('click', () => {
    stopVoiceInput(); showScreen('home');
  });
  document.getElementById('voice-mic-btn').addEventListener('click', startVoiceInput);
  document.getElementById('btn-text-send').addEventListener('click', handleTextInput);
  document.getElementById('voice-text-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleTextInput();
  });

    // Confirm / pay now — voice biometric authorises (no PIN/WebAuthn bypass)
    document.getElementById('btn-pay-now').addEventListener('click', () => {
      authorizePaymentWithVoice();
    });
    document.getElementById('btn-confirm-cancel')?.addEventListener('click', () => {
      stopSpeaking();
      showScreen('home');
    });

    document.getElementById('btn-voice-enroll-record')?.addEventListener('click', () => {
      recordEnrollSample();
    });

  // PIN keypad (login / legacy only — payments use voice)
  document.querySelectorAll('.pin-key[data-val]').forEach(key => {
    key.addEventListener('click', () => handlePinKey(key.dataset.val));
  });

  // Enroll biometric button
  const enrollBtn = document.getElementById('btn-enroll-biometric');
  if (enrollBtn) {
    enrollBtn.addEventListener('click', async () => {
      if (typeof enrollBiometric !== 'function') {
        alert('WebAuthn helper not available in this browser.');
        return;
      }
      const label = prompt('Label for this device (e.g. "My iPhone")', 'My Device');
      try {
        const res = await enrollBiometric(state.user.vpa, label || 'Unnamed device');
        alert(res.message || 'Biometric enrolled successfully');
      } catch (e) {
        alert('Enrollment failed: ' + (e.message || e));
      }
    });
  }

  // --- Login screen wiring ---
  const loginPinInput = document.getElementById('login-pin');
  const loginPinBtn = document.getElementById('login-pin-btn');
  const loginBioBtn = document.getElementById('login-biometric-btn');
  const loginEnrollBtn = document.getElementById('login-enroll-btn');
  const loginStatus = document.getElementById('login-status');
  const accountList = document.getElementById('login-accounts');
  const accountField = document.getElementById('login-account-field');
  const vpaField = document.getElementById('login-vpa-field');
  const vpaInput = document.getElementById('login-vpa');
  const btnUseOtherVpa = document.getElementById('btn-use-other-vpa');
  const btnCancelOtherVpa = document.getElementById('btn-cancel-other-vpa');

  let selectedVpa = null;
  let manualVpaMode = false;

  function setLoginStatus(msg, kind = 'info') {
    if (!loginStatus) return;
    loginStatus.textContent = msg || '';
    loginStatus.className = `login-status ${msg ? kind : ''}`;
  }

  function setLoginBusy(busy) {
    [loginPinBtn, loginBioBtn, loginEnrollBtn].forEach(b => { if (b) b.disabled = busy; });
  }

  // Which VPA is this sign-in attempt for? Either a device-linked account the
  // user tapped, or one they typed in full. There is no browsable directory.
  function requireAccount() {
    if (manualVpaMode) {
      const typed = (vpaInput.value || '').trim().toLowerCase();
      if (!typed || !typed.includes('@')) {
        setLoginStatus('Enter your full UPI ID, e.g. yourname@bank.', 'error');
        return null;
      }
      return typed;
    }
    if (!selectedVpa) {
      setLoginStatus('Pick an account to sign in with.', 'error');
      return null;
    }
    return selectedVpa;
  }

  async function finishLogin(vpa) {
    try {
      // Fetch account info (holder_name + balance)
      const res = await fetch(`${API.payerBank}/balance/${vpa}`);
      if (res.ok) {
        const acc = await res.json();
        state.user.vpa = acc.vpa;
        state.user.name = acc.holder_name || acc.vpa;
        state.user.balance = acc.balance || 0;
        document.getElementById('balance-value').textContent = formatAmount(state.user.balance);
      } else {
        state.user.vpa = vpa;
      }
    } catch {
      state.user.vpa = vpa;
    }
    state.loggedIn = true;

    // Device binding: this browser now remembers only the accounts that have
    // actually authenticated on it.
    rememberDeviceAccount(state.user.vpa, state.user.name);

    const avatar = document.getElementById('header-avatar');
    if (avatar) {
      avatar.textContent = (state.user.name || vpa).charAt(0).toUpperCase();
      avatar.title = `${state.user.vpa} — click to sign out`;
    }
    const balVpa = document.getElementById('balance-vpa');
    if (balVpa) balVpa.textContent = state.user.vpa;

    // Most important gate: voice biometric must exist for this user
    const ok = await ensureVoiceEnrolledOrSetup(vpa);
    if (ok) {
      startAfterLogin();
      showScreen('home');
    }
  }

  if (loginPinBtn) {
    loginPinBtn.addEventListener('click', async () => {
      const vpa = requireAccount();
      if (!vpa) return;
      const pin = loginPinInput.value.trim();
      if (!pin) return setLoginStatus('Enter your UPI PIN.', 'error');
      setLoginBusy(true);
      setLoginStatus('Verifying PIN…', 'info');
      try {
        await authenticateWithPin(vpa, pin);
        setLoginStatus('Signed in — opening your account…', 'ok');
        loginPinInput.value = '';
        await finishLogin(vpa);
      } catch (e) {
        setLoginStatus(`PIN sign-in failed: ${e.message || e}`, 'error');
      } finally {
        setLoginBusy(false);
      }
    });
  }

  if (loginPinInput) {
    loginPinInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') loginPinBtn?.click();
    });
    loginPinInput.addEventListener('input', () => setLoginStatus(''));
  }

  if (loginBioBtn) {
    loginBioBtn.addEventListener('click', async () => {
      const vpa = requireAccount();
      if (!vpa) return;
      setLoginBusy(true);
      setLoginStatus('Waiting for Face ID / Touch ID…', 'info');
      try {
        await authenticateWithBiometric(vpa);
        setLoginStatus('Biometric verified — opening your account…', 'ok');
        await finishLogin(vpa);
      } catch (e) {
        setLoginStatus(`Biometric sign-in failed: ${e.message || e}`, 'error');
      } finally {
        setLoginBusy(false);
      }
    });
  }

  if (loginEnrollBtn) {
    loginEnrollBtn.addEventListener('click', async () => {
      const vpa = requireAccount();
      if (!vpa) return;
      setLoginBusy(true);
      setLoginStatus('Follow your device prompt to register this passkey…', 'info');
      try {
        const res = await enrollBiometric(vpa, `${vpa} · this device`);
        setLoginStatus(res.message || 'Biometric registered — you can now sign in with it.', 'ok');
      } catch (e) {
        setLoginStatus(`Biometric setup failed: ${e.message || e}`, 'error');
      } finally {
        setLoginBusy(false);
      }
    });
  }

  document.getElementById('pin-backspace').addEventListener('click', handlePinBackspace);

  // History filter tabs
  document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      loadHistory(tab.dataset.filter);
    });
  });

  // History nav
  document.getElementById('btn-see-all').addEventListener('click', () => {
    showScreen('history'); loadHistory();
  });

  // Retry on failure
  document.getElementById('btn-retry').addEventListener('click', () => {
    state.pin = '';
    showPin();
  });

  // --- Device-linked accounts ---------------------------------------------
  // A real UPI app binds to the handset, so the sign-in screen may only offer
  // accounts that have already authenticated in THIS browser. The frontend
  // never asks the bank for a list of accounts — that would let anyone read
  // every customer's VPA, name and balance off the login screen.

  function selectAccount(vpa) {
    selectedVpa = vpa;
    accountList?.querySelectorAll('.account-card').forEach(c => {
      c.classList.toggle('selected', c.dataset.vpa === vpa);
      c.setAttribute('aria-pressed', String(c.dataset.vpa === vpa));
    });
  }

  function setManualVpaMode(on, { focus = true } = {}) {
    manualVpaMode = on;
    vpaField.classList.toggle('hidden', !on);
    accountField.classList.toggle('hidden', on);
    // With no linked accounts there is nothing to go back to.
    btnCancelOtherVpa.classList.toggle('hidden', !on || getDeviceAccounts().length === 0);
    setLoginStatus('');
    if (on && focus) vpaInput.focus();
  }

  function renderDeviceAccounts() {
    const accounts = getDeviceAccounts();

    if (accounts.length === 0) {
      // Nothing bound to this device yet — go straight to typing a UPI ID.
      setManualVpaMode(true, { focus: false });
      return;
    }

    accountList.innerHTML = '';
    accounts.forEach(acc => {
      const name = acc.holder_name || acc.vpa;
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'account-card';
      card.dataset.vpa = acc.vpa;
      card.innerHTML = `
        <div class="account-avatar">${escapeHtml(name.charAt(0).toUpperCase())}</div>
        <div class="account-info">
          <div class="account-name">${escapeHtml(name)}</div>
          <div class="account-vpa">${escapeHtml(acc.vpa)}</div>
        </div>
        <div class="account-radio"></div>`;
      card.addEventListener('click', () => {
        selectAccount(acc.vpa);
        setLoginStatus('');
        loginPinInput?.focus();
      });
      accountList.appendChild(card);
    });

    setManualVpaMode(false, { focus: false });
    selectAccount(accounts[0].vpa);
  }

  // Account sheet — tapping the avatar used to sign out instantly with no
  // warning and no visible control anywhere. Now it opens a sheet that names
  // who is signed in and offers an explicit Sign out.
  const sheet = document.getElementById('account-sheet');

  function openAccountSheet() {
    if (!state.loggedIn) return;
    document.getElementById('sheet-name').textContent = state.user.name || state.user.vpa;
    document.getElementById('sheet-vpa').textContent = state.user.vpa;
    document.getElementById('sheet-initial').textContent =
      (state.user.name || state.user.vpa).charAt(0).toUpperCase();
    sheet.classList.add('open');
  }
  function closeAccountSheet() { sheet.classList.remove('open'); }

  document.getElementById('header-avatar')?.addEventListener('click', openAccountSheet);
  document.getElementById('sheet-backdrop')?.addEventListener('click', closeAccountSheet);
  document.getElementById('btn-sheet-close')?.addEventListener('click', closeAccountSheet);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sheet?.classList.contains('open')) closeAccountSheet();
  });

  document.getElementById('btn-sign-out')?.addEventListener('click', () => {
    closeAccountSheet();
    signOut();
    loginPinInput.value = '';
    renderDeviceAccounts();
    setLoginStatus('Signed out. This device still remembers your UPI ID.', 'info');
  });

  document.getElementById('btn-forget-device')?.addEventListener('click', () => {
    const vpa = state.user.vpa;
    closeAccountSheet();
    forgetDeviceAccount(vpa);
    signOut();
    loginPinInput.value = '';
    renderDeviceAccounts();
    setLoginStatus(`${vpa} removed from this device.`, 'info');
  });

  btnUseOtherVpa?.addEventListener('click', () => setManualVpaMode(true));
  btnCancelOtherVpa?.addEventListener('click', () => {
    vpaInput.value = '';
    setManualVpaMode(false);
    renderDeviceAccounts();
  });
  vpaInput?.addEventListener('input', () => setLoginStatus(''));
  vpaInput?.addEventListener('keydown', (e) => { if (e.key === 'Enter') loginPinBtn?.click(); });

  renderDeviceAccounts();

  // --- Onboarding Flow Wiring ---
  const onboardState = {
    step: 1,
    name: '',
    identifier: '',
    selectedBank: null,
    chosenVpa: '',
    pin: '1234',
  };

  const btnStartOnboard = document.getElementById('btn-start-onboarding');
  const btnOnboardBack = document.getElementById('btn-onboard-back');

  function setOnboardStep(stepNum) {
    onboardState.step = stepNum;
    for (let i = 1; i <= 4; i++) {
      const dot = document.getElementById(`step-dot-${i}`);
      const line = document.getElementById(`step-line-${i}`);
      if (dot) {
        dot.classList.toggle('active', i === stepNum);
        dot.classList.toggle('completed', i < stepNum);
      }
      if (line) {
        line.classList.toggle('completed', i < stepNum);
      }
    }
    document.querySelectorAll('.onboard-step-pane').forEach(p => p.classList.remove('active'));
    const activePane = document.getElementById(`pane-step-${stepNum}`);
    if (activePane) activePane.classList.add('active');
  }

  if (btnStartOnboard) {
    btnStartOnboard.addEventListener('click', () => {
      setOnboardStep(1);
      showScreen('onboarding');
    });
  }

  if (btnOnboardBack) {
    btnOnboardBack.addEventListener('click', () => {
      if (onboardState.step > 1) {
        setOnboardStep(onboardState.step - 1);
      } else {
        showScreen('login');
      }
    });
  }

  // Step 1: Device Binding
  const btnVerifyBinding = document.getElementById('btn-onboard-verify-binding');
  if (btnVerifyBinding) {
    btnVerifyBinding.addEventListener('click', async () => {
      const nameInput = document.getElementById('onboard-name');
      const idInput = document.getElementById('onboard-identifier');
      const statusDiv = document.getElementById('binding-status');

      const name = nameInput.value.trim();
      const identifier = idInput.value.trim();
      if (!name) return alert('Please enter your full name');
      if (!identifier) return alert('Please enter your phone number or email');

      onboardState.name = name;
      onboardState.identifier = identifier;

      statusDiv.innerHTML = '<span style="color:var(--blue);">📡 Sending encrypted SIM / SMS binding packet…</span>';
      btnVerifyBinding.disabled = true;

      setTimeout(async () => {
        statusDiv.innerHTML = '<span style="color:var(--green);">✓ Device Bound! Querying NPCI Central Registry…</span>';
        try {
          const res = await fetch(`${API.switch}/npci/discover-accounts?identifier=${encodeURIComponent(identifier)}`);
          if (!res.ok) throw new Error('Failed to query NPCI');
          const data = await res.json();
          renderDiscoveredBanks(data.accounts || []);
          setTimeout(() => {
            btnVerifyBinding.disabled = false;
            statusDiv.textContent = '';
            setOnboardStep(2);
          }, 600);
        } catch (e) {
          statusDiv.innerHTML = `<span style="color:var(--red);">Failed: ${e.message}</span>`;
          btnVerifyBinding.disabled = false;
        }
      }, 900);
    });
  }

  // Step 2: Render Discovered Banks
  function renderDiscoveredBanks(accounts) {
    const list = document.getElementById('discovered-banks-list');
    const chooseBtn = document.getElementById('btn-onboard-choose-bank');
    list.innerHTML = '';

    accounts.forEach((acc, idx) => {
      const card = document.createElement('div');
      card.className = `discovered-bank-card ${idx === 0 ? 'selected' : ''}`;
      card.innerHTML = `
        <div class="bank-avatar">${acc.icon || '🏦'}</div>
        <div class="bank-info">
          <div class="bank-name">${acc.bank_name}</div>
          <div class="bank-meta">${acc.account_type} • A/C ${acc.account_mask}</div>
          <div class="bank-meta" style="font-size:11px;color:var(--text-3);">IFSC: ${acc.ifsc} • ${acc.branch}</div>
          <div class="bank-balance-tag">Available: ₹${formatAmount(acc.default_balance)}</div>
        </div>
        <div class="bank-check">✓</div>
      `;
      card.addEventListener('click', () => {
        document.querySelectorAll('.discovered-bank-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        onboardState.selectedBank = acc;
        chooseBtn.disabled = false;
      });
      list.appendChild(card);
    });

    onboardState.selectedBank = accounts[0] || null;
    chooseBtn.disabled = !onboardState.selectedBank;
  }

  const btnChooseBank = document.getElementById('btn-onboard-choose-bank');
  if (btnChooseBank) {
    btnChooseBank.addEventListener('click', () => {
      if (!onboardState.selectedBank) return alert('Please select a bank account');
      setupVpaStep();
      setOnboardStep(3);
    });
  }

  // Step 3: VPA Setup & Suggestions
  function setupVpaStep() {
    const vpaSuffix = onboardState.selectedBank?.vpa_suffix || '@hdfcbank';
    document.getElementById('onboard-vpa-suffix').textContent = vpaSuffix;

    const rawName = (onboardState.name || 'user').toLowerCase().replace(/[^a-z0-9]/g, '');
    const cleanDigits = (onboardState.identifier || '').replace(/\D/g, '');
    const phoneSuffix = cleanDigits.slice(-6);

    const prefixInput = document.getElementById('onboard-vpa-prefix');
    prefixInput.value = rawName;

    const suggestions = [
      rawName,
      `${rawName}.${phoneSuffix || '12'}`,
      `${rawName}${cleanDigits.slice(-4) || '99'}`,
      cleanDigits ? cleanDigits.slice(-10) : `${rawName}.upi`,
    ].filter(Boolean);

    const container = document.getElementById('vpa-suggestions');
    container.innerHTML = '';
    suggestions.forEach(s => {
      const chip = document.createElement('span');
      chip.className = 'vpa-chip';
      chip.textContent = `${s}${vpaSuffix}`;
      chip.addEventListener('click', () => {
        prefixInput.value = s;
      });
      container.appendChild(chip);
    });
  }

  const btnConfirmVpa = document.getElementById('btn-onboard-confirm-vpa');
  if (btnConfirmVpa) {
    btnConfirmVpa.addEventListener('click', () => {
      const prefix = document.getElementById('onboard-vpa-prefix').value.trim().toLowerCase();
      if (!prefix) return alert('Please enter a username for your UPI ID');
      const suffix = document.getElementById('onboard-vpa-suffix').textContent;
      onboardState.chosenVpa = `${prefix}${suffix}`;
      setOnboardStep(4);
    });
  }

  // Step 4: MPIN & Biometric Setup
  const btnFinishOnboard = document.getElementById('btn-onboard-finish');
  if (btnFinishOnboard) {
    btnFinishOnboard.addEventListener('click', async () => {
      const pinInput = document.getElementById('onboard-pin');
      const pin = pinInput.value.trim();
      const statusDiv = document.getElementById('onboard-final-status');

      if (!pin || pin.length < 4 || !pin.split('').every(c => c >= '0' && c <= '9')) {
        return alert('Please enter a valid 4-digit numeric UPI PIN');
      }
      onboardState.pin = pin;

      statusDiv.innerHTML = '<span style="color:var(--blue);">Linking account in HDFC Bank & NPCI…</span>';
      btnFinishOnboard.disabled = true;

      try {
        // 1. Register account in Payer Bank
        const regRes = await fetch(`${API.payerBank}/accounts/register`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            vpa: onboardState.chosenVpa,
            holder_name: onboardState.name,
            balance: 50000.0,
            pin: pin,
            bank_name: onboardState.selectedBank?.bank_name || 'HDFC Bank',
          }),
        });

        if (!regRes.ok) {
          const err = await regRes.json();
          throw new Error(err.detail || 'Failed to create bank account');
        }

        statusDiv.innerHTML = '<span style="color:var(--purple);">Prompting Face ID / Touch ID enrollment…</span>';

        // 2. Enroll Biometric (Passkey / WebAuthn)
        try {
          if (typeof enrollBiometric === 'function') {
            await enrollBiometric(onboardState.chosenVpa, `${onboardState.name}'s Device`);
          }
        } catch (bioErr) {
          console.warn('Biometric skipped or cancelled during onboarding:', bioErr);
        }

        statusDiv.innerHTML = '<span style="color:var(--green);">🎉 Account linked successfully! Opening dashboard…</span>';

        // 3. Bind the new account to this device, then finish login
        rememberDeviceAccount(onboardState.chosenVpa, onboardState.name, onboardState.identifier);
        renderDeviceAccounts();
        setTimeout(() => {
          btnFinishOnboard.disabled = false;
          finishLogin(onboardState.chosenVpa);
        }, 800);

      } catch (err) {
        statusDiv.innerHTML = `<span style="color:var(--red);">Error: ${err.message}</span>`;
        btnFinishOnboard.disabled = false;
      }
    });
  }

  // Show login screen first — app starts periodic tasks after successful login
  syncAuthChrome();
  showScreen('login');
});
