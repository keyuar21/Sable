/* ─────────────────────────────────────────────────────────────────────────────
   Voice biometrics client — enrollment + speaker verification for payments
   Records PCM WAV via Web Audio API, talks to Auth service /voice/* endpoints.
   ───────────────────────────────────────────────────────────────────────────── */

'use strict';

const VOICE_AUTH_API = 'http://localhost:5005';

const ENROLL_BASE_PHRASE = 'My voice is my payment PIN';
// Kept for backward-compat with app.js updateEnrollUI until challenge phrase is set
const ENROLL_PHRASES = [
  ENROLL_BASE_PHRASE,
  ENROLL_BASE_PHRASE,
  ENROLL_BASE_PHRASE,
];

async function requestVoiceChallenge(vpa, purpose = 'pay') {
  const res = await fetch(`${VOICE_AUTH_API}/voice/challenge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vpa, purpose }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || 'Could not start voice challenge');
  return body;
}

function isAffirmativeSpeech(text) {
  if (!text) return false;
  const t = text.toLowerCase().trim();
  const yes = [
    'yes', 'yeah', 'yep', 'confirm', 'confirmed', 'proceed', 'pay', 'make payment',
    'go ahead', 'ok', 'okay', 'sure', 'haan', 'ha', 'ji', 'do it', 'authorize',
    'authorise', 'approve', 'send', 'pay now',
  ];
  return yes.some(w => t === w || t.includes(w));
}

function isNegativeSpeech(text) {
  if (!text) return false;
  const t = text.toLowerCase().trim();
  return ['no', 'cancel', 'stop', 'nah', 'don\'t', 'do not', 'abort'].some(w => t.includes(w));
}

/** Prefer a cute / soft female TTS voice when the OS provides one. */
let _cachedCuteVoice = null;
let _voicesReady = false;
let _assistantAudio = null;
const LOCAL_TTS_API = 'http://localhost:5001/assistant/voice';

function _pickCuteGirlVoice() {
  if (_cachedCuteVoice) return _cachedCuteVoice;
  const voices = window.speechSynthesis?.getVoices?.() || [];
  if (!voices.length) return null;

  const prefer = [
    // macOS / iOS
    'Samantha', 'Ava', 'Nicky', 'Siri', 'Karen', 'Moira', 'Tessa', 'Fiona', 'Victoria', 'Kathy',
    // Google / Chrome
    'Google UK English Female', 'Google US English', 'Google हिन्दी',
    // Windows
    'Microsoft Zira', 'Microsoft Aria', 'Microsoft Jenny', 'Microsoft Neerja', 'Microsoft Sonia',
    // India-friendly
    'Raveena', 'Aditi', 'Neerja',
  ];

  const byName = (want) =>
    voices.find(v => v.name.toLowerCase() === want.toLowerCase()) ||
    voices.find(v => v.name.toLowerCase().includes(want.toLowerCase()));

  for (const name of prefer) {
    const hit = byName(name);
    if (hit) { _cachedCuteVoice = hit; return hit; }
  }

  // Heuristic: female / girl markers, prefer en-*
  const female = voices.filter(v =>
    /female|woman|girl|samantha|karen|zira|aria|jenny|neerja|raveena|aditi|moira|tessa|fiona/i.test(v.name)
  );
  const enFemale = female.find(v => /^en/i.test(v.lang)) || female[0];
  if (enFemale) { _cachedCuteVoice = enFemale; return enFemale; }

  const en = voices.find(v => /^en/i.test(v.lang) && !/male|david|mark|daniel|ravi|fred/i.test(v.name));
  _cachedCuteVoice = en || voices[0];
  return _cachedCuteVoice;
}

function _ensureVoicesLoaded() {
  return new Promise((resolve) => {
    const voices = window.speechSynthesis?.getVoices?.() || [];
    if (voices.length) { _voicesReady = true; resolve(voices); return; }
    if (!window.speechSynthesis) { resolve([]); return; }
    const done = () => {
      window.speechSynthesis.onvoiceschanged = null;
      _voicesReady = true;
      resolve(window.speechSynthesis.getVoices() || []);
    };
    window.speechSynthesis.onvoiceschanged = done;
    // Fallback if event never fires
    setTimeout(done, 400);
  });
}

/** Speak a prompt with a cute girl TTS voice (AI confirmation). */
async function speakPrompt(text) {
  // Piper gives every user the same default assistant voice. Browser TTS is
  // retained only as an offline fallback while Piper is being installed.
  try {
    if (_assistantAudio) {
      _assistantAudio.pause();
      URL.revokeObjectURL(_assistantAudio.src);
    }
    const response = await fetch(LOCAL_TTS_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (response.ok) {
      const audio = new Audio(URL.createObjectURL(await response.blob()));
      _assistantAudio = audio;
      await new Promise((resolve, reject) => {
        audio.onended = () => { URL.revokeObjectURL(audio.src); resolve(true); };
        audio.onerror = reject;
        audio.play().catch(reject);
      });
      return true;
    }
  } catch (err) {
    console.info('Local Piper voice unavailable; using browser voice.', err);
  }

  if (!window.speechSynthesis) return false;
  await _ensureVoicesLoaded();
  window.speechSynthesis.cancel();
  // Chrome can retain a paused or stale speech queue after a page reload.
  window.speechSynthesis.resume();

  return new Promise((resolve) => {
    const u = new SpeechSynthesisUtterance(text);
    const voice = _pickCuteGirlVoice();
    if (voice) {
      u.voice = voice;
      u.lang = voice.lang || 'en-US';
    } else {
      u.lang = 'en-US';
    }
    // Softer, brighter “cute” delivery
    u.rate = 0.96;
    u.pitch = 1.28;
    u.volume = 1.0;
    u.onend = () => resolve(true);
    u.onerror = () => {
      // A second resume handles the occasional Chrome queue interruption.
      try { window.speechSynthesis.resume(); } catch {}
      resolve(false);
    };
    window.speechSynthesis.speak(u);
  });
}

function stopSpeaking() {
  try { _assistantAudio?.pause(); } catch {}
  try { window.speechSynthesis?.cancel(); } catch {}
}

// Warm-load voices early (Chrome populates them async)
if (typeof window !== 'undefined' && window.speechSynthesis) {
  _ensureVoicesLoaded();
}

/** Encode Float32 mono PCM as 16-bit WAV ArrayBuffer. */
function encodeWav(samples, sampleRate) {
  const n = samples.length;
  const buffer = new ArrayBuffer(44 + n * 2);
  const view = new DataView(buffer);

  const writeStr = (off, str) => {
    for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
  };

  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + n * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);          // PCM
  view.setUint16(22, 1, true);          // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, n * 2, true);

  let offset = 44;
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }
  return buffer;
}

/**
 * Record microphone for `durationMs` and return a WAV Blob.
 * Also optionally runs SpeechRecognition in parallel (returns transcript).
 */
async function recordVoiceSample(durationMs = 4000, { withTranscript = false } = {}) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const ctx = new AudioCtx();
  if (ctx.state === 'suspended') await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  let capturing = false;

  let transcriptPromise = Promise.resolve('');
  let recognition = null;

  if (withTranscript) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      transcriptPromise = new Promise((resolve) => {
        recognition = new SR();
        recognition.lang = 'en-IN';
        recognition.continuous = true;
        recognition.interimResults = true;
        let finalText = '';
        recognition.onresult = (ev) => {
          let interim = '';
          for (let i = ev.resultIndex; i < ev.results.length; i++) {
            const t = ev.results[i][0].transcript;
            if (ev.results[i].isFinal) finalText += t + ' ';
            else interim += t;
          }
          if (typeof window.__onVoiceTranscript === 'function') {
            window.__onVoiceTranscript((finalText + interim).trim());
          }
        };
        recognition.onerror = () => resolve(finalText.trim());
        recognition.onend = () => resolve(finalText.trim());
        try { recognition.start(); } catch { resolve(''); }
      });
    }
  }

  source.connect(processor);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  processor.connect(mute);
  mute.connect(ctx.destination);
  processor.onaudioprocess = (e) => {
    if (!capturing) return;
    chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };

  // Warm up graph, then capture (avoids leading empty/garbled buffers)
  await new Promise((r) => setTimeout(r, 180));
  capturing = true;
  await new Promise((r) => setTimeout(r, durationMs));
  capturing = false;

  processor.disconnect();
  source.disconnect();
  mute.disconnect();
  stream.getTracks().forEach((t) => t.stop());
  try { if (recognition) recognition.stop(); } catch {}

  const sr = ctx.sampleRate || 48000;
  await ctx.close();

  const total = chunks.reduce((n, c) => n + c.length, 0);
  if (total < sr * 0.4) {
    throw new Error('Recording too short — hold the mic and speak for the full countdown.');
  }
  const merged = new Float32Array(total);
  let off = 0;
  for (const c of chunks) {
    merged.set(c, off);
    off += c.length;
  }

  // Client-side RMS check
  let sumSq = 0;
  for (let i = 0; i < merged.length; i += 8) sumSq += merged[i] * merged[i];
  const rms = Math.sqrt(sumSq / (merged.length / 8));
  if (rms < 0.008) {
    throw new Error('Too quiet — allow microphone access and speak clearly.');
  }

  const wav = encodeWav(merged, sr);
  const blob = new Blob([wav], { type: 'audio/wav' });
  const transcript = await Promise.race([
    transcriptPromise,
    new Promise((r) => setTimeout(() => r(''), 500)),
  ]);

  return { blob, transcript, durationMs };
}

async function fetchVoiceStatus(vpa) {
  const res = await fetch(`${VOICE_AUTH_API}/voice/status/${encodeURIComponent(vpa)}`);
  if (!res.ok) throw new Error((await res.json()).detail || 'Could not check voice status');
  return res.json();
}

async function enrollVoiceBiometric(vpa, wavBlobs, holderHint = '', { challengeId, spokenTexts = [] } = {}) {
  const fd = new FormData();
  fd.append('vpa', vpa);
  fd.append('holder_hint', holderHint);
  fd.append('challenge_id', challengeId);
  fd.append('spoken_texts', JSON.stringify(spokenTexts));
  wavBlobs.forEach((blob, i) => {
    fd.append('samples', blob, `enroll_${i + 1}.wav`);
  });

  const res = await fetch(`${VOICE_AUTH_API}/voice/enroll`, { method: 'POST', body: fd });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === 'string' ? detail : (detail?.detail || JSON.stringify(detail) || 'Enrollment failed'));
  }
  return body;
}

/**
 * Verify: random challenge + liveness + speaker match → step_up_token
 */
async function verifyVoiceBiometric(vpa, wavBlob, { challengeId, spokenText = '' } = {}) {
  const fd = new FormData();
  fd.append('vpa', vpa);
  fd.append('challenge_id', challengeId);
  fd.append('spoken_text', spokenText);
  fd.append('sample', wavBlob, 'verify.wav');

  const res = await fetch(`${VOICE_AUTH_API}/voice/verify`, { method: 'POST', body: fd });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail;
    const err = new Error(
      typeof detail === 'string'
        ? detail
        : (detail?.detail || detail?.message || 'Voice recognition failed')
    );
    err.voiceMismatch = res.status === 401;
    err.score = typeof detail === 'object' ? detail?.score : undefined;
    err.payload = detail;
    throw err;
  }
  return body;
}
