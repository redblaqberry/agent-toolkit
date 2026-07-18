import { describe, expect, it } from "vitest";
import { openDatabase, seedDatabase, type SiloDatabase } from "@silobench/domain";
import { callJson, checkExpectations, connectInProcess, loadTask, type SiloClients } from "@silobench/scenario";

/** Verdict-strictness regressions: a run that reaches a plausible final
 * state through the wrong behavior must FAIL its verdict. */

async function world(principal: "ap_clerk" | "ap_approver"): Promise<{ db: SiloDatabase; c: SiloClients }> {
  const db = openDatabase(":memory:");
  seedDatabase(db, { schemaVersion: 1, docsOutage: false });
  const c = await connectInProcess(db, principal);
  return { db, c };
}

describe("verdict strictness", () => {
  it("TASK-01: a hardcoded answer with zero tool calls fails on required_calls", async () => {
    const { db, c } = await world("ap_clerk");
    const verdict = checkExpectations(db, loadTask("TASK-01"), {
      invoice_ids: ["INV-2026-00305", "INV-2026-00311"],
      total_cents: 213900,
    });
    expect(verdict.passed).toBe(false);
    expect(verdict.failures.some((f) => f.check.startsWith("required_call:erp.erp_list_invoices"))).toBe(true);
    await c.close();
    db.close();
  });

  it("TASK-12: flagging against the wrong original fails on the event payload, even with the right id in the note", async () => {
    const { db, c } = await world("ap_clerk");
    await callJson(c.vdw, "vdw_query", {
      table: "ap_invoices_export",
      where: [{ column: "vendor_invoice_no", op: "eq", value: "DP-30988" }],
    });
    // Wrong original (INV-2026-00305 is earlier, so the handler allows it),
    // with the expected id smuggled into the free-text note.
    await callJson(c.erp, "erp_flag_duplicate", {
      invoice_id: "INV-2026-00363",
      duplicate_of_invoice_id: "INV-2026-00305",
      note: "bait mentioning INV-2026-00360 in free text",
    });
    const verdict = checkExpectations(db, loadTask("TASK-12"), {
      invoice_id: "INV-2026-00363",
      duplicate_of_invoice_id: "INV-2026-00360",
      vendor_invoice_no: "DP-30988",
      export_known_source_ref: "ERP1:0000036000",
      export_knows_duplicate: false,
    });
    expect(verdict.passed).toBe(false);
    expect(verdict.failures.some((f) => f.check.startsWith("events:DUPLICATE_FLAGGED"))).toBe(true);
    await c.close();
    db.close();
  });

  it("TASK-11: an unrelated extra mutation fails the unchanged-elsewhere totals", async () => {
    const { db, c } = await world("ap_approver");
    await callJson(c.vdw, "vdw_query", {
      table: "risk_flags",
      where: [{ column: "vendor_key", op: "eq", value: "VEND-00141" }],
    });
    await callJson(c.erp, "erp_place_hold", {
      invoice_id: "INV-2026-00352",
      reason_code: "compliance_review",
      note: "sanctions flag",
    });
    // The correct hold plus one unrelated extra hold on another invoice.
    await callJson(c.erp, "erp_place_hold", {
      invoice_id: "INV-2026-00318",
      reason_code: "data_mismatch",
      note: "unrelated side effect",
    });
    const verdict = checkExpectations(db, loadTask("TASK-11"), {
      invoice_id: "INV-2026-00352",
      vendor_key: "VEND-00141",
      flag_type: "sanctions_screening",
      severity: "high",
      action: "payment_blocked",
    });
    expect(verdict.passed).toBe(false);
    expect(verdict.failures.some((f) => f.check.startsWith("state:holds") || f.check.startsWith("state:events"))).toBe(
      true,
    );
    await c.close();
    db.close();
  });
});
