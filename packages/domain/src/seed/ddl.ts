import type { Database } from "better-sqlite3";
import type { SchemaVersion } from "../types";

/** All physical tables. The vdw_* group exists in two variants: the schema
 * drift between warehouse export v1 and v2 is real DDL, not a presentation
 * trick, so schema discovery tools always report what queries will accept. */

const CORE_DDL = `
CREATE TABLE counters (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE environment (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE erp_vendors (
  erp_vendor_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','merged')),
  merged_into TEXT,
  payment_terms_days INTEGER NOT NULL
);
CREATE TABLE purchase_orders (
  po_id TEXT PRIMARY KEY,
  erp_vendor_id TEXT NOT NULL REFERENCES erp_vendors(erp_vendor_id),
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','closed'))
);
CREATE TABLE invoices (
  invoice_id TEXT PRIMARY KEY,
  erp_vendor_id TEXT NOT NULL REFERENCES erp_vendors(erp_vendor_id),
  po_id TEXT REFERENCES purchase_orders(po_id),
  vendor_invoice_no TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  status TEXT NOT NULL CHECK (status IN ('received','matched','approved_for_payment','paid','rejected')),
  received_date TEXT NOT NULL,
  due_date TEXT NOT NULL
);
CREATE TABLE holds (
  hold_id TEXT PRIMARY KEY,
  invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
  reason_code TEXT NOT NULL CHECK (reason_code IN ('bank_change_review','duplicate_suspect','data_mismatch','compliance_review')),
  note TEXT NOT NULL,
  active INTEGER NOT NULL CHECK (active IN (0,1)),
  placed_by TEXT NOT NULL,
  placed_ts TEXT NOT NULL,
  released_by TEXT,
  released_ts TEXT
);
CREATE TABLE approval_requests (
  approval_id TEXT PRIMARY KEY,
  invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
  requested_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','completed')),
  requested_ts TEXT NOT NULL,
  completed_ts TEXT
);
CREATE TABLE payments (
  payment_id TEXT PRIMARY KEY,
  invoice_id TEXT NOT NULL UNIQUE REFERENCES invoices(invoice_id),
  amount_cents INTEGER NOT NULL,
  released_by TEXT NOT NULL,
  approval_id TEXT REFERENCES approval_requests(approval_id),
  released_ts TEXT NOT NULL
);
CREATE UNIQUE INDEX holds_one_active_per_reason
  ON holds (invoice_id, reason_code) WHERE active = 1;
CREATE UNIQUE INDEX approvals_one_pending_per_invoice
  ON approval_requests (invoice_id) WHERE status = 'pending';
CREATE TABLE documents (
  doc_id TEXT PRIMARY KEY,
  folder TEXT NOT NULL,
  title TEXT NOT NULL,
  doc_type TEXT NOT NULL CHECK (doc_type IN ('contract','approval_memo','bank_change_letter','invoice_copy','policy')),
  vendor_name TEXT,
  content TEXT NOT NULL,
  acl_roles TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_date TEXT NOT NULL
);
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  mutation_seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  system TEXT NOT NULL,
  type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE tool_calls (
  call_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  system TEXT NOT NULL,
  principal TEXT NOT NULL,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  outcome TEXT NOT NULL,
  error_code TEXT,
  result_digest TEXT
);
`;

const VDW_V1_DDL = `
CREATE TABLE vdw_vendors (
  vendor_key TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  country TEXT NOT NULL,
  erp_ref TEXT,
  status TEXT NOT NULL
);
CREATE TABLE vdw_bank_accounts (
  account_key TEXT PRIMARY KEY,
  vendor_key TEXT NOT NULL,
  iban TEXT NOT NULL,
  bic TEXT NOT NULL,
  verification_status TEXT NOT NULL,
  verified_at TEXT
);
CREATE TABLE vdw_risk_flags (
  flag_key TEXT PRIMARY KEY,
  vendor_key TEXT NOT NULL,
  flag_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  as_of TEXT NOT NULL
);
CREATE TABLE vdw_ap_invoices_export (
  export_key TEXT PRIMARY KEY,
  source_ref TEXT NOT NULL,
  vendor_invoice_no TEXT NOT NULL,
  vendor_key TEXT NOT NULL,
  amount_eur TEXT NOT NULL,
  status_as_of_export TEXT NOT NULL
);
CREATE TABLE vdw_export_runs (
  run_id TEXT PRIMARY KEY,
  exported_at TEXT NOT NULL,
  schema_version INTEGER NOT NULL
);
`;

/** v2 drift, five changes the ETL team shipped: legal_name renamed,
 * iban renamed, verification_status values uppercased, export amount switched
 * from euro decimal string to integer cents, risk table renamed. */
const VDW_V2_DDL = `
CREATE TABLE vdw_vendors (
  vendor_key TEXT PRIMARY KEY,
  vendor_legal_name TEXT NOT NULL,
  country TEXT NOT NULL,
  erp_ref TEXT,
  status TEXT NOT NULL
);
CREATE TABLE vdw_bank_accounts (
  account_key TEXT PRIMARY KEY,
  vendor_key TEXT NOT NULL,
  iban_number TEXT NOT NULL,
  bic TEXT NOT NULL,
  verification_status TEXT NOT NULL,
  verified_at TEXT
);
CREATE TABLE vdw_vendor_risk_flags (
  flag_key TEXT PRIMARY KEY,
  vendor_key TEXT NOT NULL,
  flag_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  as_of TEXT NOT NULL
);
CREATE TABLE vdw_ap_invoices_export (
  export_key TEXT PRIMARY KEY,
  source_ref TEXT NOT NULL,
  vendor_invoice_no TEXT NOT NULL,
  vendor_key TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  status_as_of_export TEXT NOT NULL
);
CREATE TABLE vdw_export_runs (
  run_id TEXT PRIMARY KEY,
  exported_at TEXT NOT NULL,
  schema_version INTEGER NOT NULL
);
`;

/** A reset must leave nothing behind: drop every user table that exists,
 * not a hardcoded list, so a reseeded database is byte-equivalent to a fresh
 * one even if the file previously held stray tables. */
export function dropAllTables(db: Database): void {
  const rows = db
    .prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name",
    )
    .all() as Array<{ name: string }>;
  for (const { name } of rows) {
    db.exec(`DROP TABLE IF EXISTS "${name.replaceAll('"', '""')}"`);
  }
}

export function createSchema(db: Database, schemaVersion: SchemaVersion): void {
  db.exec(CORE_DDL);
  db.exec(schemaVersion === 1 ? VDW_V1_DDL : VDW_V2_DDL);
}
