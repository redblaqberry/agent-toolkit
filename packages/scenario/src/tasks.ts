import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { tasksDir } from "./paths";

/** Task fixture format. This is the contract evaluation harnesses consume:
 * prompt + principal + profile describe the run; expected describes the
 * verdict. Reference names point at scripted deterministic runs used for
 * self-verification (the stand-in for a live agent). */

export const StateCheckSchema = z.object({
  table: z.string(),
  where: z.record(z.string(), z.union([z.string(), z.number(), z.null()])).optional(),
  count: z.number().int().optional(),
  count_min: z.number().int().optional(),
  count_max: z.number().int().optional(),
  fields: z.record(z.string(), z.union([z.string(), z.number(), z.null()])).optional(),
});

export const EventCheckSchema = z.object({
  type: z.string(),
  entity_id: z.string().optional(),
  count: z.number().int(),
  /** Fields the counted events' payloads must carry, matched by strict
   * equality against the ACTUAL event payload, never against anything the
   * agent self-reports. This is what pins a relationship (for example which
   * invoice a duplicate was flagged against) to what really happened. */
  payload_fields: z
    .record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()]))
    .optional(),
});

/** A tool that must have been called successfully at least once during the
 * run. This is the v1 slice of trajectory checking: it stops a run from
 * passing a cross-silo task while never touching the other silos. args_include
 * pins the call to the right entity (its logged arguments must contain every
 * listed value), so a run cannot satisfy the check by reading an unrelated
 * record. Ordering and full argument-shape predicates still belong to the
 * trajectory-scoring layer above the environment. */
export const RequiredCallSchema = z.object({
  system: z.enum(["erp", "docs", "vdw"]),
  tool: z.string(),
  args_include: z.array(z.union([z.string(), z.number()])).optional(),
});

export const TaskFixtureSchema = z.object({
  id: z.string().regex(/^TASK-\d{2}$/),
  title: z.string(),
  principal: z.enum(["ap_clerk", "ap_approver"]),
  profile: z.object({
    schema_version: z.union([z.literal(1), z.literal(2)]),
    docs_outage: z.boolean(),
  }),
  systems: z.array(z.enum(["erp", "docs", "vdw"])),
  prompt: z.string(),
  reference: z.string(),
  expected: z.object({
    answer: z.unknown().optional(),
    state: z.array(StateCheckSchema).optional(),
    events: z.array(EventCheckSchema).optional(),
    required_calls: z.array(RequiredCallSchema).optional(),
  }),
});

export type TaskFixture = z.infer<typeof TaskFixtureSchema>;
export type StateCheck = z.infer<typeof StateCheckSchema>;
export type EventCheck = z.infer<typeof EventCheckSchema>;
export type RequiredCall = z.infer<typeof RequiredCallSchema>;

export function loadTasks(): TaskFixture[] {
  const files = readdirSync(tasksDir())
    .filter((f) => f.endsWith(".json"))
    .sort();
  return files.map((file) => {
    const raw: unknown = JSON.parse(readFileSync(join(tasksDir(), file), "utf8"));
    const parsed = TaskFixtureSchema.safeParse(raw);
    if (!parsed.success) {
      throw new Error(`Invalid task fixture ${file}: ${parsed.error.message}`);
    }
    return parsed.data;
  });
}

export function loadTask(taskId: string): TaskFixture {
  const task = loadTasks().find((t) => t.id === taskId);
  if (!task) {
    throw new Error(`Unknown task ${taskId}. Known: ${loadTasks().map((t) => t.id).join(", ")}`);
  }
  return task;
}
