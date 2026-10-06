"""Multi-device image ownership and legacy contamination convergence.

Two orbital payloads may both use A/B slot names, yet image bytes, digests,
confirmed versions, candidate staging and diagnostic evidence must always
belong to the device that created or upgraded them. These tests cover:

* two devices initialised with the *same* version but clearly different image
  bytes/digests stay isolated;
* candidate staging (including fault-injected partial writes) and confirm
  switches on one device never touch the other device;
* digest vs. active-slot consistency after power-off recovery;
* persisted devices already affected by the historical shared-blob schema
  converge safely: reviewable digest-mismatch evidence is kept, the foreign
  image is refused, and recovery never rolls back to a SUPERSEDED slot nor
  overwrites the other device's healthy slot.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

from backend.models import Device, Slot, SlotStatus
from backend.service import UpgradeService, sha256_hex
from backend.store import Store
from tests.conftest import make_device  # noqa: F401  (re-exported helper style)

JIA_V1 = b"jia-payload-image::1.0.0::" + bytes((i * 3 + 1) & 0xFF for i in range(200))
YI_V1 = b"yi-payload-image::1.0.0::" + bytes((i * 5 + 7) & 0xFF for i in range(200))
YI_V2 = b"yi-payload-image::2.0.0::" + bytes((i * 7 + 11) & 0xFF for i in range(220))
JIA_V2 = b"jia-payload-image::2.0.0::" + bytes((i * 11 + 13) & 0xFF for i in range(220))

JIA_V1_DIGEST = sha256_hex(JIA_V1)
YI_V1_DIGEST = sha256_hex(YI_V1)
YI_V2_DIGEST = sha256_hex(YI_V2)
JIA_V2_DIGEST = sha256_hex(JIA_V2)

assert len({JIA_V1_DIGEST, YI_V1_DIGEST, YI_V2_DIGEST, JIA_V2_DIGEST}) == 4


def make_device_with_image(client, device_id, version, content: bytes):
    r = client.post(
        "/api/devices",
        json={
            "device_id": device_id,
            "version": version,
            "content_b64": base64.b64encode(content).decode(),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["device"]


def get_view(client, device_id):
    return client.get(f"/api/devices/{device_id}").json()["device"]


def power_cycle(client, device_id):
    assert client.post(f"/api/devices/{device_id}/power-off").status_code == 200
    r = client.post(f"/api/devices/{device_id}/power-on")
    assert r.status_code == 200
    return r.json()


# --------------------------------------------------------------------------- #
# Fresh databases: strict per-device isolation                                 #
# --------------------------------------------------------------------------- #
def test_same_version_different_images_stay_isolated(client):
    jia = make_device_with_image(client, "jia", "1.0.0", JIA_V1)
    yi = make_device_with_image(client, "yi", "1.0.0", YI_V1)
    assert jia["slots"]["A"]["digest"] == JIA_V1_DIGEST
    assert yi["slots"]["A"]["digest"] == YI_V1_DIGEST
    assert JIA_V1_DIGEST != YI_V1_DIGEST

    # Creating yi must not rewrite jia's image or measured digest.
    jia = get_view(client, "jia")
    assert jia["slots"]["A"]["digest"] == JIA_V1_DIGEST
    assert jia["slots"]["A"]["actual_digest"] == JIA_V1_DIGEST
    assert jia["slots"]["A"]["bootable"] is True

    # Power-off recovery re-measures jia's *own* bytes and boots jia's image.
    body = power_cycle(client, "jia")
    rec, dev = body["recovery"], body["device"]
    assert rec["active_slot"] == "A"
    assert rec["eligible"] == ["A"]
    slot_a = dev["slots"]["A"]
    assert slot_a["status"] == "CONFIRMED"
    assert slot_a["digest"] == slot_a["actual_digest"] == JIA_V1_DIGEST
    assert slot_a["bootable"] is True
    assert not any(e["reason"] == "digest_mismatch" for e in dev["evidence"])


def test_peer_upgrade_and_power_faults_do_not_touch_this_device(client):
    make_device_with_image(client, "jia", "1.0.0", JIA_V1)
    make_device_with_image(client, "yi", "1.0.0", YI_V1)
    snapshot = get_view(client, "jia")

    def jia_untouched():
        assert get_view(client, "jia") == snapshot

    # Peer suffers a power cut mid candidate-write, then recovers.
    r = client.post(
        "/api/devices/yi/candidate",
        json={
            "version": "2.0.0",
            "request_id": "yi-cut",
            "content_b64": base64.b64encode(YI_V2).decode(),
            "fault_point": "candidate_write",
        },
    )
    assert r.json()["outcome"] == "power_cut"
    jia_untouched()
    power_cycle(client, "yi")
    jia_untouched()

    # Peer stages a candidate for real (candidate phase must not leak).
    r = client.post(
        "/api/devices/yi/candidate",
        json={
            "version": "2.0.0",
            "request_id": "yi-up",
            "content_b64": base64.b64encode(YI_V2).decode(),
        },
    )
    assert r.json()["outcome"] == "staged"
    jia_untouched()

    # Peer commits the confirm switch (confirmed version must not leak).
    r = client.post("/api/devices/yi/confirm", json={})
    assert r.json()["outcome"] == "switched"
    assert r.json()["active_slot"] == "B"
    jia_untouched()

    # jia recovers from a power cut: still its own image, version, generation,
    # and no diagnostic evidence was produced by yi's activity.
    body = power_cycle(client, "jia")
    rec, dev = body["recovery"], body["device"]
    assert rec["active_slot"] == "A" and rec["generation"] == 1
    assert dev["slots"]["A"]["version"] == "1.0.0"
    assert dev["slots"]["A"]["digest"] == dev["slots"]["A"]["actual_digest"] == JIA_V1_DIGEST
    assert dev["slots"]["B"]["status"] == "EMPTY"
    assert dev["evidence"] == snapshot["evidence"]

    # yi recovered onto its own upgraded slot with its own digest.
    body_yi = power_cycle(client, "yi")
    assert body_yi["recovery"]["active_slot"] == "B"
    slot_b = body_yi["device"]["slots"]["B"]
    assert slot_b["version"] == "2.0.0"
    assert slot_b["digest"] == slot_b["actual_digest"] == YI_V2_DIGEST


def test_active_slot_digest_consistency_after_reopen(client):
    make_device_with_image(client, "jia", "1.0.0", JIA_V1)
    make_device_with_image(client, "yi", "1.0.0", YI_V1)
    client.post(
        "/api/devices/yi/candidate",
        json={
            "version": "2.0.0",
            "request_id": "yi-up",
            "content_b64": base64.b64encode(YI_V2).decode(),
        },
    )
    client.post("/api/devices/yi/confirm", json={})

    for device_id, expect in (("jia", ("A", JIA_V1_DIGEST)), ("yi", ("B", YI_V2_DIGEST))):
        body = power_cycle(client, device_id)
        rec, dev = body["recovery"], body["device"]
        slot_name, digest = expect
        assert rec["active_slot"] == slot_name
        active = dev["slots"][rec["active_slot"]]
        # The slot the recovery report selected is exactly the slot whose
        # measured digest matches this device's own manifest digest.
        assert active["digest"] == active["actual_digest"] == digest
        assert active["digest_matches"] is True
        assert active["bootable"] is True
        assert dev["active_slot"] == rec["active_slot"]


# --------------------------------------------------------------------------- #
# Legacy shared-blob databases: safe convergence of already-affected devices   #
# --------------------------------------------------------------------------- #
def _confirmed_slot(name, version, content, actual_content=None, generation=1):
    return Slot(
        name=name,
        status=SlotStatus.CONFIRMED,
        version=version,
        digest=sha256_hex(content),
        actual_digest=sha256_hex(actual_content if actual_content is not None else content),
        size=len(content),
        written=len(content),
        confirmed_generation=generation,
    )


def _seed_legacy_db(path, devices, blobs):
    """Persist devices with the *historical* schema: one blob row per slot
    name, shared by every device (the contamination this fix removes)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE devices (device_id TEXT PRIMARY KEY, data TEXT NOT NULL,"
        " powered_on INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE blobs (slot TEXT NOT NULL, content BLOB NOT NULL,"
        " PRIMARY KEY (slot))"
    )
    for dev in devices:
        conn.execute(
            "INSERT INTO devices VALUES (?, ?, 0)",
            (dev.device_id, json.dumps(dev.to_dict())),
        )
    for slot, content in blobs.items():
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (slot, content))
    conn.commit()
    conn.close()


def test_legacy_shared_blob_migrates_and_affected_device_converges(tmp_path):
    db = tmp_path / "legacy.db"
    # jia's manifest still claims JIA_V1, but the shared slot-A row holds yi's
    # bytes and jia's persisted measurement was already clobbered to yi's digest.
    jia = Device(
        device_id="jia",
        slots={
            "A": _confirmed_slot("A", "1.0.0", JIA_V1, actual_content=YI_V1),
            "B": Slot(name="B"),
        },
        active_slot="A",
        generation=1,
    )
    yi = Device(
        device_id="yi",
        slots={
            "A": _confirmed_slot("A", "1.0.0", YI_V1),
            "B": Slot(name="B"),
        },
        active_slot="A",
        generation=1,
    )
    _seed_legacy_db(db, [jia, yi], {"A": YI_V1})

    svc = UpgradeService(Store(db))  # migration runs on open

    # Migration produced a per-device table; yi's healthy slot kept its bytes.
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(blobs)")}
    assert cols == {"device_id", "slot", "content"}

    # Affected device: refuses the foreign image, keeps reviewable evidence.
    res = svc.power_on("jia")
    rec, dev = res["recovery"], res["device"]
    assert res["outcome"] == "unbootable"
    assert rec["active_slot"] is None
    assert rec["critical"]
    diag_a = next(d for d in rec["diagnoses"] if d["slot"] == "A")
    assert diag_a["reason"] == "digest_mismatch"
    slot_a = dev["slots"]["A"]
    assert slot_a["status"] == "CONFIRMED"  # historical status retained...
    assert slot_a["bootable"] is False      # ...but never enough to boot
    assert slot_a["digest"] == JIA_V1_DIGEST
    assert slot_a["actual_digest"] == YI_V1_DIGEST
    mismatches = [e for e in dev["evidence"] if e["reason"] == "digest_mismatch"]
    assert len(mismatches) == 1
    assert JIA_V1_DIGEST in mismatches[0]["detail"]
    assert YI_V1_DIGEST in mismatches[0]["detail"]

    # Re-opening again converges: same refusal, no duplicated evidence.
    again = svc.power_on("jia")
    assert again["recovery"]["active_slot"] is None
    assert [
        e for e in again["device"]["evidence"] if e["reason"] == "digest_mismatch"
    ] == mismatches

    # The healthy device is unaffected and boots its own image.
    res_yi = svc.power_on("yi")
    assert res_yi["recovery"]["active_slot"] == "A"
    slot = res_yi["device"]["slots"]["A"]
    assert slot["digest"] == slot["actual_digest"] == YI_V1_DIGEST
    assert slot["bootable"] is True


def test_no_rollback_to_superseded_when_confirmed_image_is_foreign(tmp_path):
    db = tmp_path / "legacy.db"
    # jia2 already upgraded to 2.0.0 (slot B confirmed, slot A SUPERSEDED), but
    # the shared slot-B row was later overwritten by yi2's image; jia2's
    # persisted measurement for B was clobbered to yi2's digest.
    jia2 = Device(
        device_id="jia2",
        slots={
            "A": Slot(
                name="A",
                status=SlotStatus.SUPERSEDED,
                version="1.0.0",
                digest=JIA_V1_DIGEST,
                actual_digest=JIA_V1_DIGEST,
                size=len(JIA_V1),
                written=len(JIA_V1),
                confirmed_generation=1,
            ),
            "B": _confirmed_slot("B", "2.0.0", JIA_V2, actual_content=YI_V2, generation=2),
        },
        active_slot="B",
        generation=2,
    )
    yi2 = Device(
        device_id="yi2",
        slots={
            "A": Slot(
                name="A",
                status=SlotStatus.SUPERSEDED,
                version="1.0.0",
                digest=YI_V1_DIGEST,
                actual_digest=YI_V1_DIGEST,
                size=len(YI_V1),
                written=len(YI_V1),
                confirmed_generation=1,
            ),
            "B": _confirmed_slot("B", "2.0.0", YI_V2, generation=2),
        },
        active_slot="B",
        generation=2,
    )
    _seed_legacy_db(db, [jia2, yi2], {"A": YI_V1, "B": YI_V2})

    svc = UpgradeService(Store(db))
    res = svc.power_on("jia2")
    rec, dev = res["recovery"], res["device"]
    # Safe refusal: the foreign B image is diagnosed, and recovery must NOT
    # fall back to the SUPERSEDED slot A (no rollback, ever).
    assert res["outcome"] == "unbootable"
    assert rec["active_slot"] is None
    diag = {d["slot"]: d["reason"] for d in rec["diagnoses"]}
    assert diag["A"] == "superseded_no_rollback"
    assert diag["B"] == "digest_mismatch"
    assert dev["slots"]["A"]["status"] == "SUPERSEDED"
    assert dev["slots"]["B"]["bootable"] is False
    assert any(
        e["reason"] == "digest_mismatch" and e["slot"] == "B" for e in dev["evidence"]
    )
    # Converges identically on the next re-open.
    assert svc.power_on("jia2")["recovery"]["active_slot"] is None

    # yi2's healthy slots were never touched by jia2's recovery.
    res_yi = svc.power_on("yi2")
    assert res_yi["recovery"]["active_slot"] == "B"
    slot_b = res_yi["device"]["slots"]["B"]
    assert slot_b["digest"] == slot_b["actual_digest"] == YI_V2_DIGEST
    assert slot_b["version"] == "2.0.0"
    assert res_yi["device"]["slots"]["A"]["status"] == "SUPERSEDED"
