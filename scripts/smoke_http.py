#!/usr/bin/env python3
"""HTTP smoke test for power-loss recovery and concurrent adjudication.

Talks to a *live* uvicorn server (plain stdlib only) and exits non-zero on the
first failed expectation.

Coverage:
  * single-device normal upgrade, all three power-cut points, corrupt-candidate
    rejection, reopen consistency, concurrent two-page upgrade qualification;
  * TWO devices initialised at the same version with different bytes/digests;
  * one device's candidate stage / confirmed switch never changes the other
    device's image, confirmed version, candidate stage or evidence;
  * digest <-> active-slot consistency after the other device's upgrade;
  * an already-contaminated persistent slot (legacy shared A/B bytes) safely
    refuses to boot on reopen, keeps reviewable evidence across reopens, never
    rolls back to a SUPERSEDED slot and never touches the healthy device.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
DB_PATH = os.environ.get("SMOKE_DB")
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


def _raw_conn():
    assert DB_PATH, "SMOKE_DB must point at the server's SQLite file"
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def raw_blob(device_id, slot):
    conn = _raw_conn()
    try:
        row = conn.execute(
            "SELECT content FROM blobs WHERE device_id=? AND slot=?",
            (device_id, slot),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def set_raw_blob(device_id, slot, content):
    # Directly plant bytes on flash, bypassing the API: this models a record
    # already corrupted by the legacy slot-only blob layout.
    conn = _raw_conn()
    try:
        conn.execute(
            "INSERT INTO blobs (device_id, slot, content) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id, slot) DO UPDATE SET content=excluded.content",
            (device_id, slot, content),
        )
        conn.commit()
    finally:
        conn.close()


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

    # --- two devices: same version, different bytes/digests -----------------
    call("POST", "/api/devices", {"device_id": "m1", "version": "1.0.0"}, 201)
    call("POST", "/api/devices", {"device_id": "m2", "version": "1.0.0"}, 201)
    _, m1 = call("GET", "/api/devices/m1", expect=200)
    _, m2 = call("GET", "/api/devices/m2", expect=200)
    m1, m2 = m1["device"], m2["device"]
    m1_a, m2_a = m1["slots"]["A"]["digest"], m2["slots"]["A"]["digest"]
    assert_that(m1_a != m2_a, "same-version devices must get distinct digests")
    assert_that(m1["slots"]["A"]["device_id"] == "m1"
                and m2["slots"]["A"]["device_id"] == "m2", "slot owner not stamped")
    assert_that(hashlib.sha256(raw_blob("m1", "A")).hexdigest() == m1_a
                and hashlib.sha256(raw_blob("m2", "A")).hexdigest() == m2_a,
                "persisted blob digests must match each device's own manifest")
    assert_that(raw_blob("m1", "A") != raw_blob("m2", "A"),
                "blobs must be stored per (device, slot), never shared")

    # both devices stage a 2.0.0 candidate (their bytes must still differ)
    call("POST", "/api/devices/m1/candidate",
         {"version": "2.0.0", "request_id": "m1-req"}, 200)
    call("POST", "/api/devices/m2/candidate",
         {"version": "2.0.0", "request_id": "m2-req"}, 200)
    m1 = call("GET", "/api/devices/m1", expect=200)[1]["device"]
    m2 = call("GET", "/api/devices/m2", expect=200)[1]["device"]
    m1_b_claim = m1["slots"]["B"]["digest"]
    m2_b_claim = m2["slots"]["B"]["digest"]
    assert_that(m1_b_claim != m2_b_claim, "same-version candidates must differ per device")
    assert_that(m1["slots"]["B"]["status"] == m2["slots"]["B"]["status"] == "VERIFIED",
                "both candidates staged")
    m1_evidence_before = len(m1["evidence"])

    # only m2 completes the confirmed switch
    _, b = call("POST", "/api/devices/m2/confirm", {}, 200)
    assert_that(b["outcome"] == "switched" and b["active_slot"] == "B", "m2 switch failed")

    # m1 power-recovers: its image, confirmed version AND candidate stage are
    # completely untouched by m2's upgrade.
    rec, d1 = power_cycle("m1", "A", 1)
    assert_that(d1["slots"]["A"]["status"] == "CONFIRMED"
                and d1["slots"]["A"]["version"] == "1.0.0", "m1 active changed")
    assert_that(d1["slots"]["A"]["actual_digest"] == m1_a,
                "m1 active digest was overwritten by the other device")
    assert_that(d1["slots"]["B"]["status"] == "VERIFIED"
                and d1["slots"]["B"]["version"] == "2.0.0",
                "m1 candidate stage must remain its own VERIFIED 2.0.0")
    assert_that(hashlib.sha256(raw_blob("m1", "B")).hexdigest() == m1_b_claim,
                "m1 candidate bytes overwritten by m2")
    assert_that(len(d1["evidence"]) == m1_evidence_before,
                "m2 upgrade must not append evidence to m1")

    # m2 reopens on its own B/2.0.0 with its own digest (digest<->slot consistency)
    rec, d2 = power_cycle("m2", "B", 2)
    assert_that(d2["slots"]["B"]["version"] == "2.0.0"
                and d2["slots"]["B"]["status"] == "CONFIRMED", "m2 active wrong")
    assert_that(d2["slots"]["B"]["digest"] == d2["slots"]["B"]["actual_digest"] == m2_b_claim,
                "m2 active slot digest inconsistent with its manifest")
    assert_that(hashlib.sha256(raw_blob("m2", "B")).hexdigest() == m2_b_claim,
                "m2 active bytes inconsistent with claimed digest")
    assert_that(d2["slots"]["A"]["status"] == "SUPERSEDED", "m2 old slot not superseded")

    # --- already-affected persistent record: safe convergence ---------------
    # m3 upgraded to B/2.0.0 (A is SUPERSEDED, rollback forbidden); then plant
    # m2's image bytes into m3's active CONFIRMED slot B, the exact damage the
    # legacy shared-blob layout could leave behind after reopen.
    call("POST", "/api/devices", {"device_id": "m3", "version": "1.0.0"}, 201)
    call("POST", "/api/devices/m3/candidate",
         {"version": "2.0.0", "request_id": "m3-req"}, 200)
    _, b = call("POST", "/api/devices/m3/confirm", {}, 200)
    assert_that(b["active_slot"] == "B", "m3 setup switch failed")
    m3_b_claim = b["device"]["slots"]["B"]["digest"]
    foreign = raw_blob("m2", "A")
    healthy_b = raw_blob("m2", "B")  # m2's active image; must survive m3's incident
    foreign_digest = hashlib.sha256(foreign).hexdigest()
    assert_that(foreign_digest != m3_b_claim, "planted bytes must really be foreign")
    set_raw_blob("m3", "B", foreign)

    call("POST", "/api/devices/m3/power-off", {}, 200)
    _, body = call("POST", "/api/devices/m3/power-on", {}, 200)
    rec, d3 = body["recovery"], body["device"]
    assert_that(body["outcome"] == "unbootable", "foreign-content slot must not boot")
    assert_that(rec["active_slot"] is None and rec["eligible"] == [], "nothing eligible")
    diag = {x["slot"]: x["reason"] for x in rec["diagnoses"]}
    assert_that(diag.get("B") == "digest_mismatch", "foreign CONFIRMED slot not diagnosed")
    assert_that(diag.get("A") == "superseded_no_rollback",
                "must not fall back to the superseded slot")
    assert_that(d3["slots"]["B"]["status"] == "REJECTED", "bad slot not quarantined")
    assert_that(d3["slots"]["B"]["actual_digest"] == foreign_digest
                and d3["slots"]["B"]["digest"] == m3_b_claim,
                "reviewable mismatch evidence must keep both digests")
    ev = call("GET", "/api/devices/m3/evidence", expect=200)[1]["evidence"]
    assert_that(any(e["reason"] == "digest_mismatch" for e in ev)
                and any(e["reason"] == "recovery_measurement_changed" for e in ev),
                "persistent mismatch evidence missing")
    n_ev = len(ev)

    # second reopen: still safely refused, evidence retained, nothing rolled back
    call("POST", "/api/devices/m3/power-off", {}, 200)
    _, body = call("POST", "/api/devices/m3/power-on", {}, 200)
    assert_that(body["outcome"] == "unbootable"
                and body["recovery"]["active_slot"] is None, "must keep refusing")
    d3 = body["device"]
    assert_that(d3["slots"]["B"]["status"] == "REJECTED"
                and d3["slots"]["A"]["status"] == "SUPERSEDED",
                "quarantine / no-rollback state must survive reopen")
    assert_that(len(call("GET", "/api/devices/m3/evidence", expect=200)[1]["evidence"]) >= n_ev,
                "evidence must be retained across reopens")

    # the healthy other device is untouched by m3's incident
    assert_that(raw_blob("m2", "B") == healthy_b,
                "healthy device active bytes must not be modified")
    rec, _ = power_cycle("m2", "B", 2)
    assert_that(rec["active_slot"] == "B", "healthy device must keep booting")

    print(f"SMOKE OK: {checks} HTTP assertions passed "
          "(power-loss x3, corrupt candidate, concurrent 409, reopen consistency, "
          "two-device isolation, affected-record safe convergence)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
