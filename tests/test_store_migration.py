"""Migration from the legacy storage layout.

The legacy ``blobs`` table was keyed by ``slot`` alone, so image bytes were
shared across every device. On opening such a database:

* a single-device DB keeps its (unambiguous) bytes and slots boot normally;
* a multi-device DB discards the ambiguous shared bytes rather than guessing
  an owner, so recovery fails closed instead of booting the wrong image;
* slot manifests missing the ownership field are back-filled from their
  owning device row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from backend.models import Device, Slot, SlotStatus


def _open(path):
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _legacy_device(device_id, version="1.0.0", image=b"legacy-image"):
    digest = hashlib.sha256(image).hexdigest()
    slot_a = Slot(name="A", status=SlotStatus.CONFIRMED, version=version,
                  digest=digest, actual_digest=digest,
                  size=len(image), written=len(image), confirmed_generation=1)
    slot_b = Slot(name="B")
    dev = Device(device_id=device_id, slots={"A": slot_a, "B": slot_b},
                 active_slot="A", generation=1)
    payload = dev.to_dict()
    for slot_data in payload["slots"].values():  # legacy records predate ownership
        slot_data.pop("device_id", None)
    return payload, image


def test_single_device_legacy_db_migrates_and_keeps_bytes(tmp_path):
    path = tmp_path / "old.db"
    conn = _open(path)
    conn.executescript("""
        CREATE TABLE devices (device_id TEXT PRIMARY KEY, data TEXT NOT NULL,
                              powered_on INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE blobs (slot TEXT NOT NULL, content BLOB NOT NULL,
                            PRIMARY KEY (slot));
    """)
    payload, image = _legacy_device("only")
    conn.execute("INSERT INTO devices VALUES (?, ?, 1)",
                 ("only", json.dumps(payload)))
    conn.execute("INSERT INTO blobs VALUES (?, ?)", ("A", image))
    conn.close()

    import os
    os.environ["DATA_PATH"] = str(path)
    import importlib
    import backend.api as api
    importlib.reload(api)
    from fastapi.testclient import TestClient
    with TestClient(api.app) as c:
        c.post("/api/devices/only/power-off")
        r = c.post("/api/devices/only/power-on").json()
        # Unambiguous bytes survived the migration, ownership was back-filled
        # and the slot passes the full recovery re-check and boots.
        assert r["recovery"]["active_slot"] == "A"
        dev = r["device"]
        assert dev["slots"]["A"]["status"] == "CONFIRMED"
        assert dev["slots"]["A"]["device_id"] == "only"
        assert dev["slots"]["A"]["digest"] == hashlib.sha256(image).hexdigest()
        assert dev["slots"]["B"]["device_id"] == "only"
    api.store.close()


def test_multi_device_legacy_db_drops_ambiguous_bytes(tmp_path):
    path = tmp_path / "old.db"
    conn = _open(path)
    conn.executescript("""
        CREATE TABLE devices (device_id TEXT PRIMARY KEY, data TEXT NOT NULL,
                              powered_on INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE blobs (slot TEXT NOT NULL, content BLOB NOT NULL,
                            PRIMARY KEY (slot));
    """)
    for did in ("a", "b"):
        payload, image = _legacy_device(did, image=did.encode())
        conn.execute("INSERT INTO devices VALUES (?, ?, 1)",
                     (did, json.dumps(payload)))
        # Both devices shared the same slot-keyed row (last writer wins).
        conn.execute(
            "INSERT INTO blobs (slot, content) VALUES ('A', ?) "
            "ON CONFLICT(slot) DO UPDATE SET content=excluded.content",
            (image,),
        )
    conn.close()

    import os
    os.environ["DATA_PATH"] = str(path)
    import importlib
    import backend.api as api
    importlib.reload(api)
    from fastapi.testclient import TestClient
    with TestClient(api.app) as c:
        for did in ("a", "b"):
            c.post(f"/api/devices/{did}/power-off")
            r = c.post(f"/api/devices/{did}/power-on").json()
            # No byte is guessed as the owner: image missing -> safe refusal,
            # never a boot of the other device's bytes.
            assert r["recovery"]["active_slot"] is None
            assert r["device"]["slots"]["A"]["status"] == "REJECTED"
            assert r["device"]["slots"]["A"]["device_id"] == did
    api.store.close()
