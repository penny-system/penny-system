"""
test_hourly_dedup.py
Unit test for the hourly dedup logic in notify_scan.py.
No IBKR, Telegram, or pipeline required.
"""
import json
import os
import tempfile
import sys

# -- Replicate the exact logic from notify_scan.py --------------------------

MAX_ITEMS_IN_MESSAGE = 8

def build_signature(new_recs: list) -> str:
    """Mirrors notify_scan.py line 439."""
    return "|".join([f"{r[0]}:{int(r[7] or 0)}" for r in new_recs[:MAX_ITEMS_IN_MESSAGE]])

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_action_run_id": 0, "last_action_signature": "", "sent_trigger_a": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        if "sent_trigger_a" not in s or not isinstance(s["sent_trigger_a"], dict):
            s["sent_trigger_a"] = {}
        return s
    except Exception:
        return {"last_action_run_id": 0, "last_action_signature": "", "sent_trigger_a": {}}

def save_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)

def would_send(new_recs: list, state_path: str) -> tuple[bool, str]:
    """
    Returns (send: bool, reason: str).
    Mirrors the dedup gate in notify_scan.py lines 439-444.
    Does NOT actually send or save — caller controls that.
    """
    if not new_recs:
        return False, "no new_recs"
    signature = build_signature(new_recs)
    state = load_state(state_path)
    if state.get("last_action_signature", "") == signature:
        return False, f"duplicate signature: {signature}"
    return True, f"new signature: {signature}"

def simulate_send(new_recs: list, run_id: int, state_path: str):
    """Simulate a successful send — saves state exactly as notify_scan.py does."""
    signature = build_signature(new_recs)
    state = load_state(state_path)
    state["last_action_run_id"] = run_id
    state["last_action_signature"] = signature
    save_state(state_path, state)

# -- Fake rec tuples: (symbol, score, conf, risk, ref_price, stop, take, qty, rationale) --

def make_rec(sym, qty=100):
    return (sym, 85.0, 0.75, 30.0, 3.50, 3.00, 4.50, qty, "volume surge 2.5x normal.")

RECS_A = [make_rec("SOFI", 200), make_rec("RIVN", 150)]
RECS_B = [make_rec("SOFI", 200), make_rec("RIVN", 150)]   # identical to A
RECS_C = [make_rec("SOFI", 200), make_rec("PLTR", 100)]   # different symbol
RECS_D = [make_rec("SOFI", 999), make_rec("RIVN", 150)]   # same symbols, different qty

# -- Tests ------------------------------------------------------------------─

def run_tests():
    passed = 0
    failed = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = os.path.join(tmpdir, "notify_state.json")

        def check(name, actual_send, actual_reason, expect_send, expect_reason_contains=""):
            nonlocal passed, failed
            ok = (actual_send == expect_send)
            if expect_reason_contains:
                ok = ok and (expect_reason_contains in actual_reason)
            status = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            print(f"  [{status}] {name}")
            if not ok:
                print(f"         expected send={expect_send}, got send={actual_send}")
                print(f"         reason: {actual_reason}")

        print("\n-- Test 1: Fresh state (no prior sends) ----------------------")
        send, reason = would_send(RECS_A, state_path)
        check("First run should send", send, reason, True, "new signature")
        simulate_send(RECS_A, run_id=10, state_path=state_path)

        print("\n-- Test 2: Same candidates, new run_id ----------------------─")
        # This is the bug that was fixed: old code used run_id in check,
        # so a new run_id always bypassed dedup. New code: signature only.
        send, reason = would_send(RECS_B, state_path)
        check("Identical candidates should NOT resend", send, reason, False, "duplicate signature")

        print("\n-- Test 3: Different symbol ----------------------------------")
        send, reason = would_send(RECS_C, state_path)
        check("Different symbol should send", send, reason, True, "new signature")
        simulate_send(RECS_C, run_id=11, state_path=state_path)

        print("\n-- Test 4: Same symbols, different qty ----------------------─")
        # qty is part of the signature so this should be treated as new
        send, reason = would_send(RECS_D, state_path)
        check("Different qty should send", send, reason, True, "new signature")
        simulate_send(RECS_D, run_id=12, state_path=state_path)

        print("\n-- Test 5: Repeat of RECS_D (no change) --------------------─")
        send, reason = would_send(RECS_D, state_path)
        check("Repeat after save should NOT resend", send, reason, False, "duplicate signature")

        print("\n-- Test 6: Empty new_recs ------------------------------------")
        send, reason = would_send([], state_path)
        check("Empty new_recs should not send", send, reason, False, "no new_recs")

        print("\n-- Test 7: Signature includes up to MAX_ITEMS_IN_MESSAGE ----─")
        # Build 10 recs; only first 8 should be in signature
        many = [make_rec(f"SYM{i}", qty=100) for i in range(10)]
        sig_10 = build_signature(many)
        sig_8  = build_signature(many[:8])
        ok = sig_10 == sig_8
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] Signature truncates at MAX_ITEMS_IN_MESSAGE={MAX_ITEMS_IN_MESSAGE}")

        print("\n-- Test 8: State file persists run_id correctly --------------")
        simulate_send(RECS_A, run_id=99, state_path=state_path)
        state = load_state(state_path)
        ok = state["last_action_run_id"] == 99
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] run_id saved as 99, got {state['last_action_run_id']}")

    print(f"\n{'─'*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
