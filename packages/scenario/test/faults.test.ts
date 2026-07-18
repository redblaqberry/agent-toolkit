import { describe, expect, it } from "vitest";
import { openDatabase, seedDatabase, setDocsOutage } from "@silobench/domain";
import { ToolFailure, callJson, connectInProcess } from "@silobench/scenario";

describe("document-service outage", () => {
  it("every docs tool fails with SERVICE_UNAVAILABLE while the fault is on, and recovers live", async () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 1, docsOutage: true });
    const c = await connectInProcess(db, "ap_clerk");
    for (const [tool, args] of [
      ["docs_search", { query: "Rahmenvertrag" }],
      ["docs_get_document", { doc_id: "DOC-0008" }],
      ["docs_list_folder", { folder: "/contracts/2026" }],
    ] as const) {
      await expect(callJson(c.docs, tool, args)).rejects.toMatchObject({
        name: "ToolFailure",
        code: "SERVICE_UNAVAILABLE",
      });
    }
    // The other silos are unaffected during the outage.
    const invoices = await callJson<{ items: unknown[] }>(c.erp, "erp_list_invoices", {});
    expect(invoices.items.length).toBeGreaterThan(0);

    setDocsOutage(db, false);
    const page = await callJson<{ items: unknown[] }>(c.docs, "docs_search", { query: "Rahmenvertrag" });
    expect(page.items.length).toBeGreaterThan(0);
    await c.close();
    db.close();
  });
});

describe("warehouse schema drift v1 to v2", () => {
  it("v2 renames tables and columns and the errors list what exists", async () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 2, docsOutage: false });
    const c = await connectInProcess(db, "ap_clerk");

    const tables = await callJson<{ tables: Array<{ table: string; columns: Array<{ name: string }> }> }>(
      c.vdw,
      "vdw_list_tables",
      {},
    );
    const names = tables.tables.map((t) => t.table);
    expect(names).toContain("vendor_risk_flags");
    expect(names).not.toContain("risk_flags");
    const vendors = tables.tables.find((t) => t.table === "vendors");
    expect(vendors?.columns.map((col) => col.name)).toContain("vendor_legal_name");

    let failure: ToolFailure | null = null;
    try {
      await callJson(c.vdw, "vdw_query", { table: "risk_flags" });
    } catch (err) {
      failure = err as ToolFailure;
    }
    expect(failure?.code).toBe("NOT_FOUND");
    expect(failure?.details["available_tables"]).toContain("vendor_risk_flags");

    // The unit drift: export amounts are integer cents in v2, not euro strings.
    const exportRows = await callJson<{ rows: Array<Record<string, unknown>> }>(c.vdw, "vdw_query", {
      table: "ap_invoices_export",
      where: [{ column: "source_ref", op: "eq", value: "ERP1:0000031100" }],
    });
    expect(exportRows.rows[0]?.["amount_cents"]).toBe(129900);
    expect(exportRows.rows[0]?.["amount_eur"]).toBeUndefined();
    await c.close();
    db.close();
  });

  it("verification_status values are uppercased in v2 (value drift)", async () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 2, docsOutage: false });
    const c = await connectInProcess(db, "ap_clerk");
    const rows = await callJson<{ rows: Array<Record<string, unknown>> }>(c.vdw, "vdw_query", {
      table: "bank_accounts",
      where: [{ column: "vendor_key", op: "eq", value: "VEND-00095" }],
    });
    expect(rows.rows[0]?.["verification_status"]).toBe("VERIFIED");
    await c.close();
    db.close();
  });
});
