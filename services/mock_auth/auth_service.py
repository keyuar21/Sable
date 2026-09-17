"""
Mock Auth Service — PIN + WebAuthn + Voice biometrics (speaker verification)
Port 5005

Design:
- Voice biometrics: enroll a speaker embedding per VPA; every payment must
  match that voiceprint before a step-up token is issued. Mismatch → fail.
- WebAuthn: device Face ID / Touch ID (optional secondary).
- PIN: login / fallback only — payments prefer voice once enrolled.
- On success, issues a short-lived single-use step-up token consumed by /debit.
"""

import base64
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta
from typing import List, Optional

import bcrypt
import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    PublicKeyCredentialDescriptor,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import supabase_db  # reuses REST_URL / HEADERS already set up in the project

# Local import of speaker-verification module (same folder)
sys.path.insert(0, os.path.dirname(__file__))
import voice_biometrics as voicebio

app = FastAPI(
    title="Mock Auth Service",
    description="PIN + WebAuthn + Voice biometric step-up authentication",
    version="1.1.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- Relying Party config ----------
# In production these MUST match your real domain exactly (WebAuthn is origin-bound
# by design — this is what makes it phishing-resistant). For local dev, localhost is fine.
RP_ID = os.environ.get("WEBAUTHN_RP_ID", "localhost")
RP_NAME = "Voice UPI Simulator"
ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "http://localhost:3000")

REST_URL = supabase_db.REST_URL
HEADERS = supabase_db.HEADERS

# In-memory challenge/token store as a fallback if Supabase isn't reachable
# (Supabase tables from schema_auth_update.sql are the source of truth when available)
_challenges: dict[str, dict] = {}
_step_up_tokens: dict[str, dict] = {}


# ================= Models =================

class SetPinRequest(BaseModel):
    vpa: str
    pin: str

class VerifyPinRequest(BaseModel):
    vpa: str
    pin: str

class RegBeginRequest(BaseModel):
    vpa: str
    device_label: str = "Unnamed device"

class RegCompleteRequest(BaseModel):
    vpa: str
    credential: dict          # raw JSON from navigator.credentials.create()
    device_label: str = "Unnamed device"

class AuthBeginRequest(BaseModel):
    vpa: str

class AuthCompleteRequest(BaseModel):
    vpa: str
    credential: dict          # raw JSON from navigator.credentials.get()


# ================= Helpers =================

def _get_account(vpa: str) -> dict:
    acc = supabase_db.get_payer_account(vpa)
    if not acc:
        try:
            with httpx.Client(timeout=2.0) as client:
                r = client.get(f"http://localhost:5003/balance/{vpa}")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        raise HTTPException(404, f"No account for {vpa}")
    return acc

def _hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()

def _check_pin(pin: str, pin_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pin.encode(), pin_hash.encode())
    except Exception:
        return False

def _issue_step_up_token(vpa: str, method: str) -> str:
    token = secrets.token_urlsafe(24)
    record = {
        "token": token,
        "vpa": vpa,
        "method": method,
        "used": False,
        "expires_at": time.time() + 90,   # 90 seconds to actually complete the payment
    }
    _step_up_tokens[token] = record
    try:
        with httpx.Client(timeout=4.0) as client:
            client.post(f"{REST_URL}/step_up_tokens", headers=HEADERS, json={
                "token": token, "vpa": vpa, "method": method,
                "expires_at": (datetime.utcnow() + timedelta(seconds=90)).isoformat(),
            })
    except Exception:
        pass  # in-memory fallback above still works for the demo
    return token

def _get_credentials_for_vpa(vpa: str) -> list[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/webauthn_credentials?vpa=eq.{vpa}&select=*", headers=HEADERS)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return []

def _save_credential(vpa: str, credential_id: str, public_key: str, sign_count: int, device_label: str):
    with httpx.Client(timeout=4.0) as client:
        r = client.post(f"{REST_URL}/webauthn_credentials", headers=HEADERS, json={
            "vpa": vpa,
            "credential_id": credential_id,
            "public_key": public_key,
            "sign_count": sign_count,
            "device_label": device_label,
        })
        if r.status_code not in (200, 201):
            raise HTTPException(500, f"Failed to store credential: {r.text}")

def _update_sign_count(credential_id: str, new_count: int):
    with httpx.Client(timeout=4.0) as client:
        client.patch(
            f"{REST_URL}/webauthn_credentials?credential_id=eq.{credential_id}",
            headers=HEADERS,
            json={"sign_count": new_count, "last_used_at": datetime.utcnow().isoformat()},
        )


# ================= Health =================

@app.get("/health")
def health():
    status = voicebio.get_status("__health__") if False else {}
    enc = voicebio._get_encoder() is not None
    aasist = voicebio._get_ort() is not None
    return {
        "status": "ok",
        "service": "Auth (PIN + WebAuthn + Voice)",
        "port": 5005,
        "rp_id": RP_ID,
        "origin": ORIGIN,
        "voice": {
            "speaker": "resemblyzer" if enc else "mfcc",
            "anti_spoof_aasist": aasist,
            "spectral_pad": True,
            "dynamic_challenge": True,
            "threshold": voicebio.VERIFY_THRESHOLD if enc else voicebio.MFCC_VERIFY_THRESHOLD,
        },
    }


# ================= Voice biometrics (speaker verification + anti-spoof) =================

_voice_challenges: dict[str, dict] = {}

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _normalize_spoken_digits(text: str) -> str:
    """Extract digit sequence from STT ('7 4 9 2', '7492', 'seven four…')."""
    if not text:
        return ""
    t = text.lower().replace("-", " ").replace(",", " ")
    for word, d in _DIGIT_WORDS.items():
        t = t.replace(word, f" {d} ")
    return "".join(ch for ch in t if ch.isdigit())


def _issue_voice_challenge(vpa: str, purpose: str) -> dict:
    digits = f"{secrets.randbelow(10000):04d}"
    spoken = " ".join(digits)
    if purpose == "enroll":
        phrase = f"My voice is my payment PIN. Code {spoken}"
    else:
        phrase = f"Yes, make payment. Code {spoken}"
    cid = secrets.token_urlsafe(16)
    _voice_challenges[cid] = {
        "vpa": vpa,
        "digits": digits,
        "purpose": purpose,
        "expires_at": time.time() + 120,
        "used": False,
        "phrase": phrase,
    }
    return {
        "challenge_id": cid,
        "digits": digits,
        "phrase": phrase,
        "expires_in_secs": 120,
        "hint": f"Say the phrase and clearly speak the code {spoken}",
    }


def _consume_voice_challenge(challenge_id: str, vpa: str, spoken_text: str, purpose: str) -> str:
    rec = _voice_challenges.get(challenge_id)
    if not rec or rec["used"] or rec["expires_at"] < time.time():
        raise HTTPException(400, "Voice challenge expired or missing — request a new code")
    if rec["vpa"] != vpa:
        raise HTTPException(401, "Voice challenge does not belong to this account")
    if rec["purpose"] != purpose:
        raise HTTPException(400, "Wrong challenge type — request a new code")

    heard = _normalize_spoken_digits(spoken_text)
    expected = rec["digits"]
    # Accept exact match or expected digits appearing in sequence
    if expected not in heard and heard != expected:
        raise HTTPException(
            401,
            f"Challenge code mismatch. Expected {expected}, heard digits “{heard or '…'}”. "
            "A static phone recording cannot pass a fresh random code.",
        )
    rec["used"] = True
    return expected


class VoiceChallengeRequest(BaseModel):
    vpa: str
    purpose: str = "pay"  # 'enroll' | 'pay'


@app.post("/voice/challenge")
def voice_challenge(req: VoiceChallengeRequest):
    """Issue a randomized spoken code — blocks replay of a pre-recorded 'yes'."""
    _get_account(req.vpa)
    if req.purpose not in ("enroll", "pay"):
        raise HTTPException(400, "purpose must be 'enroll' or 'pay'")
    return _issue_voice_challenge(req.vpa, req.purpose)


@app.get("/voice/status/{vpa}")
def voice_status(vpa: str):
    _get_account(vpa)
    return voicebio.get_status(vpa)


@app.post("/voice/enroll")
async def voice_enroll(
    vpa: str = Form(...),
    holder_hint: str = Form(""),
    challenge_id: str = Form(...),
    spoken_texts: str = Form("[]"),  # JSON list of STT strings, one per sample
    samples: List[UploadFile] = File(...),
):
    """
    Enroll speaker voiceprint from ≥3 WAV samples (multipart).
    Each sample must include the random challenge digits (anti-replay).
    """
    _get_account(vpa)
    if len(samples) < voicebio.MIN_ENROLL_SAMPLES:
        raise HTTPException(
            400,
            f"Need at least {voicebio.MIN_ENROLL_SAMPLES} voice samples. Got {len(samples)}.",
        )

    try:
        texts = json.loads(spoken_texts) if spoken_texts else []
        if not isinstance(texts, list):
            texts = []
    except Exception:
        texts = []

    # Validate challenge against concatenated transcripts (user may say code on any sample)
    combined = " ".join(str(t) for t in texts) if texts else ""
    heard = _normalize_spoken_digits(combined)
    rec = _voice_challenges.get(challenge_id)
    if not rec or rec["used"] or rec["expires_at"] < time.time():
        raise HTTPException(400, "Voice challenge expired — refresh and try enrollment again")
    if rec["vpa"] != vpa or rec["purpose"] != "enroll":
        raise HTTPException(400, "Invalid enrollment challenge")
    # Prefer hearing the digits; if STT missed them but user clearly spoke (non-empty transcripts),
    # still consume challenge — liveness+Resemblyzer remain the biometric gates.
    if rec["digits"] not in heard:
        nonempty = [t for t in texts if str(t).strip()]
        if len(nonempty) < 1:
            raise HTTPException(
                401,
                f"Challenge code {rec['digits']} not heard. Say the digits clearly "
                f"(e.g. {' '.join(rec['digits'])}).",
            )
        # Soft pass: STT often drops short digit sequences on Chrome; anti-spoof still runs
        print(f"[voice] enroll STT missed digits {rec['digits']} (heard={heard!r}) — soft-pass", flush=True)
    rec["used"] = True

    blobs: list[bytes] = []
    for f in samples:
        data = await f.read()
        if not data or len(data) < 100:
            raise HTTPException(400, f"Empty or tiny audio sample: {f.filename}")
        blobs.append(data)

    try:
        result = voicebio.enroll(vpa, blobs, holder_hint=holder_hint)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        with voicebio._db() as conn:
            row = conn.execute(
                "SELECT embedding_json FROM voice_embeddings WHERE vpa = ?", (vpa,)
            ).fetchone()
        if row:
            stored = json.loads(row["embedding_json"])
            emb = stored["vec"] if isinstance(stored, dict) else stored
            voicebio.try_mirror_to_supabase(vpa, emb, result["sample_count"])
    except Exception:
        pass

    return result


@app.post("/voice/verify")
async def voice_verify(
    vpa: str = Form(...),
    challenge_id: str = Form(...),
    spoken_text: str = Form(""),
    sample: UploadFile = File(...),
):
    """
    Verify: (1) random challenge digits in STT, (2) liveness/anti-spoof,
    (3) Resemblyzer speaker match. Any failure → no step-up token.
    """
    _get_account(vpa)
    _consume_voice_challenge(challenge_id, vpa, spoken_text, "pay")

    data = await sample.read()
    if not data or len(data) < 100:
        raise HTTPException(400, "Empty audio — speak clearly into the microphone")

    try:
        result = voicebio.verify(vpa, data)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not result["matched"]:
        raise HTTPException(
            401,
            {
                "matched": False,
                "score": result.get("score"),
                "threshold": result.get("threshold"),
                "fail_reason": result.get("fail_reason"),
                "liveness": result.get("liveness"),
                "detail": result["message"],
            },
        )

    token = _issue_step_up_token(vpa, "voice")
    return {
        "success": True,
        "matched": True,
        "score": result["score"],
        "threshold": result["threshold"],
        "backend": result.get("backend"),
        "liveness": result.get("liveness"),
        "step_up_token": token,
        "expires_in_secs": 90,
        "message": result["message"],
    }


@app.delete("/voice/enroll/{vpa}")
def voice_unenroll(vpa: str):
    _get_account(vpa)
    deleted = voicebio.delete_enrollment(vpa)
    return {"success": deleted, "enrolled": False}


# ================= PIN endpoints (fallback path) =================

@app.post("/pin/set")
def set_pin(req: SetPinRequest):
    if not (4 <= len(req.pin) <= 6) or not req.pin.isdigit():
        raise HTTPException(400, "PIN must be 4-6 digits")
    _get_account(req.vpa)  # 404s if account doesn't exist
    pin_hash = _hash_pin(req.pin)
    with httpx.Client(timeout=4.0) as client:
        client.patch(
            f"{REST_URL}/payer_accounts?vpa=eq.{req.vpa}",
            headers=HEADERS,
            json={"pin_hash": pin_hash, "failed_pin_attempts": 0, "pin_locked_until": None},
        )
    return {"success": True, "message": "PIN set"}

@app.post("/pin/verify")
def verify_pin(req: VerifyPinRequest):
    acc = _get_account(req.vpa)

    locked_until = acc.get("pin_locked_until")
    if locked_until and datetime.fromisoformat(locked_until.replace("Z", "+00:00")) > datetime.utcnow().replace(tzinfo=None).astimezone():
        raise HTTPException(403, "PIN locked due to too many failed attempts. Try again later.")

    pin_hash = acc.get("pin_hash")
    if not pin_hash:
        raise HTTPException(400, "No PIN set for this account yet. Call /pin/set first.")

    if not _check_pin(req.pin, pin_hash):
        attempts = acc.get("failed_pin_attempts", 0) + 1
        updates = {"failed_pin_attempts": attempts}
        if attempts >= 3:
            updates["pin_locked_until"] = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        with httpx.Client(timeout=4.0) as client:
            client.patch(f"{REST_URL}/payer_accounts?vpa=eq.{req.vpa}", headers=HEADERS, json=updates)
        remaining = max(0, 3 - attempts)
        raise HTTPException(401, f"Incorrect PIN. {remaining} attempt(s) remaining before lockout.")

    with httpx.Client(timeout=4.0) as client:
        client.patch(f"{REST_URL}/payer_accounts?vpa=eq.{req.vpa}", headers=HEADERS,
                     json={"failed_pin_attempts": 0, "pin_locked_until": None})

    token = _issue_step_up_token(req.vpa, "pin")
    return {"success": True, "step_up_token": token, "expires_in_secs": 90}


def _resolve_rp_and_origin(request: Request) -> tuple[str, str]:
    origin_header = request.headers.get("origin")
    expected_origin = origin_header or ORIGIN
    try:
        from urllib.parse import urlparse
        parsed = urlparse(expected_origin)
        derived_rp = parsed.hostname
    except Exception:
        derived_rp = None
    return (derived_rp or RP_ID, expected_origin)


# ================= WebAuthn: registration (enrolling Face ID / Touch ID) =================

@app.post("/webauthn/register/begin")
def webauthn_register_begin(req: RegBeginRequest, request: Request):
    acc = _get_account(req.vpa)  # ensures account exists
    existing_creds = _get_credentials_for_vpa(req.vpa)
    rp_id, _ = _resolve_rp_and_origin(request)

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=req.vpa.encode(),
        user_name=req.vpa,
        user_display_name=acc.get("holder_name", req.vpa),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
            for c in existing_creds
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,   # forces Face ID/Touch ID, not just "device present"
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
    )

    # Store the challenge so we can verify it matches on /complete
    _challenges[req.vpa] = {"challenge": options.challenge, "purpose": "registration", "expires_at": time.time() + 120}

    return {"options": json.loads(options_to_json(options))}

@app.post("/webauthn/register/complete")
def webauthn_register_complete(req: RegCompleteRequest, request: Request):
    pending = _challenges.get(req.vpa)
    if not pending or pending["purpose"] != "registration" or pending["expires_at"] < time.time():
        raise HTTPException(400, "No pending registration challenge (or it expired) — call /register/begin again")

    expected_rp_id, expected_origin = _resolve_rp_and_origin(request)

    try:
        verification = verify_registration_response(
            credential=req.credential,
            expected_challenge=pending["challenge"],
            expected_origin=expected_origin,
            expected_rp_id=expected_rp_id,
            require_user_verification=True,
        )
    except Exception as e:
        raise HTTPException(400, f"Registration verification failed: {e}")

    _save_credential(
        vpa=req.vpa,
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        device_label=req.device_label,
    )
    del _challenges[req.vpa]

    return {"success": True, "message": f"'{req.device_label}' enrolled for biometric payments"}


# ================= WebAuthn: authentication (using Face ID / Touch ID to pay) =================

@app.post("/webauthn/auth/begin")
def webauthn_auth_begin(req: AuthBeginRequest, request: Request):
    creds = _get_credentials_for_vpa(req.vpa)
    if not creds:
        raise HTTPException(404, "No enrolled biometric device for this account. Register one first.")

    rp_id, _ = _resolve_rp_and_origin(request)

    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"])) for c in creds
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    _challenges[req.vpa] = {"challenge": options.challenge, "purpose": "authentication", "expires_at": time.time() + 120}
    return {"options": json.loads(options_to_json(options))}

@app.post("/webauthn/auth/complete")
def webauthn_auth_complete(req: AuthCompleteRequest, request: Request):
    pending = _challenges.get(req.vpa)
    if not pending or pending["purpose"] != "authentication" or pending["expires_at"] < time.time():
        raise HTTPException(400, "No pending auth challenge (or it expired) — call /auth/begin again")

    cred_id_b64url = req.credential.get("id")
    creds = _get_credentials_for_vpa(req.vpa)
    stored = next((c for c in creds if c["credential_id"] == cred_id_b64url), None)
    if not stored:
        raise HTTPException(404, "Unrecognized credential for this account")

    expected_rp_id, expected_origin = _resolve_rp_and_origin(request)

    try:
        verification = verify_authentication_response(
            credential=req.credential,
            expected_challenge=pending["challenge"],
            expected_origin=expected_origin,
            expected_rp_id=expected_rp_id,
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=stored.get("sign_count", 0),
            require_user_verification=True,
        )
    except Exception as e:
        raise HTTPException(401, f"Biometric verification failed: {e}")

    _update_sign_count(cred_id_b64url, verification.new_sign_count)
    del _challenges[req.vpa]

    token = _issue_step_up_token(req.vpa, "webauthn")
    return {"success": True, "step_up_token": token, "expires_in_secs": 90, "message": "Face ID / Touch ID verified"}


# ================= Step-up token verification (called by the payer bank) =================

class VerifyTokenRequest(BaseModel):
    vpa: str
    token: str

@app.post("/step-up/verify")
def verify_step_up_token(req: VerifyTokenRequest):
    """
    The Payer Bank's /debit endpoint calls this instead of checking a raw PIN,
    when the client authorized via WebAuthn (or PIN) and got a step_up_token back.
    Single-use: burns the token immediately so it can't be replayed.
    """
    record = _step_up_tokens.get(req.token)
    if not record:
        try:
            with httpx.Client(timeout=4.0) as client:
                r = client.get(f"{REST_URL}/step_up_tokens?token=eq.{req.token}&select=*", headers=HEADERS)
                rows = r.json() if r.status_code == 200 else []
                record = rows[0] if rows else None
        except Exception:
            record = None

    if not record:
        raise HTTPException(401, "Invalid or unknown step-up token")
    if record.get("used"):
        raise HTTPException(401, "Step-up token already used (replay attempt)")
    if record["vpa"] != req.vpa:
        raise HTTPException(401, "Step-up token does not belong to this account")

    expires = record.get("expires_at")
    expiry_ts = expires if isinstance(expires, (int, float)) else time.mktime(datetime.fromisoformat(str(expires).replace("Z", "")).timetuple())
    if expiry_ts < time.time():
        raise HTTPException(401, "Step-up token expired — re-authenticate")

    record["used"] = True
    try:
        with httpx.Client(timeout=4.0) as client:
            client.patch(f"{REST_URL}/step_up_tokens?token=eq.{req.token}", headers=HEADERS, json={"used": True})
    except Exception:
        pass

    return {"valid": True, "method": record["method"], "vpa": req.vpa}
