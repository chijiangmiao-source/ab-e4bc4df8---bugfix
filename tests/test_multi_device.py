"""Cross-device isolation.

Two payload devices both label their slots "A"/"B", but an image belongs to
exactly the one physical device that created or upgraded it. Device 乙's
candidate writes or confirmations must never alter device 甲's bytes,
confirmed version, candidate stage or diagnostic evidence -- not even after a
power loss / reopen of device 甲.
"""
from __future__ import annotations

import hashlib

from tests.conftest import make_device


def _digest(client, device_id, slot):
    dev = client.get(f"/api/devices/{device_id}").json()["device"]
    return dev["slots"][slot]["digest"], dev["slots"][slot]["actual_digest"]


def _power_cycle(client, device_id):
    client.post(f"/api/devices/{device_id}/power-off")
    r = client.post(f"/api/devices/{device_id}/power-on")
    assert r.status_code == 200, r.text
    return r.json()["recovery"], r.json()["device"]


def test_same_version_devices_get_distinct_images_and_digests(client):
    a = make_device(client, "unit-a", "1.0.0")
    b = make_device(client, "unit-b", "1.0.0")
    assert a["slots"]["A"]["version"] == b["slots"]["A"]["version"] == "1.0.0"
    assert a["slots"]["A"]["digest"] != b["slots"]["A"]["digest"]
    assert a["slots"]["A"]["actual_digest"] != b["slots"]["B"]["actual_digest"]
    # Every manifest remembers the physical device that owns it.
    assert a["slots"]["A"]["device_id"] == "unit-a"
    assert b["slots"]["A"]["device_id"] == "unit-b"
    assert a["slots"]["B"]["device_id"] == "unit-a"
    assert b["slots"]["B"]["device_id"] == "unit-b"


def test_candidate_stage_on_one_device_does_not_touch_the_other(client):
    make_device(client, "unit-a", "1.0.0")
    make_device(client, "unit-b", "1.0.0")
    a_digest_claim, a_digest_measured = _digest(client, "unit-a", "A")

    # 乙 only stages a candidate (VERIFIED, not confirmed).
    r = client.post("/api/devices/unit-b/candidate", json={
        "version": "2.0.0", "request_id": "b1",
    })
    assert r.json()["outcome"] == "staged"

    # 甲 power-cycles: bytes, manifest digest, version, stage all unchanged.
    rec, dev = _power_cycle(client, "unit-a")
    assert rec["active_slot"] == "A" and rec["generation"] == 1
    assert dev["slots"]["A"]["version"] == "1.0.0"
    assert dev["slots"]["A"]["digest"] == a_digest_claim
    assert dev["slots"]["A"]["actual_digest"] == a_digest_measured
    assert dev["slots"]["A"]["status"] == "CONFIRMED"
    assert dev["slots"]["B"]["status"] == "EMPTY"
    assert dev["generation"] == 1 and dev["active_slot"] == "A"

    # 乙's pending candidate is likewise untouched by 甲's recovery.
    b = client.get("/api/devices/unit-b").json()["device"]
    assert b["slots"]["B"]["status"] == "VERIFIED"
    assert b["slots"]["B"]["version"] == "2.0.0"


def test_confirmed_switch_on_one_device_isolated_from_other_power_recovery(client):
    make_device(client, "unit-a", "1.0.0")
    make_device(client, "unit-b", "1.0.0")
    a_claim, a_measured = _digest(client, "unit-a", "A")

    client.post("/api/devices/unit-b/candidate", json={"version": "2.0.0", "request_id": "b1"})
    sw = client.post("/api/devices/unit-b/confirm", json={}).json()
    assert sw["outcome"] == "switched" and sw["active_slot"] == "B"

    rec, dev = _power_cycle(client, "unit-a")
    assert rec["active_slot"] == "A"
    assert rec["eligible"] == ["A"]
    assert dev["slots"]["A"]["status"] == "CONFIRMED"
    assert dev["slots"]["A"]["actual_digest"] == a_measured == a_claim
    assert dev["slots"]["B"]["status"] == "EMPTY"
    assert dev["qualified_request"] is None

    # And 乙 reopening keeps its own B/2.0.0 with its own digest.
    rec_b, dev_b = _power_cycle(client, "unit-b")
    assert rec_b["active_slot"] == "B" and dev_b["generation"] == 2
    assert dev_b["slots"]["B"]["version"] == "2.0.0"
    assert dev_b["slots"]["A"]["status"] == "SUPERSEDED"
    assert dev_b["slots"]["B"]["digest"] != dev["slots"]["A"]["digest"]


def test_both_devices_upgrade_independently_with_colliding_slot_names(client):
    make_device(client, "unit-a", "1.0.0")
    make_device(client, "unit-b", "1.0.0")
    for did in ("unit-a", "unit-b"):
        client.post(f"/api/devices/{did}/candidate", json={"version": "2.0.0", "request_id": did})
        assert client.post(f"/api/devices/{did}/confirm", json={}).json()["outcome"] == "switched"

    for did in ("unit-a", "unit-b"):
        rec, dev = _power_cycle(client, did)
        assert rec["active_slot"] == "B" and dev["slots"]["B"]["version"] == "2.0.0"
        assert dev["slots"]["B"]["status"] == "CONFIRMED"
        assert dev["slots"]["B"]["digest"] == dev["slots"]["B"]["actual_digest"]
        assert dev["slots"]["A"]["status"] == "SUPERSEDED"

    # Same version, same slot name -- but the two 2.0.0 images are distinct.
    a = client.get("/api/devices/unit-a").json()["device"]
    b = client.get("/api/devices/unit-b").json()["device"]
    assert a["slots"]["B"]["digest"] != b["slots"]["B"]["digest"]


def test_foreign_bytes_in_confirmed_slot_are_refused_and_evidence_kept(client):
    """A previously contaminated persistent record must converge safely.

    Reproduces the legacy failure at the storage boundary: 甲's slot A still
    claims CONFIRMED with 甲's manifest digest, but the bytes on flash are
    乙's image. Recovery must refuse the boot, keep reviewable mismatch
    evidence on every reopen, and never roll a SUPERSEDED slot back.
    """
    make_device(client, "unit-a", "1.0.0")
    make_device(client, "unit-b", "1.0.0")

    # Upgrade 甲 to B/2.0.0 first so A is SUPERSEDED (rollback forbidden),
    # then plant foreign bytes into the active CONFIRMED slot B.
    client.post("/api/devices/unit-a/candidate", json={"version": "2.0.0", "request_id": "a1"})
    client.post("/api/devices/unit-a/confirm", json={})
    b_claim = client.get("/api/devices/unit-a").json()["device"]["slots"]["B"]["digest"]

    import backend.api as api
    conn = api.store._conn
    foreign = conn.execute(
        "SELECT content FROM blobs WHERE device_id='unit-b' AND slot='A'"
    ).fetchone()["content"]
    foreign_digest = hashlib.sha256(foreign).hexdigest()
    assert foreign_digest != b_claim
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE blobs SET content=? WHERE device_id='unit-a' AND slot='B'",
        (foreign,),
    )
    conn.execute("COMMIT")

    client.post("/api/devices/unit-a/power-off")
    r = client.post("/api/devices/unit-a/power-on").json()
    rec, dev = r["recovery"], r["device"]
    assert rec["active_slot"] is None
    assert rec["eligible"] == []
    slot_b = dev["slots"]["B"]
    assert slot_b["status"] == "REJECTED"
    assert slot_b["digest"] == b_claim
    assert slot_b["actual_digest"] == foreign_digest
    diag = {d["slot"]: d["reason"] for d in rec["diagnoses"]}
    assert diag["B"] == "digest_mismatch"
    # The superseded 1.0.0 slot must not be selected as a "rollback".
    assert diag["A"] == "superseded_no_rollback"

    ev = client.get("/api/devices/unit-a/evidence").json()["evidence"]
    assert any(e["reason"] == "digest_mismatch" for e in ev)
    assert any(e["reason"] == "recovery_measurement_changed" for e in ev)

    # Reopen again: still refused, evidence retained (append-only).
    n_before = len(ev)
    client.post("/api/devices/unit-a/power-off")
    r2 = client.post("/api/devices/unit-a/power-on").json()
    assert r2["recovery"]["active_slot"] is None
    assert r2["device"]["slots"]["B"]["status"] == "REJECTED"
    assert len(r2["device"]["evidence"]) >= n_before

    # 乙 remains fully healthy and is never modified by 甲's incident.
    rec_b, dev_b = _power_cycle(client, "unit-b")
    assert rec_b["active_slot"] == "A"
    assert dev_b["slots"]["A"]["status"] == "CONFIRMED"
    assert dev_b["slots"]["A"]["digest"] == foreign_digest


def test_foreign_candidate_bytes_cannot_be_confirmed(client):
    make_device(client, "unit-a", "1.0.0")
    make_device(client, "unit-b", "1.0.0")
    # Stage a verified candidate on 甲, then overwrite its staged bytes with 乙's.
    client.post("/api/devices/unit-a/candidate", json={"version": "2.0.0", "request_id": "a1"})
    import backend.api as api
    conn = api.store._conn
    foreign = conn.execute(
        "SELECT content FROM blobs WHERE device_id='unit-b' AND slot='A'"
    ).fetchone()["content"]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE blobs SET content=? WHERE device_id='unit-a' AND slot='B'",
        (foreign,),
    )
    conn.execute("COMMIT")

    # The pre-commit re-measurement must stop the switch, not trust VERIFIED.
    r = client.post("/api/devices/unit-a/confirm", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "digest_mismatch"
    dev = client.get("/api/devices/unit-a").json()["device"]
    assert dev["active_slot"] == "A"
    assert dev["slots"]["B"]["status"] == "REJECTED"
    assert dev["generation"] == 1


def test_manifest_claiming_foreign_owner_is_refused(client):
    """Even byte-perfect image: a manifest whose owner is another device
    never boots here."""
    import json

    make_device(client, "unit-a", "1.0.0")
    make_device(client, "unit-b", "1.0.0")

    import backend.api as api
    conn = api.store._conn
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT data FROM devices WHERE device_id='unit-a'"
    ).fetchone()
    data = json.loads(row["data"])
    data["slots"]["A"]["device_id"] = "unit-b"  # manifest claims another owner
    conn.execute(
        "UPDATE devices SET data=? WHERE device_id='unit-a'",
        (json.dumps(data, separators=(",", ":")),),
    )
    conn.execute("COMMIT")

    client.post("/api/devices/unit-a/power-off")
    r = client.post("/api/devices/unit-a/power-on").json()
    rec, dev = r["recovery"], r["device"]
    assert rec["active_slot"] is None
    assert rec["eligible"] == []
    diag = {d["slot"]: d["reason"] for d in rec["diagnoses"]}
    assert diag["A"] == "foreign_device_image"
    assert dev["slots"]["A"]["status"] == "REJECTED"
    ev = client.get("/api/devices/unit-a/evidence").json()["evidence"]
    assert any(e["reason"] == "foreign_device_image" for e in ev)
