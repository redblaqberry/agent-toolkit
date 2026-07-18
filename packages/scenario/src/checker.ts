import {
  canonicalJson,
  listEvents,
  listStateTables,
  listToolCalls,
  quoteIdent,
  type SiloDatabase,
} from "@silobench/domain";
import type { EventCheck, StateCheck, TaskFixture } from "./tasks";

export interface CheckFailure {
  check: string;
  detail: string;
}

export interface Verdict {
  passed: boolean;
  failures: CheckFailure[];
}

function rowsMatching(db: SiloDatabase, check: StateCheck): Array<Record<string, unknown>> {
  if (!listStateTables(db).includes(check.table)) {
    throw new Error(`State check references unknown table '${check.table}'`);
  }
  const rows = db.prepare(`SELECT * FROM ${quoteIdent(check.table)} ORDER BY rowid`).all() as Array<Record<string, unknown>>;
  const where = check.where ?? {};
  return rows.filter((row) => Object.entries(where).every(([k, v]) => row[k] === v));
}

function checkState(db: SiloDatabase, check: StateCheck, failures: CheckFailure[]): void {
  const label = `state:${check.table}${check.where ? canonicalJson(check.where) : ""}`;
  const rows = rowsMatching(db, check);
  if (check.count !== undefined && rows.length !== check.count) {
    failures.push({ check: label, detail: `expected count ${check.count}, found ${rows.length}` });
  }
  if (check.count_min !== undefined && rows.length < check.count_min) {
    failures.push({ check: label, detail: `expected at least ${check.count_min}, found ${rows.length}` });
  }
  if (check.count_max !== undefined && rows.length > check.count_max) {
    failures.push({ check: label, detail: `expected at most ${check.count_max}, found ${rows.length}` });
  }
  if (check.fields !== undefined) {
    if (rows.length !== 1) {
      failures.push({ check: label, detail: `fields check needs exactly one matching row, found ${rows.length}` });
      return;
    }
    const row = rows[0];
    if (!row) return;
    for (const [field, expected] of Object.entries(check.fields)) {
      if (row[field] !== expected) {
        failures.push({
          check: label,
          detail: `field ${field}: expected ${JSON.stringify(expected)}, found ${JSON.stringify(row[field])}`,
        });
      }
    }
  }
}

function checkEvents(db: SiloDatabase, check: EventCheck, failures: CheckFailure[]): void {
  const events = listEvents(db).filter(
    (e) =>
      e.type === check.type &&
      (check.entity_id === undefined || e.entity_id === check.entity_id) &&
      (check.payload_fields === undefined ||
        Object.entries(check.payload_fields).every(([field, value]) => e.payload[field] === value)),
  );
  if (events.length !== check.count) {
    const payloadNote = check.payload_fields ? ` with payload ${canonicalJson(check.payload_fields)}` : "";
    failures.push({
      check: `events:${check.type}${check.entity_id ? `:${check.entity_id}` : ""}${payloadNote}`,
      detail: `expected ${check.count}, found ${events.length}`,
    });
  }
}

/** Deterministic verdict: answer deep-equality (canonical JSON), state-row
 * expectations, and audit-event expectations. This is the self-check layer;
 * richer trajectory scoring and cross-snapshot invariants belong to tooling
 * built on top of the exports. */
export function checkExpectations(db: SiloDatabase, fixture: TaskFixture, answer: unknown): Verdict {
  const failures: CheckFailure[] = [];
  if (fixture.expected.answer !== undefined) {
    const got = canonicalJson(answer ?? null);
    const want = canonicalJson(fixture.expected.answer);
    if (got !== want) {
      failures.push({ check: "answer", detail: `expected ${want}, got ${got}` });
    }
  }
  for (const stateCheck of fixture.expected.state ?? []) checkState(db, stateCheck, failures);
  for (const eventCheck of fixture.expected.events ?? []) checkEvents(db, eventCheck, failures);
  const requiredCalls = fixture.expected.required_calls ?? [];
  if (requiredCalls.length > 0) {
    const calls = listToolCalls(db);
    for (const req of requiredCalls) {
      const seen = calls.some((call) => {
        if (call.system !== req.system || call.tool !== req.tool || call.outcome !== "ok") return false;
        if (!req.args_include) return true;
        return req.args_include.every((needle) => call.args_json.includes(String(needle)));
      });
      if (!seen) {
        failures.push({
          check: `required_call:${req.system}.${req.tool}`,
          detail: req.args_include
            ? `no successful call whose arguments include ${JSON.stringify(req.args_include)}`
            : "this tool was never called successfully during the run",
        });
      }
    }
  }
  return { passed: failures.length === 0, failures };
}
