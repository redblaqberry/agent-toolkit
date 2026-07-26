import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import {
  openDatabase,
  seedDatabase,
  stateHash,
  type EnvironmentProfile,
  type SchemaVersion,
} from "@silobench/domain";
import { ToolFailure, connectInProcess } from "./client";
import { checkExpectations, type Verdict } from "./checker";
import { goldenHashesPath } from "./paths";
import { REFERENCE_RUNS } from "./reference";
import { loadTask, loadTasks, type TaskFixture } from "./tasks";

export interface RunOptions {
  schemaVersion?: SchemaVersion;
  docsOutage?: boolean;
  reference?: string;
}

export interface FailureInfo {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface TaskRunResult {
  task_id: string;
  title: string;
  principal: string;
  reference: string;
  profile: { schema_version: SchemaVersion; docs_outage: boolean };
  answer: unknown;
  failure: FailureInfo | null;
  verdict: Verdict;
  final_state_hash: string;
  is_default_run: boolean;
}

/** Run one task's scripted reference against a fresh in-memory environment
 * and produce the deterministic verdict plus the final state hash. */
export async function runReferenceTask(taskId: string, opts: RunOptions = {}): Promise<TaskRunResult> {
  const fixture: TaskFixture = loadTask(taskId);
  const profile: EnvironmentProfile = {
    schemaVersion: opts.schemaVersion ?? fixture.profile.schema_version,
    docsOutage: opts.docsOutage ?? fixture.profile.docs_outage,
  };
  const referenceKey = opts.reference ?? fixture.reference;
  const referenceRun = REFERENCE_RUNS[referenceKey];
  if (!referenceRun) {
    throw new Error(`Unknown reference run '${referenceKey}'. Known: ${Object.keys(REFERENCE_RUNS).sort().join(", ")}`);
  }
  const isDefaultRun =
    referenceKey === fixture.reference &&
    profile.schemaVersion === fixture.profile.schema_version &&
    profile.docsOutage === fixture.profile.docs_outage;

  const db = openDatabase(":memory:");
  seedDatabase(db, profile);
  const clients = await connectInProcess(db, fixture.principal);
  let answer: unknown = null;
  let failure: FailureInfo | null = null;
  try {
    answer = await referenceRun(clients);
  } catch (err) {
    if (err instanceof ToolFailure) {
      failure = { code: err.code, message: err.message, details: err.details };
    } else {
      await clients.close();
      db.close();
      throw err;
    }
  }
  await clients.close();

  const verdict: Verdict = failure
    ? { passed: false, failures: [{ check: "reference", detail: `${failure.code}: ${failure.message}` }] }
    : checkExpectations(db, fixture, answer);
  const finalHash = stateHash(db);
  db.close();

  return {
    task_id: fixture.id,
    title: fixture.title,
    principal: fixture.principal,
    reference: referenceKey,
    profile: { schema_version: profile.schemaVersion, docs_outage: profile.docsOutage },
    answer,
    failure,
    verdict,
    final_state_hash: finalHash,
    is_default_run: isDefaultRun,
  };
}

export async function runAllTasks(): Promise<TaskRunResult[]> {
  const results: TaskRunResult[] = [];
  for (const fixture of loadTasks()) {
    results.push(await runReferenceTask(fixture.id));
  }
  return results;
}

export interface GoldenFile {
  format: "silobench-golden.v1";
  initial: Record<string, string>;
  tasks: Record<string, string>;
}

export function computeInitialHashes(): Record<string, string> {
  const hashes: Record<string, string> = {};
  for (const version of [1, 2] as SchemaVersion[]) {
    const db = openDatabase(":memory:");
    seedDatabase(db, { schemaVersion: version, docsOutage: false });
    hashes[`v${version}`] = stateHash(db);
    db.close();
  }
  return hashes;
}

export function loadGolden(): GoldenFile | null {
  const path = goldenHashesPath();
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, "utf8")) as GoldenFile;
}

/** Regenerate the committed golden hashes: seeded initial states for both
 * schema versions plus the final state of every default reference run. */
export async function writeGolden(): Promise<GoldenFile> {
  const tasks: Record<string, string> = {};
  for (const result of await runAllTasks()) {
    if (!result.verdict.passed) {
      throw new Error(`Refusing to write golden hashes: ${result.task_id} failed its own verdict`);
    }
    tasks[result.task_id] = result.final_state_hash;
  }
  const golden: GoldenFile = { format: "silobench-golden.v1", initial: computeInitialHashes(), tasks };
  const path = goldenHashesPath();
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(golden, null, 2)}\n`, "utf8");
  return golden;
}
