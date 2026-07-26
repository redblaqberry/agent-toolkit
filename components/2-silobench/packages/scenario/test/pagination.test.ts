import { describe, expect, it } from "vitest";
import { openDatabase, seedDatabase } from "@silobench/domain";
import { ToolFailure, callJson, connectInProcess } from "@silobench/scenario";

/** Malformed cursors are agent-caused input and must come back as a structured
 * INVALID_REQUEST, never a raw internal TypeError or a SQLite datatype error. */
describe("cursor robustness", () => {
  it("rejects non-object cursors with INVALID_REQUEST instead of crashing", async () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 1, docsOutage: false });
    const c = await connectInProcess(db, "ap_clerk");
    for (const raw of ["null", "123", "[]", "\"x\""]) {
      const cursor = Buffer.from(raw, "utf8").toString("base64url");
      await expect(callJson(c.vdw, "vdw_query", { table: "vendors", cursor })).rejects.toMatchObject({
        name: "ToolFailure",
        code: "INVALID_REQUEST",
      });
      await expect(callJson(c.erp, "erp_list_invoices", { cursor })).rejects.toMatchObject({
        name: "ToolFailure",
        code: "INVALID_REQUEST",
      });
      await expect(callJson(c.docs, "docs_search", { query: "Rahmenvertrag", cursor })).rejects.toMatchObject({
        name: "ToolFailure",
        code: "INVALID_REQUEST",
      });
    }
    await c.close();
    db.close();
  });

  it("rejects an unsafe-integer offset instead of leaking a SQLite datatype error", async () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 1, docsOutage: false });
    const c = await connectInProcess(db, "ap_clerk");
    const first = await callJson<{ next_cursor: string | null }>(c.vdw, "vdw_query", { table: "vendors", limit: 1 });
    expect(first.next_cursor).not.toBeNull();
    const payload = JSON.parse(Buffer.from(first.next_cursor as string, "base64url").toString("utf8")) as {
      o: number;
      h: string;
    };
    payload.o = Number.MAX_SAFE_INTEGER + 100;
    const tampered = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
    let failure: ToolFailure | null = null;
    try {
      await callJson(c.vdw, "vdw_query", { table: "vendors", limit: 1, cursor: tampered });
    } catch (err) {
      failure = err as ToolFailure;
    }
    expect(failure?.name).toBe("ToolFailure");
    expect(failure?.code).toBe("INVALID_REQUEST");
    await c.close();
    db.close();
  });
});
