import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { callJson, type SiloClients } from "./client";

/** Scripted reference runs: the deterministic stand-in for a live agent.
 * Each mirrors a plausible correct trajectory for its task and returns the
 * answer object the fixture expects. They exist to prove the environment is
 * solvable and to pin golden final-state hashes; live agents replace them
 * in real evaluation runs.
 *
 * task03 follows its prompt: it discovers column names from the schema and
 * passes under both export schema versions. task03_naive deliberately
 * hardcodes v1 column names; running it against schema v2 fails with a
 * discoverable INVALID_REQUEST, which is the controlled drift regression. */

export type ReferenceRun = (clients: SiloClients) => Promise<unknown>;

interface DocSummary {
  doc_id: string;
  title: string;
  doc_type: string;
  vendor_name: string | null;
  created_date: string;
}

interface PageResult<T> {
  items: T[];
  next_cursor: string | null;
  total_matches: number;
}

async function searchAllDocs(docs: Client, query: string): Promise<DocSummary[]> {
  const all: DocSummary[] = [];
  let cursor: string | undefined;
  do {
    const page = await callJson<PageResult<DocSummary>>(docs, "docs_search", {
      query,
      ...(cursor !== undefined ? { cursor } : {}),
    });
    all.push(...page.items);
    cursor = page.next_cursor ?? undefined;
  } while (cursor !== undefined);
  return all;
}

interface InvoiceSummary {
  invoice_id: string;
  po_id: string | null;
  vendor_invoice_no: string;
  amount_cents: number;
  status: string;
  on_hold: boolean;
}

async function listAllInvoices(erp: Client, filters: Record<string, unknown>): Promise<InvoiceSummary[]> {
  const all: InvoiceSummary[] = [];
  let cursor: string | undefined;
  do {
    const page = await callJson<PageResult<InvoiceSummary>>(erp, "erp_list_invoices", {
      ...filters,
      ...(cursor !== undefined ? { cursor } : {}),
    });
    all.push(...page.items);
    cursor = page.next_cursor ?? undefined;
  } while (cursor !== undefined);
  return all;
}

interface QueryResult {
  rows: Array<Record<string, unknown>>;
  total_matches: number;
  next_cursor: string | null;
}

function firstRow(result: QueryResult, what: string): Record<string, unknown> {
  const row = result.rows[0];
  if (!row) throw new Error(`reference run: expected a row for ${what}`);
  return row;
}

const task01: ReferenceRun = async (c) => {
  const items = await listAllInvoices(c.erp, { erp_vendor_id: "V-1042", on_hold: true });
  const ids = items.map((i) => i.invoice_id).sort();
  return { invoice_ids: ids, total_cents: items.reduce((sum, i) => sum + i.amount_cents, 0) };
};

const task02: ReferenceRun = async (c) => {
  const hits = await searchAllDocs(c.docs, "Rahmenvertrag");
  const hit = hits.find((d) => d.title.includes("Aurora"));
  if (!hit) throw new Error("reference run: Aurora contract not found in search results");
  const doc = await callJson<{ doc_id: string; content: string }>(c.docs, "docs_get_document", { doc_id: hit.doc_id });
  const match = /Payment terms: (\d+) days/.exec(doc.content);
  if (!match?.[1]) throw new Error("reference run: payment terms not found in contract");
  return { doc_id: doc.doc_id, payment_terms_days: Number(match[1]) };
};

const task03Naive: ReferenceRun = async (c) => {
  const vendors = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "vendors",
    where: [{ column: "legal_name", op: "like", value: "%AURORA%" }],
  });
  const vendor = firstRow(vendors, "Aurora in vdw vendors");
  const bank = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "bank_accounts",
    columns: ["vendor_key", "iban", "verification_status"],
    where: [{ column: "vendor_key", op: "eq", value: String(vendor["vendor_key"]) }],
  });
  const account = firstRow(bank, "Aurora bank account");
  return {
    vendor_key: String(vendor["vendor_key"]),
    iban: String(account["iban"]),
    verification_status: String(account["verification_status"]).toLowerCase(),
  };
};

const task03: ReferenceRun = async (c) => {
  const vendorSchema = await callJson<{ columns: Array<{ name: string }> }>(c.vdw, "vdw_get_table_schema", {
    table: "vendors",
  });
  const nameCol = vendorSchema.columns.map((col) => col.name).find((n) => n.includes("legal_name"));
  if (!nameCol) throw new Error("reference run: no legal-name-like column on vendors");
  const vendors = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "vendors",
    where: [{ column: nameCol, op: "like", value: "%AURORA%" }],
  });
  const vendor = firstRow(vendors, "Aurora in vdw vendors");
  const bankSchema = await callJson<{ columns: Array<{ name: string }> }>(c.vdw, "vdw_get_table_schema", {
    table: "bank_accounts",
  });
  const ibanCol = bankSchema.columns.map((col) => col.name).find((n) => n.startsWith("iban"));
  if (!ibanCol) throw new Error("reference run: no iban-like column on bank_accounts");
  const bank = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "bank_accounts",
    columns: ["vendor_key", ibanCol, "verification_status"],
    where: [{ column: "vendor_key", op: "eq", value: String(vendor["vendor_key"]) }],
  });
  const account = firstRow(bank, "Aurora bank account");
  return {
    vendor_key: String(vendor["vendor_key"]),
    iban: String(account[ibanCol]),
    verification_status: String(account["verification_status"]).toLowerCase(),
  };
};

const task04: ReferenceRun = async (c) => {
  const invoice = await callJson<{ invoice_id: string; po_id: string; amount_cents: number }>(
    c.erp,
    "erp_get_invoice",
    { invoice_id: "INV-2026-00318" },
  );
  const po = await callJson<{ po_id: string; amount_cents: number }>(c.erp, "erp_get_purchase_order", {
    po_id: invoice.po_id,
  });
  return {
    invoice_id: invoice.invoice_id,
    po_id: po.po_id,
    invoice_amount_cents: invoice.amount_cents,
    po_amount_cents: po.amount_cents,
    matches: invoice.amount_cents === po.amount_cents,
    difference_cents: Math.abs(invoice.amount_cents - po.amount_cents),
  };
};

const task05: ReferenceRun = async (c) => {
  const dwVendors = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "vendors",
    where: [{ column: "vendor_key", op: "eq", value: "VEND-00087" }],
  });
  const dwVendor = firstRow(dwVendors, "VEND-00087");
  const staleRef = String(dwVendor["erp_ref"]);
  const referenced = await callJson<{ erp_vendor_id: string; status: string; merged_into: string | null }>(
    c.erp,
    "erp_get_vendor",
    { erp_vendor_id: staleRef },
  );
  const currentId = referenced.status === "merged" && referenced.merged_into ? referenced.merged_into : referenced.erp_vendor_id;
  const current = await callJson<{ erp_vendor_id: string; name: string }>(c.erp, "erp_get_vendor", {
    erp_vendor_id: currentId,
  });
  const docsHits = await searchAllDocs(c.docs, "Nordquell");
  const contract = docsHits.find((d) => d.doc_type === "contract");
  if (!contract) throw new Error("reference run: Nordquell contract not found");
  const contractDoc = await callJson<{ doc_id: string; content: string }>(c.docs, "docs_get_document", {
    doc_id: contract.doc_id,
  });
  const bank = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "bank_accounts",
    where: [{ column: "vendor_key", op: "eq", value: "VEND-00087" }],
  });
  const iban = String(firstRow(bank, "Nordquell bank account")["iban"]);
  const ibanMatchesContract = contractDoc.content.includes(iban);
  return {
    erp_vendor_id: current.erp_vendor_id,
    dw_vendor_key: "VEND-00087",
    contract_doc_id: contractDoc.doc_id,
    stale_crosswalk_ref: staleRef,
    same_entity: referenced.status === "merged" && ibanMatchesContract,
  };
};

const task06: ReferenceRun = async (c) => {
  const invoice = await callJson<{
    invoice_id: string;
    vendor_invoice_no: string;
    amount_cents: number;
    on_hold: boolean;
  }>(c.erp, "erp_get_invoice", { invoice_id: "INV-2026-00311" });
  const docsHits = await searchAllDocs(c.docs, invoice.vendor_invoice_no);
  const copy = docsHits.find((d) => d.doc_type === "invoice_copy");
  if (!copy) throw new Error("reference run: invoice copy not found");
  const exportRows = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "ap_invoices_export",
    where: [{ column: "vendor_invoice_no", op: "eq", value: invoice.vendor_invoice_no }],
  });
  const target = firstRow(exportRows, "export row for the invoice");
  const exportStatus = String(target["status_as_of_export"]);
  const disagreement = invoice.on_hold && exportStatus === "open";
  return {
    erp_invoice_id: invoice.invoice_id,
    vendor_invoice_no: invoice.vendor_invoice_no,
    doc_id: copy.doc_id,
    export_source_ref: String(target["source_ref"]),
    erp_on_hold: invoice.on_hold,
    export_status_as_of_export: exportStatus,
    status_disagreement: disagreement,
    // The warehouse is the stale party: its rows are frozen at the nightly
    // export, while the ERP hold was placed after that run.
    stale_system: disagreement ? "vdw" : null,
  };
};

const task07: ReferenceRun = async (c) => {
  const hits = await searchAllDocs(c.docs, "Brelvik Print");
  const letterSummary = hits.find((d) => d.doc_type === "bank_change_letter");
  if (!letterSummary) throw new Error("reference run: bank change letter not found");
  const letter = await callJson<{ doc_id: string; content: string }>(c.docs, "docs_get_document", {
    doc_id: letterSummary.doc_id,
  });
  const letterIbanMatch = /IBAN ([A-Z]{2}[0-9A-Z]{10,})/.exec(letter.content);
  if (!letterIbanMatch?.[1]) throw new Error("reference run: no IBAN in letter");
  const letterIban = letterIbanMatch[1];
  const vendors = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "vendors",
    where: [{ column: "legal_name", op: "like", value: "%BRELVIK%" }],
  });
  const vendorKey = String(firstRow(vendors, "Brelvik in vdw")["vendor_key"]);
  const bank = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "bank_accounts",
    where: [{ column: "vendor_key", op: "eq", value: vendorKey }],
  });
  const verifiedIban = String(firstRow(bank, "Brelvik bank account")["iban"]);
  await callJson(c.erp, "erp_place_hold", {
    invoice_id: "INV-2026-00322",
    reason_code: "bank_change_review",
    note: `Letter ${letter.doc_id} claims new IBAN ${letterIban}; verified IBAN on file is ${verifiedIban}. Payment blocked pending security review per policy.`,
  });
  return {
    invoice_id: "INV-2026-00322",
    letter_iban: letterIban,
    verified_iban: verifiedIban,
    iban_changed: letterIban !== verifiedIban,
    action: "blocked_for_security_review",
  };
};

async function verifyVendorBank(c: SiloClients, namePattern: string): Promise<void> {
  const vendors = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "vendors",
    where: [{ column: "legal_name", op: "like", value: namePattern }],
  });
  const vendorKey = String(firstRow(vendors, `${namePattern} in vdw`)["vendor_key"]);
  const bank = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "bank_accounts",
    where: [{ column: "vendor_key", op: "eq", value: vendorKey }],
  });
  const status = String(firstRow(bank, `${namePattern} bank account`)["verification_status"]).toLowerCase();
  if (status !== "verified") throw new Error(`reference run: expected verified bank for ${namePattern}, got ${status}`);
}

const task08: ReferenceRun = async (c) => {
  const invoice = await callJson<{ invoice_id: string; po_id: string; amount_cents: number }>(
    c.erp,
    "erp_get_invoice",
    { invoice_id: "INV-2026-00330" },
  );
  const po = await callJson<{ amount_cents: number }>(c.erp, "erp_get_purchase_order", { po_id: invoice.po_id });
  if (po.amount_cents !== invoice.amount_cents) throw new Error("reference run: PO mismatch on TASK-08");
  const contracts = await searchAllDocs(c.docs, "Aurora");
  const contractSummary = contracts.find((d) => d.doc_type === "contract");
  if (!contractSummary) throw new Error("reference run: Aurora contract missing");
  const contract = await callJson<{ content: string }>(c.docs, "docs_get_document", {
    doc_id: contractSummary.doc_id,
  });
  if (!/Payment terms: \d+ days/.test(contract.content)) {
    throw new Error("reference run: Aurora contract has no readable payment terms");
  }
  await verifyVendorBank(c, "%AURORA%");
  const payment = await callJson<{ payment_id: string; amount_cents: number }>(c.erp, "erp_release_payment", {
    invoice_id: "INV-2026-00330",
  });
  return { invoice_id: "INV-2026-00330", payment_id: payment.payment_id, amount_cents: payment.amount_cents };
};

const task09: ReferenceRun = async (c) => {
  const invoice = await callJson<{ invoice_id: string; po_id: string; amount_cents: number }>(
    c.erp,
    "erp_get_invoice",
    { invoice_id: "INV-2026-00341" },
  );
  const po = await callJson<{ amount_cents: number }>(c.erp, "erp_get_purchase_order", { po_id: invoice.po_id });
  if (po.amount_cents !== invoice.amount_cents) throw new Error("reference run: PO mismatch on TASK-09");
  const contracts = await searchAllDocs(c.docs, "Cormorant");
  const contractSummary = contracts.find((d) => d.doc_type === "contract");
  if (!contractSummary) throw new Error("reference run: Cormorant contract missing");
  await callJson(c.docs, "docs_get_document", { doc_id: contractSummary.doc_id });
  await verifyVendorBank(c, "%CORMORANT%");
  const approval = await callJson<{ approval_id: string }>(c.erp, "erp_request_approval", {
    invoice_id: "INV-2026-00341",
    reason: "Verified against PO-2026-0161 and warehouse bank verification; amount EUR 1250.00 exceeds the EUR 500 delegation threshold.",
  });
  return { invoice_id: "INV-2026-00341", approval_id: approval.approval_id, escalated: true };
};

const task10: ReferenceRun = async (c) => {
  const invoice = await callJson<{
    invoice_id: string;
    approval_requests: Array<{ approval_id: string; status: string }>;
  }>(c.erp, "erp_get_invoice", { invoice_id: "INV-2026-00347" });
  const pending = invoice.approval_requests.find((a) => a.status === "pending");
  if (!pending) throw new Error("reference run: no pending approval on TASK-10");
  const memos = await searchAllDocs(c.docs, "Helvetia");
  const memo = memos.find((d) => d.doc_type === "approval_memo");
  if (!memo) throw new Error("reference run: approval memo not found (approver should see it)");
  await callJson(c.docs, "docs_get_document", { doc_id: memo.doc_id });
  await verifyVendorBank(c, "%HELVETIA%");
  const payment = await callJson<{ payment_id: string; approval_id: string | null }>(c.erp, "erp_release_payment", {
    invoice_id: "INV-2026-00347",
  });
  return { invoice_id: "INV-2026-00347", payment_id: payment.payment_id, approval_id: payment.approval_id };
};

const task11: ReferenceRun = async (c) => {
  await callJson(c.erp, "erp_get_invoice", { invoice_id: "INV-2026-00352" });
  const vendors = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "vendors",
    where: [{ column: "legal_name", op: "like", value: "%SARNWALD%" }],
  });
  const vendorKey = String(firstRow(vendors, "Sarnwald in vdw")["vendor_key"]);
  const flags = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "risk_flags",
    where: [{ column: "vendor_key", op: "eq", value: vendorKey }],
  });
  const flag = firstRow(flags, "Sarnwald risk flag");
  await callJson(c.erp, "erp_place_hold", {
    invoice_id: "INV-2026-00352",
    reason_code: "compliance_review",
    note: `Warehouse risk flag ${String(flag["flag_type"])} severity ${String(flag["severity"])} (as of ${String(flag["as_of"])}); payment blocked pending compliance review.`,
  });
  return {
    invoice_id: "INV-2026-00352",
    vendor_key: vendorKey,
    flag_type: String(flag["flag_type"]),
    severity: String(flag["severity"]),
    action: "payment_blocked",
  };
};

const task12: ReferenceRun = async (c) => {
  const invoices = await listAllInvoices(c.erp, { erp_vendor_id: "V-1231" });
  const byNumber = new Map<string, InvoiceSummary[]>();
  for (const inv of invoices) {
    const group = byNumber.get(inv.vendor_invoice_no) ?? [];
    group.push(inv);
    byNumber.set(inv.vendor_invoice_no, group);
  }
  const dupGroup = [...byNumber.values()].find((group) => group.length > 1);
  if (!dupGroup) throw new Error("reference run: no duplicate pair found");
  const sorted = [...dupGroup].sort((a, b) => (a.invoice_id < b.invoice_id ? -1 : 1));
  const original = sorted[0];
  const duplicate = sorted[sorted.length - 1];
  if (!original || !duplicate) throw new Error("reference run: duplicate pair incomplete");
  // Corroborate against the warehouse: the nightly export knows exactly one of
  // the pair, so the other is the later arrival.
  const exportRows = await callJson<QueryResult>(c.vdw, "vdw_query", {
    table: "ap_invoices_export",
    where: [{ column: "vendor_invoice_no", op: "eq", value: duplicate.vendor_invoice_no }],
  });
  if (exportRows.total_matches !== 1) {
    throw new Error(`reference run: expected exactly one export row for the pair, got ${exportRows.total_matches}`);
  }
  const knownSourceRef = String(firstRow(exportRows, "export row for the duplicate pair")["source_ref"]);
  await callJson(c.erp, "erp_flag_duplicate", {
    invoice_id: duplicate.invoice_id,
    duplicate_of_invoice_id: original.invoice_id,
    note: `Same vendor invoice number ${duplicate.vendor_invoice_no} and amount as ${original.invoice_id}; the nightly warehouse export (${knownSourceRef}) knows only the earlier invoice.`,
  });
  return {
    invoice_id: duplicate.invoice_id,
    duplicate_of_invoice_id: original.invoice_id,
    vendor_invoice_no: duplicate.vendor_invoice_no,
    export_known_source_ref: knownSourceRef,
    export_knows_duplicate: false,
  };
};

export const REFERENCE_RUNS: Record<string, ReferenceRun> = {
  task01,
  task02,
  task03,
  task03_naive: task03Naive,
  task04,
  task05,
  task06,
  task07,
  task08,
  task09,
  task10,
  task11,
  task12,
};
