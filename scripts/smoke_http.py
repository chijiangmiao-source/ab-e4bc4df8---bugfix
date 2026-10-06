#!/usr/bin/env python3
"""HTTP smoke test for power-loss recovery, concurrent adjudication and
multi-device image ownership.

Talks to a *live* uvicorn server (plain stdlib only) and exits non-zero on the
first failed expectation. If the server DB was seeded with
``scripts/make_legacy_db.py``, the already-affected legacy devices are also
checked for safe convergence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
checks = 0


def call(method: str, path: str, body=None, expect=None):
    global checks
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, payload = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read().decode())
    if expect is not None:
        checks += 1
        assert status == expect, f"{method} {path}: expected {expect}, got {status} {payload}"
    return status, payload


def assert_that(cond, text):
    global checks
    checks += 1
    assert cond, text


def power_cycle(dev, expected_slot, expected_gen):
    call("POST", f"/api/devices/{dev}/power-off", {}, 200)
    _, body = call("POST", f"/api/devices/{dev}/power-on", {}, 200)
    rec, d = body["recovery"], body["device"]
    assert_that(rec["active_slot"] == expected_slot,
                f"{dev}: recovery selected {rec['active_slot']}, want {expected_slot}")
    assert_that(rec["generation"] == expected_gen,
                f"{dev}: generation {rec['generation']}, want {expected_gen}")
    return rec, d


def power_cycle_refusal(dev):
    """Power-cycle a device that must *refuse* to boot its persisted image."""
    call("POST", f"/api/devices/{dev}/power-off", {}, 200)
    _, body = call("POST", f"/api/devices/{dev}/power-on", {}, 200)
    rec, d = body["recovery"], body["device"]
    assert_that(body["outcome"] == "unbootable",
                f"{dev}: expected unbootable, got {body['outcome']}")
    assert_that(rec["active_slot"] is None,
                f"{dev}: recovery must refuse the mismatched image")
    assert_that(bool(rec["critical"]), f"{dev}: critical rationale missing")
    return rec, d


def main() -> int:
    # --- health + built page ------------------------------------------------
    _, health = call("GET", "/api/health", expect=200)
    assert_that(health["status"] == "ok", "health status not ok")
    with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
        html = resp.read().decode()
    assert_that("轨道载荷" in html and "/src/main.js" not in html,
                "served page is not the built bundle")

    # --- power-loss scenarios on device s1 ----------------------------------
    call("POST", "/api/devices", {"device_id": "s1", "version": "1.0.0"}, 201)

    # fault 1: cut during candidate write -> incomplete, old slot boots
    _, b = call("POST", "/api/devices/s1/candidate",
                {"version": "2.0.0", "request_id": "w1", "fault_point": "candidate_write"}, 200)
    assert_that(b["outcome"] == "power_cut" and b["fault_point"] == "candidate_write",
                "candidate_write fault not injected")
    rec, d = power_cycle("s1", "A", 1)
    diag = {x["slot"]: x for x in rec["diagnoses"]}
    assert_that(diag["B"]["reason"] == "incomplete_write", "want incomplete_write diagnosis")
    assert_that(d["slots"]["B"]["written"] < d["slots"]["B"]["size"], "partial write expected")

    # fault 2: cut during digest check -> fully written but unproven
    _, b = call("POST", "/api/devices/s1/candidate",
                {"version": "2.0.0", "request_id": "w2", "fault_point": "digest_check"}, 200)
    assert_that(b["outcome"] == "power_cut", "digest_check fault not injected")
    rec, d = power_cycle("s1", "A", 1)
    diag = {x["slot"]: x for x in rec["diagnoses"]}
    assert_that(diag["B"]["reason"] == "unverified_candidate", "want unverified_candidate")
    assert_that(d["slots"]["B"]["actual_digest"] is None, "no verdict may be committed")

    # corrupt candidate -> REJECTED, evidence kept, never boots
    _, b = call("POST", "/api/devices/s1/candidate",
                {"version": "2.0.0", "request_id": "w3", "corrupt": True}, 200)
    assert_that(b["outcome"] == "verification_failed", "corrupt candidate must fail verification")
    assert_that(b["claimed_digest"] != b["actual_digest"], "digests must differ")
    rec, d = power_cycle("s1", "A", 1)
    assert_that(d["slots"]["B"]["status"] == "REJECTED", "corrupt slot must stay REJECTED")

    # normal stage, fault 3: cut at confirm -> old version, candidate unconfirmed
    call("POST", "/api/devices/s1/candidate",
         {"version": "2.0.0", "request_id": "w4"}, 200)
    _, b = call("POST", "/api/devices/s1/confirm", {"fault_point": "confirm_switch"}, 200)
    assert_that(b["outcome"] == "power_cut", "confirm_switch fault not injected")
    rec, d = power_cycle("s1", "A", 1)
    assert_that(d["slots"]["B"]["status"] == "VERIFIED", "candidate remains VERIFIED")

    # commit for real -> B active, generation 2, no rollback after reopen
    _, b = call("POST", "/api/devices/s1/confirm", {}, 200)
    assert_that(b["outcome"] == "switched" and b["generation"] == 2, "switch not committed")
    digest_b = b["device"]["slots"]["B"]["digest"]
    rec, d = power_cycle("s1", "B", 2)
    assert_that(d["slots"]["B"]["version"] == "2.0.0", "reopen version mismatch")
    assert_that(d["slots"]["B"]["digest"] == digest_b, "reopen digest mismatch")
    assert_that(d["slots"]["A"]["status"] == "SUPERSEDED", "old slot must be SUPERSEDED")
    assert_that(any("防回退" in x for x in rec["rationale"]), "no-rollback rationale missing")

    # --- concurrent different candidates on device s2 -----------------------
    call("POST", "/api/devices", {"device_id": "s2", "version": "1.0.0"}, 201)
    barrier = threading.Barrier(2)
    outcomes = []

    def concurrent_submit(version, request_id):
        barrier.wait()
        outcomes.append(call("POST", "/api/devices/s2/candidate",
                             {"version": version, "request_id": request_id}))

    t1 = threading.Thread(target=concurrent_submit, args=("2.0.0", "page-one"))
    t2 = threading.Thread(target=concurrent_submit, args=("2.1.0", "page-two"))
    t1.start(); t2.start(); t1.join(); t2.join()
    statuses = sorted(code for code, _ in outcomes)
    assert_that(statuses == [200, 409], f"concurrent statuses {statuses}")
    winner = next((p for code, p in outcomes if code == 200), None)
    loser = next((p for code, p in outcomes if code == 409), None)
    assert_that(loser["error"]["code"] == "upgrade_conflict", "loser must be upgrade_conflict")
    assert_that(loser["error"]["holder_request"] == winner["request_id"], "holder mismatch")
    _, d = call("GET", "/api/devices/s2", expect=200)
    d = d["device"]
    assert_that(d["active_slot"] == "A" and d["slots"]["A"]["version"] == "1.0.0",
                "loser must not rewrite active version")

    # winner confirms; reopen shows identical slot/version/digest/generation
    _, b = call("POST", "/api/devices/s2/confirm", {}, 200)
    assert_that(b["generation"] == 2 and b["active_slot"] == "B", "winner switch failed")
    before = b["device"]
    rec, after = power_cycle("s2", "B", 2)
    for field in ("status", "version", "digest", "confirmed_generation"):
        assert_that(after["slots"]["B"][field] == before["slots"]["B"][field],
                    f"field {field} changed across reopen")
    assert_that(after["generation"] == before["generation"], "generation changed across reopen")

    # --- multi-device isolation: same A/B slot names, different images ------
    m1_img = b"m1-image::1.0.0::" + bytes((i * 3 + 5) & 0xFF for i in range(200))
    m2_img = b"m2-image::1.0.0::" + bytes((i * 7 + 9) & 0xFF for i in range(200))
    m2_img2 = b"m2-image::2.0.0::" + bytes((i * 11 + 13) & 0xFF for i in range(220))
    m1_digest = hashlib.sha256(m1_img).hexdigest()
    m2_digest = hashlib.sha256(m2_img).hexdigest()
    m2_digest2 = hashlib.sha256(m2_img2).hexdigest()
    assert_that(len({m1_digest, m2_digest, m2_digest2}) == 3,
                "test images must have distinct digests")

    call("POST", "/api/devices",
         {"device_id": "m1", "version": "1.0.0",
          "content_b64": base64.b64encode(m1_img).decode()}, 201)
    call("POST", "/api/devices",
         {"device_id": "m2", "version": "1.0.0",
          "content_b64": base64.b64encode(m2_img).decode()}, 201)

    # m2's creation must not rewrite m1's image, digest or measured digest.
    _, d = call("GET", "/api/devices/m1", expect=200)
    a = d["device"]["slots"]["A"]
    assert_that(a["digest"] == a["actual_digest"] == m1_digest and a["bootable"],
                "m1 slot A changed after m2 creation (cross-device clobber)")

    # m1 power cycle: recovery re-measures m1's own bytes and boots A.
    rec, d = power_cycle("m1", "A", 1)
    a = d["slots"]["A"]
    assert_that(a["digest"] == a["actual_digest"] == m1_digest,
                "m1 digest/active-slot inconsistency after reopen")
    snapshot = d

    def m1_untouched():
        _, cur = call("GET", "/api/devices/m1", expect=200)
        assert_that(cur["device"] == snapshot,
                    "m1 slots/version/candidate/evidence changed by peer activity")

    # Peer power cut mid candidate-write, then recovery: m1 untouched.
    _, b = call("POST", "/api/devices/m2/candidate",
                {"version": "2.0.0", "request_id": "m2-cut",
                 "content_b64": base64.b64encode(m2_img2).decode(),
                 "fault_point": "candidate_write"}, 200)
    assert_that(b["outcome"] == "power_cut", "m2 candidate_write fault not injected")
    m1_untouched()
    power_cycle("m2", "A", 1)
    m1_untouched()

    # Peer stages and commits a full upgrade: m1 untouched at every step.
    _, b = call("POST", "/api/devices/m2/candidate",
                {"version": "2.0.0", "request_id": "m2-up",
                 "content_b64": base64.b64encode(m2_img2).decode()}, 200)
    assert_that(b["outcome"] == "staged", "m2 candidate not staged")
    m1_untouched()
    _, b = call("POST", "/api/devices/m2/confirm", {}, 200)
    assert_that(b["outcome"] == "switched" and b["active_slot"] == "B",
                "m2 confirm switch failed")
    m1_untouched()

    # m1 recovers from a power cut: still its own image/version/generation,
    # and its diagnostic evidence was never touched by m2's activity.
    rec, d = power_cycle("m1", "A", 1)
    a = d["slots"]["A"]
    assert_that(a["version"] == "1.0.0" and a["status"] == "CONFIRMED",
                "m1 confirmed version changed")
    assert_that(a["digest"] == a["actual_digest"] == m1_digest,
                "m1 digest mismatch after peer upgrade")
    assert_that(d["slots"]["B"]["status"] == "EMPTY", "m1 candidate stage leaked")
    assert_that(d["evidence"] == snapshot["evidence"], "m1 evidence changed by peer")

    # m2 recovered onto its own upgraded slot with its own digest.
    rec, d2 = power_cycle("m2", "B", 2)
    slot_b = d2["slots"]["B"]
    assert_that(slot_b["digest"] == slot_b["actual_digest"] == m2_digest2,
                "m2 digest/active-slot inconsistency after reopen")

    # --- legacy contaminated devices: safe convergence ----------------------
    _, listing = call("GET", "/api/devices", expect=200)
    legacy_ids = {x["device_id"] for x in listing["devices"]}
    if "legacy-jia" in legacy_ids:
        # Seeded by scripts/make_legacy_db.py: jia's confirmed slot-A manifest
        # claims its own digest while the persisted image belongs to yi.
        rec, d = power_cycle_refusal("legacy-jia")
        diag = {x["slot"]: x["reason"] for x in rec["diagnoses"]}
        assert_that(diag.get("A") == "digest_mismatch",
                    f"legacy-jia: want digest_mismatch diagnosis, got {diag}")
        a = d["slots"]["A"]
        assert_that(a["status"] == "CONFIRMED" and not a["bootable"],
                    "legacy-jia: CONFIRMED retained but must not be bootable")
        assert_that(a["digest"] != a["actual_digest"],
                    "legacy-jia: manifest vs measured digests must stay reviewable")
        mism = [e for e in d["evidence"]
                if e["reason"] == "digest_mismatch" and e["slot"] == "A"]
        assert_that(len(mism) == 1 and a["digest"] in mism[0]["detail"]
                    and a["actual_digest"] in mism[0]["detail"],
                    "legacy-jia: reviewable digest_mismatch evidence missing")

        # The healthy peer keeps booting its own image...
        rec_yi, d_yi = power_cycle("legacy-yi", "A", 1)
        ay = d_yi["slots"]["A"]
        assert_that(ay["digest"] == ay["actual_digest"] and ay["bootable"],
                    "legacy-yi: healthy slot damaged by peer recovery")

        # ...and jia converges: same refusal, no duplicated evidence, and yi's
        # slot is never overwritten by jia's recovery.
        rec2, d2 = power_cycle_refusal("legacy-jia")
        mism2 = [e for e in d2["evidence"] if e["reason"] == "digest_mismatch"]
        assert_that(len(mism2) == 1, "legacy-jia: evidence not convergent")
        rec_yi2, d_yi2 = power_cycle("legacy-yi", "A", 1)
        assert_that(d_yi2["slots"]["A"] == ay, "legacy-yi: slot changed by peer recovery")

        # jia2 already ran 2.0.0 on B (A SUPERSEDED) but B's image is foreign:
        # refuse B, never roll back to the SUPERSEDED A.
        rec, d = power_cycle_refusal("legacy-jia2")
        diag = {x["slot"]: x["reason"] for x in rec["diagnoses"]}
        assert_that(diag.get("A") == "superseded_no_rollback",
                    f"legacy-jia2: rollback guard missing, got {diag}")
        assert_that(diag.get("B") == "digest_mismatch",
                    f"legacy-jia2: want digest_mismatch for B, got {diag}")
        assert_that(d["slots"]["A"]["status"] == "SUPERSEDED",
                    "legacy-jia2: superseded slot must stay superseded")
        assert_that(any(e["reason"] == "digest_mismatch" and e["slot"] == "B"
                        for e in d["evidence"]),
                    "legacy-jia2: digest_mismatch evidence for B missing")
        rec_y2, d_y2 = power_cycle("legacy-yi2", "B", 2)
        by2 = d_y2["slots"]["B"]
        assert_that(by2["digest"] == by2["actual_digest"] and by2["bootable"],
                    "legacy-yi2: healthy upgraded slot damaged")

    print(f"SMOKE OK: {checks} HTTP assertions passed "
          f"(power-loss x3, corrupt candidate, concurrent 409, reopen consistency, "
          f"multi-device isolation, legacy safe-convergence)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
