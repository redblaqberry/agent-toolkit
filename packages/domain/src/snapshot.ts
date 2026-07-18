import type { Database } from "better-sqlite3";
import { SEED } from "./clock";
import { quoteIdent } from "./db";
import { currentCounter } from "./ids";
import { getProfile } from "./profile";
import { canonicalJson, sha256Hex } from "./hash";

/** Three hashes with three meanings:
 * - world_hash: business tables plus profile only. Two runs that reach the
 *   same business outcome hash identically even if one explored more, was
 *   denied more, or triggered more audit events.
 * - audit_hash: the append-only event log alone.
 * - state_hash: everything except the tool-call log; the strict fingerprint
 *   used for golden reference-run reproducibility.
 * The observability log (tool_calls) is never hashed: extra reads must not
 * move any fingerprint. */
const EXCLUDED_TABLES = new Set(["tool_calls"]);
const NON_WORLD_TABLES = new Set(["tool_calls", "events", "counters", "environment"]);

export interface SnapshotV1 {
  format: "snapshot.v1";
  meta: {
    seed: number;
    schema_version: number;
    docs_outage: boolean;
    mutation_seq: number;
  };
  tables: Record<string, Array<Record<string, unknown>>>;
  world_hash: string;
  audit_hash: string;
  state_hash: string;
}

export function listStateTables(db: Database): string[] {
  const rows = db
    .prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name",
    )
    .all() as Array<{ name: string }>;
  return rows.map((r) => r.name).filter((n) => !EXCLUDED_TABLES.has(n));
}

/** The whole snapshot runs inside one read transaction so every table, the
 * profile, and the mutation counter come from a single committed state. A
 * writer committing mid-snapshot can otherwise produce a snapshot of a state
 * that never existed. */
export function takeSnapshot(db: Database): SnapshotV1 {
  const run = db.transaction((): SnapshotV1 => takeSnapshotInner(db));
  return run();
}

function takeSnapshotInner(db: Database): SnapshotV1 {
  const profile = getProfile(db);
  const tables: Record<string, Array<Record<string, unknown>>> = {};
  for (const name of listStateTables(db)) {
    tables[name] = db
      .prepare(`SELECT * FROM ${quoteIdent(name)} ORDER BY rowid`)
      .all() as Array<Record<string, unknown>>;
  }
  const meta = {
    seed: SEED,
    schema_version: profile.schemaVersion,
    docs_outage: profile.docsOutage,
    mutation_seq: currentCounter(db, "mutation_seq"),
  };
  const worldMeta = {
    seed: meta.seed,
    schema_version: meta.schema_version,
    docs_outage: meta.docs_outage,
  };
  const worldTables = Object.fromEntries(
    Object.entries(tables).filter(([name]) => !NON_WORLD_TABLES.has(name)),
  );
  const world_hash = sha256Hex(canonicalJson({ meta: worldMeta, tables: worldTables }));
  const audit_hash = sha256Hex(canonicalJson(tables["events"] ?? []));
  const state_hash = sha256Hex(canonicalJson({ meta, tables }));
  return { format: "snapshot.v1", meta, tables, world_hash, audit_hash, state_hash };
}

export function stateHash(db: Database): string {
  return takeSnapshot(db).state_hash;
}

export function worldHash(db: Database): string {
  return takeSnapshot(db).world_hash;
}
