/** Shared domain types for the SiloBench synthetic enterprise. */

export type Principal = "ap_clerk" | "ap_approver";
export type Actor = Principal | "system";

export type SchemaVersion = 1 | 2;

export interface EnvironmentProfile {
  schemaVersion: SchemaVersion;
  docsOutage: boolean;
}

export type SystemId = "erp" | "docs" | "vdw";

export type InvoiceStatus =
  | "received"
  | "matched"
  | "approved_for_payment"
  | "paid"
  | "rejected";

export type HoldReason =
  | "bank_change_review"
  | "duplicate_suspect"
  | "data_mismatch"
  | "compliance_review";

export type EventType =
  | "HOLD_PLACED"
  | "HOLD_RELEASED"
  | "APPROVAL_REQUESTED"
  | "PAYMENT_RELEASED"
  | "DUPLICATE_FLAGGED"
  | "ACCESS_DENIED";

export interface DomainEvent {
  event_id: string;
  mutation_seq: number;
  ts: string;
  actor: Actor;
  system: SystemId;
  type: EventType;
  entity_type: string;
  entity_id: string;
  payload: Record<string, unknown>;
}

export interface ErpVendorRow {
  erp_vendor_id: string;
  name: string;
  status: "active" | "merged";
  merged_into: string | null;
  payment_terms_days: number;
}

export interface PurchaseOrderRow {
  po_id: string;
  erp_vendor_id: string;
  amount_cents: number;
  status: "open" | "closed";
}

export interface InvoiceRow {
  invoice_id: string;
  erp_vendor_id: string;
  po_id: string | null;
  vendor_invoice_no: string;
  amount_cents: number;
  currency: "EUR";
  status: InvoiceStatus;
  received_date: string;
  due_date: string;
}

export interface HoldRow {
  hold_id: string;
  invoice_id: string;
  reason_code: HoldReason;
  note: string;
  active: 0 | 1;
  placed_by: Actor;
  placed_ts: string;
  released_by: Actor | null;
  released_ts: string | null;
}

export interface ApprovalRequestRow {
  approval_id: string;
  invoice_id: string;
  requested_by: Actor;
  reason: string;
  status: "pending" | "completed";
  requested_ts: string;
  completed_ts: string | null;
}

export interface PaymentRow {
  payment_id: string;
  invoice_id: string;
  amount_cents: number;
  released_by: Principal;
  approval_id: string | null;
  released_ts: string;
}

export type DocType =
  | "contract"
  | "approval_memo"
  | "bank_change_letter"
  | "invoice_copy"
  | "policy";

export interface DocumentRow {
  doc_id: string;
  folder: string;
  title: string;
  doc_type: DocType;
  vendor_name: string | null;
  content: string;
  acl_roles: string; // JSON array of Principal
  version: number;
  created_date: string;
}

/** Warehouse rows are stored with version-specific physical columns; these are
 * the version-independent logical shapes used by the seed data. */
export interface DwVendorSeed {
  vendor_key: string;
  legal_name: string;
  country: string;
  erp_ref: string | null;
  status: "active" | "inactive";
}

export interface DwBankAccountSeed {
  vendor_key: string;
  iban: string;
  bic: string;
  verification_status: "verified" | "pending" | "failed";
  verified_at: string | null;
}

export interface DwRiskFlagSeed {
  vendor_key: string;
  flag_type: "sanctions_screening" | "insolvency_watch";
  severity: "low" | "medium" | "high";
  as_of: string;
}

export interface DwInvoiceExportSeed {
  export_key: string;
  source_ref: string;
  vendor_invoice_no: string;
  vendor_key: string;
  amount_cents: number;
  status_as_of_export: "open" | "paid";
}
