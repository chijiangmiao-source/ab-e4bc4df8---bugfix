#!/usr/bin/env python3
"""Seed a *legacy-format* database for the HTTP acceptance run.

The historical schema kept one blob row per slot name, shared by every
device, so a second payload's image silently replaced the first one's. This
script recreates that already-affected persisted state:

* ``legacy-jia`` / ``legacy-yi`` -- both on version 1.0.0 with slot A
  CONFIRMED; the shared slot-A row holds yi's bytes, and jia's persisted
  measurement was already clobbered to yi's digest while its manifest still
  claims jia's own digest.
* ``legacy-jia2`` / ``legacy-yi2`` -- both already upgraded to 2.0.0 (slot B
  CONFIRMED, slot A SUPERSEDED); the shared slot-B row holds yi2's image and
  jia2's persisted measurement for B was clobbered accordingly.

The current Store migrates this DB on first open. Acceptance then expects the
affected devices to keep reviewable digest-mismatch evidence and safely
refuse the foreign image (never rolling back to SUPERSEDED slots), while the
healthy devices keep booting their own images untouched.

Usage: python3 scripts/make_legacy_db.py /path/to/upgrade.db
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.models import Device, Slot, SlotStatus  # noqa: E402
from backend.service import sha256_hex  # noqa: E402

JIA_V1 = b"legacy-jia-image::1.0.0::" + bytes((i * 3 + 1) & 0xFF for i in range(200))
YI_V1 = b"legacy-yi-image::1.0.0::" + bytes((i * 5 + 7) & 0xFF for i in range(200))
JIA_V2 = b"legacy-jia-image::2.0.0::" + bytes((i * 11 + 13) & 0xFF for i in range(220))
YI_V2 = b"legacy-yi-image::2.0.0::" + bytes((i * 7 + 11) & 0xFF for i in range(220))


def confirmed(name, version, content, actual_content=None, generation=1):
    return Slot(
        name=name,
        status=SlotStatus.CONFIRMED,
        version=version,
        digest=sha256_hex(content),
        actual_digest=sha256_hex(
            actual_content if actual_content is not None else content
        ),
        size=len(content),
        written=len(content),
        confirmed_generation=generation,
    )


def superseded(name, version, content, generation=1):
    return Slot(
        name=name,
        status=SlotStatus.SUPERSEDED,
        version=version,
        digest=sha256_hex(content),
        actual_digest=sha256_hex(content),
        size=len(content),
        written=len(content),
        confirmed_generation=generation,
    )


def build_devices() -> list[Device]:
    jia = Device(
        device_id="legacy-jia",
        slots={
            "A": confirmed("A", "1.0.0", JIA_V1, actual_content=YI_V1),
            "B": Slot(name="B"),
        },
        active_slot="A",
        generation=1,
    )
    jia.add_evidence(
        "A",
        "recovery_measurement_changed",
        "（历史遗留）恢复时测得的摘要曾被另一设备的共享镜像覆盖",
    )
    yi = Device(
        device_id="legacy-yi",
        slots={"A": confirmed("A", "1.0.0", YI_V1), "B": Slot(name="B")},
        active_slot="A",
        generation=1,
    )
    jia2 = Device(
        device_id="legacy-jia2",
        slots={
            "A": superseded("A", "1.0.0", JIA_V1),
            "B": confirmed("B", "2.0.0", JIA_V2, actual_content=YI_V2, generation=2),
        },
        active_slot="B",
        generation=2,
    )
    yi2 = Device(
        device_id="legacy-yi2",
        slots={
            "A": superseded("A", "1.0.0", YI_V1),
            "B": confirmed("B", "2.0.0", YI_V2, generation=2),
        },
        active_slot="B",
        generation=2,
    )
    return [jia, yi, jia2, yi2]


def main() -> int:
    db_path = Path(sys.argv[1])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    # Historical schema: blobs keyed by slot name only, shared across devices.
    conn.execute(
        "CREATE TABLE devices (device_id TEXT PRIMARY KEY, data TEXT NOT NULL,"
        " powered_on INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE blobs (slot TEXT NOT NULL, content BLOB NOT NULL,"
        " PRIMARY KEY (slot))"
    )
    for dev in build_devices():
        conn.execute(
            "INSERT INTO devices VALUES (?, ?, 0)",
            (dev.device_id, json.dumps(dev.to_dict())),
        )
    # Shared rows: the second device's image silently replaced the first's.
    conn.execute("INSERT INTO blobs VALUES ('A', ?)", (YI_V1,))
    conn.execute("INSERT INTO blobs VALUES ('B', ?)", (YI_V2,))
    conn.commit()
    conn.close()
    print(f"seeded legacy shared-blob DB at {db_path} "
          f"(devices: legacy-jia, legacy-yi, legacy-jia2, legacy-yi2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
