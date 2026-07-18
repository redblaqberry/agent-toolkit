import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import express from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { getProfile, stateHash, type SiloDatabase, type SystemId } from "@silobench/domain";
import { parsePrincipal } from "./auth";
import { buildServer } from "./factory";

export const DEFAULT_PORTS: Record<SystemId, number> = {
  erp: 4712,
  docs: 4713,
  vdw: 4714,
};

/** Stateless streamable-HTTP hosting: a fresh McpServer + transport per POST,
 * bound to the principal from the X-Test-Principal header. One shared SQLite
 * connection carries the world state across all three systems. */
const LOCAL_HOSTNAMES = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

function hostnameOf(value: string): string | null {
  try {
    return new URL(value.includes("://") ? value : `http://${value}`).hostname;
  } catch {
    return null;
  }
}

export function appFor(system: SystemId, db: SiloDatabase): express.Express {
  const app = express();
  app.use(express.json({ limit: "1mb" }));

  // DNS-rebinding guard: this is a local test server; refuse any request whose
  // Origin or Host does not resolve to loopback. Auth itself stays the
  // deliberately spoofable X-Test-Principal header (the perimeter under test
  // is what each principal may do, not how identity is proven).
  app.use((req, res, next) => {
    const origin = req.header("origin");
    if (origin !== undefined && !LOCAL_HOSTNAMES.has(hostnameOf(origin) ?? "")) {
      res.status(403).json({
        error: { code: "FORBIDDEN_ORIGIN", message: "Cross-origin access to this local test server is not allowed" },
      });
      return;
    }
    const host = req.header("host");
    if (host !== undefined && !LOCAL_HOSTNAMES.has(hostnameOf(host) ?? "")) {
      res.status(403).json({
        error: { code: "FORBIDDEN_HOST", message: "This local test server only accepts loopback hosts" },
      });
      return;
    }
    next();
  });

  app.get("/healthz", (_req, res) => {
    res.json({ ok: true, system, profile: getProfile(db), state_hash: stateHash(db) });
  });

  app.post("/mcp", (req, res) => {
    void (async () => {
      const principal = parsePrincipal(req.header("x-test-principal"));
      if (!principal) {
        res.status(401).json({
          error: {
            code: "UNAUTHENTICATED",
            message: "Missing or invalid X-Test-Principal header (expected ap_clerk or ap_approver)",
          },
        });
        return;
      }
      const server = buildServer(system, { db, principal });
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
        enableJsonResponse: true,
      });
      res.on("close", () => {
        void transport.close();
        void server.close();
      });
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    })().catch((err: unknown) => {
      if (!res.headersSent) {
        res.status(500).json({ error: { code: "INTERNAL", message: String(err) } });
      }
    });
  });

  const methodNotAllowed = (_req: express.Request, res: express.Response): void => {
    res.status(405).json({
      error: { code: "METHOD_NOT_ALLOWED", message: "This server runs in stateless mode; use POST /mcp" },
    });
  };
  app.get("/mcp", methodNotAllowed);
  app.delete("/mcp", methodNotAllowed);

  return app;
}

export interface RunningServers {
  addresses: Array<{ system: SystemId; url: string }>;
  close: () => Promise<void>;
}

export async function startHttpServers(options: {
  db: SiloDatabase;
  host?: string;
  ports?: Partial<Record<SystemId, number>>;
}): Promise<RunningServers> {
  const host = options.host ?? "127.0.0.1";
  const running: Array<{ system: SystemId; server: Server; port: number }> = [];
  const closeAll = async (): Promise<void> => {
    await Promise.all(
      running.map(
        (r) =>
          new Promise<void>((resolve) => {
            r.server.close(() => resolve());
          }),
      ),
    );
  };
  try {
    for (const system of ["erp", "docs", "vdw"] as SystemId[]) {
      const port = options.ports?.[system] ?? DEFAULT_PORTS[system];
      const app = appFor(system, options.db);
      const server = await new Promise<Server>((resolve, reject) => {
        const s = app.listen(port, host, () => resolve(s));
        s.on("error", reject);
      });
      const boundPort = (server.address() as AddressInfo).port;
      running.push({ system, server, port: boundPort });
    }
  } catch (err) {
    // If a later port fails to bind, do not leak the servers already bound.
    await closeAll();
    throw err;
  }
  return {
    addresses: running.map((r) => ({ system: r.system, url: `http://${host}:${r.port}/mcp` })),
    close: async () => {
      await Promise.all(
        running.map(
          (r) =>
            new Promise<void>((resolve, reject) => {
              r.server.close((err) => (err ? reject(err) : resolve()));
            }),
        ),
      );
    },
  };
}
