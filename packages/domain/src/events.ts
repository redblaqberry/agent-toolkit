import type { Database } from "better-sqlite3";
import { mutationTs } from "./clock";
import type { MutationTick } from "./db";
import { currentCounter, nextId } from "./ids";
import type { Actor, DomainEvent, EventType, SystemId } from "./types";

export interface NewEvent {
  actor: Actor;
  system: SystemId;
  type: EventType;
  entityType: string;
  entityId: string;
  payload: Record<string, unknown>;
}

/** Append-only audit log (Nordlicht constraint T19: every state change and
 * every denial lands in an append-only log with the acting identity
 * attached). Must be called inside withMutation so the event shares the
 * mutation's logical timestamp. */
export function appendEvent(db: Database, tick: MutationTick, evt: NewEvent): string {
  const eventId = nextId(db, "event");
  db.prepare(
    `INSERT INTO events (event_id, mutation_seq, ts, actor, system, type, entity_type, entity_id, payload_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    eventId,
    tick.seq,
    tick.ts,
    evt.actor,
    evt.system,
    evt.type,
    evt.entityType,
    evt.entityId,
    JSON.stringify(evt.payload),
  );
  return eventId;
}

/** Audit-only occurrences (denied attempts) must NOT advance the mutation
 * clock: world time is business time, and a denial changes no business row.
 * The event borrows the current mutation seq for ordering; event ids stay
 * globally monotonic, so the log order is still total and deterministic. */
export function appendAuditOnlyEvent(db: Database, evt: NewEvent): string {
  const run = db.transaction(() => {
    const seq = currentCounter(db, "mutation_seq");
    return appendEvent(db, { seq, ts: mutationTs(seq) }, evt);
  });
  return run.immediate();
}

export function listEvents(db: Database): DomainEvent[] {
  // Insertion order (rowid), not lexical event_id order: EVT-10000 would sort
  // before EVT-9999 as a string, while rowid stays chronological forever.
  const rows = db
    .prepare("SELECT * FROM events ORDER BY mutation_seq, rowid")
    .all() as Array<Omit<DomainEvent, "payload"> & { payload_json: string }>;
  return rows.map(({ payload_json, ...rest }) => ({
    ...rest,
    payload: JSON.parse(payload_json) as Record<string, unknown>,
  }));
}
