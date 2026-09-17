/* ─────────────────────────────────────────────────────────────────────────────
   WebAuthn client — Face ID / Touch ID / Windows Hello enrollment + payment auth
   Talks to the mock_auth service (port 5005). Uses the browser's native
   navigator.credentials API — this file never sees or handles biometric data,
   only the base64url-encoded public-key ceremony objects the browser hands back.
   ───────────────────────────────────────────────────────────────────────────── */

'use strict';

const AUTH_API = 'http://localhost:5005';

// ---------- base64url <-> ArrayBuffer helpers (WebAuthn moves binary data) ----------

function base64urlToBuffer(base64url) {
  if (!base64url || typeof base64url !== 'string') return new ArrayBuffer(0);
  const padding = '='.repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const buffer = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) buffer[i] = raw.charCodeAt(i);
  return buffer.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = '';
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function isWebAuthnSupported() {
  return !!(window.PublicKeyCredential && navigator.credentials);
}

// ---------- Enrollment (one-time setup: "Add Face ID / Touch ID") ----------

async function enrollBiometric(vpa, deviceLabel) {
  if (!isWebAuthnSupported()) {
    throw new Error('This browser/device does not support WebAuthn (Face ID / Touch ID / Windows Hello).');
  }

  // 1. Ask the server for a registration challenge
  const beginRes = await fetch(`${AUTH_API}/webauthn/register/begin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vpa, device_label: deviceLabel }),
  });
  if (!beginRes.ok) throw new Error((await beginRes.json()).detail || 'Failed to start registration');
  let { options } = await beginRes.json();
  if (typeof options === 'string') {
    try {
      options = JSON.parse(options);
    } catch (err) {
      console.error('Failed to parse options string', err);
    }
  }

  // 2. Convert server's base64url fields into the ArrayBuffers navigator.credentials expects
  const publicKey = {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    user: { ...options.user, id: base64urlToBuffer(options.user?.id) },
    excludeCredentials: (options.excludeCredentials || []).map(c => ({
      ...c, id: base64urlToBuffer(c.id),
    })),
  };

  // 3. This is the actual OS prompt — Face ID scan / Touch ID / Windows Hello dialog
  let credential;
  try {
    credential = await navigator.credentials.create({ publicKey });
  } catch (err) {
    console.error('navigator.credentials.create failed', err);
    throw err;
  }
  if (!credential) {
    console.warn('navigator.credentials.create returned null (cancelled)');
    throw new Error('Biometric enrollment was cancelled');
  }

  // 4. Serialize the credential back to plain JSON for the server to verify
  const credentialJSON = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      attestationObject: bufferToBase64url(credential.response.attestationObject),
    },
  };

  // 4. POST the credential to the server for verification/persistence
  try {
    console.debug('Sending credential to server', { vpa, deviceLabel, credentialJSON });
    const completeRes = await fetch(`${AUTH_API}/webauthn/register/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vpa, credential: credentialJSON, device_label: deviceLabel }),
    });
    if (!completeRes.ok) {
      let bodyText;
      try { bodyText = await completeRes.text(); } catch (e) { bodyText = '<unreadable>'; }
      console.error('Registration complete failed', completeRes.status, bodyText);
      throw new Error((await completeRes.json()).detail || 'Registration verification failed');
    }
    return completeRes.json();
  } catch (err) {
    console.error('Error sending registration complete', err);
    throw err;
  }
}

// ---------- Authentication (per-payment: "Confirm with Face ID") ----------

async function authenticateWithBiometric(vpa) {
  if (!isWebAuthnSupported()) {
    throw new Error('WebAuthn not supported — falling back to PIN.');
  }

  const beginRes = await fetch(`${AUTH_API}/webauthn/auth/begin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vpa }),
  });
  if (!beginRes.ok) throw new Error((await beginRes.json()).detail || 'No biometric enrolled for this account');
  let { options } = await beginRes.json();
  if (typeof options === 'string') {
    try {
      options = JSON.parse(options);
    } catch (err) {
      console.error('Failed to parse options string', err);
    }
  }

  const publicKey = {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    allowCredentials: (options.allowCredentials || []).map(c => ({
      ...c, id: base64urlToBuffer(c.id),
    })),
  };

  // The actual Face ID / Touch ID prompt happens here
  const assertion = await navigator.credentials.get({ publicKey });
  if (!assertion) throw new Error('Biometric authentication was cancelled');

  const credentialJSON = {
    id: assertion.id,
    rawId: bufferToBase64url(assertion.rawId),
    type: assertion.type,
    response: {
      clientDataJSON: bufferToBase64url(assertion.response.clientDataJSON),
      authenticatorData: bufferToBase64url(assertion.response.authenticatorData),
      signature: bufferToBase64url(assertion.response.signature),
      userHandle: assertion.response.userHandle ? bufferToBase64url(assertion.response.userHandle) : null,
    },
  };

  const completeRes = await fetch(`${AUTH_API}/webauthn/auth/complete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vpa, credential: credentialJSON }),
  });
  if (!completeRes.ok) throw new Error((await completeRes.json()).detail || 'Biometric verification failed');

  const result = await completeRes.json();
  return result.step_up_token;   // pass this straight into the /debit call instead of a PIN
}

// ---------- PIN fallback (also issues a step-up token, same shape as biometric) ----------

async function authenticateWithPin(vpa, pin) {
  const res = await fetch(`${AUTH_API}/pin/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vpa, pin }),
  });
  if (!res.ok) throw new Error((await res.json()).detail || 'Incorrect PIN');
  const result = await res.json();
  return result.step_up_token;
}

// ---------- Example: wiring into the existing payment flow ----------
//
// Replace the old `fetch(API.psp + '/initiate', { ..., pin: state.pin })` call with:
//
//   let step_up_token;
//   try {
//     step_up_token = await authenticateWithBiometric(state.user.vpa);
//   } catch (e) {
//     // Face ID unavailable/cancelled → show your existing PIN screen instead
//     const pin = await promptForPin();
//     step_up_token = await authenticateWithPin(state.user.vpa, pin);
//   }
//
//   await fetch(`${API.psp}/initiate`, {
//     method: 'POST',
//     headers: { 'Content-Type': 'application/json' },
//     body: JSON.stringify({
//       payer_vpa: state.user.vpa,
//       payee_vpa: state.payment.payeeVpa,
//       amount: state.payment.amount,
//       step_up_token,          // instead of `pin: state.pin`
//     }),
//   });
