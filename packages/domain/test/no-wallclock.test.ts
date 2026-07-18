import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/** Determinism guard: no wall-clock time and no unseeded randomness anywhere
 * in runtime source. The logical epoch in clock.ts is built from Date.UTC
 * with fixed literals, which is allowed; wall clocks (Date.now, zero-argument
 * new Date, performance.now, process.hrtime) and entropy sources
 * (Math.random, crypto randomUUID/randomBytes/randomInt/randomFill/
 * getRandomValues) are not. */

const FORBIDDEN = [
  /Date\.now\s*\(/,
  /Math\.random\s*\(/,
  /new Date\(\s*\)/,
  /performance\.now\s*\(/,
  /process\.hrtime/,
  /randomUUID/,
  /randomBytes/,
  /randomInt\s*\(/,
  /randomFill/,
  /getRandomValues/,
];

function collectSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...collectSourceFiles(full));
    else if (entry.endsWith(".ts")) out.push(full);
  }
  return out;
}

describe("no wall clock, no unseeded randomness", () => {
  const packagesDir = join(import.meta.dirname, "..", "..");
  const sourceDirs = readdirSync(packagesDir)
    .map((p) => join(packagesDir, p, "src"))
    .filter((p) => {
      try {
        return statSync(p).isDirectory();
      } catch {
        return false;
      }
    });

  it("scans every package's src tree", () => {
    expect(sourceDirs.length).toBeGreaterThanOrEqual(3);
  });

  for (const dir of sourceDirs) {
    it(`finds no forbidden time or randomness calls under ${dir.split(/[\\/]/).slice(-2).join("/")}`, () => {
      for (const file of collectSourceFiles(dir)) {
        const text = readFileSync(file, "utf8");
        for (const pattern of FORBIDDEN) {
          expect(pattern.test(text), `${file} matches ${pattern}`).toBe(false);
        }
      }
    });
  }
});
