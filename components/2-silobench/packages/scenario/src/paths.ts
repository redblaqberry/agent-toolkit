import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

export function tasksDir(): string {
  return join(packageRoot, "tasks");
}

export function goldenHashesPath(): string {
  return join(packageRoot, "fixtures", "golden-hashes.json");
}

export function defaultDbPath(): string {
  return join(process.cwd(), ".silobench", "state.db");
}
