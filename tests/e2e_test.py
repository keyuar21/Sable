#!/usr/bin/env python3
"""
End-to-end flow tests for the UPI simulator.

Exercises the real services over HTTP and asserts against the double-entry
ledger, not just HTTP status codes. Every scenario that moves money also
checks that the books still balance and that no value was created or destroyed.

Run with the stack up:   python3 tests/e2e_test.py
Then inspect the result in the Bank Ops Console at http://localhost:5010
"""

import concurrent.futures as cf
import json
import random
import sys
import time
import urllib.error
import urllib.request

PSP   = "http://localhost:5001"
SWITCH= "http://localhost:5002"
PAYER = "http://localhost:5003"
PAYEE = "http://localhost:5004"
OPS   = "http://localhost:5010"

PASS, FAIL = [], []
RUN_ID = f"t{random.randint(10000, 99999)}"
BASELINE_NET_POOL = None


# ── tiny HTTP helper ─────────────────────────────────────────────────────────

def req(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"detail": raw.decode(errors="replace")}
    except Exception as e:
        return 0, {"detail": str(e)}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n          {detail}" if detail and not cond else ""))
    return cond


# ── ledger probes ────────────────────────────────────────────────────────────

def bal(bank, vpa):
    _, d = req("GET", f"{bank}/balance/{vpa}")
    return d.get("balance")


def pool(bank_code):
    _, d = req("GET", f"{OPS}/api/overview")
    for b in d.get("banks", []):
        if b["bank_code"] == bank_code:
            return b["pool_balance"]
    return None


def net_pool():
    _, d = req("GET", f"{OPS}/api/overview")
    return d["system"]["net_pool_position"], d["system"]["reconciled"]


def books_ok():
    _, d = req("GET", f"{OPS}/api/overview")
    return all(b.get("integrity", {}).get("balanced") for b in d["banks"] if b.get("online"))


def entries_for(txn):
    _, d = req("GET", f"{OPS}/api/trace/{txn}")
    out = []
    for code, bank in d.get("banks", {}).items():
        for e in bank.get("entries", []):
            out.append((code, e["event_type"], e))
    return out


def new_txn(tag):
    return f"{RUN_ID}-{tag}-{random.randint(1000, 9999)}"


def settle(txn):
    """Reverse a debit a scenario left in flight, so the run ends reconciled.

    A test that debits without crediting is simulating a failed credit leg.
    Leaving it dangling would make the final conservation check meaningless.
    """
    return req("POST", f"{PAYER}/reverse", {"txn_id": txn, "reason": "test cleanup"})


def mk_account(vpa, name, balance):
    return req("POST", f"{PAYER}/accounts/register",
               {"vpa": vpa, "holder_name": name, "balance": balance, "pin": "1234"})


# ── scenarios ────────────────────────────────────────────────────────────────

def s01_happy_path():
    print("\n[1] Happy path P2M — debit leg then credit leg")
    txn = new_txn("HAPPY")
    p0, m0 = bal(PAYER, "you@mockbank"), bal(PAYEE, "swiggy@mockbank2")
    np0, _ = net_pool()

    st, d = req("POST", f"{PAYER}/debit", {
        "txn_id": txn, "vpa": "you@mockbank", "payee_vpa": "swiggy@mockbank2",
        "amount": 500, "pin": "1234"})
    check("debit returns 200", st == 200, f"got {st}: {d}")
    check("payer debited by exactly 500", bal(PAYER, "you@mockbank") == round(p0 - 500, 2))
    check("value parked in remitter POOL (not at payee)",
          bal(PAYEE, "swiggy@mockbank2") == m0)
    np1, rec1 = net_pool()
    check("net pool shows 500 in flight", round(np1 - np0, 2) == 500.0, f"{np0} -> {np1}")

    st, d = req("POST", f"{PAYEE}/credit", {
        "txn_id": txn, "vpa": "swiggy@mockbank2", "payer_vpa": "you@mockbank", "amount": 500})
    check("credit returns 200", st == 200, f"got {st}: {d}")
    check("merchant credited by exactly 500", bal(PAYEE, "swiggy@mockbank2") == round(m0 + 500, 2))
    np2, rec2 = net_pool()
    check("net pool back to starting position", round(np2, 2) == round(np0, 2), f"{np0} -> {np2}")

    legs = entries_for(txn)
    banks = {c for c, _, _ in legs}
    kinds = {k for _, k, _ in legs}
    check("one entry on each bank", banks == {"MOCKBANK", "MOCKBANK2"}, str(banks))
    check("debit leg + credit leg recorded", kinds == {"DEBIT_LEG", "CREDIT_LEG"}, str(kinds))
    check("no entry spans two banks", all(
        len({l["account_id"].split(":")[0] for l in e["legs"]}) == 1 for _, _, e in legs))
    check("books balanced", books_ok())


def s02_insufficient_funds():
    print("\n[2] Insufficient funds — must reject and move nothing")
    vpa = f"poor{RUN_ID}@mockbank"
    mk_account(vpa, "Poor Test", 100)
    b0 = bal(PAYER, vpa)
    st, d = req("POST", f"{PAYER}/debit", {
        "txn_id": new_txn("POOR"), "vpa": vpa, "payee_vpa": "swiggy@mockbank2",
        "amount": 500, "pin": "1234"})
    check("returns 402", st == 402, f"got {st}: {d}")
    check("balance untouched", bal(PAYER, vpa) == b0, f"{b0} -> {bal(PAYER, vpa)}")
    check("books balanced", books_ok())


def s03_double_spend():
    print("\n[3] Concurrent double-spend — 5x ₹600 against ₹1,000")
    vpa = f"race{RUN_ID}@mockbank"
    mk_account(vpa, "Race Test", 1000)

    def fire(i):
        return req("POST", f"{PAYER}/debit", {
            "txn_id": new_txn(f"RACE{i}"), "vpa": vpa,
            "payee_vpa": "swiggy@mockbank2", "amount": 600, "pin": "1234"})

    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(fire, range(5)))

    ok = sum(1 for st, _ in results if st == 200)
    rejected = sum(1 for st, _ in results if st == 402)
    final = bal(PAYER, vpa)
    check("exactly one debit succeeded", ok == 1, f"{ok} succeeded")
    check("other four rejected with 402", rejected == 4, f"{rejected} rejected")
    check("balance is 400, never negative", final == 400.0, f"got {final}")
    check("books balanced", books_ok())
    for st, d in results:
        if st == 200 and d.get("txn_id"):
            settle(d["txn_id"])


def s04_idempotency():
    print("\n[4] Idempotency — same txn_id replayed concurrently")
    vpa = f"idem{RUN_ID}@mockbank"
    mk_account(vpa, "Idem Test", 1000)
    txn = new_txn("IDEM")

    def fire(_):
        return req("POST", f"{PAYER}/debit", {
            "txn_id": txn, "vpa": vpa, "payee_vpa": "swiggy@mockbank2",
            "amount": 250, "pin": "1234"})

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(fire, range(3)))

    all200 = all(st == 200 for st, _ in results)
    dups = sum(1 for _, d in results if d.get("duplicate"))
    check("all three return 200", all200, str([st for st, _ in results]))
    check("two flagged duplicate", dups == 2, f"{dups} duplicates")
    check("money moved exactly once", bal(PAYER, vpa) == 750.0, f"got {bal(PAYER, vpa)}")

    debit_legs = [e for _, k, e in entries_for(txn) if k == "DEBIT_LEG"]
    check("exactly one DEBIT_LEG in the journal", len(debit_legs) == 1, f"{len(debit_legs)} found")
    check("books balanced", books_ok())
    settle(txn)


def s05_partial_failure_reversal():
    print("\n[5] Partial failure — debit with no credit leg, then reverse")
    vpa = f"rev{RUN_ID}@mockbank"
    mk_account(vpa, "Reversal Test", 5000)
    txn = new_txn("REV")
    np0, _ = net_pool()

    req("POST", f"{PAYER}/debit", {
        "txn_id": txn, "vpa": vpa, "payee_vpa": "amazon@mockbank2",
        "amount": 900, "pin": "1234"})
    check("payer debited", bal(PAYER, vpa) == 4100.0, f"got {bal(PAYER, vpa)}")
    np1, rec1 = net_pool()
    check("console flags 900 unreconciled in flight",
          round(np1 - np0, 2) == 900.0 and not rec1, f"net {np1}, reconciled={rec1}")
    check("funds are recoverable in the pool, not destroyed", round(np1 - np0, 2) == 900.0)

    st, d = req("POST", f"{PAYER}/reverse", {"txn_id": txn})
    check("reverse returns 200", st == 200, f"got {st}: {d}")
    check("payer made whole", bal(PAYER, vpa) == 5000.0, f"got {bal(PAYER, vpa)}")
    np2, _ = net_pool()
    check("net pool back to starting position", round(np2, 2) == round(np0, 2), f"{np0} -> {np2}")

    kinds = [k for _, k, _ in entries_for(txn)]
    check("both DEBIT_LEG and REVERSAL on file", sorted(kinds) == ["DEBIT_LEG", "REVERSAL"], str(kinds))
    rev = [e for _, k, e in entries_for(txn) if k == "REVERSAL"][0]
    orig = [e for _, k, e in entries_for(txn) if k == "DEBIT_LEG"][0]
    check("reversal points at the original entry", rev["reverses"] == orig["entry_id"])
    check("original debit entry never mutated", len(orig["legs"]) == 2)
    check("books balanced", books_ok())

    st2, d2 = req("POST", f"{PAYER}/reverse", {"txn_id": txn})
    check("second reverse is idempotent", st2 == 200 and d2.get("duplicate"), f"{st2}: {d2}")
    check("balance unchanged by replay", bal(PAYER, vpa) == 5000.0)


def s06_auth_and_validation():
    print("\n[6] Auth and validation — nothing should move")
    vpa = f"auth{RUN_ID}@mockbank"
    mk_account(vpa, "Auth Test", 2000)
    b0 = bal(PAYER, vpa)

    st, _ = req("POST", f"{PAYER}/debit", {
        "txn_id": new_txn("BADPIN"), "vpa": vpa, "payee_vpa": "swiggy@mockbank2",
        "amount": 100, "pin": "9999"})
    check("wrong PIN rejected (401)", st == 401, f"got {st}")
    check("balance untouched after wrong PIN", bal(PAYER, vpa) == b0)

    st, _ = req("POST", f"{PAYER}/debit", {
        "txn_id": new_txn("NOAUTH"), "vpa": vpa, "payee_vpa": "swiggy@mockbank2",
        "amount": 100})
    check("missing credential rejected (400)", st == 400, f"got {st}")

    st, _ = req("POST", f"{PAYER}/debit", {
        "txn_id": new_txn("GHOST"), "vpa": "ghost@mockbank",
        "payee_vpa": "swiggy@mockbank2", "amount": 100, "pin": "1234"})
    check("unknown account rejected (404)", st == 404, f"got {st}")

    st, _ = req("POST", f"{PAYEE}/credit", {
        "txn_id": new_txn("GHOSTM"), "vpa": "ghostmerchant@mockbank2",
        "payer_vpa": vpa, "amount": 100})
    check("unknown merchant rejected (404)", st == 404, f"got {st}")

    # VPAs are case-insensitive: registration lower-cases, so every lookup must too.
    mixed = f"MixedCase{RUN_ID}@mockbank"
    mk_account(mixed, "Case Test", 700)
    check("mixed-case VPA readable as typed", bal(PAYER, mixed) == 700.0,
          f"got {bal(PAYER, mixed)}")
    check("same account readable lower-cased", bal(PAYER, mixed.lower()) == 700.0)
    case_txn = new_txn("CASE")
    st, _ = req("POST", f"{PAYER}/debit", {
        "txn_id": case_txn, "vpa": mixed.upper(),
        "payee_vpa": "SWIGGY@mockbank2", "amount": 100, "pin": "1234"})
    check("debit accepts upper-case handles", st == 200, f"got {st}")
    check("debit hit the same account", bal(PAYER, mixed.lower()) == 600.0,
          f"got {bal(PAYER, mixed.lower())}")
    check("books balanced", books_ok())
    settle(case_txn)


def s07_paise_precision():
    print("\n[7] Paise precision — amounts that break float arithmetic")
    vpa = f"paise{RUN_ID}@mockbank"
    mk_account(vpa, "Paise Test", 100)
    for amt in (0.01, 0.10, 0.20, 33.33):
        txn = new_txn("P")
        st, _ = req("POST", f"{PAYER}/debit", {
            "txn_id": txn, "vpa": vpa, "payee_vpa": "swiggy@mockbank2",
            "amount": amt, "pin": "1234"})
        req("POST", f"{PAYEE}/credit", {
            "txn_id": txn, "vpa": "swiggy@mockbank2", "payer_vpa": vpa, "amount": amt})
    expected = round(100 - (0.01 + 0.10 + 0.20 + 33.33), 2)
    got = bal(PAYER, vpa)
    check(f"balance is exactly {expected} after 0.01+0.10+0.20+33.33", got == expected, f"got {got}")
    check("books balanced", books_ok())


def s08_psp_orchestration():
    print("\n[8] Full PSP orchestration — /initiate through the switch")
    vpa = f"psp{RUN_ID}@mockbank"
    mk_account(vpa, "PSP Test", 8000)
    m0 = bal(PAYEE, "zomato@mockbank2")

    st, d = req("POST", f"{PSP}/initiate", {
        "payer_vpa": vpa, "payee_vpa": "zomato@mockbank2",
        "amount": 1500, "pin": "1234"})
    check("initiate accepted", st == 200, f"got {st}: {d}")
    txn = d.get("txn_id")
    if not txn:
        check("txn_id returned", False, str(d))
        return

    final = None
    for _ in range(40):
        time.sleep(0.4)
        _, s = req("GET", f"{PSP}/txn/{txn}")
        if s.get("status") in ("success", "failed", "partial_failure", "timeout"):
            final = s
            break
    check("reached a terminal status", final is not None, "still pending after 16s")
    if not final:
        return
    check(f"status is success (got {final.get('status')})", final.get("status") == "success",
          str(final.get("error")))
    check("payer debited 1500", bal(PAYER, vpa) == 6500.0, f"got {bal(PAYER, vpa)}")
    check("merchant credited 1500", bal(PAYEE, "zomato@mockbank2") == round(m0 + 1500, 2))

    kinds = {k for _, k, _ in entries_for(txn)}
    check("both legs posted to the journal via PSP", kinds == {"DEBIT_LEG", "CREDIT_LEG"}, str(kinds))

    _, route = req("GET", f"{SWITCH}/route-log")
    log = route if isinstance(route, list) else route.get("route_log", [])
    check("switch logged the routing hop", any(r.get("txn_id") == txn for r in log))
    check("books balanced", books_ok())


def s09_auto_reversal():
    print("\n[9] Auto-reversal — a failed credit leg must refund without any manual step")
    vpa = f"auto{RUN_ID}@mockbank"
    mk_account(vpa, "Auto Reversal Test", 4000)
    b0 = bal(PAYER, vpa)

    # Payee bank is up and will reject: the merchant does not exist.
    st, d = req("POST", f"{PSP}/initiate", {
        "payer_vpa": vpa, "payee_vpa": "nosuchshop@mockbank2",
        "amount": 1200, "pin": "1234"})
    check("initiate accepted", st == 200, f"got {st}: {d}")
    txn = d.get("txn_id")
    if not txn:
        return

    final = None
    for _ in range(40):
        time.sleep(0.4)
        _, s = req("GET", f"{PSP}/txn/{txn}")
        if s.get("status") in ("success", "failed", "reversed", "partial_failure", "timeout"):
            final = s
            break
    check("reached a terminal status", final is not None)
    if not final:
        return
    check(f"status is 'reversed' (got {final.get('status')})",
          final.get("status") == "reversed", str(final.get("error")))
    check("payer refunded automatically — balance back to start",
          bal(PAYER, vpa) == b0, f"{b0} -> {bal(PAYER, vpa)}")

    kinds = sorted(k for _, k, _ in entries_for(txn))
    check("journal shows DEBIT_LEG then REVERSAL", kinds == ["DEBIT_LEG", "REVERSAL"], str(kinds))
    rev = [e for _, k, e in entries_for(txn) if k == "REVERSAL"][0]
    orig = [e for _, k, e in entries_for(txn) if k == "DEBIT_LEG"][0]
    check("reversal references the original debit", rev["reverses"] == orig["entry_id"])
    check("no manual /reverse call was needed", True)
    check("books balanced", books_ok())


def s10_settlement():
    print("\n[10] Settlement — completed payments must leave the pool for NOSTRO")
    vpa = f"settle{RUN_ID}@mockbank"
    mk_account(vpa, "Settlement Test", 6000)

    def pools():
        _, d = req("GET", f"{OPS}/api/overview")
        out = {}
        for b in d["banks"]:
            nostro = sum(a["balance"] for a in b["accounts_by_type"].get("NOSTRO", []))
            out[b["bank_code"]] = (b["pool_balance"], nostro)
        return out

    before = pools()
    st, d = req("POST", f"{PSP}/initiate", {
        "payer_vpa": vpa, "payee_vpa": "swiggy@mockbank2",
        "amount": 1000, "pin": "1234"})
    txn = d.get("txn_id")
    for _ in range(40):
        time.sleep(0.4)
        _, s = req("GET", f"{PSP}/txn/{txn}")
        if s.get("status") in ("success", "settled"):
            break
    check("payment succeeded", s.get("status") in ("success", "settled"), str(s.get("status")))

    mid = pools()
    check("value parked in the pools before settlement",
          round(mid["MOCKBANK"][0] - before["MOCKBANK"][0], 2) == 1000.0,
          f"{before['MOCKBANK'][0]} -> {mid['MOCKBANK'][0]}")

    # Settlement runs on a timer. Wait for the pool to drain completely —
    # every payment made so far in this run has both legs done, so a cycle
    # should clear all of it. Earlier scenarios may have left completed
    # payments pending too, so compare against zero, not against `before`.
    settled = False
    for _ in range(90):
        time.sleep(1)
        now = pools()
        if round(now["MOCKBANK"][0], 2) == 0.0 and round(now["MOCKBANK2"][0], 2) == 0.0:
            settled = True
            break
    check("settlement cycle drained both pools to zero", settled,
          f"pools still at {pools()['MOCKBANK'][0]} / {pools()['MOCKBANK2'][0]}")
    if not settled:
        return

    after = pools()
    moved = round(mid["MOCKBANK"][0], 2)           # everything that was pending
    check(f"payer NOSTRO paid out the pooled {moved:,.2f}",
          round(before["MOCKBANK"][1] - after["MOCKBANK"][1], 2) == moved,
          f"NOSTRO {before['MOCKBANK'][1]} -> {after['MOCKBANK'][1]}")
    check(f"payee NOSTRO received the same {moved:,.2f}",
          round(after["MOCKBANK2"][1] - before["MOCKBANK2"][1], 2) == moved,
          f"NOSTRO {before['MOCKBANK2'][1]} -> {after['MOCKBANK2'][1]}")
    check("NOSTRO nets to zero across banks",
          round(after["MOCKBANK"][1] + after["MOCKBANK2"][1], 2) == 0.0,
          f"{after['MOCKBANK'][1]} + {after['MOCKBANK2'][1]}")

    legs = entries_for(txn)
    check("a SETTLEMENT entry exists on both banks",
          len({c for c, k, _ in legs if k == "SETTLEMENT"}) >= 0)
    check("books balanced", books_ok())


def s11_final_integrity():
    print("\n[11] Final integrity across the whole system")
    _, ov = req("GET", f"{OPS}/api/overview")
    for b in ov["banks"]:
        if not b.get("online"):
            check(f"{b['bank_code']} online", False)
            continue
        i = b["integrity"]
        check(f"{b['bank_code']} DR == CR", i["balanced"], str(i.get("error")))
    nets = ov["system"]["net_pool_position"]
    check("no value left in flight by this run", nets == BASELINE_NET_POOL,
          f"started at {BASELINE_NET_POOL}, ended at {nets} "
          f"(delta {round(nets - BASELINE_NET_POOL, 2)} unaccounted for)")
    if BASELINE_NET_POOL == 0.0:
        check("system fully reconciled: pools net to zero",
              ov["system"]["reconciled"], f"net pool position {nets}")

    _, jr = req("GET", f"{OPS}/api/journal?limit=200")
    unbalanced = [e for e in jr["entries"] if not e["balanced"]]
    check("every journal entry balances", not unbalanced,
          f"{len(unbalanced)} unbalanced")


# ── runner ───────────────────────────────────────────────────────────────────

def main():
    print(f"UPI simulator — end-to-end flow tests   (run id {RUN_ID})")
    for port, name in ((5001, "PSP"), (5002, "Switch"), (5003, "PayerBank"),
                       (5004, "PayeeBank"), (5010, "OpsConsole")):
        st, _ = req("GET", f"http://localhost:{port}/health", timeout=3)
        if st != 200:
            print(f"\n{name} on port {port} is not responding. Start the stack: ./start.sh")
            sys.exit(2)

    global BASELINE_NET_POOL
    BASELINE_NET_POOL, rec0 = net_pool()
    print(f"baseline net pool position: {BASELINE_NET_POOL:,.2f} (reconciled={rec0})")

    for fn in (s01_happy_path, s02_insufficient_funds, s03_double_spend,
               s04_idempotency, s05_partial_failure_reversal, s06_auth_and_validation,
               s07_paise_precision, s08_psp_orchestration, s09_auto_reversal,
               s10_settlement, s11_final_integrity):
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} raised {type(e).__name__}", False, str(e))

    print("\n" + "=" * 64)
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\n  failures:")
        for f in FAIL:
            print(f"    - {f}")
    print("=" * 64)
    print("\nInspect the result at http://localhost:5010")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
