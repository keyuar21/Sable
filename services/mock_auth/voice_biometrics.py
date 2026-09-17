"""
Voice biometrics — speaker ID + anti-spoof (replay / deepfake) for Voice UPI.

Stack (all free / open-source):
  1. Resemblyzer GE2E  — neural speaker embeddings (who is speaking)
  2. AASIST ONNX       — ASVspoof anti-spoofing (live vs synthetic/spoof)
  3. Spectral PAD      — phone-speaker replay heuristics (sub-bass + flatness)
  4. Dynamic challenge — randomized digits (app layer; blocks static recordings)

Fallback: classic MFCC/LPC if Resemblyzer is unavailable.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import wave
from io import BytesIO
from typing import Optional

import numpy as np

VERIFY_THRESHOLD = 0.72          # Resemblyzer cosine (same speaker typically >0.75)
MFCC_VERIFY_THRESHOLD = 0.72
MIN_ENROLL_SAMPLES = 3
MIN_AUDIO_SECONDS = 0.5
ENROLL_PAIR_MIN_SIM = 0.35       # Resemblyzer pairs are more stable
TARGET_SR = 16000
AASIST_LEN = 64600               # ~4.0375 s @ 16 kHz
# Softmax P(bona fide); AASIST is strict on non-ASVspoof audio — combine with PAD
AASIST_LIVE_MIN = 0.08
AASIST_SPOOF_HARD = 0.02         # below this → hard reject as spoof/replay

MODELS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "models")
)
AASIST_ONNX = os.path.join(MODELS_DIR, "aasist.onnx")
DB_PATH = os.path.join(os.path.dirname(__file__), "voice_biometrics.db")

_encoder = None
_ort_session = None
_use_resemblyzer = None


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_embeddings (
            vpa TEXT PRIMARY KEY,
            holder_hint TEXT,
            embedding_json TEXT NOT NULL,
            samples_json TEXT NOT NULL,
            sample_count INT NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


# ── Audio I/O ─────────────────────────────────────────────────────────────────

def _decode_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    bio = BytesIO(data)
    try:
        with wave.open(bio, "rb") as wf:
            nch, sw, sr, nframes = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
            raw = wf.readframes(nframes)
        if sw == 1:
            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sw == 2:
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sw == 4:
            samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"Unsupported sample width: {sw}")
        if nch > 1:
            samples = samples.reshape(-1, nch).mean(axis=1)
        return samples, sr
    except Exception:
        pass
    if len(data) >= 4 and len(data) % 4 == 0:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float32)
        if samples.size:
            return samples, TARGET_SR
    raise ValueError("Could not decode audio — send 16-bit PCM WAV")


def _resample(x: np.ndarray, sr: int, target: int = TARGET_SR) -> np.ndarray:
    if sr == target or x.size == 0:
        return x.astype(np.float32)
    n_out = max(1, int(round(x.size / float(sr) * target)))
    t_old = np.linspace(0.0, 1.0, num=x.size, endpoint=False)
    t_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(t_new, t_old, x).astype(np.float32)


def _trim_silence(x: np.ndarray, sr: int = TARGET_SR, top_db: float = 28.0) -> np.ndarray:
    if x.size < sr // 10:
        return x
    hop, frame = 160, 400
    n = 1 + max(0, (x.size - frame) // hop)
    if n < 3:
        return x
    energies = np.array(
        [float(np.sqrt(np.mean(x[i * hop : i * hop + frame] ** 2) + 1e-12)) for i in range(n)],
        dtype=np.float32,
    )
    peak = float(np.max(energies) + 1e-12)
    thresh = peak * (10.0 ** (-top_db / 20.0))
    voiced = np.where(energies >= thresh)[0]
    if voiced.size == 0:
        return x
    start = max(0, int(voiced[0]) * hop - hop)
    end = min(x.size, int(voiced[-1]) * hop + frame + hop)
    trimmed = x[start:end]
    return trimmed if trimmed.size > sr // 5 else x


def load_mono_16k(audio_bytes: bytes) -> np.ndarray:
    samples, sr = _decode_wav_bytes(audio_bytes)
    samples = _resample(samples, sr, TARGET_SR)
    samples = _trim_silence(samples, TARGET_SR)
    duration = samples.size / float(TARGET_SR)
    if duration < MIN_AUDIO_SECONDS:
        raise ValueError(
            f"Not enough speech detected ({duration:.2f}s). Speak louder / closer to the mic."
        )
    rms = float(np.sqrt(np.mean(samples ** 2) + 1e-12))
    if rms < 0.01:
        raise ValueError("Audio too quiet — check microphone permissions and speak clearly.")
    peak = float(np.max(np.abs(samples)) + 1e-9)
    return (samples / peak).astype(np.float32)


# ── Spectral presentation-attack detection (replay / phone speaker) ───────────

def spectral_liveness_score(samples: np.ndarray, sr: int = TARGET_SR) -> dict:
    """
    Heuristic PAD: phone loudspeakers cut sub-bass and flatten high bands.
    Returns score in [0,1] (higher = more likely live close-talk mic).
    """
    # STFT
    n_fft, hop = 512, 160
    if samples.size < n_fft:
        samples = np.pad(samples, (0, n_fft - samples.size))
    window = np.hanning(n_fft).astype(np.float32)
    n_frames = 1 + (samples.size - n_fft) // hop
    specs = []
    for i in range(n_frames):
        frame = samples[i * hop : i * hop + n_fft] * window
        specs.append(np.abs(np.fft.rfft(frame)) ** 2)
    spec = np.stack(specs, axis=0) + 1e-12  # (T, F)
    freqs = np.linspace(0, sr / 2, spec.shape[1])

    total = float(np.sum(spec))
    sub_bass = float(np.sum(spec[:, freqs <= 80])) / total
    low = float(np.sum(spec[:, freqs <= 300])) / total
    high = float(np.sum(spec[:, freqs >= 4000])) / total

    # Spectral flatness (geometric / arithmetic) — replay tends flatter
    geo = np.exp(np.mean(np.log(spec), axis=1))
    arith = np.mean(spec, axis=1)
    flatness = float(np.mean(geo / (arith + 1e-12)))

    # Flux variance — live speech is more dynamic
    flux = np.sqrt(np.sum(np.diff(np.sqrt(spec), axis=0) ** 2, axis=1))
    flux_var = float(np.var(flux)) if flux.size else 0.0

    # Score: reward sub-bass + dynamics, penalize extreme flatness / missing lows
    score = 0.0
    score += 0.35 * min(1.0, sub_bass / 0.02)          # live close-talk usually has some LF
    score += 0.20 * min(1.0, low / 0.15)
    score += 0.15 * min(1.0, high / 0.05)               # air / fricatives
    score += 0.20 * min(1.0, flux_var / 0.5)
    score += 0.10 * max(0.0, 1.0 - flatness * 8.0)
    score = float(np.clip(score, 0.0, 1.0))

    return {
        "score": round(score, 4),
        "sub_bass_ratio": round(sub_bass, 5),
        "flatness": round(flatness, 5),
        "flux_var": round(flux_var, 5),
        "live": score >= 0.35,
    }


# ── AASIST ONNX anti-spoof ────────────────────────────────────────────────────

def _get_ort():
    global _ort_session
    if _ort_session is not None:
        return _ort_session
    if not os.path.isfile(AASIST_ONNX):
        return None
    try:
        import onnxruntime as ort
        _ort_session = ort.InferenceSession(
            AASIST_ONNX, providers=["CPUExecutionProvider"]
        )
        return _ort_session
    except Exception as e:
        print(f"[voice] AASIST ONNX unavailable: {e}", flush=True)
        return None


def aasist_liveness(samples: np.ndarray) -> dict:
    """
    AASIST (Jung et al.) via ONNX — logits[0]=spoof, logits[1]=bona fide.
    """
    sess = _get_ort()
    if sess is None:
        return {"available": False, "prob_live": None, "live": True, "message": "AASIST model missing"}

    y = samples.astype(np.float32)
    if y.size < AASIST_LEN:
        y = np.pad(y, (0, AASIST_LEN - y.size))
    else:
        # Prefer center crop of speech
        start = max(0, (y.size - AASIST_LEN) // 2)
        y = y[start : start + AASIST_LEN]

    logits = sess.run(None, {"wav": y[None, :]})[0][0]
    ex = np.exp(logits - np.max(logits))
    probs = ex / ex.sum()
    prob_spoof, prob_live = float(probs[0]), float(probs[1])
    return {
        "available": True,
        "prob_live": round(prob_live, 4),
        "prob_spoof": round(prob_spoof, 4),
        "logits": [round(float(logits[0]), 3), round(float(logits[1]), 3)],
        "live": prob_live >= AASIST_LIVE_MIN,
        "hard_spoof": prob_live < AASIST_SPOOF_HARD,
    }


def assert_live(samples: np.ndarray) -> dict:
    """
    Combined PAD against phone replay + deepfake.

    Policy (practical for browser mics; AASIST was trained on ASVspoof and can
    false-positive on domain-shifted laptop audio):
      • Spectral score < 0.25  → reject (classic phone-speaker footprint)
      • AASIST hard-spoof AND spectral < 0.50 → reject
      • Otherwise accept (dynamic challenge still blocks static recordings)
    """
    spectral = spectral_liveness_score(samples)
    aasist = aasist_liveness(samples)

    reasons = []
    if spectral["score"] < 0.25:
        reasons.append(
            f"Spectral PAD suggests phone-speaker replay (score {spectral['score']})"
        )
    if (
        aasist.get("available")
        and aasist.get("hard_spoof")
        and spectral["score"] < 0.50
    ):
        reasons.append(
            f"AASIST anti-spoof flagged fake audio (live_p={aasist.get('prob_live')}) "
            "with weak spectral liveness"
        )

    ok = len(reasons) == 0
    return {
        "live": ok,
        "spectral": spectral,
        "aasist": aasist,
        "message": "Live voice OK" if ok else "; ".join(reasons),
    }


# ── Speaker embeddings (Resemblyzer + MFCC fallback) ──────────────────────────

def _get_encoder():
    global _encoder, _use_resemblyzer
    if _use_resemblyzer is False:
        return None
    if _encoder is not None:
        return _encoder
    try:
        from resemblyzer import VoiceEncoder
        _encoder = VoiceEncoder()
        _use_resemblyzer = True
        print("[voice] Resemblyzer GE2E encoder ready", flush=True)
        return _encoder
    except Exception as e:
        print(f"[voice] Resemblyzer unavailable, MFCC fallback: {e}", flush=True)
        _use_resemblyzer = False
        return None


def _mfcc_embedding(samples: np.ndarray) -> np.ndarray:
    """Classic MFCC+LPC fallback (same spirit as earlier demo)."""
    from math import pi

    def preemphasis(x, coef=0.97):
        y = np.empty_like(x)
        y[0] = x[0]
        y[1:] = x[1:] - coef * x[:-1]
        return y

    x = preemphasis(samples)
    frame, hop, n_fft = 400, 160, 512
    if x.size < frame:
        x = np.pad(x, (0, frame - x.size))
    n = 1 + (x.size - frame) // hop
    win = np.hamming(frame).astype(np.float32)
    frames = np.stack([x[i * hop : i * hop + frame] * win for i in range(n)])
    specs = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2
    # mel filterbank
    n_mels = 40
    fmin, fmax = 50.0, TARGET_SR / 2
    def hz2mel(hz): return 2595 * np.log10(1 + np.asarray(hz) / 700)
    def mel2hz(m): return 700 * (10 ** (np.asarray(m) / 2595) - 1)
    mels = np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2)
    hz = mel2hz(mels)
    bins = np.floor((n_fft + 1) * hz / TARGET_SR).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        l, c, r = bins[i], bins[i + 1], bins[i + 2]
        c = max(c, l + 1); r = max(r, c + 1)
        for j in range(l, c):
            if 0 <= j < fb.shape[1]:
                fb[i, j] = (j - l) / (c - l)
        for j in range(c, r):
            if 0 <= j < fb.shape[1]:
                fb[i, j] = (r - j) / (r - c)
    mel = np.log(specs @ fb.T + 1e-10)
    n_mfcc = 20
    k = np.arange(n_mfcc)[:, None]
    n_idx = np.arange(n_mels)[None, :]
    basis = np.cos(pi / n_mels * (n_idx + 0.5) * k).astype(np.float32)
    mfcc = mel @ basis.T
    vec = np.concatenate([mfcc.mean(0), mfcc.std(0)]).astype(np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9)


def extract_embedding(audio_bytes: bytes) -> tuple[np.ndarray, dict]:
    samples = load_mono_16k(audio_bytes)
    meta = {"duration": samples.size / float(TARGET_SR), "backend": "mfcc"}

    # Pitch estimate for soft gate
    pitches = []
    frame, hop = 400, 320
    for i in range(0, max(1, samples.size - frame), hop):
        fr = samples[i : i + frame]
        corr = np.correlate(fr, fr, mode="full")[len(fr) - 1 :]
        lo, hi = int(TARGET_SR / 350), int(TARGET_SR / 70)
        seg = corr[lo:hi]
        if seg.size and corr[int(np.argmax(seg)) + lo] > 0.3 * corr[0]:
            pitches.append(TARGET_SR / (int(np.argmax(seg)) + lo))
    meta["pitch_mean"] = float(np.mean(pitches)) if pitches else 0.0

    enc = _get_encoder()
    if enc is not None:
        try:
            from resemblyzer import preprocess_wav
            wav = preprocess_wav(samples, source_sr=TARGET_SR)
            if wav.size < TARGET_SR // 4:
                raise ValueError("Too little speech after Resemblyzer preprocess")
            emb = enc.embed_utterance(wav).astype(np.float32)
            meta["backend"] = "resemblyzer"
            return emb / (np.linalg.norm(emb) + 1e-9), meta
        except Exception as e:
            meta["resemblyzer_error"] = str(e)

    return _mfcc_embedding(samples), meta


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ── Persistence / enroll / verify ─────────────────────────────────────────────

def is_enrolled(vpa: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT sample_count FROM voice_embeddings WHERE vpa = ?", (vpa,)
        ).fetchone()
    return bool(row and row["sample_count"] >= MIN_ENROLL_SAMPLES)


def get_status(vpa: str) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT sample_count, updated_at, created_at FROM voice_embeddings WHERE vpa = ?",
            (vpa,),
        ).fetchone()
    backend = "resemblyzer" if _get_encoder() else "mfcc"
    aasist_ok = _get_ort() is not None
    base = {
        "required_samples": MIN_ENROLL_SAMPLES,
        "threshold": VERIFY_THRESHOLD if backend == "resemblyzer" else MFCC_VERIFY_THRESHOLD,
        "speaker_backend": backend,
        "anti_spoof": {"aasist_onnx": aasist_ok, "spectral_pad": True},
    }
    if not row:
        return {**base, "enrolled": False, "sample_count": 0}
    return {
        **base,
        "enrolled": row["sample_count"] >= MIN_ENROLL_SAMPLES,
        "sample_count": row["sample_count"],
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
    }


def enroll(vpa: str, audio_blobs: list[bytes], holder_hint: str = "") -> dict:
    if len(audio_blobs) < MIN_ENROLL_SAMPLES:
        raise ValueError(f"Need at least {MIN_ENROLL_SAMPLES} voice samples for enrollment")

    sample_vecs, pitch_means, liveness_reports = [], [], []
    for i, blob in enumerate(audio_blobs):
        samples = load_mono_16k(blob)
        live = assert_live(samples)
        liveness_reports.append(live)
        if not live["live"]:
            raise ValueError(
                f"Sample {i + 1} failed liveness / anti-spoof check: {live['message']}. "
                "Use a live mic — phone playbacks and deepfakes are blocked."
            )
        vec, meta = extract_embedding(blob)
        sample_vecs.append(vec)
        if meta.get("pitch_mean"):
            pitch_means.append(meta["pitch_mean"])

    pair_scores = []
    for i in range(len(sample_vecs)):
        for j in range(i + 1, len(sample_vecs)):
            pair_scores.append(cosine_similarity(sample_vecs[i], sample_vecs[j]))
    worst = min(pair_scores) if pair_scores else 1.0
    min_sim = ENROLL_PAIR_MIN_SIM if (_use_resemblyzer is not False) else 0.10
    if worst < min_sim:
        raise ValueError(
            f"Enrollment samples look too different (lowest similarity {worst:.2f}). "
            "Retry in a quiet room with the same live voice each time."
        )

    mean_vec = np.mean(np.stack(sample_vecs, axis=0), axis=0)
    mean_vec = mean_vec / (np.linalg.norm(mean_vec) + 1e-9)
    enroll_pitch = float(np.mean(pitch_means)) if pitch_means else 0.0
    backend = "resemblyzer" if sample_vecs[0].size == 256 else "mfcc"

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO voice_embeddings (vpa, holder_hint, embedding_json, samples_json, sample_count, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(vpa) DO UPDATE SET
                holder_hint = excluded.holder_hint,
                embedding_json = excluded.embedding_json,
                samples_json = excluded.samples_json,
                sample_count = excluded.sample_count,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                vpa,
                holder_hint,
                json.dumps({
                    "vec": mean_vec.tolist(),
                    "pitch_mean": enroll_pitch,
                    "backend": backend,
                }),
                json.dumps([v.tolist() for v in sample_vecs]),
                len(sample_vecs),
            ),
        )
        conn.commit()

    return {
        "success": True,
        "enrolled": True,
        "sample_count": len(sample_vecs),
        "backend": backend,
        "anti_spoof": "aasist+spectral",
        "message": "Voice biometric enrolled with anti-spoof protection. "
                   "Only your live voice can authorize payments.",
    }


def verify(vpa: str, audio_bytes: bytes) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT embedding_json, samples_json, sample_count FROM voice_embeddings WHERE vpa = ?",
            (vpa,),
        ).fetchone()
    if not row or row["sample_count"] < MIN_ENROLL_SAMPLES:
        raise LookupError("No voice biometric enrolled for this account")

    samples = load_mono_16k(audio_bytes)
    live = assert_live(samples)
    if not live["live"]:
        return {
            "matched": False,
            "score": 0.0,
            "threshold": VERIFY_THRESHOLD,
            "liveness": live,
            "fail_reason": "liveness_failed",
            "message": f"Replay/fake voice blocked — {live['message']}",
        }

    probe, meta = extract_embedding(audio_bytes)
    stored = json.loads(row["embedding_json"])
    if isinstance(stored, list):
        mean_vec = np.asarray(stored, dtype=np.float32)
        enroll_pitch = 0.0
        backend = "legacy"
    else:
        mean_vec = np.asarray(stored["vec"], dtype=np.float32)
        enroll_pitch = float(stored.get("pitch_mean") or 0.0)
        backend = stored.get("backend", "unknown")

    if probe.size != mean_vec.size:
        raise ValueError(
            "Voice model upgraded since enrollment — please re-enroll Voice ID."
        )

    sample_vecs = [np.asarray(s, dtype=np.float32) for s in json.loads(row["samples_json"])]
    score_mean = cosine_similarity(probe, mean_vec)
    score_best = max(cosine_similarity(probe, s) for s in sample_vecs)
    score = 0.55 * score_mean + 0.45 * score_best

    thr = VERIFY_THRESHOLD if probe.size == 256 else MFCC_VERIFY_THRESHOLD

    pitch_ok = True
    probe_pitch = float(meta.get("pitch_mean") or 0.0)
    if enroll_pitch > 60 and probe_pitch > 60:
        ratio = probe_pitch / enroll_pitch
        pitch_ok = 0.70 <= ratio <= 1.40

    matched = score >= thr and pitch_ok
    reason = ""
    if not pitch_ok:
        reason = "pitch_mismatch"
    elif score < thr:
        reason = "embedding_mismatch"

    return {
        "matched": matched,
        "score": round(score, 4),
        "threshold": thr,
        "score_mean": round(score_mean, 4),
        "score_best": round(score_best, 4),
        "pitch_ok": pitch_ok,
        "probe_pitch": round(probe_pitch, 1),
        "enroll_pitch": round(enroll_pitch, 1),
        "backend": backend,
        "liveness": live,
        "fail_reason": reason,
        "message": (
            "Voice verified — payment authorized"
            if matched
            else "Voice does not match enrolled biometric — payment blocked"
        ),
    }


def delete_enrollment(vpa: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM voice_embeddings WHERE vpa = ?", (vpa,))
        conn.commit()
        return cur.rowcount > 0


def try_mirror_to_supabase(vpa: str, embedding: list[float], sample_count: int) -> Optional[str]:
    try:
        import httpx
        import sys
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        import supabase_db
        with httpx.Client(timeout=4.0) as client:
            client.delete(
                f"{supabase_db.REST_URL}/voice_embeddings?vpa=eq.{vpa}",
                headers=supabase_db.HEADERS,
            )
            r = client.post(
                f"{supabase_db.REST_URL}/voice_embeddings",
                headers=supabase_db.HEADERS,
                json={"vpa": vpa, "embedding": embedding, "sample_count": sample_count},
            )
            return "synced" if r.status_code in (200, 201) else f"skip:{r.status_code}"
    except Exception as e:
        return f"skip:{e}"
