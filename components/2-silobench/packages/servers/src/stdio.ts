import { existsSync } from "node:fs";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { openDatabase, type SystemId } from "@silobench/domain";
import { parsePrincipal } from "./auth";
import { buildServer } from "./factory";

/** stdio hosting for one system. The principal travels in SILOBENCH_PRINCIPAL
 * because stdio has no headers; the database path in SILOBENCH_DB. */
export async function runStdioServer(system: SystemId): Promise<void> {
  const dbPath = process.env["SILOBENCH_DB"] ?? ".silobench/state.db";
  const principal = parsePrincipal(process.env["SILOBENCH_PRINCIPAL"]);
  if (!principal) {
    process.stderr.write("SILOBENCH_PRINCIPAL must be ap_clerk or ap_approver\n");
    process.exit(1);
  }
  if (!existsSync(dbPath)) {
    process.stderr.write(`Database not found at ${dbPath}. Run: pnpm silobench seed\n`);
    process.exit(1);
  }
  const db = openDatabase(dbPath);
  const server = buildServer(system, { db, principal });
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
