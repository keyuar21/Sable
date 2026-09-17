# Voice UPI Simulator — Architecture & Feature Guide

A technically honest **end-to-end UPI payment flow simulator** with a **voice-first interface**, **speaker biometrics** (who is speaking), **anti-spoof / liveness** (live human vs phone recording), and a full mock stack of PSP → NPCI Switch → Payer Bank → Payee Bank, plus deferred settlement.

> **Simulation only — no real money, no real NPCI, no production banking.**

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [What this project does](#2-what-this-project-does)
3. [High-level architecture](#3-high-level-architecture)
4. [Voice biometric stack (deep dive)](#4-voice-biometric-stack-deep-dive)
5. [End-to-end user journeys](#5-end-to-end-user-journeys)
6. [Service map & ports](#6-service-map--ports)
7. [Repository layout (every file)](#7-repository-layout-every-file)
8. [API reference](#8-api-reference)
9. [Database / storage](#9-database--storage)
10. [Frontend screens & UX](#10-frontend-screens--ux)
11. [Security model](#11-security-model)
12. [Configuration](#12-configuration)
13. [Dependencies](#13-dependencies)
14. [Demo accounts](#14-demo-accounts)
15. [Limitations & honesty notes](#15-limitations--honesty-notes)

---

## 1. Quick start

```bash
cd "voice-upi-simulator 2"
./start.sh
```

Then open **http://localhost:3000** in **Chrome** or **Edge** (best support for Web Speech API + Web Audio + WebAuthn).

Optional (Supabase cloud sync): run these SQL files in the Supabase SQL Editor:

1. `schema.sql`
2. `schema_auth_update.sql`
3. `schema_voice_biometrics.sql`

Local ledgers and local voice SQLite work even without Supabase.

**Demo login PIN:** `1234`  
**Accounts:** `you@mockbank`, `priya@mockbank`, `rahul@mockbank`

---

## 2. What this project does

| Layer | Capability |
|--------|------------|
| **Intent** | Speak or type “Pay 500 to Swiggy” → rule-based parse → confirm screen |
| **AI voice UX** | Browser TTS (cute female voice) asks to confirm amount + merchant |
| **Voice ID** | Mandatory per-user enrollment; Resemblyzer GE2E speaker embeddings |
| **Anti-spoof** | AASIST ONNX + spectral PAD; blocks many replay / synthetic attempts |
| **Challenge** | Random 4-digit spoken code each payment (blocks static “yes” recordings) |
| **Auth token** | Successful voice verify → short-lived `step_up_token` → debit |
| **UPI hops** | PSP orchestrates Switch route → Payer debit → Payee credit |
| **Settlement** | Periodic batch settlement simulation |
| **Extras** | WebAuthn (Face ID / Touch ID) + PIN for *login*; QR / merchant chips |

---

## 3. High-level architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Browser (http://localhost:3000)                                         │
│  index.html + app.js + voicebio.js + webauthn.js + styles.css            │
│                                                                          │
│  • Web Speech API …… STT (intent + challenge digits)                     │
│  • Speech Synthesis … TTS AI prompts (female / cute voice)               │
│  • Web Audio API ……… PCM WAV capture for biometrics                      │
│  • WebAuthn …………… optional device biometric login                        │
└────────────┬───────────────────────────────┬─────────────────────────────┘
             │                               │
             ▼                               ▼
   ┌─────────────────────┐         ┌─────────────────────┐
   │  Mock Auth :5005    │         │  Mock PSP :5001     │
   │  PIN / WebAuthn /   │◄────────│  /initiate          │
   │  Voice enroll+verify│ step-up │  hop orchestration  │
   │  AASIST + Resemblyzer│  token │  poll /txn/{id}     │
   └──────────┬──────────┘         └──────────┬──────────┘
              │                               │
              │                    ┌──────────┼──────────┐
              │                    ▼          ▼          ▼
              │            ┌──────────┐ ┌──────────┐ ┌──────────┐
              │            │ Switch   │ │ Payer    │ │ Payee    │
              │            │ :5002    │ │ Bank     │ │ Bank     │
              │            │ VPA route│ │ :5003    │ │ :5004    │
              │            └──────────┘ │ /debit   │ │ /credit  │
              │                         │ SQLite   │ │ SQLite   │
              │                         └────┬─────┘ └────┬─────┘
              │                              │            │
              ▼                              └─────┬──────┘
   voice_biometrics.db                       ┌─────▼──────┐
   (+ optional Supabase                      │ supabase_db│
    voice_embeddings)                        │ REST sync  │
                                             └────────────┘
```

### Design principles

1. **Microservices** — each UPI role is an independent FastAPI process (realistic hop visibility).
2. **Authoritative balances** live in **payer/payee SQLite**; Supabase is sync/analytics when configured.
3. **Voice is the payment PIN** after enrollment — PIN/WebAuthn remain for login / legacy, not the primary pay gate.
4. **Fail closed** — challenge fail, liveness fail, or speaker mismatch → **no** `step_up_token` → payment blocked.

---

## 4. Voice biometric stack (deep dive)

Voice security is **not** just speech-to-text. Three problems are solved separately:

| Problem | Meaning | Solution in this repo |
|---------|---------|------------------------|
| **What was said?** | Intent / “yes” / digits | Web Speech API (browser STT) |
| **Who said it?** | Speaker verification | **Resemblyzer** GE2E (256-d embedding) + cosine match |
| **Is it live?** | Anti-replay / anti-deepfake | **AASIST ONNX** + spectral PAD + **random challenge code** |

### 4.1 Enrollment (mandatory after login)

1. `GET /voice/status/{vpa}` — if not enrolled → force `#screen-voice-enroll`.
2. `POST /voice/challenge` (`purpose=enroll`) → random 4-digit code + phrase.
3. User records **3 live WAV samples** saying phrase + code.
4. `POST /voice/enroll` multipart:
   - Challenge validation (STT digits; soft-pass if STT drops digits but speech existed)
   - Per-sample **liveness** (`assert_live`)
   - **Resemblyzer** embeddings → mean voiceprint stored in SQLite
5. Payments unlocked only after `enrolled: true`.

### 4.2 Payment authorization

1. User initiates pay (voice/text/QR/chip) → confirm screen.
2. AI TTS (cute female voice) announces amount + merchant.
3. User taps **Confirm with your voice**.
4. Fresh `POST /voice/challenge` (`purpose=pay`) → e.g. `Yes, make payment. Code 0 2 1 8`.
5. Record WAV + STT transcript.
6. `POST /voice/verify`:
   1. Digits must appear in transcript (**hard** — blocks static recordings of “yes”)
   2. Spectral PAD + AASIST liveness
   3. Resemblyzer match vs enrolled print (threshold ~0.72) + soft pitch gate
7. On success → `step_up_token` → `POST /initiate` → debit accepts token instead of raw PIN.

### 4.3 Models & algorithms

| Component | Location | Notes |
|-----------|----------|--------|
| Resemblyzer | Python package + Torch weights | GE2E speaker encoder; falls back to MFCC/LPC if unavailable |
| AASIST | `models/aasist.onnx` | Input `[batch, 64600]` @ 16 kHz; logits `[spoof, bona fide]` |
| Spectral PAD | `voice_biometrics.py` | Sub-bass ratio, flatness, flux variance (phone-speaker heuristics) |
| Challenge | In-memory dict in Auth | 120s TTL, single-use |

**Model file:** download once (already present if you ran the upgrade):

`models/aasist.onnx` ← Hugging Face `SpeechAntiSpoofingBenchmarks/AASIST`

---

## 5. End-to-end user journeys

### 5.1 Login → Voice ID → Home

```
Login (PIN 1234 or WebAuthn)
    → finishLogin(vpa)
    → ensureVoiceEnrolledOrSetup()
         ├─ enrolled → home
         └─ not enrolled → Voice ID screen (3 samples + challenge)
    → home (balance, merchants, voice pay)
```

### 5.2 Voice payment

```
“Pay 500 to Swiggy”
    → parseIntent()
    → showConfirm()
    → AI TTS prompt
    → Confirm with your voice
    → challenge + live record
    → /voice/verify → step_up_token
    → PSP /initiate
         → Switch /route
         → Payer /debit (token)
         → Payee /credit
    → poll /txn/{id} → success | failed
```

### 5.3 Replay-attack resistance (intended)

| Attack | Mitigations |
|--------|-------------|
| Play a recording of “Yes” from another phone | Random code each time + STT must match digits |
| Replay enrolled passphrase | Fresh challenge digits; liveness PAD |
| Deepfake / TTS voice | AASIST score + speaker embedding mismatch |
| Another person speaking live | Resemblyzer cosine below threshold |

---

## 6. Service map & ports

| Service | Port | Process entry | Role |
|---------|------|---------------|------|
| Frontend | **3000** | `python -m http.server` | Static SPA |
| Mock PSP | **5001** | `mock_psp.psp_service:app` | Payment orchestrator |
| Mock NPCI Switch | **5002** | `mock_switch.switch_service:app` | VPA routing / discover |
| Mock Payer Bank | **5003** | `mock_payer_bank.bank_service:app` | Debit + payer ledger |
| Mock Payee Bank | **5004** | `mock_payee_bank.bank_service:app` | Credit + payee ledger |
| Mock Auth | **5005** | `mock_auth.auth_service:app` | PIN, WebAuthn, Voice ID |

Launched together by `./start.sh` (installs deps, clears ports, seeds PINs, health-checks).

---

## 7. Repository layout (every file)

```
voice-upi-simulator 2/
├── start.sh                          # One-command launcher
├── requirements.txt                  # Python dependencies
├── .env                              # Supabase + WebAuthn env (local secrets)
├── schema.sql                        # Core Postgres schema + seed
├── schema_auth_update.sql            # PIN hash + WebAuthn tables
├── schema_voice_biometrics.sql       # Optional Supabase voice_embeddings
├── models/
│   └── aasist.onnx                   # AASIST anti-spoof ONNX (~1.5 MB)
├── scripts/
│   └── init_pins.py                  # Seeds bcrypt PINs via /pin/set
├── frontend/
│   ├── index.html                    # All UI screens
│   ├── styles.css                    # Phone-frame UI theme
│   ├── app.js                        # App state, intents, payment UX, enroll gate
│   ├── voicebio.js                   # Mic WAV, challenges, TTS cute voice, enroll/verify client
│   └── webauthn.js                   # Face ID / Touch ID client
└── services/
    ├── supabase_db.py                # Shared Supabase REST adapter
    ├── mock_auth/
    │   ├── __init__.py
    │   ├── auth_service.py           # Auth HTTP API
    │   ├── voice_biometrics.py       # Speaker + liveness engine
    │   └── voice_biometrics.db       # Local SQLite voiceprints (created at runtime)
    ├── mock_psp/
    │   └── psp_service.py
    ├── mock_switch/
    │   └── switch_service.py
    ├── mock_payer_bank/
    │   └── bank_service.py           # (+ local SQLite ledger at runtime)
    └── mock_payee_bank/
        └── bank_service.py
```

### 7.1 Root files

#### `start.sh`
Bash launcher that:

1. Loads `.env` if present  
2. `pip install`s FastAPI stack + voice deps (`onnxruntime`, `librosa`, `resemblyzer`, …)  
3. Kills stale processes on ports `5001–5005`, `3000`  
4. Starts Payer, Payee, Switch, Auth, seeds PINs (`scripts/init_pins.py`), starts PSP, serves frontend  
5. Prints health summary and PIDs / log paths (`/tmp/*.log`)

#### `requirements.txt`
Pinned/range Python deps: FastAPI, uvicorn, httpx, pydantic, webauthn, bcrypt, python-multipart, numpy, onnxruntime, librosa, soundfile, resemblyzer, webrtcvad-wheels, torch.

#### `.env`
Local environment (do not commit secrets in public forks). Typical keys:

- `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
- `WEBAUTHN_RP_ID`, `WEBAUTHN_ORIGIN`

#### `schema.sql`
Creates `payer_accounts`, `payee_accounts`, `transactions`, `settlement_batches`, `route_logs`, indexes, RLS policies, and demo seed rows.

#### `schema_auth_update.sql`
Adds `pin_hash`, lockout columns; creates `webauthn_credentials`, `auth_challenges`, `step_up_tokens`.

#### `schema_voice_biometrics.sql`
Optional cloud mirror table `voice_embeddings(vpa, embedding JSONB, sample_count, …)`. **Source of truth for voice is local SQLite** in Auth.

#### `models/aasist.onnx`
Pretrained AASIST countermeasure (ASVspoof-oriented). Loaded by `onnxruntime` in `voice_biometrics.py`.

#### `scripts/init_pins.py`
HTTP client that calls Auth `POST /pin/set` for demo VPAs so login PIN `1234` works after each cold start.

---

### 7.2 Frontend

#### `frontend/index.html`
Single-page shell with phone chrome and screens:

| Screen id | Purpose |
|-----------|---------|
| `screen-login` | Account select, PIN / biometric login, onboard link |
| `screen-voice-enroll` | Mandatory Voice ID (3 samples + challenge phrase) |
| `screen-onboarding` | Simulated device bind → NPCI discover → VPA → PIN |
| `screen-home` | Balance, merchants, voice/QR actions |
| `screen-voice` | Listen for payment intent |
| `screen-confirm` | Amount/merchant + AI panel + voice confirm CTA |
| `screen-pin` | Legacy UPI PIN keypad (not primary pay path) |
| `screen-processing` | Hop animation / status |
| `screen-success` / `screen-failed` | Outcomes |
| `screen-history` / `screen-settlement` | Txn history & settlement cycles |

Also includes a desktop **architecture sidebar** with live service dots.

#### `frontend/styles.css`
Dark phone-UI theme: gradients, waveform animation, enroll steps, confirm panel, PIN keypad, hop diagram, nav, responsive frame.

#### `frontend/app.js`
Main application logic:

- Global `state` (user, payment, voice enroll, pollers)
- `parseIntent()` — amount + merchant keywords (`MERCHANTS` map)
- `startVoiceInput()` / text fallback
- `showConfirm()`, `authorizePaymentWithVoice()` — challenge + verify + `submitPayment`
- `startVoiceEnrollment()`, `recordEnrollSample()`, `ensureVoiceEnrolledOrSetup()`
- `submitPayment()` → PSP `/initiate` with `step_up_token` or PIN
- Balance refresh, history, settlement countdown, service health, onboarding wizard, reset demo

#### `frontend/voicebio.js`
Voice client SDK:

- Cute female **TTS** voice picker (`speakPrompt`) — Samantha / Zira / Google Female, pitch ≈ 1.35
- `recordVoiceSample()` — Web Audio → 16-bit PCM WAV Blob (+ optional STT)
- `requestVoiceChallenge()`, `enrollVoiceBiometric()`, `verifyVoiceBiometric()`
- Helpers: `isAffirmativeSpeech`, `isNegativeSpeech`

#### `frontend/webauthn.js`
WebAuthn ceremony helpers talking to Auth `:5005`:

- `enrollBiometric(vpa, label)`
- `authenticateWithBiometric(vpa)` → `step_up_token`
- `authenticateWithPin(vpa, pin)` → `step_up_token`
- base64url ↔ ArrayBuffer utilities

---

### 7.3 Shared backend helper

#### `services/supabase_db.py`
HTTPX REST client for Supabase PostgREST:

- Health check with short TTL cache  
- CRUD helpers for payer/payee accounts, transactions, settlements, route logs  
- Used by banks / PSP / Auth when cloud sync is configured  

---

### 7.4 Mock Auth (`services/mock_auth/`)

#### `__init__.py`
Package marker so `mock_auth.auth_service` imports cleanly under `PYTHONPATH=services`.

#### `auth_service.py` (port **5005**)
FastAPI app combining:

| Area | Endpoints |
|------|-----------|
| Health | `GET /health` (reports Resemblyzer / AASIST / challenge flags) |
| Voice | `POST /voice/challenge`, `GET /voice/status/{vpa}`, `POST /voice/enroll`, `POST /voice/verify`, `DELETE /voice/enroll/{vpa}` |
| PIN | `POST /pin/set`, `POST /pin/verify` |
| WebAuthn | `/webauthn/register/*`, `/webauthn/auth/*` |
| Step-up | `POST /step-up/verify` (consumed by Payer Bank `/debit`) |

Issues opaque **90-second, single-use** `step_up_tokens` after PIN / WebAuthn / Voice success. Challenges for WebAuthn and voice are held in memory (and mirrored to Supabase when tables exist).

#### `voice_biometrics.py`
Core biometric engine:

- Decode / resample / silence-trim WAV  
- `spectral_liveness_score()` — presentation-attack heuristics  
- `aasist_liveness()` — ONNX inference  
- `assert_live()` — combined PAD policy  
- `extract_embedding()` — Resemblyzer (preferred) or MFCC/LPC fallback  
- `enroll()` / `verify()` / `get_status()` / SQLite persistence  
- Optional `try_mirror_to_supabase()`

#### `voice_biometrics.db` (runtime)
SQLite table `voice_embeddings(vpa, holder_hint, embedding_json, samples_json, sample_count, …)`.

---

### 7.5 Mock PSP (`services/mock_psp/psp_service.py`, port **5001**)

Payment Service Provider simulator:

- `POST /initiate` — create txn, background `process_payment`  
- Calls Switch `/route`, Payer `/debit`, Payee `/credit`  
- Velocity checks, hop log, status machine  
- Settlement loop (`/settlement/*`)  
- `GET /txn/{txn_id}`, `GET /transactions`, `POST /reset`  
- `GET /supabase-status`

---

### 7.6 Mock Switch (`services/mock_switch/switch_service.py`, port **5002**)

NPCI-like VPA router:

- Routing table `@mockbank` → payer bank, `@mockbank2` → payee bank  
- `POST /route`, `GET /resolve/{vpa}`, `GET /routing-table`  
- `GET /npci/discover-accounts` (onboarding)  
- `GET /route-log`, `POST /reset`

---

### 7.7 Mock Payer Bank (`services/mock_payer_bank/bank_service.py`, port **5003**)

- Local SQLite ledger (authoritative balances)  
- `GET /accounts`, `POST /accounts/register`, `GET /balance/{vpa}`  
- `POST /debit` — accepts `step_up_token` (verified via Auth) or legacy PIN  
- Ledger / reset endpoints  

---

### 7.8 Mock Payee Bank (`services/mock_payee_bank/bank_service.py`, port **5004**)

- Merchant accounts & credits  
- `POST /credit`, balances, ledger, reset  

---

## 8. API reference

### Auth `:5005`

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| GET | `/health` | — | Service + voice backend flags |
| POST | `/voice/challenge` | `{vpa, purpose}` | Random spoken code |
| GET | `/voice/status/{vpa}` | — | Enrolled? sample_count? |
| POST | `/voice/enroll` | multipart: `vpa`, `challenge_id`, `spoken_texts`, `samples[]` | Create voiceprint |
| POST | `/voice/verify` | multipart: `vpa`, `challenge_id`, `spoken_text`, `sample` | Match + liveness → token |
| DELETE | `/voice/enroll/{vpa}` | — | Clear voiceprint |
| POST | `/pin/set` | `{vpa, pin}` | Hash & store PIN |
| POST | `/pin/verify` | `{vpa, pin}` | Login / step-up |
| POST | `/webauthn/register/begin\|complete` | WebAuthn JSON | Enroll passkey |
| POST | `/webauthn/auth/begin\|complete` | WebAuthn JSON | Assert passkey |
| POST | `/step-up/verify` | `{vpa, token}` | Bank consumes token |

### PSP `:5001` (selected)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/initiate` | Start payment (`payer_vpa`, `payee_vpa`, `amount`, `step_up_token` \| `pin`) |
| GET | `/txn/{txn_id}` | Poll status / hops |
| GET | `/transactions` | List |
| GET | `/settlement/history`, `/settlement/next` | Settlement UI |
| POST | `/reset` | Demo reset |

### Banks & Switch

See §6 — standard REST: `/health`, `/balance/{vpa}`, `/debit`, `/credit`, `/route`, `/accounts`, `/reset`.

---

## 9. Database / storage

| Store | What lives there | Authoritative? |
|-------|------------------|----------------|
| Payer SQLite | Balances, payer txns | **Yes** for payer money |
| Payee SQLite | Merchant balances | **Yes** for payee money |
| Auth `voice_biometrics.db` | Voice embeddings | **Yes** for Voice ID |
| Supabase Postgres | Sync of accounts/txns/auth/voice (optional) | Mirror / analytics |
| Auth memory | Challenges, step-up tokens (also mirrored) | Ephemeral |

---

## 10. Frontend screens & UX

- **Phone frame UI** with dynamic island / status bar aesthetic  
- **Cute female TTS** for AI prompts (`voicebio.js` voice picker + pitch)  
- **Waveform** animations while listening  
- **Architecture panel** with live green/red service health  
- Merchants: Sharma Store, Blinkit, Swiggy, Zomato, Amazon, HP Petrol, …

---

## 11. Security model

```
Payment authorization = ALL of:
  ✓ Fresh random challenge digits spoken (anti-replay of static audio)
  ✓ Liveness: spectral PAD (+ AASIST when confident)
  ✓ Speaker match: Resemblyzer cosine ≥ threshold
  ✓ step_up_token single-use, ~90s TTL, bound to VPA
  ✓ Payer bank verifies token with Auth before debit
```

**Login** may use PIN or WebAuthn; **paying** after Voice ID enrollment uses voice path above.

---

## 12. Configuration

| Variable | Default / example | Used by |
|----------|-------------------|---------|
| `SUPABASE_URL` | project URL | `supabase_db.py` |
| `SUPABASE_SECRET_KEY` | service key | REST auth |
| `WEBAUTHN_RP_ID` | `localhost` | Auth WebAuthn |
| `WEBAUTHN_ORIGIN` | `http://localhost:3000` | Auth WebAuthn |

Voice thresholds (code constants in `voice_biometrics.py`):

- `VERIFY_THRESHOLD ≈ 0.72` (Resemblyzer)
- AASIST / spectral policy in `assert_live()`

---

## 13. Dependencies

**Python:** FastAPI, Uvicorn, httpx, pydantic, webauthn, bcrypt, python-multipart, numpy, onnxruntime, librosa, soundfile, resemblyzer, torch (for Resemblyzer weights), webrtcvad-wheels.

**Browser:** Chrome/Edge recommended — Web Speech, Web Audio, WebAuthn, Speech Synthesis voices.

---

## 14. Demo accounts

| VPA | Typical name | PIN |
|-----|--------------|-----|
| `you@mockbank` | Keyur Arbhelonde | `1234` |
| `priya@mockbank` | Priya | `1234` |
| `rahul@mockbank` | Rahul | `1234` |

Merchants live under `@mockbank2` (e.g. `swiggy@mockbank2`).

Each **VPA has its own Voice ID**. Switching accounts requires that user’s enrollment.

---

## 15. Limitations & honesty notes

1. **Not production banking** — educational simulator of UPI hops.  
2. **AASIST** was trained mainly on ASVspoof-style attacks; browser mic domain can differ — hence combined with spectral PAD + **mandatory random challenge**.  
3. **Resemblyzer** quality depends on quiet room, consistent distance, and Chrome mic permissions.  
4. **STT** may drop short digit sequences; enroll allows a soft-pass if speech was detected; **pay verify still requires digits**.  
5. **Cute TTS voice** depends on voices installed in the OS/browser (Samantha, Zira, etc.).  
6. Torch / Resemblyzer first load can take a few seconds on Auth startup.  
7. Do not expose `.env` service keys publicly.

---

## Typical developer workflow

```bash
./start.sh                  # boot everything
# open http://localhost:3000
# login → enroll Voice ID (3× phrase + code)
# “Pay 500 to Swiggy” → confirm with live voice + fresh code

tail -f /tmp/auth.log       # voice / AASIST / Resemblyzer logs
curl -s localhost:5005/health | jq .voice
```

---

## License / intent

Built as a **teaching and demo simulator** for UPI flow visibility and modern voice-biometric UX patterns. Not affiliated with NPCI, banks, or commercial biometric vendors (ID R&D, Veridas, Pindrop, etc.). Open-source pieces used: **Resemblyzer**, **AASIST** (ONNX export from public ASVspoof research lineage), browser Web APIs.
# Sable
