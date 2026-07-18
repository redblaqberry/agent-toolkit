import BetterSqlite3 from "better-sqlite3";
import type { Database } from "better-sqlite3";
import { mutationTs, toolTs } from "./clock";
import { nextCounter } from "./ids";

export type SiloDatabase = Database;

/** Quote an SQL identifier, doubling embedded quotes. Every dynamic
 * identifier interpolation in the codebase must go through this. */
export function quoteIdent(name: string): string {
  return `"${name.replaceAll('"', '""')}"`;
}

export function openDatabase(path: string): SiloDatabase {
  const db = new BetterSqlite3(path);
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  return db;
}

export interface MutationTick {
  seq: number;
  ts: string;
}

/** Every state mutation runs through here: one immediate (write-locking)
 * transaction, one mutation-clock tick shared by all rows and events written
 * inside it. Immediate mode keeps counter allocation and row writes atomic
 * even if a second process ever attaches to the same database file. */
export function withMutation<T>(db: SiloDatabase, fn: (tick: MutationTick) => T): T {
  const run = db.transaction(() => {
    const seq = nextCounter(db, "mutation_seq");
    return fn({ seq, ts: mutationTs(seq) });
  });
  return run.immediate();
}

export interface ToolCallEntry {
  system: string;
  principal: string;
  tool: string;
  args: unknown;
  outcome: "ok" | "error";
  errorCode: string | null;
  resultDigest: string | null;
}

export interface ToolCallRow {
  call_seq: number;
  ts: string;
  system: string;
  principal: string;
  tool: string;
  args_json: string;
  outcome: string;
  error_code: string | null;
  result_digest: string | null;
}

export function listToolCalls(db: SiloDatabase): ToolCallRow[] {
  return db.prepare("SELECT * FROM tool_calls ORDER BY call_seq").all() as ToolCallRow[];
}

/** Observability log. Excluded from the state hash by design: an agent that
 * merely reads more must not change the world's fingerprint. */
export function recordToolCall(db: SiloDatabase, entry: ToolCallEntry): void {
  const run = db.transaction(() => {
    const inserted = db
      .prepare(
        `INSERT INTO tool_calls (ts, system, principal, tool, args_json, outcome, error_code, result_digest)
         VALUES ('', ?, ?, ?, ?, ?, ?, ?) RETURNING call_seq`,
      )
      .get(
        entry.system,
        entry.principal,
        entry.tool,
        JSON.stringify(entry.args ?? {}),
        entry.outcome,
        entry.errorCode,
        entry.resultDigest,
      ) as { call_seq: number };
    db.prepare("UPDATE tool_calls SET ts = ? WHERE call_seq = ?").run(
      toolTs(inserted.call_seq),
      inserted.call_seq,
    );
  });
  run();
}
