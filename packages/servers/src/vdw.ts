import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { canonicalJson, quoteIdent, type SiloDatabase } from "@silobench/domain";
import { invalidRequest, notFound } from "./errors";
import { makeCursor, readCursor } from "./paginate";
import { defineTool, type ServerContext } from "./toolkit";

/** BigQuery-shaped, read-only. The dataset is a nightly ETL copy of the
 * on-prem vendor master; freshness and schema version come from
 * vdw_get_export_info. Table and column names differ between export schema
 * v1 and v2, and every validation error lists what actually exists, so
 * schema drift is discoverable instead of silent. */

const DATASET = "vendor_master";
const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 50;

const LOGICAL_BY_PHYSICAL: Record<string, string> = {
  vdw_vendors: "vendors",
  vdw_bank_accounts: "bank_accounts",
  vdw_risk_flags: "risk_flags",
  vdw_vendor_risk_flags: "vendor_risk_flags",
  vdw_ap_invoices_export: "ap_invoices_export",
  vdw_export_runs: "_export_runs",
};

interface ColumnInfo {
  name: string;
  type: string;
}

function tableMap(db: SiloDatabase): Map<string, string> {
  const rows = db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'vdw~_%' ESCAPE '~' ORDER BY name")
    .all() as Array<{ name: string }>;
  const map = new Map<string, string>();
  for (const { name } of rows) {
    const logical = LOGICAL_BY_PHYSICAL[name];
    if (logical) map.set(logical, name);
  }
  return map;
}

function columnsOf(db: SiloDatabase, physical: string): ColumnInfo[] {
  const rows = db.prepare(`PRAGMA table_info(${quoteIdent(physical)})`).all() as Array<{
    cid: number;
    name: string;
    type: string;
  }>;
  return rows.sort((a, b) => a.cid - b.cid).map((r) => ({ name: r.name, type: r.type || "TEXT" }));
}

function resolveTable(db: SiloDatabase, logical: string): { physical: string; columns: ColumnInfo[] } {
  const map = tableMap(db);
  const physical = map.get(logical);
  if (!physical) {
    throw notFound("table", logical, { available_tables: [...map.keys()].sort() });
  }
  return { physical, columns: columnsOf(db, physical) };
}

const WHERE_OPS = { eq: "=", like: "LIKE", gte: ">=", lte: "<=" } as const;

export function buildVdwServer(ctx: Omit<ServerContext, "system">): McpServer {
  const server = new McpServer({ name: "silobench-vendor-dw", version: "0.1.0" });
  const c: ServerContext = { ...ctx, system: "vdw" };
  const { db } = c;

  defineTool(server, c, {
    name: "vdw_list_tables",
    title: "List warehouse tables",
    description: `List the tables of the ${DATASET} dataset with their column schemas. Always check schemas before querying: the export schema changes between versions.`,
    inputSchema: {},
    handler: () => {
      const map = tableMap(db);
      return {
        dataset: DATASET,
        tables: [...map.entries()]
          .sort(([a], [b]) => (a < b ? -1 : 1))
          .map(([logical, physical]) => ({ table: logical, columns: columnsOf(db, physical) })),
      };
    },
  });

  defineTool(server, c, {
    name: "vdw_get_table_schema",
    title: "Get table schema",
    description: `Column names and types for one table of the ${DATASET} dataset.`,
    inputSchema: { table: z.string() },
    handler: (args) => {
      const logical = String(args["table"]);
      const { columns } = resolveTable(db, logical);
      return { dataset: DATASET, table: logical, columns };
    },
  });

  defineTool(server, c, {
    name: "vdw_query",
    title: "Query warehouse table",
    description:
      "Read rows from one table with optional column projection, filters (eq, like, gte, lte), ordering, and pagination. Read-only. Unknown tables or columns fail with the list of what exists.",
    inputSchema: {
      table: z.string(),
      // Caps keep schema-valid requests inside SQLite's column/expression
      // limits, so oversized input fails as INVALID_REQUEST at validation
      // instead of leaking an internal prepare error.
      columns: z.array(z.string()).min(1).max(32).optional(),
      where: z
        .array(
          z.object({
            column: z.string(),
            op: z.enum(["eq", "like", "gte", "lte"]),
            value: z.union([z.string(), z.number()]),
          }),
        )
        .max(16)
        .optional(),
      order_by: z.string().optional(),
      limit: z.number().int().min(1).max(MAX_LIMIT).optional(),
      cursor: z.string().optional(),
    },
    handler: (args) => {
      const logical = String(args["table"]);
      const { physical, columns } = resolveTable(db, logical);
      const known = new Set(columns.map((col) => col.name));
      const requested = (args["columns"] as string[] | undefined) ?? columns.map((col) => col.name);
      for (const col of requested) {
        if (!known.has(col)) {
          throw invalidRequest(`Unknown column '${col}' on table '${logical}'`, {
            unknown_column: col,
            available_columns: columns.map((x) => x.name),
          });
        }
      }
      const where = (args["where"] ?? []) as Array<{ column: string; op: keyof typeof WHERE_OPS; value: string | number }>;
      const clauses: string[] = [];
      const params: unknown[] = [];
      for (const cond of where) {
        if (!known.has(cond.column)) {
          throw invalidRequest(`Unknown column '${cond.column}' in where clause on table '${logical}'`, {
            unknown_column: cond.column,
            available_columns: columns.map((x) => x.name),
          });
        }
        clauses.push(`${quoteIdent(cond.column)} ${WHERE_OPS[cond.op]} ?`);
        params.push(cond.value);
      }
      const orderBy = args["order_by"] as string | undefined;
      if (orderBy !== undefined && !known.has(orderBy)) {
        throw invalidRequest(`Unknown order_by column '${orderBy}' on table '${logical}'`, {
          unknown_column: orderBy,
          available_columns: columns.map((x) => x.name),
        });
      }
      const limit = (args["limit"] as number | undefined) ?? DEFAULT_LIMIT;
      const queryKey = canonicalJson({
        tool: "vdw_query",
        table: logical,
        columns: requested,
        where,
        order_by: orderBy ?? null,
        limit,
      });
      const offset = readCursor(queryKey, args["cursor"] as string | undefined);
      const selectList = requested.map((col) => quoteIdent(col)).join(", ");
      const whereSql = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
      const orderSql = orderBy ? `ORDER BY ${quoteIdent(orderBy)} ASC, rowid ASC` : "ORDER BY rowid ASC";
      const total = (
        db.prepare(`SELECT COUNT(*) AS n FROM ${quoteIdent(physical)} ${whereSql}`).get(...params) as { n: number }
      ).n;
      const rows = db
        .prepare(`SELECT ${selectList} FROM ${quoteIdent(physical)} ${whereSql} ${orderSql} LIMIT ? OFFSET ?`)
        .all(...params, limit, offset) as Array<Record<string, unknown>>;
      const nextOffset = offset + rows.length;
      return {
        dataset: DATASET,
        table: logical,
        rows,
        total_matches: total,
        next_cursor: nextOffset < total ? makeCursor(queryKey, nextOffset) : null,
      };
    },
  });

  defineTool(server, c, {
    name: "vdw_get_export_info",
    title: "Get export freshness",
    description:
      "Metadata of the nightly ETL run that produced this dataset: run id, export timestamp, schema version. The warehouse is a copy; anything that changed in source systems after exported_at is not reflected here.",
    inputSchema: {},
    handler: () => {
      const run = db.prepare("SELECT * FROM vdw_export_runs ORDER BY run_id DESC LIMIT 1").get() as
        | { run_id: string; exported_at: string; schema_version: number }
        | undefined;
      if (!run) throw notFound("export_run", "latest");
      return {
        dataset: DATASET,
        run_id: run.run_id,
        exported_at: run.exported_at,
        schema_version: run.schema_version,
        note: "Nightly ETL copy of the on-prem vendor master. Data is as of exported_at, not live.",
      };
    },
  });

  return server;
}
