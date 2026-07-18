import { intBetween, mulberry32, pickFrom } from "../rng";
import { SEED } from "../clock";
import type {
  DocumentRow,
  DwBankAccountSeed,
  DwInvoiceExportSeed,
  DwVendorSeed,
  ErpVendorRow,
  InvoiceRow,
  PurchaseOrderRow,
} from "../types";

/** Background population so lists paginate and searches are not trivially
 * small. Generated once per seed from the fixed PRNG; the generation order is
 * part of the golden state, never reorder it. */

const FIRST = ["Bel", "Cor", "Dan", "Fen", "Gral", "Hol", "Jur", "Kel", "Lin", "Mar", "Ost", "Pel", "Quin", "Ros", "Sten", "Tur", "Ulm", "Ver", "Wes", "Zel"];
const SECOND = ["mont", "berg", "feld", "hafen", "stein", "wald", "brook", "dal", "gard", "mark"];
const TRADE = ["Logistik", "Metall", "Print", "Office", "Chemie", "Paper", "Systems", "Handel"];
const LEGAL = ["GmbH", "AG", "BV", "SA", "OU"];
const TERMS = [14, 30, 45, 60];
const RECEIVED_DATES = ["2025-12-18", "2025-12-20", "2025-12-22", "2025-12-27", "2026-01-02", "2026-01-03"];

export interface FillerData {
  erpVendors: ErpVendorRow[];
  purchaseOrders: PurchaseOrderRow[];
  invoices: InvoiceRow[];
  dwVendors: DwVendorSeed[];
  dwBankAccounts: DwBankAccountSeed[];
  dwInvoiceExports: DwInvoiceExportSeed[];
  documents: DocumentRow[];
}

export function buildFiller(): FillerData {
  const rng = mulberry32(SEED);
  const erpVendors: ErpVendorRow[] = [];
  const purchaseOrders: PurchaseOrderRow[] = [];
  const invoices: InvoiceRow[] = [];
  const dwVendors: DwVendorSeed[] = [];
  const dwBankAccounts: DwBankAccountSeed[] = [];
  const dwInvoiceExports: DwInvoiceExportSeed[] = [];
  const documents: DocumentRow[] = [];

  const vendorNames: string[] = [];
  for (let i = 0; i < 16; i++) {
    const name = `${pickFrom(rng, FIRST)}${pickFrom(rng, SECOND)} ${pickFrom(rng, TRADE)} ${pickFrom(rng, LEGAL)}`;
    vendorNames.push(name);
    const erpVendorId = `V-${2001 + i}`;
    erpVendors.push({
      erp_vendor_id: erpVendorId,
      name,
      status: "active",
      merged_into: null,
      payment_terms_days: pickFrom(rng, TERMS),
    });
    if (i < 10) {
      const vendorKey = `VEND-${String(201 + i).padStart(5, "0")}`;
      dwVendors.push({
        vendor_key: vendorKey,
        legal_name: name.toUpperCase(),
        country: pickFrom(rng, ["DE", "AT", "NL", "FR", "EE"]),
        erp_ref: erpVendorId,
        status: "active",
      });
      let digits = "";
      for (let d = 0; d < 18; d++) digits += String(intBetween(rng, 0, 9));
      dwBankAccounts.push({
        vendor_key: vendorKey,
        iban: `DE${digits}`,
        bic: "GENODEF1XXX",
        verification_status: i % 5 === 4 ? "pending" : "verified",
        verified_at: i % 5 === 4 ? null : "2025-11-05",
      });
    }
  }

  for (let i = 0; i < 24; i++) {
    const vendorIdx = i % 16;
    const serial = 1001 + i;
    const invoiceId = `INV-2026-0${serial}`;
    const amount = intBetween(rng, 8000, 950000);
    const withPo = i % 2 === 0;
    const poId = withPo ? `PO-2026-0${501 + i}` : null;
    if (withPo && poId) {
      purchaseOrders.push({
        po_id: poId,
        erp_vendor_id: `V-${2001 + vendorIdx}`,
        amount_cents: amount,
        status: "open",
      });
    }
    const received = pickFrom(rng, RECEIVED_DATES);
    invoices.push({
      invoice_id: invoiceId,
      erp_vendor_id: `V-${2001 + vendorIdx}`,
      po_id: poId,
      vendor_invoice_no: `FL-${4200 + i}`,
      amount_cents: amount,
      currency: "EUR",
      status: i % 3 === 0 ? "received" : "matched",
      received_date: received,
      due_date: "2026-02-10",
    });
    if (vendorIdx < 10 && received < "2026-01-04") {
      dwInvoiceExports.push({
        export_key: `EXP-0${serial}`,
        source_ref: `ERP1:${String(serial * 100).padStart(10, "0")}`,
        vendor_invoice_no: `FL-${4200 + i}`,
        vendor_key: `VEND-${String(201 + vendorIdx).padStart(5, "0")}`,
        amount_cents: amount,
        status_as_of_export: "open",
      });
    }
  }

  for (let i = 0; i < 8; i++) {
    const vendorName = vendorNames[i];
    if (vendorName === undefined) throw new Error("filler: vendor name missing");
    documents.push({
      doc_id: `DOC-0${101 + i}`,
      folder: "/contracts/2026",
      title: `Rahmenvertrag ${vendorName} 2026`,
      doc_type: "contract",
      vendor_name: vendorName,
      content: [
        `# Rahmenvertrag ${vendorName} 2026`,
        "",
        `- Payment terms: ${pickFrom(rng, TERMS)} days net.`,
        "- Standard purchasing conditions of Nordlicht GmbH apply.",
      ].join("\n"),
      acl_roles: JSON.stringify(["ap_clerk", "ap_approver"]),
      version: 1,
      created_date: `2025-12-${16 + i}`,
    });
  }

  return { erpVendors, purchaseOrders, invoices, dwVendors, dwBankAccounts, dwInvoiceExports, documents };
}
