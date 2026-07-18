import { describe, expect, it } from "vitest";
import {
  openDatabase,
  seedDatabase,
  stateHash,
  takeSnapshot,
  withMutation,
  appendEvent,
} from "@silobench/domain";

describe("deterministic seed", () => {
  it("produces identical state hashes over 20 consecutive fresh seeds (v1)", () => {
    const hashes = new Set<string>();
    for (let i = 0; i < 20; i++) {
      const db = openDatabase(":memory:");
      seedDatabase(db, { schemaVersion: 1, docsOutage: false });
      hashes.add(stateHash(db));
      db.close();
    }
    expect(hashes.size).toBe(1);
  });

  it("reset on the same database restores the exact initial hash", () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 1, docsOutage: false });
    const initial = stateHash(db);
    withMutation(db, (tick) => {
      appendEvent(db, tick, {
        actor: "ap_clerk",
        system: "erp",
        type: "ACCESS_DENIED",
        entityType: "invoice",
        entityId: "INV-2026-00330",
        payload: { test: true },
      });
    });
    expect(stateHash(db)).not.toBe(initial);
    seedDatabase(db, { schemaVersion: 1, docsOutage: false });
    expect(stateHash(db)).toBe(initial);
    db.close();
  });

  it("schema v2 seeds deterministically to a different hash than v1", () => {
    const a = openDatabase(":memory:");
    const b = openDatabase(":memory:");
    const c = openDatabase(":memory:");
    seedDatabase(a, { schemaVersion: 2, docsOutage: false });
    seedDatabase(b, { schemaVersion: 2, docsOutage: false });
    seedDatabase(c, { schemaVersion: 1, docsOutage: false });
    expect(stateHash(a)).toBe(stateHash(b));
    expect(stateHash(a)).not.toBe(stateHash(c));
    a.close();
    b.close();
    c.close();
  });

  it("snapshot carries the planted cross-silo conflicts", () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 1, docsOutage: false });
    const snap = takeSnapshot(db);

    const dwVendors = snap.tables["vdw_vendors"] ?? [];
    const nordquell = dwVendors.find((v) => v["vendor_key"] === "VEND-00087");
    expect(nordquell?.["erp_ref"]).toBe("V-0899"); // stale crosswalk

    const erpVendors = snap.tables["erp_vendors"] ?? [];
    const merged = erpVendors.find((v) => v["erp_vendor_id"] === "V-0899");
    expect(merged?.["merged_into"]).toBe("V-1042");

    const invoices = snap.tables["invoices"] ?? [];
    expect(invoices.length).toBe(34);
    const exportsRows = snap.tables["vdw_ap_invoices_export"] ?? [];
    expect(exportsRows.some((r) => r["source_ref"] === "ERP1:0000031100")).toBe(true);
    expect(snap.tables["events"]?.length).toBe(0);
    db.close();
  });
});
