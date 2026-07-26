import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { ZodRawShapeCompat } from "@modelcontextprotocol/sdk/server/zod-compat.js";
import {
  appendAuditOnlyEvent,
  canonicalJson,
  getProfile,
  recordToolCall,
  sha256Hex,
  type Principal,
  type SiloDatabase,
  type SystemId,
} from "@silobench/domain";
import { SiloError, outage } from "./errors";

export interface ServerContext {
  db: SiloDatabase;
  principal: Principal;
  system: SystemId;
}

export interface ToolSpec<Args extends ZodRawShapeCompat> {
  name: string;
  title: string;
  description: string;
  inputSchema: Args;
  handler: (args: Record<string, unknown>) => unknown;
}

/** Uniform tool wrapper: docs outage gate, structured SiloError results,
 * tool-call logging with a result digest, and an ACCESS_DENIED audit event on
 * every FORBIDDEN attempt (Nordlicht T19: denials are auditable actions). */
export function defineTool<Args extends ZodRawShapeCompat>(
  server: McpServer,
  ctx: ServerContext,
  spec: ToolSpec<Args>,
): void {
  // Observability must never mask the handler outcome: the business mutation
  // has already committed (or already failed) by the time we log, so a
  // logging failure is swallowed rather than converted into a fake error.
  const record = (entry: Parameters<typeof recordToolCall>[1]): void => {
    try {
      recordToolCall(ctx.db, entry);
    } catch {
      // best-effort by design
    }
  };
  server.registerTool(
    spec.name,
    {
      title: spec.title,
      description: spec.description,
      inputSchema: spec.inputSchema,
    },
    (async (args: Record<string, unknown>): Promise<CallToolResult> => {
      try {
        if (ctx.system === "docs" && getProfile(ctx.db).docsOutage) {
          throw outage();
        }
        const result = spec.handler(args ?? {});
        record({
          system: ctx.system,
          principal: ctx.principal,
          tool: spec.name,
          args: args ?? {},
          outcome: "ok",
          errorCode: null,
          resultDigest: sha256Hex(canonicalJson(result ?? null)),
        });
        return {
          content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        };
      } catch (err) {
        if (err instanceof SiloError) {
          if (err.code === "FORBIDDEN") {
            // Audit-only append: a denial must not advance the mutation clock,
            // or it would shift the timestamps of later legitimate writes and
            // leak exploration history into the world hash.
            appendAuditOnlyEvent(ctx.db, {
              actor: ctx.principal,
              system: ctx.system,
              type: "ACCESS_DENIED",
              entityType: String(err.details["entity_type"] ?? "tool"),
              entityId: String(err.details["entity_id"] ?? spec.name),
              payload: { tool: spec.name, message: err.message, ...err.details },
            });
          }
          record({
            system: ctx.system,
            principal: ctx.principal,
            tool: spec.name,
            args: args ?? {},
            outcome: "error",
            errorCode: err.code,
            resultDigest: null,
          });
          return {
            isError: true,
            content: [
              {
                type: "text",
                text: JSON.stringify(
                  { error: { code: err.code, message: err.message, details: err.details } },
                  null,
                  2,
                ),
              },
            ],
          };
        }
        record({
          system: ctx.system,
          principal: ctx.principal,
          tool: spec.name,
          args: args ?? {},
          outcome: "error",
          errorCode: "INTERNAL",
          resultDigest: null,
        });
        throw err;
      }
    }) as never,
  );
}
