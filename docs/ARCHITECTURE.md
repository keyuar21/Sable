# UPI Payment Flow Simulator — Target Architecture

> **Status:** design spec. Describes the *target* system, and the migration path
> from what exists today. Nothing here has been built yet unless a section says so.

---

## 0. Scope and the legal boundary

This project simulates the Indian UPI payment rails end to end. It moves **no real
money** and touches **no real bank account**, by design and by law.

Both sides of a real payment system are licensed activities under the **Payment and
Settlement Systems Act, 2007**, regulated by RBI, with UPI operated by NPCI:

| Role | What it is | Gate |
|---|---|---|
| **Issuer** | Holds the customer account, performs the debit | Banking licence. Lowest rung below it is a **PPI** (wallet) authorisation — company + net worth in crores |
| **Acquirer / PA** | Onboards merchants, collects on their behalf | **Payment Aggregator** authorisation under RBI's PA/PG guidelines — company, net worth in crores, escrow with a scheduled commercial bank |
| **TPAP** | A UPI app (PhonePe, GPay) | Cannot exist standalone. Must operate through a sponsoring **PSP bank** that is a UPI member, plus NPCI onboarding + certification, CERT-In empanelled system audit, data localisation |

An individual developer cannot hold any of these. Operating an unauthorised payment
system is an offence under the PSS Act — this is not a terms-of-service question.

> ⚠️ **Every monetary and regulatory figure in this document is indicative.**
> RBI and NPCI revise thresholds, limits, response codes and timelines regularly.
> Verify against the current circular before quoting any of it outside this repo.

### What is legitimately available with real rails

| Option | What you get | Entity needed |
|---|---|---|
| **UPI deep link** to your own VPA (`upi://pay?pa=…`) | A real UPI app opens and pays you. It is just a URI | None |
| **Payment aggregator test mode** (Razorpay, Cashfree, Setu, Decentro, Juspay) | Real API contracts, real webhook payloads, real settlement-report formats | None for test mode |
| **Account Aggregator** (Sahamati) sandbox | Read-only financial data. Not payments | Varies |
| **NPCI published UPI specs** | The message grammar this document mirrors | None |

**The highest-leverage action for fidelity:** sign up for an aggregator's test mode and
copy the *shapes* — request bodies, webhook payloads, status enums, settlement report
columns — into this simulator. Not to call them at runtime; to make our contracts match
what a reviewer has seen in production.

---

## 1. Participant model

Today the flow is `PSP → Switch → PayerBank → PayeeBank`, with one PSP and the payer
bank calling the payee bank. Real UPI separates more roles, and the two legs of a
transfer are **independent**.

```
                            ┌───────────────────────┐
                            │   NPCI / UPI Switch   │
                            │  routing · netting ·  │
                            │  obligation register  │
                            └───┬───────────────┬───┘
            ReqPay / RespPay    │               │   ReqPay / RespPay
                ┌───────────────┘               └──────────────┐
                ▼                                              ▼
      ┌──────────────────┐                           ┌──────────────────┐
      │  Remitter Bank   │                           │ Beneficiary Bank │
      │    (issuer)      │                           │   (acquirer)     │
      │ payer a/c + POOL │                           │ payee a/c + POOL │
      └────────▲─────────┘                           └─────────▲────────┘
               │ sponsors                                      │ sponsors
      ┌────────┴─────────┐                           ┌─────────┴────────┐
      │   Payer PSP      │                           │    Payee PSP     │
      │  (TPAP + bank)   │                           │  (TPAP + bank)   │
      └────────▲─────────┘                           └─────────▲────────┘
               │                                               │
          Payer app                                   Merchant / payee app
       (this frontend)                                  (QR, collect)
```

**Key property to preserve:** the remitter bank never calls the beneficiary bank. Each
side talks only to NPCI, and each keeps its own books. This is exactly *why*
"debited but not credited" happens in the real world — and the current direct
bank-to-bank call makes that failure mode impossible to simulate honestly.

### Service map

| Service | Port | Role today | Role in target |
|---|---|---|---|
| `mock_psp` | 5001 | Orchestrator + settlement job | **Payer PSP** only. Settlement moves to the switch |
| `mock_switch` | 5002 | VPA → URL resolver | **NPCI**: routing, obligation register, netting, recon files |
| `mock_payer_bank` | 5003 | Payer accounts + debit | **Remitter bank** core: journal, pool a/c, holds |
| `mock_payee_bank` | 5004 | Payee accounts + credit | **Beneficiary bank** core: journal, pool a/c, merchant settlement |
| `mock_auth` | 5005 | PIN, WebAuthn, voice | **Credential service** — unchanged in shape, gains response codes |
| *new* `mock_payee_psp` | 5006 | — | Payee-side PSP: collect requests, QR, merchant callbacks |
| `ops_console` | 5010 | **built** | Read-only back office: trial balance, live journal, conservation check, cross-bank tracer |

---

## 2. Identifier model

One `txn_id` is not enough. Real UPI carries several identifiers owned by different
parties, and reconciliation depends on telling them apart.

| Identifier | Owner | Shape | Purpose |
|---|---|---|---|
| **UPI transaction ID** | Initiating PSP | up to 35 chars | End-to-end correlation across all hops |
| **RRN** | NPCI | 12 digits | **The reconciliation key.** What appears on a bank statement and in dispute flows |
| **Customer reference no.** | NPCI / bank | 12 digits | What the customer quotes to support |
| **Order ID / merchant ref** | Merchant | merchant-defined | Ties the payment to the merchant's own order |
| **Approval / auth code** | Remitter bank | 6 chars | Authorisation reference for the debit leg |

All five belong on the transaction record. Today only the first exists
(`generate_txn_id()` in `services/mock_psp/psp_service.py`).

---

## 3. Core ledger model

**This is the most important section.** Today `services/mock_payer_bank/bank_service.py`
does `UPDATE accounts SET balance = balance - ?`. No core banking system mutates a
balance column. Balance is **derived**, never stored as truth.

### 3.1 Chart of accounts

Every account in a bank's books is a node, not just customers.

| `account_type` | Nature | Example |
|---|---|---|
| `CUSTOMER` | Liability (bank owes the customer) | `you@mockbank` |
| `MERCHANT` | Liability | `sharmastore@mockbank2` |
| `POOL` | Liability | The bank's UPI pool / settlement account |
| `SUSPENSE` | Liability | Unapplied credits awaiting investigation |
| `NOSTRO` | Asset | The bank's current account with RBI |
| `INCOME` | Income | Interchange, fees |
| `PENALTY` | Expense | TAT compensation paid to customers |

### 3.2 Double-entry journal — append-only

```sql
-- Never UPDATE. Never DELETE. Corrections are new, reversing entries.
CREATE TABLE journal_entries (
    entry_id      TEXT PRIMARY KEY,
    txn_ref       TEXT NOT NULL,          -- UPI transaction ID
    rrn           TEXT,
    event_type    TEXT NOT NULL,          -- DEBIT_LEG | CREDIT_LEG | REVERSAL |
                                          -- SETTLEMENT | REFUND | PENALTY
    reverses      TEXT REFERENCES journal_entries(entry_id),
    narration     TEXT NOT NULL,
    posted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE postings (
    posting_id    BIGSERIAL PRIMARY KEY,
    entry_id      TEXT NOT NULL REFERENCES journal_entries(entry_id),
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    direction     TEXT NOT NULL CHECK (direction IN ('DR','CR')),
    amount        NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    currency      TEXT NOT NULL DEFAULT 'INR'
);
```

**Invariant, enforced on every write:**
`SUM(amount WHERE direction='DR') = SUM(amount WHERE direction='CR')` per `entry_id`.
An entry that does not balance is rejected. This single rule is what makes the
simulator behave like a real ledger.

### 3.3 Balances are computed, not stored

Sign convention — customer and pool accounts are **liabilities** of the bank:

```
ledger_balance(acct)    = SUM(CR) - SUM(DR)          -- liability accounts
ledger_balance(acct)    = SUM(DR) - SUM(CR)          -- asset accounts (NOSTRO)
available_balance(acct) = ledger_balance(acct) - SUM(active holds)
```

A materialised `balances` table may be kept as a cache, but it is rebuilt from
`postings` and never written to directly. A startup assertion should recompute every
balance from the journal and refuse to boot on a mismatch.

### 3.4 Holds / earmarks

A real debit is two steps: earmark, then post.

```sql
CREATE TABLE holds (
    hold_id     TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    txn_ref     TEXT NOT NULL,
    amount      NUMERIC(14,2) NOT NULL,
    status      TEXT NOT NULL,   -- ACTIVE | CAPTURED | RELEASED | EXPIRED
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);
```

This is what makes `available_balance < ledger_balance` possible, which is what the
customer actually sees in a real app — and it closes the double-spend race that two
concurrent debits against a mutable column would otherwise allow.

### 3.5 How money actually moves — P2M, happy path

Four balanced entries. **No entry ever spans two banks.**

```
① Remitter bank — earmark
   HOLD  ₹500 on  you@mockbank                       (no posting yet)

② Remitter bank — debit leg
   DR    you@mockbank            ₹500
   CR    REMITTER_UPI_POOL       ₹500
   → hold CAPTURED

③ NPCI — obligation recorded (not a posting; an inter-bank claim)
   MOCKBANK owes MOCKBANK2 ₹500, rrn=…

④ Beneficiary bank — credit leg
   DR    BENEFICIARY_UPI_POOL    ₹500
   CR    sharmastore@mockbank2   ₹500

⑤ At settlement cycle — each bank, against its RBI current account
   Remitter:     DR REMITTER_UPI_POOL     ₹500   CR NOSTRO_RBI  ₹500
   Beneficiary:  DR NOSTRO_RBI            ₹500   CR BENEFICIARY_UPI_POOL ₹500
```

Each bank's books balance on their own at every instant. The inter-bank imbalance lives
at NPCI as an *obligation* until cycle settlement. That is the real design, and it is
what makes steps ② and ④ independently failable.

### 3.6 Reversal

If ④ fails, the remitter bank posts a **new** entry — it never touches ②:

```
⑥ Remitter bank — reversal
   DR    REMITTER_UPI_POOL       ₹500
   CR    you@mockbank            ₹500
   journal_entries.reverses = <entry_id of ②>
```

The customer's statement correctly shows a debit *and* a credit, which is what a real
statement shows. Deleting or mutating ② would be the fintech equivalent of forging books.

---

## 4. Message flow and API verbs

Rename endpoints to the NPCI verb set. This is cheap and it is the clearest signal that
the author read the spec.

| Current | Target | Meaning |
|---|---|---|
| `GET /resolve/{vpa}` | `ReqValAdd` / `RespValAdd` | Validate a VPA, return the masked holder name |
| `GET /npci/discover-accounts` | `ReqListAccount` / `RespListAccount` | List accounts for a verified mobile number |
| *(onboarding step 1)* | `ReqRegMob` / `RespRegMob` | Device + mobile binding |
| `POST /debit`, `POST /credit` | `ReqPay` / `RespPay` | The pay request, both legs |
| *(none)* | `ReqChkTxn` / `RespChkTxn` | Status enquiry — the reconciliation sweep |
| *(none)* | `ReqComplaint` / `RespComplaint` | Dispute raising (UDIR) |
| *(none)* | `ReqMandate` | Autopay / recurring mandates |

### Sequence — P2M pay, happy path

```
app → payerPSP    : initiate(payeeVpa, amount, orderId)
payerPSP → NPCI   : ReqValAdd(payeeVpa)
NPCI → payeePSP   : resolve
payeePSP → NPCI   : RespValAdd(maskedName, mcc)        [code 00]
payerPSP → app    : show confirm screen (name + amount)
app → payerPSP    : authorise(credential block)
payerPSP → NPCI   : ReqPay(txnId, rrn, amount, credBlock)
NPCI → remitter   : ReqPay  → HOLD → DEBIT → RespPay   [code 00, authCode]
NPCI              : record obligation(MOCKBANK → MOCKBANK2, ₹500, rrn)
NPCI → beneficiary: ReqPay  → CREDIT → RespPay         [code 00]
NPCI → payerPSP   : RespPay(00, rrn)
NPCI → payeePSP   : RespPay(00, rrn) → merchant webhook
…later…
NPCI              : settlement cycle → net positions → ⑤ postings at both banks
```

**Credential block:** the MPIN/biometric is never sent to the PSP in clear. Today
`services/mock_payer_bank/bank_service.py` already prefers a `step_up_token` over a raw
PIN, with the raw path marked legacy — that is the right instinct and should be
finished by deleting the legacy branch.

---

## 5. Transaction state machine

Today `transactions.status` is a free-text column with no enforced transitions. Target:

```
                    ┌──────────────┐
                    │  INITIATED   │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐   invalid VPA / limits
                    │  VALIDATED   │────────────────────────▶ VALIDATION_FAILED
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐   wrong MPIN / declined
                    │  AUTHORISED  │────────────────────────▶ AUTH_FAILED
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐   insufficient funds
                    │   DEBITED    │────────────────────────▶ DEBIT_FAILED
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐   invalid/frozen payee a/c
                    │   CREDITED   │──────────┬─────────────▶ CREDIT_FAILED
                    └──────┬───────┘          │                     │
                           ▼                  │                     ▼
                    ┌──────────────┐          │            ┌─────────────────┐
                    │   SUCCESS    │          │            │ REVERSAL_PENDING│
                    └──────┬───────┘          │            └────────┬────────┘
                           ▼                  │                     ▼
                    ┌──────────────┐          │               ┌──────────┐
                    │   SETTLED    │          │               │ REVERSED │
                    └──────┬───────┘          │               └──────────┘
                           ▼                  ▼
                    ┌──────────────┐   ┌──────────────┐
                    │   DISPUTED   │   │   DEEMED_*   │  ← no response within timeout
                    └──────┬───────┘   └──────┬───────┘
                           ▼                  ▼
                    ┌──────────────┐    ReqChkTxn sweep resolves
                    │   ADJUSTED   │    to SUCCESS / REVERSAL_PENDING
                    └──────────────┘
```

Rules:

- Transitions are **validated in code**; an illegal transition raises, it does not silently write.
- **Timeout ≠ failure.** A timed-out leg goes to `DEEMED_*` and is resolved only by
  `ReqChkTxn`. The current code already carries this comment at
  `services/mock_psp/psp_service.py` — the state machine should enforce it.
- Every transition appends to the hop log with an RRN and a response code.

---

## 6. Response codes

The single cheapest fidelity win. Today failures return HTTP 402/401 with prose.
Target: every outcome carries a UPI response code, and the UI shows it.

> Representative subset. The full NPCI list is long and versioned — verify before use.

| Code | Meaning | Where it fires |
|---|---|---|
| `00` | Approved / success | Happy path |
| `ZM` | Invalid MPIN | Auth service, wrong PIN |
| `ZA` | Transaction declined by customer | User cancels on confirm screen |
| `Z9` | Insufficient funds | Remitter bank, available balance check |
| `ZX` | Invalid / closed / frozen account | Either bank |
| `XB` `XD` `XH` | Invalid beneficiary account | Beneficiary bank |
| `U16` | Risk threshold exceeded | Velocity / fraud rules |
| `U28` | Beneficiary PSP unavailable | Payee PSP down |
| `U30` | Debit failure | Remitter leg |
| `U31` | Credit failure → **triggers reversal** | Beneficiary leg |
| `U69` | Collect request expired | Payee-PSP collect flow |
| `BT` | Acquirer / beneficiary timeout | Timeout on either leg |
| `YG` | Beneficiary bank system failure | Infra failure |

Implementation: a single `upi_codes.py` module shared by all services, mapping
`code → (short_description, customer_message, is_retryable, triggers_reversal)`. Both
the API response and the frontend failure screen render from it.

---

## 7. Settlement — multilateral netting

Today `run_settlement_cycle()` in `services/mock_psp/psp_service.py` counts debits and
credits and stamps a batch ID. `settlement_batches.from_bank` / `to_bank` are scalars,
so an N-bank matrix cannot be expressed.

Target: settlement moves to the **switch** (NPCI owns it, not a PSP), and runs as true
multilateral net settlement.

**Step 1 — build the gross obligation matrix** for the cycle:

```
            to→   MOCKBANK   MOCKBANK2   HDFCBANK
  from↓
  MOCKBANK            —        12,400      3,100
  MOCKBANK2        1,900          —          800
  HDFCBANK         2,200        4,600         —
```

**Step 2 — reduce to a net position per bank** (`owed_to − owes`):

```
  MOCKBANK   : (1,900 + 2,200) − (12,400 + 3,100) = −11,400   (net payer)
  MOCKBANK2  : (12,400 + 4,600) − (1,900 +   800) = +14,300   (net receiver)
  HDFCBANK   : (3,100 +   800) − (2,200 + 4,600)  =  −2,900   (net payer)
                                            SUM   =       0   ← invariant
```

**Step 3 — post against each bank's RBI current account** (entry ⑤ in §3.5). The sum
of all net positions must be exactly zero; a non-zero sum is a hard failure.

Schema change:

```sql
CREATE TABLE settlement_cycles (
    cycle_id      TEXT PRIMARY KEY,
    cycle_number  INT  NOT NULL,
    opened_at     TIMESTAMPTZ NOT NULL,
    closed_at     TIMESTAMPTZ,
    txn_count     INT  NOT NULL DEFAULT 0,
    gross_value   NUMERIC(16,2) NOT NULL DEFAULT 0,
    status        TEXT NOT NULL      -- OPEN | NETTING | SETTLED | FAILED
);

CREATE TABLE settlement_positions (        -- one row per bank per cycle
    cycle_id      TEXT NOT NULL REFERENCES settlement_cycles(cycle_id),
    bank_code     TEXT NOT NULL,
    gross_owed    NUMERIC(16,2) NOT NULL,
    gross_due     NUMERIC(16,2) NOT NULL,
    net_position  NUMERIC(16,2) NOT NULL,  -- + receiver, − payer
    PRIMARY KEY (cycle_id, bank_code)
);
```

---

## 8. Exceptions: reversals, disputes, TAT

The current code detects the partial-failure case at
`services/mock_psp/psp_service.py` and logs "reversal initiated" — but nothing reverses.

### 8.1 Auto-reversal

A `CREDIT_FAILED` (`U31`) or an unresolved `DEEMED_CREDIT` past its deadline moves the
transaction to `REVERSAL_PENDING`. A reversal worker posts entry ⑥ (§3.6) and moves it
to `REVERSED`.

### 8.2 Adjustment flows

| Flow | Raised by | Meaning |
|---|---|---|
| **TCC** (transaction credit confirmation) | Beneficiary bank | "We did credit the payee" — resolves a dispute in favour of success |
| **RET** (return) | Beneficiary bank | "We could not credit" — returns funds to the remitter |
| **UDIR** | Customer, in-app | Dispute raised against a transaction; drives TCC/RET |

### 8.3 TAT compensation

RBI's turn-around-time harmonisation requires that a failed transaction's debit be
reversed within a prescribed window, failing which the customer is owed a **daily
compensation**. Modelling this is the standout detail of the whole project:

- A `reversal_deadline` timestamp set when the transaction enters `REVERSAL_PENDING`.
- A worker that, past the deadline, posts a penalty entry per day:
  `DR PENALTY_EXPENSE / CR customer_account`.
- A "compensation" line visible in the customer's transaction history.

> Verify the current window and per-day amount against the governing circular.

---

## 9. Acquiring side

Largely absent today. A real acquirer needs:

**Merchant onboarding**

```sql
CREATE TABLE merchants (
    mid            TEXT PRIMARY KEY,       -- merchant ID
    legal_name     TEXT NOT NULL,
    brand_name     TEXT NOT NULL,
    mcc            TEXT NOT NULL,          -- merchant category code
    vpa            TEXT NOT NULL UNIQUE,
    settlement_account TEXT NOT NULL,
    kyc_status     TEXT NOT NULL,          -- PENDING | VERIFIED | REJECTED
    risk_category  TEXT NOT NULL,
    onboarded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE terminals (
    tid TEXT PRIMARY KEY, mid TEXT REFERENCES merchants(mid), type TEXT  -- QR | POS | ONLINE
);
```

**P2M vs P2P** — different limits, different settlement, and interchange applies only to
certain P2M categories (notably PPI-funded transactions above a threshold). The
transaction record needs a `txn_type` and the pricing engine needs to read MCC.

**Merchant settlement** — distinct from inter-bank settlement. The acquirer holds
merchant funds in a nodal/escrow account and settles to the merchant's bank account on a
T+1 basis, net of charges, with an **MIS / reconciliation file** per settlement:

```
settlement_id, mid, cycle_date, txn_count, gross_amount,
charges, tax_on_charges, refunds, chargebacks, net_payable, utr
```

**Refunds** — merchant-initiated, a *different object* from a reversal. A refund is a
fresh transaction with its own RRN that references the original; a reversal is a
correcting entry on a failed one. Conflating them is a common tell.

**Dynamic vs static QR** — static encodes just the VPA; dynamic encodes VPA + amount +
a transaction reference, and expires. Today the QR button is a stub.

---

## 10. Risk and limits

`check_velocity()` in `services/mock_psp/psp_service.py` is a good start. A realistic
rule set:

| Rule | Shape |
|---|---|
| Per-transaction cap | Standard cap, with higher caps for specific categories (verify current values) |
| Daily count cap | N transactions per VPA per day |
| Daily value cap | Aggregate per VPA per day |
| New-payee cooling | First payment to a new payee above a threshold is delayed / capped for 24h |
| Device binding | One active device per VPA — **already implemented** (`frontend/app.js`, `DEVICE_ACCOUNTS_KEY`) |
| Velocity / anomaly | Sudden deviation from the payer's normal pattern → `U16` |

Each breach returns a response code, not prose.

---

## 11. Reconciliation

Per-service SQLite is **correct** — each bank owning its own books is how it really
works. The weakness is that Supabase is currently a shared mirror, which blurs the
ownership boundary.

Target: Supabase becomes a **reporting/analytics sink only**, never a source of truth.
Inter-party truth is established by a daily recon job:

1. NPCI publishes a **raw data file** for the cycle (every transaction, with RRN).
2. Each bank produces its own extract from its journal.
3. The recon job three-way matches on RRN: NPCI file ↔ remitter book ↔ beneficiary book.
4. Mismatches are written to a `recon_breaks` table with a break type:
   `MISSING_AT_REMITTER`, `MISSING_AT_BENEFICIARY`, `AMOUNT_MISMATCH`,
   `STATUS_MISMATCH`, `DUPLICATE`.
5. Breaks drive TCC/RET.

A dashboard screen showing zero breaks after a day of traffic — and showing a break when
one is deliberately injected — demonstrates the whole model works.

---

## 12. Gap map: current → target

| # | Gap | Where it lives today | Target section |
|---|---|---|---|
| ~~1~~ | ~~Mutable balance column~~ | **DONE** — journal in `services/ledger.py`, balances derived | §3.2 |
| 2a | ~~No pool/suspense accounts~~ | **DONE** — both legs post via each bank's POOL | §3.5 |
| 2b | Banks still call each other directly (not via the switch) | `services/mock_psp/psp_service.py` `process_payment()` | §1 |
| 3 | No response codes | all services, prose in `HTTPException` | §6 |
| 4 | Single identifier | `generate_txn_id()` | §2 |
| 5 | Settlement is counting, not netting | `run_settlement_cycle()` | §7 |
| 6a | ~~No reversal mechanism~~ | **DONE** — `POST /reverse` posts a balanced reversing entry | §3.6 |
| 6b | Nothing *calls* the reversal automatically | `process_payment()`, `partial_failure` branch | §8.1 |
| 7 | No payee PSP | — | §1 |
| 8 | Free-text status, no transition rules | `transactions.status` in `schema.sql` | §5 |
| 9 | Acquiring side absent | — | §9 |
| 10 | No reconciliation | — | §11 |
| 11 | Legacy raw-PIN debit path still live | `services/mock_payer_bank/bank_service.py` | §4 |
| 12 | Supabase as shared source of truth | `services/supabase_db.py` | §11 |

### Already done

**Phase 1, part 1 — the money model** (verified by test, see §14)

- `services/ledger.py`: append-only journal, integer paise, `SUM(DR) = SUM(CR)`
  enforced on every write, holds/earmarks, derived balances, `assert_books_balance()`
- Both banks bootstrap the ledger and migrate legacy balances as opening entries
- Debit posts `DR customer / CR pool`; credit posts `DR pool / CR merchant`
- `POST /reverse` returns pooled funds without mutating the original entry
- `GET /ledger/trial-balance`, `GET /ledger/statement/{vpa}`
- Concurrency: `BEGIN IMMEDIATE` around every read-check-write

**Earlier**

- Idempotent debit keyed on `txn_id`
- Step-up token auth preferred over raw PIN
- Velocity limits
- Hop log and route log
- Device binding on the client
- No account-enumeration endpoint

---

## 13. Phased migration

**Phase 1 — the money model** (gaps 1, 2, 8)
Journal + postings + holds in both banks. Pool accounts. Rewrite the debit and credit
legs as balanced entries. Remove the direct bank-to-bank call; route both legs through
the switch. Add the state machine with validated transitions.
*Exit test:* every account's balance recomputes exactly from the journal; the sum of all
postings across both banks is zero.

**Phase 2 — the language** (gaps 3, 4, 11)
`upi_codes.py`. RRN and the full identifier set. Rename to NPCI verbs. Delete the
legacy PIN branch. Render codes in the UI.
*Exit test:* every failure screen shows a real code and its customer message.

**Phase 3 — exceptions** (gap 6)
Reversal worker, `ReqChkTxn` sweep for `DEEMED_*`, TAT compensation clock.
*Exit test:* kill the payee bank mid-flow; the debit auto-reverses and the customer's
history shows debit + reversal.

**Phase 4 — settlement and recon** (gaps 5, 10, 12)
Netting matrix at the switch. Net-position postings. Daily recon with break detection.
Supabase demoted to a reporting sink.
*Exit test:* net positions sum to zero; an injected break is detected and reported.

**Phase 5 — acquiring** (gaps 7, 9)
Payee PSP service. Merchant onboarding, MID/TID/MCC. P2M vs P2P. Dynamic QR. T+1
merchant settlement with an MIS file. Refunds and UDIR disputes.
*Exit test:* a merchant can be onboarded, paid by QR, refunded, and settled T+1 with a
downloadable MIS file.

Phases 1 and 2 are where the credibility is. A reviewer who opens this repo will believe
it the moment they see a balanced journal and a `ZM` response code.


---

## 14. Verification log

Evidence for claims made above. Re-runnable against a running stack.

### Double-spend, before the fix

Five concurrent `POST /debit` of ₹600 against a ₹1,000 account:

```
req1..req5 → all HTTP 200 "debited"
new_balance reported: -2000, -200, -1400, -800, 400   (each caller told a different figure)
FINAL BALANCE: -2000.00
```

Cause: `SELECT balance` → `if balance < amount` → `UPDATE` ran as three statements
with no transaction boundary, in FastAPI's threadpool (`def debit`, not `async def`).
All five reads saw ₹1,000.

### After the fix

Same test, with `BEGIN IMMEDIATE` wrapping the read-check-write:

```
req4 → 200 debited, new_balance 400.00
req1,2,3,5 → 402 "Insufficient balance. Available: ₹400.00, Required: ₹600.00"
FINAL BALANCE: 400.00
```

Idempotency under concurrency, 3 simultaneous requests with the same `txn_id`:
one real debit, two `duplicate: true`, balance moved exactly once.

### Ledger invariants (unit test)

```
unbalanced entry (DR 100 / CR 110)  → rejected: UnbalancedEntry
hold of 300 on 1000                 → ledger=1000.00  available=700.00
debit leg                           → customer 700.00   pool 300.00
reversal                            → customer 1000.00  pool 0.00, original entry intact
0.1 + 0.2 in paise                  → exactly 30 paise
```

### Money conservation, end to end

```
                        before      after debit    after credit
payer you@mockbank    46,400.00      46,150.00       46,150.00
payer POOL               250.00         500.00          500.00
payee POOL              -250.00        -250.00         -500.00
payee swiggy           3,900.00       3,900.00        4,150.00
books balanced          both           both            both
```

Payer POOL `+500` and payee POOL `−500` are the two sides of the unsettled
inter-bank obligation. They net to zero against each bank's NOSTRO at settlement.

### Partial failure — the money-destruction bug

Payee bank killed mid-flow, ₹700 debit:

```
after failed credit:  customer 45,450.00   POOL 1,200.00   ← the 700 is in the pool
after POST /reverse:  customer 46,150.00   POOL   500.00   ← returned to the customer
```

Before this work the ₹700 left the payer and arrived nowhere. The customer statement
now shows both legs, as a real statement would:

```
DEBIT_LEG   DR   700.00   45,450.00   UPI debit ₹700.00 you@mockbank → swiggy@…
REVERSAL    CR   700.00   46,150.00   Reversal of ₹700.00 — Credit leg failed
```

The reversal entry carries `reverses = <original entry_id>`; the original debit is
still on file, unmodified.
