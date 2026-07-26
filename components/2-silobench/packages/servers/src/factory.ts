import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { Principal, SiloDatabase, SystemId } from "@silobench/domain";
import { buildDocsServer } from "./docs";
import { buildErpServer } from "./erp";
import { buildVdwServer } from "./vdw";

export const SYSTEMS: SystemId[] = ["erp", "docs", "vdw"];

export interface BuildOptions {
  db: SiloDatabase;
  principal: Principal;
}

export function buildServer(system: SystemId, options: BuildOptions): McpServer {
  switch (system) {
    case "erp":
      return buildErpServer(options);
    case "docs":
      return buildDocsServer(options);
    case "vdw":
      return buildVdwServer(options);
  }
}
