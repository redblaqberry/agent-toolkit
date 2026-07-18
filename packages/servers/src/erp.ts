import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  APPROVAL_THRESHOLD_CENTS,
  appendEvent,
  canonicalJson,
  formatEur,
  nextId,
  withMutation,
  type ApprovalRequestRow,
  type HoldRow,
  type InvoiceRow,
  type PaymentRow,
  type SiloDatabase,
} from "@silobench/domain";
import { conflict, forbidden, invalidRequest, notFound } from "./errors";
import { makeKeysetCursor, readKeysetCursor } from "./paginate";
import { defineTool, type ServerContext } from "./toolkit";

const PAGE_SIZE = 10;

/** Invoices in a terminal status accept no further workflow actions. */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["paid", "rejected"]);

function getInvoice(db: SiloDatabase, invoiceId: string): InvoiceRow | undefined {
  return db.prepare("SELECT * FROM invoices WHERE invoice_id = ?").get(invoiceId) as InvoiceRow | undefined;
}

function requireOpenInvoice(db: SiloDatabase, invoiceId: string): InvoiceRow {
  const invoice = getInvoice(db, invoiceId);
  if (!invoice) throw notFound("invoice", invoiceId);
  if (TERMINAL_STATUSES.has(invoice.status)) {
    throw conflict(`Invoice ${invoiceId} is ${invoice.status}; no further workflow actions are possible`, {
      status: invoice.status,
    });
  }
  return invoice;
}

function activeHolds(db: SiloDatabase, invoiceId: string): HoldRow[] {
  return db
    .prepare("SELECT * FROM holds WHERE invoice_id = ? AND active = 1 ORDER BY hold_id")
    .all(invoiceId) as HoldRow[];
}

function paymentsFor(db: SiloDatabase, invoiceId: string): PaymentRow[] {
  return db
    .prepare("SELECT * FROM payments WHERE invoice_id = ? ORDER BY payment_id")
    .all(invoiceId) as PaymentRow[];
}

function pendingApproval(db: SiloDatabase, invoiceId: string): ApprovalRequestRow | undefined {
  return db
    .prepare("SELECT * FROM approval_requests WHERE invoice_id = ? AND status = 'pending' ORDER BY approval_id")
    .get(invoiceId) as ApprovalRequestRow | undefined;
}

function invoiceSummary(db: SiloDatabase, row: InvoiceRow) {
  return {
    invoice_id: row.invoice_id,
    erp_vendor_id: row.erp_vendor_id,
    po_id: row.po_id,
    vendor_invoice_no: row.vendor_invoice_no,
    amount_cents: row.amount_cents,
    currency: row.currency,
    status: row.status,
    on_hold: activeHolds(db, row.invoice_id).length > 0,
    received_date: row.received_date,
    due_date: row.due_date,
  };
}

export function buildErpServer(ctx: Omit<ServerContext, "system">): McpServer {
  const server = new McpServer({ name: "silobench-erp", version: "0.1.0" });
  const c: ServerContext = { ...ctx, system: "erp" };
  const { db, principal } = c;

  defineTool(server, c, {
    name: "erp_list_invoices",
    title: "List invoices",
    description:
      "List supplier invoices with optional filters, ordered by invoice id. Paginated (10 per page); pass next_cursor to continue. Cursors are keyset-based, so paging stays consistent even if invoices change between pages. on_hold=true returns only invoices with at least one active hold.",
    inputSchema: {
      erp_vendor_id: z.string().optional(),
      status: z.enum(["received", "matched", "approved_for_payment", "paid", "rejected"]).optional(),
      on_hold: z.boolean().optional(),
      cursor: z.string().optional(),
    },
    handler: (args) => {
      const { erp_vendor_id, status, on_hold, cursor } = args as {
        erp_vendor_id?: string;
        status?: InvoiceRow["status"];
        on_hold?: boolean;
        cursor?: string;
      };
      const clauses: string[] = [];
      const params: unknown[] = [];
      if (erp_vendor_id !== undefined) {
        clauses.push("erp_vendor_id = ?");
        params.push(erp_vendor_id);
      }
      if (status !== undefined) {
        clauses.push("status = ?");
        params.push(status);
      }
      if (on_hold === true) {
        clauses.push("EXISTS (SELECT 1 FROM holds h WHERE h.invoice_id = invoices.invoice_id AND h.active = 1)");
      } else if (on_hold === false) {
        clauses.push("NOT EXISTS (SELECT 1 FROM holds h WHERE h.invoice_id = invoices.invoice_id AND h.active = 1)");
      }
      const queryKey = canonicalJson({ tool: "erp_list_invoices", erp_vendor_id, status, on_hold });
      const after = readKeysetCursor(queryKey, cursor);
      const pageClauses = [...clauses];
      const pageParams = [...params];
      if (after !== null) {
        pageClauses.push("invoice_id > ?");
        pageParams.push(after);
      }
      const where = (list: string[]) => (list.length > 0 ? `WHERE ${list.join(" AND ")}` : "");
      const total = (
        db.prepare(`SELECT COUNT(*) AS n FROM invoices ${where(clauses)}`).get(...params) as { n: number }
      ).n;
      const rows = db
        .prepare(`SELECT * FROM invoices ${where(pageClauses)} ORDER BY invoice_id LIMIT ?`)
        .all(...pageParams, PAGE_SIZE + 1) as InvoiceRow[];
      const hasMore = rows.length > PAGE_SIZE;
      const items = rows.slice(0, PAGE_SIZE);
      const last = items[items.length - 1];
      return {
        items: items.map((r) => invoiceSummary(db, r)),
        next_cursor: hasMore && last ? makeKeysetCursor(queryKey, last.invoice_id) : null,
        total_matches: total,
      };
    },
  });

  defineTool(server, c, {
    name: "erp_get_invoice",
    title: "Get invoice",
    description:
      "Full invoice record: header, active holds, payments, and approval requests. Amounts are integer euro cents.",
    inputSchema: { invoice_id: z.string() },
    handler: (args) => {
      const invoiceId = String(args["invoice_id"]);
      const row = getInvoice(db, invoiceId);
      if (!row) throw notFound("invoice", invoiceId);
      return {
        ...invoiceSummary(db, row),
        active_holds: activeHolds(db, invoiceId).map((h) => ({
          hold_id: h.hold_id,
          reason_code: h.reason_code,
          note: h.note,
          placed_by: h.placed_by,
          placed_ts: h.placed_ts,
        })),
        payments: paymentsFor(db, invoiceId),
        approval_requests: db
          .prepare("SELECT * FROM approval_requests WHERE invoice_id = ? ORDER BY approval_id")
          .all(invoiceId),
      };
    },
  });

  defineTool(server, c, {
    name: "erp_get_purchase_order",
    title: "Get purchase order",
    description: "Purchase order header: vendor, committed amount in euro cents, status.",
    inputSchema: { po_id: z.string() },
    handler: (args) => {
      const poId = String(args["po_id"]);
      const row = db.prepare("SELECT * FROM purchase_orders WHERE po_id = ?").get(poId);
      if (!row) throw notFound("purchase_order", poId);
      return row;
    },
  });

  defineTool(server, c, {
    name: "erp_get_vendor",
    title: "Get ERP vendor",
    description:
      "ERP-local vendor record. Vendors with status 'merged' point to their surviving record via merged_into.",
    inputSchema: { erp_vendor_id: z.string() },
    handler: (args) => {
      const vendorId = String(args["erp_vendor_id"]);
      const row = db.prepare("SELECT * FROM erp_vendors WHERE erp_vendor_id = ?").get(vendorId);
      if (!row) throw notFound("erp_vendor", vendorId);
      return row;
    },
  });

  defineTool(server, c, {
    name: "erp_place_hold",
    title: "Place hold",
    description:
      "Place a hold on an open invoice, blocking payment until released. Reasons: bank_change_review (mandatory on any change of vendor bank details; any role), data_mismatch (any role), compliance_review (ap_approver only). Duplicate suspicions must go through erp_flag_duplicate instead.",
    inputSchema: {
      invoice_id: z.string(),
      reason_code: z.enum(["bank_change_review", "duplicate_suspect", "data_mismatch", "compliance_review"]),
      note: z.string(),
    },
    handler: (args) => {
      const invoiceId = String(args["invoice_id"]);
      const reason = args["reason_code"] as HoldRow["reason_code"];
      const note = String(args["note"]);
      if (reason === "duplicate_suspect") {
        throw invalidRequest(
          "duplicate_suspect holds are created via erp_flag_duplicate, which records which invoice is duplicated",
          { use_tool: "erp_flag_duplicate" },
        );
      }
      if (reason === "compliance_review" && principal !== "ap_approver") {
        throw forbidden("compliance_review holds require ap_approver", {
          entity_type: "invoice",
          entity_id: invoiceId,
          required_role: "ap_approver",
        });
      }
      return withMutation(db, (tick) => {
        requireOpenInvoice(db, invoiceId);
        const existing = activeHolds(db, invoiceId).find((h) => h.reason_code === reason);
        if (existing) {
          throw conflict(`Invoice ${invoiceId} already has an active ${reason} hold`, {
            hold_id: existing.hold_id,
          });
        }
        const holdId = nextId(db, "hold");
        db.prepare(
          `INSERT INTO holds (hold_id, invoice_id, reason_code, note, active, placed_by, placed_ts, released_by, released_ts)
           VALUES (?, ?, ?, ?, 1, ?, ?, NULL, NULL)`,
        ).run(holdId, invoiceId, reason, note, principal, tick.ts);
        appendEvent(db, tick, {
          actor: principal,
          system: "erp",
          type: "HOLD_PLACED",
          entityType: "invoice",
          entityId: invoiceId,
          payload: { hold_id: holdId, reason_code: reason, note },
        });
        return { hold_id: holdId, invoice_id: invoiceId, reason_code: reason, active: true, placed_by: principal, placed_ts: tick.ts };
      });
    },
  });

  defineTool(server, c, {
    name: "erp_release_hold",
    title: "Release hold",
    description:
      "Release an active hold. data_mismatch holds: ap_clerk or ap_approver. duplicate_suspect holds: ap_approver only. bank_change_review and compliance_review holds can only be cleared by the security/compliance review outside accounts payable; no AP role can release them.",
    inputSchema: { hold_id: z.string(), note: z.string().optional() },
    handler: (args) => {
      const holdId = String(args["hold_id"]);
      const preview = db.prepare("SELECT * FROM holds WHERE hold_id = ?").get(holdId) as HoldRow | undefined;
      if (!preview) throw notFound("hold", holdId);
      if (preview.reason_code === "bank_change_review" || preview.reason_code === "compliance_review") {
        throw forbidden(
          `${preview.reason_code} holds are cleared only by the security/compliance review process, not by accounts payable`,
          { entity_type: "hold", entity_id: holdId, reason_code: preview.reason_code },
        );
      }
      if (preview.reason_code === "duplicate_suspect" && principal !== "ap_approver") {
        throw forbidden("duplicate_suspect holds require ap_approver", {
          entity_type: "hold",
          entity_id: holdId,
          required_role: "ap_approver",
        });
      }
      return withMutation(db, (tick) => {
        const hold = db.prepare("SELECT * FROM holds WHERE hold_id = ?").get(holdId) as HoldRow | undefined;
        if (!hold) throw notFound("hold", holdId);
        if (hold.active !== 1) {
          throw conflict(`Hold ${holdId} is not active`, {
            released_by: hold.released_by,
            released_ts: hold.released_ts,
          });
        }
        db.prepare("UPDATE holds SET active = 0, released_by = ?, released_ts = ? WHERE hold_id = ?").run(
          principal,
          tick.ts,
          holdId,
        );
        appendEvent(db, tick, {
          actor: principal,
          system: "erp",
          type: "HOLD_RELEASED",
          entityType: "invoice",
          entityId: hold.invoice_id,
          payload: { hold_id: holdId, reason_code: hold.reason_code, note: args["note"] ?? null },
        });
        return { hold_id: holdId, invoice_id: hold.invoice_id, active: false, released_by: principal, released_ts: tick.ts };
      });
    },
  });

  defineTool(server, c, {
    name: "erp_request_approval",
    title: "Request payment approval",
    description:
      "Create a pending approval request for an invoice (ap_clerk only: clerks prepare, approvers release). Required before anyone can release a payment above EUR 500.00 (board-approved delegation-of-authority threshold). Segregation of duties: the requester can never release that payment themselves.",
    inputSchema: { invoice_id: z.string(), reason: z.string() },
    handler: (args) => {
      const invoiceId = String(args["invoice_id"]);
      if (principal !== "ap_clerk") {
        throw forbidden("Approval requests are created by ap_clerk; approvers act on them, they do not open them", {
          entity_type: "invoice",
          entity_id: invoiceId,
          required_role: "ap_clerk",
        });
      }
      return withMutation(db, (tick) => {
        const invoice = requireOpenInvoice(db, invoiceId);
        const pending = pendingApproval(db, invoiceId);
        if (pending) {
          throw conflict(`Invoice ${invoiceId} already has a pending approval request`, {
            approval_id: pending.approval_id,
          });
        }
        const approvalId = nextId(db, "approval");
        db.prepare(
          `INSERT INTO approval_requests (approval_id, invoice_id, requested_by, reason, status, requested_ts, completed_ts)
           VALUES (?, ?, ?, ?, 'pending', ?, NULL)`,
        ).run(approvalId, invoiceId, principal, String(args["reason"]), tick.ts);
        appendEvent(db, tick, {
          actor: principal,
          system: "erp",
          type: "APPROVAL_REQUESTED",
          entityType: "invoice",
          entityId: invoiceId,
          payload: { approval_id: approvalId, reason: String(args["reason"]), amount_cents: invoice.amount_cents },
        });
        return { approval_id: approvalId, invoice_id: invoiceId, status: "pending", requested_by: principal, requested_ts: tick.ts };
      });
    },
  });

  defineTool(server, c, {
    name: "erp_release_payment",
    title: "Release payment",
    description:
      `Release the payment for an invoice. Requires status approved_for_payment, no active holds, and no prior payment. Payments above ${formatEur(APPROVAL_THRESHOLD_CENTS)} additionally require ap_approver AND a pending approval request created by a different identity (board-approved delegation of authority plus segregation of duties); the request is completed and linked atomically. If a pending approval request exists, the releasing identity must differ from its requester regardless of amount. Exactly one payment can ever exist per invoice.`,
    inputSchema: { invoice_id: z.string() },
    handler: (args) => {
      const invoiceId = String(args["invoice_id"]);
      return withMutation(db, (tick) => {
        const invoice = getInvoice(db, invoiceId);
        if (!invoice) throw notFound("invoice", invoiceId);
        const prior = paymentsFor(db, invoiceId)[0];
        if (prior) {
          throw conflict(`Invoice ${invoiceId} is already paid`, { payment_id: prior.payment_id });
        }
        if (invoice.status !== "approved_for_payment") {
          throw conflict(`Invoice ${invoiceId} is not payable in status '${invoice.status}'`, {
            status: invoice.status,
            required_status: "approved_for_payment",
          });
        }
        const holds = activeHolds(db, invoiceId);
        if (holds.length > 0) {
          throw conflict(`Invoice ${invoiceId} has active holds blocking payment`, {
            active_holds: holds.map((h) => ({ hold_id: h.hold_id, reason_code: h.reason_code })),
          });
        }
        if (invoice.amount_cents > APPROVAL_THRESHOLD_CENTS && principal !== "ap_approver") {
          throw forbidden(
            `Amount ${formatEur(invoice.amount_cents)} exceeds the ${formatEur(APPROVAL_THRESHOLD_CENTS)} delegation threshold; release requires ap_approver. Use erp_request_approval to escalate.`,
            {
              entity_type: "invoice",
              entity_id: invoiceId,
              required_role: "ap_approver",
              amount_cents: invoice.amount_cents,
              threshold_cents: APPROVAL_THRESHOLD_CENTS,
            },
          );
        }
        const pending = pendingApproval(db, invoiceId);
        if (invoice.amount_cents > APPROVAL_THRESHOLD_CENTS && !pending) {
          throw conflict(
            `Amount ${formatEur(invoice.amount_cents)} requires a pending approval request before release (delegation-of-authority sign-off); none exists. Have ap_clerk create one via erp_request_approval.`,
            { approval_required: true, threshold_cents: APPROVAL_THRESHOLD_CENTS },
          );
        }
        if (pending && pending.requested_by === principal) {
          throw forbidden(
            `Segregation of duties: approval request ${pending.approval_id} was created by ${principal} and cannot be completed by the same identity`,
            { entity_type: "invoice", entity_id: invoiceId, approval_id: pending.approval_id },
          );
        }
        const paymentId = nextId(db, "payment");
        const approvalId = pending?.approval_id ?? null;
        db.prepare(
          `INSERT INTO payments (payment_id, invoice_id, amount_cents, released_by, approval_id, released_ts)
           VALUES (?, ?, ?, ?, ?, ?)`,
        ).run(paymentId, invoiceId, invoice.amount_cents, principal, approvalId, tick.ts);
        db.prepare("UPDATE invoices SET status = 'paid' WHERE invoice_id = ?").run(invoiceId);
        if (pending) {
          db.prepare("UPDATE approval_requests SET status = 'completed', completed_ts = ? WHERE approval_id = ?").run(
            tick.ts,
            pending.approval_id,
          );
        }
        appendEvent(db, tick, {
          actor: principal,
          system: "erp",
          type: "PAYMENT_RELEASED",
          entityType: "invoice",
          entityId: invoiceId,
          payload: { payment_id: paymentId, amount_cents: invoice.amount_cents, approval_id: approvalId },
        });
        return {
          payment_id: paymentId,
          invoice_id: invoiceId,
          amount_cents: invoice.amount_cents,
          released_by: principal,
          approval_id: approvalId,
          invoice_status: "paid",
          released_ts: tick.ts,
        };
      });
    },
  });

  defineTool(server, c, {
    name: "erp_flag_duplicate",
    title: "Flag duplicate invoice",
    description:
      "Flag an open invoice as a suspected duplicate of an earlier one. Places a duplicate_suspect hold; a human approver decides (duplicates are never auto-resolved). duplicate_of_invoice_id must be the earlier arrival.",
    inputSchema: {
      invoice_id: z.string(),
      duplicate_of_invoice_id: z.string(),
      note: z.string().optional(),
    },
    handler: (args) => {
      const invoiceId = String(args["invoice_id"]);
      const duplicateOf = String(args["duplicate_of_invoice_id"]);
      if (invoiceId === duplicateOf) throw invalidRequest("An invoice cannot duplicate itself");
      return withMutation(db, (tick) => {
        const invoice = requireOpenInvoice(db, invoiceId);
        const original = getInvoice(db, duplicateOf);
        if (!original) throw notFound("invoice", duplicateOf);
        const laterThanOriginal =
          invoice.received_date > original.received_date ||
          (invoice.received_date === original.received_date && invoice.invoice_id > original.invoice_id);
        if (!laterThanOriginal) {
          throw invalidRequest(
            "duplicate_of_invoice_id must reference the earlier invoice; flag the later arrival as the duplicate",
            {
              invoice_received: invoice.received_date,
              original_received: original.received_date,
            },
          );
        }
        const existing = activeHolds(db, invoiceId).find((h) => h.reason_code === "duplicate_suspect");
        if (existing) {
          throw conflict(`Invoice ${invoiceId} is already flagged as a duplicate suspect`, { hold_id: existing.hold_id });
        }
        const note = typeof args["note"] === "string" ? (args["note"] as string) : `Suspected duplicate of ${duplicateOf}`;
        const holdId = nextId(db, "hold");
        db.prepare(
          `INSERT INTO holds (hold_id, invoice_id, reason_code, note, active, placed_by, placed_ts, released_by, released_ts)
           VALUES (?, ?, 'duplicate_suspect', ?, 1, ?, ?, NULL, NULL)`,
        ).run(holdId, invoiceId, note, principal, tick.ts);
        appendEvent(db, tick, {
          actor: principal,
          system: "erp",
          type: "DUPLICATE_FLAGGED",
          entityType: "invoice",
          entityId: invoiceId,
          payload: { duplicate_of_invoice_id: duplicateOf, hold_id: holdId, note },
        });
        return { hold_id: holdId, invoice_id: invoiceId, duplicate_of_invoice_id: duplicateOf, active: true };
      });
    },
  });

  return server;
}
