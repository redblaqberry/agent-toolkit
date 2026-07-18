import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { parseArgs } from "node:util";
import {
  getProfile,
  listEvents,
  listToolCalls,
  openDatabase,
  seedDatabase,
  setDocsOutage,
  stateHash,
  takeSnapshot,
  SEED,
  type EnvironmentProfile,
  type SchemaVersion,
} from "@silobench/domain";
import { startHttpServers, DEFAULT_PORTS } from "@silobench/servers";
import { defaultDbPath } from "./paths";
import { computeInitialHashes, loadGolden, runAllTasks, runReferenceTask, writeGolden, type TaskRunResult } from "./runner";
import { loadTasks } from "./tasks";

const USAGE = `silobench: deterministic synthetic enterprise for agent evaluation

Usage: pnpm silobench <command> [options]

Commands
  seed        Create or reset the environment database (alias: reset)
                --db <path> --schema 1|2 --docs-outage
  serve       Start the three MCP servers over streamable HTTP
                --db <path> --host <host> (ports ${DEFAULT_PORTS.erp}/${DEFAULT_PORTS.docs}/${DEFAULT_PORTS.vdw})
                Loopback only: requests with a non-local Host/Origin are rejected
                Auth: X-Test-Principal: ap_clerk | ap_approver
  snapshot    Print the state hash, or export snapshot.v1 with --out <file>
  events      Export the event-log.v1 JSONL (--out <file> or stdout)
  calls       Export the tool-call log as tool-log.v1 JSONL (--out <file> or stdout)
  fault       Toggle the document-service outage: fault docs-outage on|off
  verify      Run scripted reference tasks against fresh environments
                --task TASK-NN --schema 1|2 --docs-outage --reference <key> --json
  golden      Recompute committed golden hashes: golden --write
  tasks       List the twelve task fixtures
`;

interface Flags {
  db: string;
  schema: SchemaVersion | undefined;
  docsOutage: boolean;
  out: string | undefined;
  task: string | undefined;
  reference: string | undefined;
  json: boolean;
  write: boolean;
  host: string | undefined;
}

function fail(message: string): never {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function openExisting(dbPath: string) {
  if (!existsSync(dbPath)) {
    fail(`Database not found at ${dbPath}. Run: pnpm silobench seed`);
  }
  return openDatabase(dbPath);
}

function profileLine(profile: EnvironmentProfile): string {
  return `schema v${profile.schemaVersion}, docs outage ${profile.docsOutage ? "ON" : "off"}`;
}

function seedCommand(flags: Flags): void {
  const profile: EnvironmentProfile = {
    schemaVersion: flags.schema ?? 1,
    docsOutage: flags.docsOutage,
  };
  mkdirSync(dirname(flags.db), { recursive: true });
  const db = openDatabase(flags.db);
  seedDatabase(db, profile);
  const hash = stateHash(db);
  db.close();
  process.stdout.write(`Seeded ${flags.db} (seed ${SEED}, ${profileLine(profile)})\n`);
  process.stdout.write(`Initial state hash: ${hash}\n`);
}

async function serveCommand(flags: Flags): Promise<void> {
  if (!existsSync(flags.db)) {
    process.stdout.write(`No database at ${flags.db}; seeding schema v1 first.\n`);
    seedCommand({ ...flags, schema: 1, docsOutage: false });
  }
  const db = openDatabase(flags.db);
  const running = await startHttpServers({ db, ...(flags.host !== undefined ? { host: flags.host } : {}) });
  process.stdout.write(`Profile: ${profileLine(getProfile(db))}\n`);
  for (const addr of running.addresses) {
    process.stdout.write(`  ${addr.system.padEnd(4)} ${addr.url}\n`);
  }
  process.stdout.write(`Auth header: X-Test-Principal: ap_clerk | ap_approver\n`);
  process.stdout.write(`Press Ctrl+C to stop.\n`);
  await new Promise<void>(() => undefined);
}

function snapshotCommand(flags: Flags): void {
  const db = openExisting(flags.db);
  const snap = takeSnapshot(db);
  db.close();
  if (flags.out !== undefined) {
    writeFileSync(flags.out, `${JSON.stringify(snap, null, 2)}\n`, "utf8");
    process.stdout.write(`Wrote snapshot.v1 to ${flags.out}\n`);
  }
  const tableCounts = Object.fromEntries(Object.entries(snap.tables).map(([name, rows]) => [name, rows.length]));
  process.stdout.write(`State hash: ${snap.state_hash}\n`);
  process.stdout.write(`Mutations:  ${snap.meta.mutation_seq}\n`);
  process.stdout.write(`Tables:     ${JSON.stringify(tableCounts)}\n`);
}

function eventsCommand(flags: Flags): void {
  const db = openExisting(flags.db);
  const profile = getProfile(db);
  const events = listEvents(db);
  const header = {
    format: "event-log.v1",
    seed: SEED,
    schema_version: profile.schemaVersion,
    docs_outage: profile.docsOutage,
    events: events.length,
  };
  const lines = [JSON.stringify(header), ...events.map((event) => JSON.stringify(event))];
  db.close();
  const text = `${lines.join("\n")}\n`;
  if (flags.out !== undefined) {
    writeFileSync(flags.out, text, "utf8");
    process.stdout.write(`Wrote ${lines.length - 1} events to ${flags.out}\n`);
  } else {
    process.stdout.write(text);
  }
}

function callsCommand(flags: Flags): void {
  const db = openExisting(flags.db);
  const profile = getProfile(db);
  const calls = listToolCalls(db);
  const header = {
    format: "tool-log.v1",
    seed: SEED,
    schema_version: profile.schemaVersion,
    docs_outage: profile.docsOutage,
    calls: calls.length,
  };
  const lines = [JSON.stringify(header), ...calls.map((call) => JSON.stringify(call))];
  db.close();
  const text = `${lines.join("\n")}\n`;
  if (flags.out !== undefined) {
    writeFileSync(flags.out, text, "utf8");
    process.stdout.write(`Wrote ${lines.length - 1} tool calls to ${flags.out}\n`);
  } else {
    process.stdout.write(text);
  }
}

function faultCommand(flags: Flags, positionals: string[]): void {
  const [fault, state] = positionals;
  if (fault !== "docs-outage" || (state !== "on" && state !== "off")) {
    fail("Usage: silobench fault docs-outage on|off");
  }
  const db = openExisting(flags.db);
  setDocsOutage(db, state === "on");
  process.stdout.write(`Profile now: ${profileLine(getProfile(db))}\n`);
  db.close();
}

function reportResult(
  result: TaskRunResult,
  goldenHash: string | undefined,
  goldenRequired: boolean,
  write: (line: string) => void,
): boolean {
  // A default run without a committed golden hash is a failure, not a skip:
  // otherwise a missing or truncated golden file silently disables the
  // reproducibility check the command exists for.
  const goldenMissing = goldenRequired && goldenHash === undefined;
  const goldenOk = !goldenMissing && (goldenHash === undefined || goldenHash === result.final_state_hash);
  const passed = result.verdict.passed && goldenOk;
  const marker = passed ? "PASS" : "FAIL";
  write(`${result.task_id}  ${marker}  ${result.principal.padEnd(11)} ${result.title}\n`);
  for (const failure of result.verdict.failures) {
    write(`    ${failure.check}: ${failure.detail}\n`);
  }
  if (goldenMissing) {
    write(`    golden-hash: no committed hash for this default run; run: pnpm silobench golden --write\n`);
  } else if (!goldenOk) {
    write(`    golden-hash: expected ${goldenHash}, got ${result.final_state_hash}\n`);
  }
  return passed;
}

async function verifyCommand(flags: Flags): Promise<void> {
  const golden = loadGolden();
  const options = {
    ...(flags.schema !== undefined ? { schemaVersion: flags.schema } : {}),
    ...(flags.docsOutage ? { docsOutage: true } : {}),
    ...(flags.reference !== undefined ? { reference: flags.reference } : {}),
  };
  const fullRun = flags.task === undefined;
  const results: TaskRunResult[] = flags.task
    ? [await runReferenceTask(flags.task, options)]
    : Object.keys(options).length > 0
      ? fail("--schema/--docs-outage/--reference require --task TASK-NN")
      : await runAllTasks();

  // With --json, stdout is machine-readable JSON only; the human report and
  // notes go to stderr so the output pipes cleanly.
  const write = flags.json
    ? (line: string) => process.stderr.write(line)
    : (line: string) => process.stdout.write(line);
  if (flags.json) {
    process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);
  }
  let allPassed = true;
  for (const result of results) {
    const goldenHash = result.is_default_run ? golden?.tasks[result.task_id] : undefined;
    if (!reportResult(result, goldenHash, result.is_default_run, write)) allPassed = false;
  }
  if (fullRun && golden !== null) {
    const initial = computeInitialHashes();
    for (const [key, hash] of Object.entries(initial)) {
      const expected = golden.initial[key];
      if (expected !== hash) {
        allPassed = false;
        write(`initial(${key})  FAIL  golden ${expected ?? "missing"}, got ${hash}\n`);
      }
    }
  }
  if (golden === null) {
    write("Note: no golden hashes on disk yet; run: pnpm silobench golden --write\n");
  }
  write(allPassed ? "All verified runs passed.\n" : "FAILURES present.\n");
  if (!allPassed) process.exit(1);
}

async function goldenCommand(flags: Flags): Promise<void> {
  if (!flags.write) fail("Usage: silobench golden --write");
  const golden = await writeGolden();
  process.stdout.write(`Wrote golden hashes for ${Object.keys(golden.tasks).length} tasks.\n`);
  process.stdout.write(`Initial hashes: ${JSON.stringify(golden.initial)}\n`);
}

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      db: { type: "string" },
      schema: { type: "string" },
      "docs-outage": { type: "boolean" },
      out: { type: "string" },
      task: { type: "string" },
      reference: { type: "string" },
      json: { type: "boolean" },
      write: { type: "boolean" },
      host: { type: "string" },
      help: { type: "boolean" },
    },
  });
  const [command, ...rest] = positionals;
  if (values.help === true || command === undefined) {
    process.stdout.write(USAGE);
    process.exit(command === undefined && values.help !== true ? 1 : 0);
  }
  const schemaRaw = values.schema;
  if (schemaRaw !== undefined && schemaRaw !== "1" && schemaRaw !== "2") {
    fail("--schema must be 1 or 2");
  }
  const flags: Flags = {
    db: values.db ?? defaultDbPath(),
    schema: schemaRaw === undefined ? undefined : ((Number(schemaRaw) as SchemaVersion)),
    docsOutage: values["docs-outage"] === true,
    out: values.out,
    task: values.task,
    reference: values.reference,
    json: values.json === true,
    write: values.write === true,
    host: values.host,
  };

  switch (command) {
    case "seed":
    case "reset":
      seedCommand(flags);
      return;
    case "serve":
      await serveCommand(flags);
      return;
    case "snapshot":
      snapshotCommand(flags);
      return;
    case "events":
      eventsCommand(flags);
      return;
    case "calls":
      callsCommand(flags);
      return;
    case "fault":
      faultCommand(flags, rest);
      return;
    case "verify":
      await verifyCommand(flags);
      return;
    case "golden":
      await goldenCommand(flags);
      return;
    case "tasks":
      for (const t of loadTasks()) {
        process.stdout.write(`${t.id}  ${t.principal.padEnd(11)} [${t.systems.join(",")}]  ${t.title}\n`);
      }
      return;
    default:
      fail(`Unknown command '${command}'.\n\n${USAGE}`);
  }
}

main().catch((err: unknown) => {
  process.stderr.write(`${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
  process.exit(1);
});
