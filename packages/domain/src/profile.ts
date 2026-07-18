import type { Database } from "better-sqlite3";
import type { EnvironmentProfile, SchemaVersion } from "./types";

function getValue(db: Database, key: string): string {
  const row = db.prepare("SELECT value FROM environment WHERE key = ?").get(key) as
    | { value: string }
    | undefined;
  if (!row) throw new Error(`environment key missing: ${key} (database not seeded?)`);
  return row.value;
}

export function getProfile(db: Database): EnvironmentProfile {
  return {
    schemaVersion: Number(getValue(db, "schema_version")) as SchemaVersion,
    docsOutage: getValue(db, "docs_outage") === "1",
  };
}

/** The docs outage is the only live-togglable fault. The schema version is
 * fixed at seed time because it decides the warehouse DDL. */
export function setDocsOutage(db: Database, on: boolean): void {
  db.prepare("UPDATE environment SET value = ? WHERE key = 'docs_outage'").run(on ? "1" : "0");
}
