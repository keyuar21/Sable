# Project Status

Working log of what has been changed, what was proven by test, and what is
still open. Companion to [ARCHITECTURE.md](ARCHITECTURE.md), which describes the
target design.

**Last updated:** 2026-10-07
**Branch:** `main` — ⚠️ all of the work below is **uncommitted**

Legend: ✅ verified by a test run · ⚠️ implemented, not verified · ❌ not started

---

## 1. Summary

Three bodies of work landed:

| Area | Outcome |
|---|---|
| **Frontend / UX** | Sign-in rebuilt, real desktop layout, a layout bug fixed |
| **Security** | Auth bypass closed, account enumeration removed, device binding added |
| **Ledger (Phase 1, part 1)** | Double-entry journal replaces mutable balances; double-spend and money-destruction bugs fixed |
| **Bank Ops Console** | New read-only service on :5010 that renders the live journal, balances and conservation check |
| **E2E test suite** | `tests/e2e_test.py` — 76 assertions across 11 scenarios, **76 passed / 0 failed**. Found and fixed 3 real bugs |
| **Settlement posts to the ledger** | B2 closed — completed payments now leave the pool and move through NOSTRO |
| **Auto-reversal** | Gap 6b closed — a failed credit leg refunds itself, with a recon sweep for the unconfirmed case |

Three defects found during the work were **live bugs, not cosmetic**:

1. A **double-spend** — five concurrent debits took a ₹1,000 account to −₹2,000.
2. **Money destruction** — a failed credit leg left funds existing nowhere.
3. A **full auth bypass** — clicking the nav walked straight past sign-in.

All three are fixed and tested.

---

## 2. Done and verified ✅

### 2.1 Ledger — `services/ledger.py` (new)

| Item | Evidence |
|---|---|
| Append-only double-entry journal | Unit test: entries post, nothing mutates |
| `SUM(DR) = SUM(CR)` enforced per entry | `DR 100 / CR 110` → rejected with `UnbalancedEntry` |
| Integer paise, never floats | `0.1 + 0.2` → exactly 30 paise |
| Holds / earmarks | hold 300 on 1000 → `ledger=1000, available=700` |
| Derived balances | `/balance` recomputes from postings, no stored truth |
| `assert_books_balance()` | Runs on every boot; both banks print balanced totals |
| Trial balance + statement endpoints | `/ledger/trial-balance`, `/ledger/statement/{vpa}` return 200 |

### 2.2 Concurrency — both bank services

| Item | Evidence |
|---|---|
| `BEGIN IMMEDIATE` around read-check-write | **Before:** 5× concurrent ₹600 debit on ₹1,000 → all 200 OK, final **−₹2,000**. **After:** 1× 200, 4× 402, final **₹400** |
| Auth moved outside the lock | No network call held under the write lock |
| Idempotency under concurrency | 3× same `txn_id` → 1 debit + 2 `duplicate:true`, balance moved once |
| WAL + 30s busy timeout | No lock contention errors across all test runs |

### 2.3 Money model — pool accounts

| Item | Evidence |
|---|---|
| Legacy balances migrated to the journal | Startup: `MOCKBANK balanced 35,515,000 paise`; balances unchanged (₹46,650 / ₹3,650) |
| Debit posts `DR customer / CR payer-POOL` | Conservation test below |
| Credit posts `DR payee-POOL / CR merchant` | Conservation test below |
| Books balance throughout | `balanced=True` at every step |

```
                        before      after debit    after credit
payer you@mockbank    46,400.00      46,150.00       46,150.00
payer POOL               250.00         500.00          500.00
payee POOL              -250.00        -250.00         -500.00
payee swiggy           3,900.00       3,900.00        4,150.00
```

Payer POOL `+500` / payee POOL `−500` are the two sides of the unsettled
inter-bank obligation. **The negative pool is correct, not a bug.**

### 2.4 Reversal — `POST /reverse`

Payee bank killed mid-payment with a ₹700 debit in flight:

```
after failed credit:  customer 45,450.00   POOL 1,200.00   ← funds in the pool
after POST /reverse:  customer 46,150.00   POOL   500.00   ← returned
```

Statement shows both legs, original entry untouched:

```
DEBIT_LEG   DR   700.00   45,450.00   UPI debit ₹700.00 you@mockbank → swiggy@…
REVERSAL    CR   700.00   46,150.00   Reversal of ₹700.00 — Credit leg failed
```

Reversal is idempotent (second call returns `duplicate: true`).

### 2.5 Security

| Item | Evidence |
|---|---|
| Route guard in `showScreen()` | From console, logged out: `showScreen('home')`, `startVoiceInput()`, `showScreen('history'/'settlement'/'confirm')` → **all redirect to `screen-login`** |
| Nav removed + inert until unlocked | `display:none` + `pointer-events:none`; desktop sidebar column collapses |
| Two gates enforced | Credential **and** voice ID; `isUnlocked()` requires both |
| `GET /accounts` enumeration removed (payer bank) | Returns **404**; previously leaked every VPA, name and balance unauthenticated |
| Device binding via `localStorage` | Cleared storage → no account list, UPI ID must be typed in full. After sign-in → exactly 1 card. **Wrong PIN binds nothing** (verified with `harsh@hdfcbank`) |

### 2.6 Bank Operations Console — `services/ops_console/` (new, port 5010)

A separate read-only back-office app. It opens each bank's SQLite file in
`mode=ro` and queries the journal directly — every figure on the page is a live
`SUM` over `postings`, not a cached or mocked number.

| Item | Evidence |
|---|---|
| Per-bank trial balance grouped by account type | Renders all 8 customer accounts + POOL/NOSTRO/OPENING per bank |
| `DR = CR` integrity badge per bank | Both show `DR = CR` |
| **Cross-bank conservation check** | Headline figure: `MOCKBANK:POOL + MOCKBANK2:POOL`. Reads **₹0.00 reconciled** when settled |
| **Break detection** | Debit with no credit leg → banner flips to `₹900.00` in flight, `reconciled=false`. After `/reverse` → back to `₹0.00` |
| Live journal, each entry showing DR leg → CR leg | Every card carries `✓ DR ₹X = CR ₹X` |
| Cross-bank transaction tracer | `GET /api/trace/{txn_ref}` shows the debit leg on MOCKBANK and the credit leg on MOCKBANK2 as two separate entries |
| Wired into `start.sh` | Steps renumbered to `/7`, port 5010 cleared on boot, added to health loop and shutdown trap, `bash -n` clean |
| Launches the way `start.sh` invokes it | Verified from `$SVC_DIR` with `PYTHONPATH` set |

Why this matters: it is the proof surface for the whole ledger design. The
negative payee POOL and the zero net position are visible facts rather than
claims in a document.

### 2.7 End-to-end test suite — `tests/e2e_test.py` (new)

Nine scenarios, 60 assertions, driven over HTTP against the live stack and
asserted against the **ledger**, not just HTTP status codes. Re-runnable:

```bash
python3 tests/e2e_test.py
```

Final run: **60 passed, 0 failed.**

| # | Scenario | Asserts |
|---|---|---|
| 1 | Happy path P2M | Debit parks value in the remitter POOL, credit draws from the beneficiary POOL, one entry per bank, no entry spans two banks |
| 2 | Insufficient funds | 402, balance untouched, nothing posted |
| 3 | Concurrent double-spend | 5× ₹600 on ₹1,000 → exactly 1 success, 4× 402, balance ₹400, never negative |
| 4 | Idempotency | 3× same `txn_id` concurrently → 1 posting, 2 `duplicate:true` |
| 5 | Partial failure + reversal | Funds recoverable in pool, reversal points at the original, original never mutated, replay idempotent |
| 6 | Auth and validation | Wrong PIN 401, missing credential 400, unknown payer/merchant 404, **VPA case-insensitivity** |
| 7 | Paise precision | `0.01 + 0.10 + 0.20 + 33.33` lands exactly |
| 8 | Full PSP orchestration | `/initiate` → switch → both banks, terminal status `success`, both legs journalled, switch logged the hop |
| 9 | System integrity | DR=CR per bank, every entry balances, no value left in flight |

Each scenario that debits without crediting now reverses its own transaction, so
a clean run ends with the pools netting to exactly zero.

### 2.9 Settlement now clears the pools (B2 closed)

Each bank gained `POST /settlement/post`. The switch's cycle sends the explicit
set of transactions whose **both legs completed**, and each bank posts its own
balanced clearing entry:

| Bank | Entry |
|---|---|
| Remitter | `DR POOL / CR NOSTRO` — cash leaves its RBI account |
| Beneficiary | `DR NOSTRO / CR POOL` — cash arrives in its RBI account |

Verified live:

```
                     POOL        NOSTRO
before payment       0.00          0.00
after ₹2,000 pay  2,000.00         0.00     value parked in the pool
after settlement     0.00     -2,000.00     payer bank paid out
payee bank           0.00     +2,000.00     payee bank received
                                 -------
                     NOSTRO nets to 0.00
```

The ₹8,850.92 legacy backlog from before ledger settlement existed also cleared.

**A bug I introduced and then caught here:** the first version settled whatever
was in the pool, which meant a debit with no matching credit got settled out
into NOSTRO — recreating money destruction one layer down. It cost ₹1,750 of
real damage in testing, repaired with a `SETTLEMENT_CORRECTION` entry. Settlement
now only ever covers transactions the switch has matched on both legs, so money
still in flight stays in the pool where a reversal can reach it.

### 2.10 Auto-reversal and the recon sweep (gap 6b closed)

`/reverse` existed but nothing called it. Now every post-debit failure routes
through `resolve_stranded()`, which **asks the beneficiary bank whether the
credit actually landed** before deciding — a timeout or dropped connection is
not a failure.

| Situation | Behaviour |
|---|---|
| Credit leg rejected (bank reachable) | Auto-reverse immediately → `reversed` |
| Timeout / connection dropped, credit confirmed landed | → `success` |
| Timeout / connection dropped, confirmed not credited | Auto-reverse → `reversed` |
| Beneficiary bank unreachable, outcome unknown | **Does not guess.** → `partial_failure`, retried by the sweep |

A `recon_sweep()` retries `partial_failure` transactions every 20s. Verified:
killed the payee bank mid-payment → `partial_failure` (correctly refused to
reverse blindly) → brought the bank back → sweep resolved it to `reversed` and
refunded within 6s, with no manual step.

The frontend gained a `reversed` terminal status ("Payment failed — amount
refunded"), distinct from `partial_failure` ("Stuck — needs recon").

### 2.11 Frontend

| Item | Evidence |
|---|---|
| Sign-in rebuilt — account cards, not a `<select>` | Screenshots at 1440px and 375px |
| Inline errors replace `alert()` | Wrong PIN → `"Incorrect PIN. 2 attempt(s) remaining before lockout."` rendered in-page |
| Desktop layout: sidebar, 2-col home, no phone frame >1024px | Screenshots; no horizontal overflow at either width |
| Mobile unchanged below 1024px | `.home-col` is `display:contents` on mobile |
| **Flex-shrink layout bug fixed** | Mobile home was squashing — balance amount vanished, merchant row overlapped the next section. Pre-existing, not introduced |
| `maximum-scale=1.0` removed | Was blocking pinch-zoom on phones |
| Sign-in works end to end | `you@mockbank` + PIN → home, balance ₹46,150, matches journal exactly |

---

## 3. Done but NOT verified ⚠️

These are written and syntactically valid, but no test was run against them.

| Item | Risk |
|---|---|
| **Onboarding flow end-to-end** | Its completion path was rewired from `loadAccountsList()` to `rememberDeviceAccount()` + `renderDeviceAccounts()`. Scope was checked statically (the call sits inside the same closure) but the 4-step flow was **never run through**. Highest-risk untested item |
| Sign-out via header avatar | Implemented, never clicked |
| `escapeHtml()` on account names | Added, not exercised with a hostile name |
| Biometric / WebAuthn sign-in | Untouched this session, untested |
| Voice enrollment + voice confirm | Untouched this session, untested |
| `/reverse` on an account held at a non-`@mockbank` handle | Only tested with `you@mockbank` |

---

## 4. Regressions found by the test suite — now fixed ✅

Both were introduced by the ledger migration and would have broken real usage.

| # | Regression | Symptom | Fix |
|---|---|---|---|
| R1 | **`/accounts/register` never opened a ledger account** | Every account created through onboarding was unusable — HTTP 500 on both `/balance` and `/debit`, because the chart-of-accounts lookup raised `KeyError`. This is exactly the untested onboarding path flagged in §3 | Registration now opens the ledger account and posts a balanced opening-balance entry inside one transaction. Re-registering an account that already has history keeps the journal's balance instead of silently resetting it. The startup migration also self-heals accounts broken before the fix |
| R2 | **VPA lookups were case-sensitive** | Registration lower-cased the handle but reads did not, so a handle typed with any capital letter returned 404 and could never be paid | `norm_vpa()` applied at every boundary in both banks. UPI handles are case-insensitive |

Guards added so neither can return a 500 again: `/debit` refuses cleanly when an
account has no chart-of-accounts entry, and the payee balance treats a
not-yet-opened merchant as zero.

### Data damage found and repaired

The two *failing* test runs (before R1/R2 were fixed) left **8 orphan credits
totalling ₹67.28** — the debit leg 404'd while the credit leg succeeded, so a
merchant was credited money nobody paid. That is created money.

Repaired the correct way: **8 balanced correcting entries** posted through
`post_entry()`, each referencing the orphan via `reverses`. Nothing was deleted
or mutated — the append-only rule held even for cleanup. Net pool returned to
₹0.00 and both banks still report DR = CR.

---

## 5. Known problems introduced or left open 🐛

Honest list of things that are currently wrong.

| # | Problem | Impact |
|---|---|---|
| B1 | **`/reverse` does not sync to Supabase** | SQLite is correct; the Supabase mirror drifts after any reversal. Confirmed: zero `supabase_db` calls in the endpoint |
| ~~B2~~ | ~~Settlement posts nothing to the journal~~ | **FIXED** — see §2.9 |
| B3 | **Payee bank still enumerates accounts** | `GET /accounts` on port 5004 returns every merchant. Less severe than the payer-side leak (a merchant directory is arguably public) but inconsistent |
| B4 | Unauthenticated reads remain open | `/balance/{vpa}`, `/ledger/{vpa}`, `/all-transactions` answer anyone who can reach the port. Structural; §2/§11 of the architecture spec is the real fix |
| B5 | `harsh@hdfcbank` PIN attempts | My test burned attempts; it sat at "2 remaining". Resets on next successful sign-in |
| B6 | Two stray screenshot PNGs in the repo root | Untracked clutter |
| B8 | Ops console has no auth and exposes every account's balance | Acceptable for a local back-office tool, but do **not** expose port 5010 beyond localhost |
| B9 | Test runs leave throwaway accounts in the payer ledger (`racet…`, `idemt…`, `pspt…` etc.) | Harmless but clutters the console. They cannot be deleted without breaking the append-only journal — a `/reset` that re-seeds a fresh ledger file is the clean answer |
| B11 | **The recon sweep only sees transactions in the PSP's in-memory store** | A PSP restart loses pending transactions, so a `partial_failure` from before the restart is never retried. One such payment (₹1,750) had to be repaired by hand. Pending transactions need to be persisted |
| B10 | ~~`python3` resolves to an Xcode-gated stub~~ | **Not an issue in practice.** Plain `python3 tests/e2e_test.py` runs fine in the normal shell (pyenv/conda on PATH). Only a bare non-login shell hits `/usr/bin/python3`, which is Xcode-gated |
| B7 | **Everything is uncommitted** | One `rm -rf` from gone |

---

## 6. Not started ❌

Mapped to [ARCHITECTURE.md](ARCHITECTURE.md) phases.

### Phase 1 remainder — the money model
| Gap | What is needed |
|---|---|
| 2b | Credit leg still goes PSP → payee bank directly. Should route through the switch so the banks never touch each other |
| 8 | `transactions.status` is a free string. No enforced state machine, no illegal-transition guard |
| ~~B2~~ | ~~Settlement must post `POOL → NOSTRO` entries~~ — **DONE** |

### Phase 2 — the language
| Gap | What is needed |
|---|---|
| 3 | `upi_codes.py` — `00`, `ZM`, `Z9`, `ZX`, `U16`, `U30`, `U31`, `BT`… surfaced in API and UI |
| 4 | RRN (12-digit), customer ref no., approval code, merchant order ID |
| — | Rename endpoints to NPCI verbs (`ReqPay`, `ReqValAdd`, `ReqChkTxn`, `ReqListAccount`) |
| 11 | Delete the legacy raw-PIN debit branch |

### Phase 3 — exceptions
| Gap | What is needed |
|---|---|
| ~~6b~~ | ~~Nothing calls `/reverse` automatically~~ — **DONE**, see §2.10 |
| ~~—~~ | ~~`ReqChkTxn` sweep~~ — **DONE**, `recon_sweep()` every 20s |
| — | TAT compensation clock + penalty postings |

### Phase 4 — settlement and recon
| Gap | What is needed |
|---|---|
| 5 | Multilateral netting matrix at the switch; net position per bank summing to zero |
| 10 | Daily three-way recon (NPCI file ↔ remitter book ↔ beneficiary book), `recon_breaks` table |
| 12 | Demote Supabase to a reporting sink only |

### Phase 5 — acquiring
| Gap | What is needed |
|---|---|
| 7 | Payee PSP service (port 5006) |
| 9 | Merchant onboarding, MID/TID/MCC, P2M vs P2P, dynamic QR, T+1 merchant settlement + MIS file, refunds, UDIR disputes |

---

## 7. Suggested next steps

In order of value per unit of effort:

1. **Commit everything.** Nothing below matters if this is lost.
2. ~~Fix B2 (settlement posts to the journal)~~ — **done**, §2.9.
3. **Fix B1** (reverse → Supabase sync). Small, stops silent drift.
4. ~~Wire gap 6b (auto-reversal)~~ — **done**, §2.10.
4b. **Fix B11** — persist pending transactions so the recon sweep survives a PSP restart.
5. ~~Test the onboarding flow end to end~~ — **done**; it was broken (R1) and is now fixed and covered by scenarios 2–8, which all create accounts through `/accounts/register`.
6. Then Phase 2 (`upi_codes.py` + RRN) — cheapest credibility win in the project.

---

## 8. How to re-verify

Open the ops console at <http://localhost:5010> — it shows most of the below live.


```bash
./start.sh
```

Books balance on both banks:

```bash
curl -s localhost:5003/ledger/trial-balance | python3 -m json.tool | head -20
curl -s localhost:5004/ledger/trial-balance | python3 -m json.tool | head -20
```

Double-spend regression — expect **one** 200 and four 402s, final balance ₹400:

```bash
curl -s -X POST localhost:5003/accounts/register -H 'Content-Type: application/json' \
  -d '{"vpa":"racetest@mockbank","holder_name":"Race Test","balance":1000,"pin":"1234"}'
for i in 1 2 3 4 5; do
  curl -s -X POST localhost:5003/debit -H 'Content-Type: application/json' \
    -d "{\"txn_id\":\"RACE-$RANDOM-$i\",\"vpa\":\"racetest@mockbank\",\"payee_vpa\":\"swiggy@mockbank2\",\"amount\":600,\"pin\":\"1234\"}" \
    -o /dev/null -w "%{http_code} " &
done; wait; echo
curl -s localhost:5003/balance/racetest@mockbank
```

Auth bypass regression — all three must print `screen-login`, in the browser console
while signed out:

```javascript
showScreen('home');      document.querySelector('.screen.active').id
startVoiceInput();       document.querySelector('.screen.active').id
showScreen('settlement');document.querySelector('.screen.active').id
```
