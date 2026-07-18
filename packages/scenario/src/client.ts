import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { buildServer } from "@silobench/servers";
import type { Principal, SiloDatabase, SystemId } from "@silobench/domain";

/** Structured failure surfaced by a tool (the environment answering "no").
 * Anything else that throws is a defect in the environment itself. */
export class ToolFailure extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(code: string, message: string, details: Record<string, unknown>) {
    super(message);
    this.name = "ToolFailure";
    this.code = code;
    this.details = details;
  }
}

export interface SiloClients {
  erp: Client;
  docs: Client;
  vdw: Client;
  close: () => Promise<void>;
}

/** One MCP client per silo over in-memory transports, all sharing the same
 * database and principal. This is how reference runs and tests talk to the
 * environment; HTTP serving is byte-identical at the tool layer. */
export async function connectInProcess(db: SiloDatabase, principal: Principal): Promise<SiloClients> {
  const servers: McpServer[] = [];
  const clients: Partial<Record<SystemId, Client>> = {};
  for (const system of ["erp", "docs", "vdw"] as SystemId[]) {
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    const server = buildServer(system, { db, principal });
    servers.push(server);
    await server.connect(serverTransport);
    const client = new Client({ name: `silobench-reference-${system}`, version: "0.1.0" });
    await client.connect(clientTransport);
    clients[system] = client;
  }
  return {
    erp: clients.erp as Client,
    docs: clients.docs as Client,
    vdw: clients.vdw as Client,
    close: async () => {
      for (const client of Object.values(clients)) await client.close();
      for (const server of servers) await server.close();
    },
  };
}

/** Call a tool and parse its JSON text payload; throw ToolFailure on isError. */
export async function callJson<T = Record<string, unknown>>(
  client: Client,
  name: string,
  args: Record<string, unknown> = {},
): Promise<T> {
  const res = (await client.callTool({ name, arguments: args })) as {
    isError?: boolean;
    content?: Array<{ type: string; text?: string }>;
  };
  const text = res.content?.find((part) => part.type === "text")?.text;
  let data: unknown = null;
  let parseFailed = false;
  if (text !== undefined) {
    try {
      data = JSON.parse(text);
    } catch {
      parseFailed = true;
    }
  }
  if (res.isError) {
    const err = parseFailed
      ? undefined
      : (data as { error?: { code?: string; message?: string; details?: Record<string, unknown> } } | null)?.error;
    // A structured SiloError body is a business "no" the caller can handle.
    // An isError result WITHOUT that structure means an internal/environment
    // defect leaked through as plain text; surface it as a hard error so the
    // runner does not record it as an ordinary graceful task failure.
    if (!err || typeof err.code !== "string") {
      throw new Error(`tool ${name} failed with an unstructured error: ${String(text).slice(0, 200)}`);
    }
    throw new ToolFailure(err.code, err.message ?? "tool call failed", err.details ?? {});
  }
  if (parseFailed) {
    throw new Error(`tool ${name} returned non-JSON text: ${String(text).slice(0, 200)}`);
  }
  return data as T;
}
