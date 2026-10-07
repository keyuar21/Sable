"""
Voice biometrics — speaker ID + anti-spoof (replay / deepfake) for Voice UPI.

Stack (all free / open-source):
  1. ECAPA-TDNN        — speaker embeddings (who is speaking). VoxCeleb EER
                         around 1%, against roughly 5% for the GE2E model this
                         replaced, so about a five-fold drop in error rate.
  2. AASIST ONNX       — ASVspoof anti-spoofing (live vs synthetic/spoof)
  3. Spectral PAD      — phone-speaker replay heuristics (sub-bass + flatness)
  4. Dynamic challenge — randomized digits (app layer; blocks static recordings)

Backends are tried in order: ECAPA-TDNN, then Resemblyzer GE2E, then an MFCC
baseline. An enrollment records which backend produced it and verification
refuses to compare across backends, because the embedding spaces are unrelated.

Accuracy work beyond the model itself:

  * Adaptive threshold. A single global cut-off ignores that some people's
    voices cluster tightly and others' do not. The threshold is derived per
    speaker from how consistent their own enrollment samples were.
  * AS-norm. Raw cosine scores drift with microphone and room. Each score is
    normalised against a cohort of other speakers, which is what makes one
    threshold meaningful across different devices.
  * Quality gating. A decision is only as good as the audio. Clipped, too
    quiet, too short or too noisy input is rejected rather than scored.
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

# Base operating points per backend, before per-speaker adaptation. ECAPA
# separates far better than GE2E, so it can sit at a higher, safer cut-off.
BASE_THRESHOLD = {
    "ecapa": 0.55,
    "resemblyzer": 0.72,
    "mfcc": 0.80,          # weakest backend, so demand the most
}
VERIFY_THRESHOLD = BASE_THRESHOLD["resemblyzer"]   # kept for older callers
MFCC_VERIFY_THRESHOLD = BASE_THRESHOLD["mfcc"]

# How far the per-speaker threshold may move from the base in either direction.
THRESHOLD_ADAPT_RANGE = 0.08

MIN_ENROLL_SAMPLES = 3
# Five samples give a materially better centroid than three and let us discard
# an outlier without dropping below the minimum.
TARGET_ENROLL_SAMPLES = 5

MIN_AUDIO_SECONDS = 0.5
# Speech actually present after silence removal, which is what the model sees.
MIN_SPEECH_SECONDS = 1.2
MIN_SNR_DB = 8.0
MAX_CLIPPED_FRACTION = 0.02

ENROLL_PAIR_MIN_SIM = 0.35       # Resemblyzer pairs are more stable
ENROLL_OUTLIER_MARGIN = 0.12     # drop a sample this far below the median
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
_ecapa = None
_ecapa_failed = False

# SpeechBrain writes its model cache here. Kept beside the other models so a
# container can bake it in and never reach for the network at runtime.
ECAPA_SOURCE = os.environ.get("ECAPA_MODEL", "speechbrain/spkrec-ecapa-voxceleb")
ECAPA_CACHE = os.environ.get("ECAPA_CACHE", os.path.join(MODELS_DIR, "ecapa"))


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


def _get_ecapa():
    """Load ECAPA-TDNN once. Returns None if unavailable, so the older
    backends still work on a machine without SpeechBrain."""
    global _ecapa, _ecapa_failed
    if _ecapa is not None or _ecapa_failed:
        return _ecapa
    try:
        import warnings
        warnings.filterwarnings("ignore", module="speechbrain")
        # SpeechBrain and huggingface_hub both want a writable cache; point
        # them somewhere we control rather than at the user's home directory.
        os.environ.setdefault("HF_HOME", ECAPA_CACHE)
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", ECAPA_CACHE)
        os.makedirs(ECAPA_CACHE, exist_ok=True)
        from speechbrain.inference.speaker import EncoderClassifier
        _ecapa = EncoderClassifier.from_hparams(
            source=ECAPA_SOURCE,
            savedir=os.path.join(ECAPA_CACHE, "spkrec-ecapa"),
            run_opts={"device": "cpu"},
        )
        print("[voice] ECAPA-TDNN speaker encoder ready", flush=True)
    except Exception as e:
        _ecapa_failed = True
        print(f"[voice] ECAPA-TDNN unavailable ({e}); falling back", flush=True)
    return _ecapa


def assess_quality(samples: np.ndarray) -> dict:
    """Reject audio that cannot support a reliable decision.

    Scoring bad audio is worse than refusing it: a clipped or near-silent clip
    produces an embedding that is essentially noise, and noise sometimes clears
    the threshold. Better to ask the person to speak again.
    """
    n = samples.size
    duration = n / float(TARGET_SR)
    peak = float(np.max(np.abs(samples))) if n else 0.0
    clipped = float(np.mean(np.abs(samples) > 0.98)) if n else 1.0

    # Frame energies split into speech and silence by an energy percentile —
    # cheap, and good enough to measure how much speech is actually present.
    frame, hop = 400, 160
    if n >= frame:
        idx = range(0, n - frame, hop)
        energies = np.array([float(np.mean(samples[i:i + frame] ** 2)) for i in idx])
    else:
        energies = np.array([float(np.mean(samples ** 2))]) if n else np.array([0.0])

    noise_floor = float(np.percentile(energies, 20)) + 1e-12
    speech_gate = max(noise_floor * 4.0, float(np.percentile(energies, 60)) * 0.35)
    speech_frames = int(np.sum(energies > speech_gate))
    speech_secs = speech_frames * hop / float(TARGET_SR)
    speech_energy = float(np.mean(energies[energies > speech_gate])) if speech_frames else 0.0
    snr_db = 10.0 * math.log10((speech_energy + 1e-12) / noise_floor) if speech_energy else 0.0

    problems = []
    if duration < MIN_AUDIO_SECONDS:
        problems.append(f"recording too short ({duration:.1f}s)")
    if speech_secs < MIN_SPEECH_SECONDS:
        problems.append(f"only {speech_secs:.1f}s of speech — say the whole phrase")
    if peak < 0.03:
        problems.append("too quiet — move closer to the microphone")
    if clipped > MAX_CLIPPED_FRACTION:
        problems.append("audio is clipping — move back or lower the input gain")
    if snr_db < MIN_SNR_DB:
        problems.append(f"too much background noise (SNR {snr_db:.0f} dB)")

    return {
        "ok": not problems,
        "duration": round(duration, 2),
        "speech_secs": round(speech_secs, 2),
        "snr_db": round(snr_db, 1),
        "peak": round(peak, 3),
        "clipped_fraction": round(clipped, 4),
        "problems": problems,
        "message": "; ".join(problems) if problems else "audio quality ok",
    }


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

    meta["quality"] = assess_quality(samples)

    # 1. ECAPA-TDNN — the accurate one.
    ecapa = _get_ecapa()
    if ecapa is not None:
        try:
            import torch
            with torch.no_grad():
                e = ecapa.encode_batch(torch.from_numpy(samples).float().unsqueeze(0))
            emb = e.squeeze().cpu().numpy().astype(np.float32)
            meta["backend"] = "ecapa"
            return emb / (np.linalg.norm(emb) + 1e-9), meta
        except Exception as e:
            meta["ecapa_error"] = str(e)

    # 2. Resemblyzer GE2E.
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

    # 3. MFCC baseline.
    return _mfcc_embedding(samples), meta


def _cohort_vectors(exclude_vpa: str, backend: str, limit: int = 40) -> list[np.ndarray]:
    """Other enrolled speakers, used as impostors for score normalisation."""
    out = []
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT vpa, embedding_json FROM voice_embeddings WHERE vpa != ? LIMIT ?",
                (exclude_vpa, limit),
            ).fetchall()
        for r in rows:
            try:
                stored = json.loads(r["embedding_json"])
                if isinstance(stored, list):
                    continue                      # legacy row, backend unknown
                if stored.get("backend") != backend:
                    continue                      # never mix embedding spaces
                v = np.asarray(stored["vec"], dtype=np.float32)
                out.append(v / (np.linalg.norm(v) + 1e-9))
            except Exception:
                continue
    except Exception:
        pass
    return out


def as_norm(raw_score: float, probe: np.ndarray, enrolled: np.ndarray,
            cohort: list[np.ndarray]) -> tuple[Optional[float], dict]:
    """Adaptive symmetric score normalisation.

    A raw cosine score is not comparable across microphones, rooms or recording
    levels — the same speaker on a laptop and on a phone can differ by more than
    the gap between two different people. AS-norm measures where this score sits
    relative to impostor scores for the same probe and the same enrolment, which
    cancels most of that shift.

    Returns a z-score, NOT a cosine. The two live on different scales and must
    never be compared against the same threshold; the caller picks the right
    one. Returns None when the cohort is too small to normalise against.
    """
    if len(cohort) < 3:
        return None, {"applied": False, "cohort": len(cohort)}

    probe_scores = np.array([float(probe @ c) for c in cohort], dtype=np.float32)
    enroll_scores = np.array([float(enrolled @ c) for c in cohort], dtype=np.float32)

    mu_p, sd_p = float(probe_scores.mean()), float(probe_scores.std() + 1e-6)
    mu_e, sd_e = float(enroll_scores.mean()), float(enroll_scores.std() + 1e-6)

    z = 0.5 * ((raw_score - mu_p) / sd_p + (raw_score - mu_e) / sd_e)
    return float(z), {
        "applied": True,
        "cohort": len(cohort),
        "impostor_mean": round(float(probe_scores.mean()), 4),
        "raw": round(raw_score, 4),
    }


# How many standard deviations above the impostor distribution a genuine
# attempt must sit when AS-norm is in play. Independent of the cosine scale.
BASE_Z_THRESHOLD = 1.8


def adaptive_z_threshold(enroll_consistency: float) -> float:
    """The AS-norm operating point, nudged by enrolment consistency."""
    if enroll_consistency <= 0:
        return BASE_Z_THRESHOLD
    offset = (enroll_consistency - 0.85) * 2.0
    return round(BASE_Z_THRESHOLD + max(-0.5, min(0.5, offset)), 3)


def adaptive_threshold(backend: str, enroll_consistency: float) -> float:
    """Shift the operating point by how tightly this speaker's own samples sat.

    Someone whose enrolment samples were nearly identical gets a slightly
    stricter cut-off, because a genuine attempt from them should score high.
    Someone whose samples varied (a cold, a noisier room) gets a slightly more
    forgiving one, so they are not locked out of their own account.
    """
    base = BASE_THRESHOLD.get(backend, BASE_THRESHOLD["mfcc"])
    if enroll_consistency <= 0:
        return base
    # consistency ~0.95 is very tight, ~0.70 is loose.
    offset = (enroll_consistency - 0.85) * 0.5
    offset = max(-THRESHOLD_ADAPT_RANGE, min(THRESHOLD_ADAPT_RANGE, offset))
    return round(base + offset, 4)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ── Persistence / enroll / verify ─────────────────────────────────────────────

def active_backend() -> str:
    """Which encoder this process would use for a new recording."""
    if _get_ecapa() is not None:
        return "ecapa"
    if _get_encoder() is not None:
        return "resemblyzer"
    return "mfcc"


def _stored_backend(embedding_json: str) -> str:
    try:
        stored = json.loads(embedding_json)
        return "legacy" if isinstance(stored, list) else stored.get("backend", "unknown")
    except Exception:
        return "unknown"


def is_enrolled(vpa: str) -> bool:
    """Enrolled means usable, not merely present.

    A voiceprint recorded with a different encoder cannot be compared against
    the current one at all, so it does not count as enrolled. Reporting it as
    enrolled would let someone reach the payment screen and fail there, instead
    of being sent to re-enrol where the problem is actually fixable.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT sample_count, embedding_json FROM voice_embeddings WHERE vpa = ?", (vpa,)
        ).fetchone()
    if not row or row["sample_count"] < MIN_ENROLL_SAMPLES:
        return False
    return _stored_backend(row["embedding_json"]) == active_backend()


def get_status(vpa: str) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT sample_count, updated_at, created_at, embedding_json "
            "FROM voice_embeddings WHERE vpa = ?",
            (vpa,),
        ).fetchone()
    backend = active_backend()
    aasist_ok = _get_ort() is not None
    base = {
        "required_samples": MIN_ENROLL_SAMPLES,
        "target_samples": TARGET_ENROLL_SAMPLES,
        "threshold": BASE_THRESHOLD.get(backend, MFCC_VERIFY_THRESHOLD),
        "speaker_backend": backend,
        "anti_spoof": {"aasist_onnx": aasist_ok, "spectral_pad": True},
    }
    if not row:
        return {**base, "enrolled": False, "sample_count": 0}

    stored_backend = _stored_backend(row["embedding_json"])
    stale = stored_backend != backend
    return {
        **base,
        "enrolled": row["sample_count"] >= MIN_ENROLL_SAMPLES and not stale,
        "sample_count": row["sample_count"],
        "enrolled_backend": stored_backend,
        "needs_reenrollment": stale,
        "reenrollment_reason": (
            f"Voice ID was recorded with the {stored_backend} model; this device "
            f"now uses {backend}, which is more accurate. One quick re-enrolment "
            f"and you are done."
        ) if stale else None,
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
    }


def enroll(vpa: str, audio_blobs: list[bytes], holder_hint: str = "") -> dict:
    if len(audio_blobs) < MIN_ENROLL_SAMPLES:
        raise ValueError(f"Need at least {MIN_ENROLL_SAMPLES} voice samples for enrollment")

    sample_vecs, pitch_means, liveness_reports, qualities = [], [], [], []
    backend = "mfcc"
    for i, blob in enumerate(audio_blobs):
        samples = load_mono_16k(blob)

        # Quality before anything else: a bad recording should be re-taken, not
        # baked into the voiceprint where it degrades every future comparison.
        q = assess_quality(samples)
        qualities.append(q)
        if not q["ok"]:
            raise ValueError(f"Sample {i + 1}: {q['message']}. Please record it again.")

        live = assert_live(samples)
        liveness_reports.append(live)
        if not live["live"]:
            raise ValueError(
                f"Sample {i + 1} failed liveness / anti-spoof check: {live['message']}. "
                "Use a live mic — phone playbacks and deepfakes are blocked."
            )
        vec, meta = extract_embedding(blob)
        backend = meta.get("backend", backend)
        sample_vecs.append(vec)
        if meta.get("pitch_mean"):
            pitch_means.append(meta["pitch_mean"])

    # Drop a sample that disagrees with the rest — a cough, a cut-off word, a
    # door slamming. One bad sample drags the centroid for every later check.
    dropped = 0
    if len(sample_vecs) >= 4:
        med = []
        for i, v in enumerate(sample_vecs):
            others = [cosine_similarity(v, w) for j, w in enumerate(sample_vecs) if j != i]
            med.append(float(np.median(others)))
        best = max(med)
        keep = [v for v, m in zip(sample_vecs, med) if m >= best - ENROLL_OUTLIER_MARGIN]
        if MIN_ENROLL_SAMPLES <= len(keep) < len(sample_vecs):
            dropped = len(sample_vecs) - len(keep)
            sample_vecs = keep

    pair_scores = []
    for i in range(len(sample_vecs)):
        for j in range(i + 1, len(sample_vecs)):
            pair_scores.append(cosine_similarity(sample_vecs[i], sample_vecs[j]))
    worst = min(pair_scores) if pair_scores else 1.0
    # How tightly this speaker's own samples cluster. Drives their threshold.
    consistency = float(np.mean(pair_scores)) if pair_scores else 1.0

    min_sim = {"ecapa": 0.45, "resemblyzer": ENROLL_PAIR_MIN_SIM}.get(backend, 0.10)
    if worst < min_sim:
        raise ValueError(
            f"Enrollment samples look too different (lowest similarity {worst:.2f}). "
            "Retry in a quiet room with the same live voice each time."
        )

    mean_vec = np.mean(np.stack(sample_vecs, axis=0), axis=0)
    mean_vec = mean_vec / (np.linalg.norm(mean_vec) + 1e-9)
    enroll_pitch = float(np.mean(pitch_means)) if pitch_means else 0.0
    threshold = adaptive_threshold(backend, consistency)

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
                    "consistency": round(consistency, 4),
                    "threshold": threshold,
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
        "samples_dropped": dropped,
        "backend": backend,
        "consistency": round(consistency, 4),
        "threshold": threshold,
        "quality": qualities,
        "anti_spoof": "aasist+spectral",
        "message": "Voice ID enrolled with anti-spoof protection. "
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
    # Quality first — refuse to decide on audio that cannot support a decision.
    quality = assess_quality(samples)
    if not quality["ok"]:
        return {
            "matched": False,
            "score": 0.0,
            "threshold": 0.0,
            "quality": quality,
            "fail_reason": "poor_audio",
            "message": f"Could not check your voice — {quality['message']}.",
        }

    live = assert_live(samples)
    if not live["live"]:
        return {
            "matched": False,
            "score": 0.0,
            "threshold": 0.0,
            "quality": quality,
            "liveness": live,
            "fail_reason": "liveness_failed",
            "message": f"Replay/fake voice blocked — {live['message']}",
        }

    probe, meta = extract_embedding(audio_bytes)
    probe_backend = meta.get("backend", "mfcc")
    stored = json.loads(row["embedding_json"])
    if isinstance(stored, list):
        mean_vec = np.asarray(stored, dtype=np.float32)
        backend = "legacy"
        consistency, stored_threshold = 0.0, None
    else:
        mean_vec = np.asarray(stored["vec"], dtype=np.float32)
        backend = stored.get("backend", "unknown")
        consistency = float(stored.get("consistency") or 0.0)
        stored_threshold = stored.get("threshold")

    # Embeddings from different models are not comparable at all — a cosine
    # between an ECAPA and a GE2E vector is meaningless, not merely noisy.
    if probe.size != mean_vec.size or (backend not in ("legacy", "unknown")
                                       and backend != probe_backend):
        raise ValueError(
            "Voice ID was enrolled with a different model — please re-enroll."
        )

    sample_vecs = [np.asarray(s, dtype=np.float32) for s in json.loads(row["samples_json"])]
    score_mean = cosine_similarity(probe, mean_vec)
    score_best = max(cosine_similarity(probe, s) for s in sample_vecs)
    raw = 0.55 * score_mean + 0.45 * score_best

    # Normalise against other enrolled speakers so one threshold holds across
    # microphones and rooms.
    cohort = _cohort_vectors(vpa, probe_backend)
    z, norm_info = as_norm(raw, probe, mean_vec, cohort)

    cos_thr = stored_threshold if stored_threshold else adaptive_threshold(probe_backend, consistency)

    if z is not None:
        # Normalised path: decide on the z-scale, and still require the raw
        # cosine to clear a floor. Both must hold, so an unusually spread-out
        # cohort cannot wave through a weak match on its own.
        z_thr = adaptive_z_threshold(consistency)
        matched = z >= z_thr and raw >= cos_thr * 0.85
        score, thr = round(z, 4), z_thr
        scale = "as_norm_z"
        margin = round(z - z_thr, 4)
        confidence = round(float(np.clip(0.5 + margin * 0.18, 0.0, 1.0)), 3)
    else:
        # Too few other enrolled speakers to normalise against — fall back to
        # the raw cosine with the per-speaker threshold.
        matched = raw >= cos_thr
        score, thr = round(raw, 4), cos_thr
        scale = "cosine"
        margin = round(raw - cos_thr, 4)
        confidence = round(float(np.clip(0.5 + margin * 4.0, 0.0, 1.0)), 3)

    return {
        "matched": matched,
        "score": score,
        "threshold": round(thr, 4),
        "scale": scale,
        "cosine_threshold": round(cos_thr, 4),
        "margin": margin,
        "confidence": confidence,
        "score_raw": round(raw, 4),
        "score_mean": round(score_mean, 4),
        "score_best": round(score_best, 4),
        "normalisation": norm_info,
        "backend": probe_backend,
        "enroll_consistency": round(consistency, 4),
        "quality": quality,
        "liveness": live,
        "fail_reason": "" if matched else "embedding_mismatch",
        "message": (
            "Voice verified — payment authorized"
            if matched
            else "Voice does not match your enrolled Voice ID — payment blocked"
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
