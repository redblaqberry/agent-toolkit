/** Logical time. The environment has no wall clock: every timestamp derives
 * from a fixed epoch plus a sequence number.
 *
 * Two clocks exist on purpose:
 * - the mutation clock ticks only on state mutations and stamps domain rows
 *   and audit events, so exploratory reads can never change the state hash;
 * - the tool clock (the tool_calls rowid) orders the observability log.
 */

export const SEED = 4711;

/** 2026-01-05T09:00:00Z, the Monday morning the scenario is frozen at. */
export const LOGICAL_EPOCH_MS = Date.UTC(2026, 0, 5, 9, 0, 0);

/** The nightly warehouse export ran the day before the epoch. */
export const EXPORT_RAN_AT = "2026-01-04T02:00:00.000Z";

export function mutationTs(seq: number): string {
  return new Date(LOGICAL_EPOCH_MS + seq * 1000).toISOString();
}

export function toolTs(seq: number): string {
  return new Date(LOGICAL_EPOCH_MS + seq * 1000).toISOString();
}
