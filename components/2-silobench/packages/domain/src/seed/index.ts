import type { Database } from "better-sqlite3";
import { EXPORT_RAN_AT, SEED } from "../clock";
import { centsToEuroString } from "../money";
import type {
  DocumentRow,
  DwBankAccountSeed,
  DwInvoiceExportSeed,
  DwRiskFlagSeed,
  DwVendorSeed,
  EnvironmentProfile,
  ErpVendorRow,
  HoldRow,
  InvoiceRow,
  PurchaseOrderRow,
  ApprovalRequestRow,
} from "../types";
import { createSchema, dropAllTables } from "./ddl";
import * as FIX from "./fixtures";
import { buildFiller } from "./filler";

export const DEFAULT_PROFILE: EnvironmentProfile = { schemaVersion: 1, docsOutage: false };

/** Full deterministic reset: drop, recreate for the profile's schema version,
 * insert fixtures plus filler, initialize counters. Identical inputs produce
 * an identical database, byte-for-byte at the canonical-snapshot level. */
export function seedDatabase(db: Database, profile: EnvironmentProfile = DEFAULT_PROFILE): void {
  db.pragma("foreign_keys = OFF");
  const filler = buildFiller();
  const run = db.transaction(() => {
    dropAllTables(db);
    createSchema(db, profile.schemaVersion);
    insertErp(db, filler);
    insertDocuments(db, [...FIX.DOCUMENTS, ...filler.documents]);
    insertWarehouse(db, profile.schemaVersion, filler);
    initCounters(db);
    initEnvironment(db, profile);
  });
  run();
  db.pragma("foreign_keys = ON");
}

function insertErp(db: Database, filler: ReturnType<typeof buildFiller>): void {
  const vendors: ErpVendorRow[] = [...FIX.ERP_VENDORS, ...filler.erpVendors];
  const pos: PurchaseOrderRow[] = [...FIX.PURCHASE_ORDERS, ...filler.purchaseOrders];
  const invoices: InvoiceRow[] = [...FIX.INVOICES, ...filler.invoices];
  const holds: HoldRow[] = FIX.HOLDS;
  const approvals: ApprovalRequestRow[] = FIX.APPROVAL_REQUESTS;

  const insVendor = db.prepare(
    "INSERT INTO erp_vendors (erp_vendor_id, name, status, merged_into, payment_terms_days) VALUES (?, ?, ?, ?, ?)",
  );
  for (const v of vendors) insVendor.run(v.erp_vendor_id, v.name, v.status, v.merged_into, v.payment_terms_days);

  const insPo = db.prepare(
    "INSERT INTO purchase_orders (po_id, erp_vendor_id, amount_cents, status) VALUES (?, ?, ?, ?)",
  );
  for (const p of pos) insPo.run(p.po_id, p.erp_vendor_id, p.amount_cents, p.status);

  const insInvoice = db.prepare(
    `INSERT INTO invoices (invoice_id, erp_vendor_id, po_id, vendor_invoice_no, amount_cents, currency, status, received_date, due_date)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  for (const i of invoices) {
    insInvoice.run(i.invoice_id, i.erp_vendor_id, i.po_id, i.vendor_invoice_no, i.amount_cents, i.currency, i.status, i.received_date, i.due_date);
  }

  const insHold = db.prepare(
    `INSERT INTO holds (hold_id, invoice_id, reason_code, note, active, placed_by, placed_ts, released_by, released_ts)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  for (const h of holds) insHold.run(h.hold_id, h.invoice_id, h.reason_code, h.note, h.active, h.placed_by, h.placed_ts, h.released_by, h.released_ts);

  const insApproval = db.prepare(
    `INSERT INTO approval_requests (approval_id, invoice_id, requested_by, reason, status, requested_ts, completed_ts)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  );
  for (const a of approvals) insApproval.run(a.approval_id, a.invoice_id, a.requested_by, a.reason, a.status, a.requested_ts, a.completed_ts);
}

function insertDocuments(db: Database, docs: DocumentRow[]): void {
  const ins = db.prepare(
    `INSERT INTO documents (doc_id, folder, title, doc_type, vendor_name, content, acl_roles, version, created_date)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  );
  for (const d of docs) {
    ins.run(d.doc_id, d.folder, d.title, d.doc_type, d.vendor_name, d.content, d.acl_roles, d.version, d.created_date);
  }
}

function insertWarehouse(db: Database, schemaVersion: 1 | 2, filler: ReturnType<typeof buildFiller>): void {
  const vendors: DwVendorSeed[] = [...FIX.DW_VENDORS, ...filler.dwVendors];
  const accounts: DwBankAccountSeed[] = [...FIX.DW_BANK_ACCOUNTS, ...filler.dwBankAccounts];
  const flags: DwRiskFlagSeed[] = FIX.DW_RISK_FLAGS;
  const exportsRows: DwInvoiceExportSeed[] = [...FIX.DW_INVOICE_EXPORTS, ...filler.dwInvoiceExports];

  if (schemaVersion === 1) {
    const insVendor = db.prepare(
      "INSERT INTO vdw_vendors (vendor_key, legal_name, country, erp_ref, status) VALUES (?, ?, ?, ?, ?)",
    );
    for (const v of vendors) insVendor.run(v.vendor_key, v.legal_name, v.country, v.erp_ref, v.status);

    const insAccount = db.prepare(
      "INSERT INTO vdw_bank_accounts (account_key, vendor_key, iban, bic, verification_status, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
    );
    accounts.forEach((a, idx) => {
      insAccount.run(accountKey(idx), a.vendor_key, a.iban, a.bic, a.verification_status, a.verified_at);
    });

    const insFlag = db.prepare(
      "INSERT INTO vdw_risk_flags (flag_key, vendor_key, flag_type, severity, as_of) VALUES (?, ?, ?, ?, ?)",
    );
    flags.forEach((f, idx) => insFlag.run(flagKey(idx), f.vendor_key, f.flag_type, f.severity, f.as_of));

    const insExport = db.prepare(
      "INSERT INTO vdw_ap_invoices_export (export_key, source_ref, vendor_invoice_no, vendor_key, amount_eur, status_as_of_export) VALUES (?, ?, ?, ?, ?, ?)",
    );
    for (const e of exportsRows) insExport.run(e.export_key, e.source_ref, e.vendor_invoice_no, e.vendor_key, centsToEuroString(e.amount_cents), e.status_as_of_export);
  } else {
    const insVendor = db.prepare(
      "INSERT INTO vdw_vendors (vendor_key, vendor_legal_name, country, erp_ref, status) VALUES (?, ?, ?, ?, ?)",
    );
    for (const v of vendors) insVendor.run(v.vendor_key, v.legal_name, v.country, v.erp_ref, v.status);

    const insAccount = db.prepare(
      "INSERT INTO vdw_bank_accounts (account_key, vendor_key, iban_number, bic, verification_status, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
    );
    accounts.forEach((a, idx) => {
      insAccount.run(accountKey(idx), a.vendor_key, a.iban, a.bic, a.verification_status.toUpperCase(), a.verified_at);
    });

    const insFlag = db.prepare(
      "INSERT INTO vdw_vendor_risk_flags (flag_key, vendor_key, flag_type, severity, as_of) VALUES (?, ?, ?, ?, ?)",
    );
    flags.forEach((f, idx) => insFlag.run(flagKey(idx), f.vendor_key, f.flag_type, f.severity, f.as_of));

    const insExport = db.prepare(
      "INSERT INTO vdw_ap_invoices_export (export_key, source_ref, vendor_invoice_no, vendor_key, amount_cents, status_as_of_export) VALUES (?, ?, ?, ?, ?, ?)",
    );
    for (const e of exportsRows) insExport.run(e.export_key, e.source_ref, e.vendor_invoice_no, e.vendor_key, e.amount_cents, e.status_as_of_export);
  }

  db.prepare("INSERT INTO vdw_export_runs (run_id, exported_at, schema_version) VALUES (?, ?, ?)").run(
    "RUN-2026-004",
    EXPORT_RAN_AT,
    schemaVersion,
  );
}

function accountKey(idx: number): string {
  return `BA-${String(idx + 1).padStart(4, "0")}`;
}

function flagKey(idx: number): string {
  return `RF-${String(idx + 1).padStart(4, "0")}`;
}

function initCounters(db: Database): void {
  const ins = db.prepare("INSERT INTO counters (name, value) VALUES (?, ?)");
  ins.run("mutation_seq", 0);
  ins.run("payment", 0);
  ins.run("hold", FIX.HOLDS.length);
  ins.run("approval", FIX.APPROVAL_REQUESTS.length);
  ins.run("event", 0);
}

function initEnvironment(db: Database, profile: EnvironmentProfile): void {
  const ins = db.prepare("INSERT INTO environment (key, value) VALUES (?, ?)");
  ins.run("seed", String(SEED));
  ins.run("schema_version", String(profile.schemaVersion));
  ins.run("docs_outage", profile.docsOutage ? "1" : "0");
}
