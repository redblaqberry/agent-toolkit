import type { Database } from "better-sqlite3";

/** Deterministic runtime identifiers: per-kind counters stored in the
 * counters table, formatted as PAY-0001 style ids. Counters advance only
 * inside mutations, never on reads. */

export type IdKind = "payment" | "hold" | "approval" | "event";

const PREFIX: Record<IdKind, string> = {
  payment: "PAY",
  hold: "HOLD",
  approval: "APR",
  event: "EVT",
};

export function nextCounter(db: Database, name: string): number {
  const row = db
    .prepare("UPDATE counters SET value = value + 1 WHERE name = ? RETURNING value")
    .get(name) as { value: number } | undefined;
  if (!row) throw new Error(`unknown counter: ${name}`);
  return row.value;
}

export function currentCounter(db: Database, name: string): number {
  const row = db.prepare("SELECT value FROM counters WHERE name = ?").get(name) as
    | { value: number }
    | undefined;
  if (!row) throw new Error(`unknown counter: ${name}`);
  return row.value;
}

export function formatId(kind: IdKind, n: number): string {
  return `${PREFIX[kind]}-${String(n).padStart(4, "0")}`;
}

export function nextId(db: Database, kind: IdKind): string {
  return formatId(kind, nextCounter(db, kind));
}
