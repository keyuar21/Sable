/* ─────────────────────────────────────────────────────────────────────────────
   Voice UPI Simulator — Main Application Logic
   ───────────────────────────────────────────────────────────────────────────── */

'use strict';

// ── API base URLs ─────────────────────────────────────────────────────────────
const API = {
  psp:       'http://localhost:5001',
  switch:    'http://localhost:5002',
  payerBank: 'http://localhost:5003',
  payeeBank: 'http://localhost:5004',
  auth:      'http://localhost:5005',
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
};

// Start periodic tasks after a successful login
function startAfterLogin() {
  refreshBalance();

  // Health check architecture sidebar every 5s
  checkServiceHealth();
  setInterval(checkServiceHealth, 5_000);

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

// ── Screen navigation ─────────────────────────────────────────────────────────
function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const screen = document.getElementById(`screen-${name}`);
  if (screen) screen.classList.add('active');

  // Update bottom nav active state
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const navMap = { home: 'nav-home', history: 'nav-history', settlement: 'nav-settlement', voice: 'nav-voice' };
  const navId = navMap[name];
  if (navId) document.getElementById(navId)?.classList.add('active');
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

  // Amount: support "500", "1,000", "1.5k", "1 thousand"
  let amount = null;
  const amtMatch = text.match(/(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:k|thousand|lakh|rupees?|rs\.?)?/);
  if (amtMatch) {
    let raw = amtMatch[1].replace(/,/g, '');
    amount = parseFloat(raw);
    if (text.match(/\d+\s*k\b/)) amount *= 1000;
    if (text.match(/\d+\s*(lakh|lac)/)) amount *= 100000;
  }

  if (!amount || amount <= 0 || amount > 100000) return null;

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
      if (intent) {
        stopVoiceInput();
        showConfirm(intent.merchant, intent.amount);
      } else {
        statusText.textContent = 'Could not understand';
        hint.textContent = 'Be specific: "Pay 500 to Swiggy"';
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

  if (intent) {
    input.value = '';
    stopVoiceInput();
    showConfirm(intent.merchant, intent.amount);
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

/** Voice = payment PIN: challenge code + live speaker match → pay; else fail. */
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
      `Voice ID setup. Say: my voice is my payment PIN, then code ${challenge.digits.split('').join(' ')}`
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
    step < 3
      ? `Sample ${step + 1} of 3 — say the phrase + code ${code} (live mic only)`
      : 'Uploading voiceprint + anti-spoof checks…';

  document.querySelectorAll('.voice-enroll-step').forEach((el) => {
    const i = Number(el.dataset.step);
    el.classList.toggle('active', i === step);
    el.classList.toggle('done', i < step);
  });

  const btn = document.getElementById('btn-voice-enroll-record');
  if (btn) {
    btn.disabled = state.voiceEnroll.recording || step >= 3 || !state.voiceEnroll.challengeId;
    btn.textContent = step >= 3 ? 'Saving…' : `🎤 Record sample ${step + 1} of 3`;
  }
}

async function recordEnrollSample() {
  if (state.voiceEnroll.recording || state.voiceEnroll.step >= 3) return;
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

    if (state.voiceEnroll.step >= 3) {
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
    if (state.voiceEnroll.step < 3) updateEnrollUI();
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

      const terminal = ['success', 'settled', 'failed', 'timeout', 'partial_failure'];
      if (terminal.includes(data.status)) {
        clearInterval(state.pollInterval);

        if (data.status === 'success' || data.status === 'settled') {
          await refreshBalance();
          setTimeout(() => showSuccess(data), 600);
        } else {
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
    partial_failure: 'Partial failure — reversal initiated',
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
    if (filter !== 'all') txns = txns.filter(t => t.status === filter || (filter === 'success' && t.status === 'settled'));

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
  icon.className = `txn-icon ${['success','settled','debited','credited'].includes(t.status) ? 'debit' : t.status === 'failed' ? 'debit' : 'pending'}`;
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

// ── Architecture panel health check ──────────────────────────────────────────
async function checkServiceHealth() {
  const services = [
    { id: 'arch-psp',    url: `${API.psp}/health`       },
    { id: 'arch-switch', url: `${API.switch}/health`    },
    { id: 'arch-payer',  url: `${API.payerBank}/health` },
    { id: 'arch-payee',  url: `${API.payeeBank}/health` },
  ];

  await Promise.allSettled(services.map(async ({ id, url }) => {
    const el = document.getElementById(id);
    if (!el) return;
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(1500) });
      el.classList.toggle('alive', res.ok);
      el.classList.toggle('dead',  !res.ok);
    } catch {
      el.classList.remove('alive');
      el.classList.add('dead');
    }
  }));

  // Check Supabase DB status
  try {
    const sbRes = await fetch(`${API.psp}/supabase-status`, { signal: AbortSignal.timeout(2000) });
    const el = document.getElementById('arch-supabase');
    const textEl = document.getElementById('supabase-status-text');
    if (sbRes.ok && el && textEl) {
      const data = await sbRes.json();
      if (data.health && data.health.tables_ready) {
        el.className = 'arch-service alive';
        textEl.textContent = 'PostgreSQL Tables Live ✓';
      } else if (data.health && data.health.status === 'schema_pending') {
        el.className = 'arch-service pending';
        textEl.innerHTML = '<span style="color:var(--amber);">Tables pending (Run schema.sql)</span>';
      } else {
        el.className = 'arch-service dead';
        textEl.textContent = 'Supabase unreachable';
      }
    }
  } catch {
    const el = document.getElementById('arch-supabase');
    if (el) el.className = 'arch-service dead';
  }
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
    partial_failure: '⚠️ Partial failure',
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
  document.getElementById('btn-qr-demo').addEventListener('click', () => {
    // Simulate QR scan — pick a random merchant
    const merchants = Object.values(MERCHANTS);
    const m = merchants[Math.floor(Math.random() * merchants.length)];
    const amt = parseFloat(prompt(`QR scanned: ${m.name}\nEnter amount (₹):`, '100') || '0');
    if (amt > 0) showConfirm(m, amt);
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

    // Confirm / pay now — voice biometric is the payment PIN (no PIN/WebAuthn bypass)
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
  const loginSelect = document.getElementById('login-select');
  const loginPinInput = document.getElementById('login-pin');
  const loginPinBtn = document.getElementById('login-pin-btn');
  const loginBioBtn = document.getElementById('login-biometric-btn');
  const loginEnrollBtn = document.getElementById('login-enroll-btn');

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

    const avatar = document.getElementById('header-avatar');
    if (avatar) {
      avatar.textContent = (state.user.name || vpa).charAt(0).toUpperCase();
      avatar.title = state.user.vpa;
    }

    // Most important gate: voice biometric must exist for this user
    const ok = await ensureVoiceEnrolledOrSetup(vpa);
    if (ok) {
      startAfterLogin();
      showScreen('home');
    }
  }

  if (loginPinBtn) {
    loginPinBtn.addEventListener('click', async () => {
      const vpa = loginSelect.value;
      const pin = loginPinInput.value.trim();
      if (!pin) return alert('Enter PIN');
      try {
        await authenticateWithPin(vpa, pin);
        finishLogin(vpa);
      } catch (e) {
        alert('PIN login failed: ' + (e.message || e));
      }
    });
  }

  if (loginBioBtn) {
    loginBioBtn.addEventListener('click', async () => {
      const vpa = loginSelect.value;
      try {
        await authenticateWithBiometric(vpa);
        finishLogin(vpa);
      } catch (e) {
        alert('Biometric login failed: ' + (e.message || e));
      }
    });
  }

  if (loginEnrollBtn) {
    loginEnrollBtn.addEventListener('click', async () => {
      const vpa = loginSelect.value;
      const label = prompt('Label for this device (e.g. "My iPhone")', 'My Device');
      try {
        const res = await enrollBiometric(vpa, label || 'Unnamed device');
        alert(res.message || 'Enrolled');
      } catch (e) {
        alert('Enrollment failed: ' + (e.message || e));
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

  // --- Dynamic accounts loader ---
  async function loadAccountsList(selectVpa = null) {
    const select = document.getElementById('login-select');
    if (!select) return;
    try {
      const res = await fetch(`${API.payerBank}/accounts`);
      if (res.ok) {
        const accounts = await res.json();
        if (accounts && accounts.length > 0) {
          select.innerHTML = '';
          accounts.forEach(acc => {
            const opt = document.createElement('option');
            opt.value = acc.vpa;
            opt.textContent = `${acc.vpa} — ${acc.holder_name || acc.vpa}`;
            select.appendChild(opt);
          });
          if (selectVpa) {
            select.value = selectVpa;
          }
        }
      }
    } catch (e) {
      console.warn('Could not load dynamic accounts list:', e);
    }
  }
  loadAccountsList();

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

        // 3. Reload account select and finish login
        await loadAccountsList(onboardState.chosenVpa);
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
  showScreen('login');
});
