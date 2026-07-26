import { describe, expect, it } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { openDatabase, seedDatabase } from "@silobench/domain";
import { startHttpServers } from "@silobench/servers";
import { callJson } from "@silobench/scenario";

/** End-to-end over real HTTP: header auth, stateless streamable transport,
 * all three silos on their own ports. */
describe("streamable HTTP hosting", () => {
  it("serves all three systems with header auth and rejects missing principals", async () => {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: 1, docsOutage: false });
    const running = await startHttpServers({ db, ports: { erp: 0, docs: 0, vdw: 0 } });
    try {
      const erpUrl = running.addresses.find((a) => a.system === "erp")?.url;
      expect(erpUrl).toBeDefined();

      const client = new Client({ name: "http-test", version: "0.1.0" });
      const transport = new StreamableHTTPClientTransport(new URL(erpUrl as string), {
        requestInit: { headers: { "X-Test-Principal": "ap_clerk" } },
      });
      await client.connect(transport);
      const tools = await client.listTools();
      expect(tools.tools.map((t) => t.name)).toContain("erp_release_payment");
      const invoice = await callJson<{ invoice_id: string }>(client, "erp_get_invoice", {
        invoice_id: "INV-2026-00330",
      });
      expect(invoice.invoice_id).toBe("INV-2026-00330");
      await client.close();

      const anonymous = new Client({ name: "anon-test", version: "0.1.0" });
      const anonTransport = new StreamableHTTPClientTransport(new URL(erpUrl as string));
      await expect(anonymous.connect(anonTransport)).rejects.toThrow(/UNAUTHENTICATED/);

      const response = await fetch((erpUrl as string).replace("/mcp", "/healthz"));
      const health = (await response.json()) as { ok: boolean; system: string; state_hash: string };
      expect(health.ok).toBe(true);
      expect(health.system).toBe("erp");
      expect(health.state_hash).toHaveLength(64);
    } finally {
      await running.close();
      db.close();
    }
  });
});
