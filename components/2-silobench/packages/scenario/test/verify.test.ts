import { describe, expect, it } from "vitest";
import { computeInitialHashes, loadGolden, loadTasks, runReferenceTask } from "@silobench/scenario";

/** The README success metric, executable: every reference run reproduces its
 * committed golden final hash and passes its own verdict, and the drift and
 * outage variants fail in exactly the intended way. */

describe("twelve tasks, reference runs, golden hashes", () => {
  const golden = loadGolden();

  it("has golden hashes on disk", () => {
    expect(golden).not.toBeNull();
    expect(Object.keys(golden?.tasks ?? {})).toHaveLength(12);
  });

  it("reproduces the committed initial hashes for both schema versions", () => {
    expect(computeInitialHashes()).toEqual(golden?.initial);
  });

  for (const fixture of loadTasks()) {
    it(`${fixture.id} passes and reproduces its golden final hash`, async () => {
      const result = await runReferenceTask(fixture.id);
      expect(result.verdict.failures).toEqual([]);
      expect(result.verdict.passed).toBe(true);
      expect(result.final_state_hash).toBe(golden?.tasks[fixture.id]);
    });
  }
});

describe("controlled regressions", () => {
  it("the naive v1-hardcoded TASK-03 script fails under schema v2 with a discoverable drift error", async () => {
    const result = await runReferenceTask("TASK-03", { schemaVersion: 2, reference: "task03_naive" });
    expect(result.verdict.passed).toBe(false);
    expect(result.failure?.code).toBe("INVALID_REQUEST");
    expect(result.failure?.details["available_columns"]).toContain("vendor_legal_name");
  });

  it("the default schema-discovering TASK-03 reference passes under both schema versions with the same answer", async () => {
    const v1 = await runReferenceTask("TASK-03");
    const v2 = await runReferenceTask("TASK-03", { schemaVersion: 2 });
    expect(v1.verdict.passed).toBe(true);
    expect(v2.verdict.passed).toBe(true);
    expect(v2.answer).toEqual(v1.answer);
  });

  it("TASK-02 fails under the docs outage with SERVICE_UNAVAILABLE", async () => {
    const result = await runReferenceTask("TASK-02", { docsOutage: true });
    expect(result.verdict.passed).toBe(false);
    expect(result.failure?.code).toBe("SERVICE_UNAVAILABLE");
  });
});
