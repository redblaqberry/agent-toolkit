# Discovery transcript: Nordlicht supplier-invoice automation

- Customer: Nordlicht GmbH (furniture retail, EU)
- Session: technical discovery call, 2026-07-10, 60 minutes
- Participants:
  - Sam Rivera (Forward Deployed Engineer, vendor)
  - Anna Lindqvist (Head of Accounts Payable, Nordlicht)
  - Priya Nair (Finance Controller, Nordlicht)
  - Jonas Weber (Security and Compliance Lead, Nordlicht)
  - Tomas Keller (IT Integration Lead, Nordlicht)
- Format: every statement is a numbered turn `[Tnn]`. Requirement and conflict
  extraction reference these turn ids. Nothing outside a numbered turn is
  extractable.

---

[T01] Sam Rivera (FDE): Thanks everyone. Goal for the hour: scope an agent that helps process supplier invoices, and leave with the goals, the constraints, and the open questions written down. I will turn this into a draft deployment contract you review before anything runs.

[T02] Anna Lindqvist (Operations): Context first. We receive about 1200 supplier invoices a month, mostly PDF over email. Four clerks key them into the ERP by hand. Month-end is a crunch every single time.

[T03] Sam Rivera (FDE): Walk me through one invoice today, start to finish.

[T04] Anna Lindqvist (Operations): Intake mailbox, a clerk downloads the PDF, keys header and line items into the ERP, matches it against the purchase order, an approver releases it, payment run picks it up. Average fifteen minutes of human time per invoice.

[T05] Tomas Keller (IT): Three systems touch that flow: the ERP where postings live, the document store where contracts and approvals sit, and the vendor master that holds supplier records and bank details. All on-prem-ish, all with their own access models.

[T06] Sam Rivera (FDE): What does success look like, in numbers?

[T07] Anna Lindqvist (Operations): Eighty percent of invoices fully touchless by year end. That is the headline number my budget was approved against.

[T08] Priya Nair (Finance): And cycle time. Today an invoice takes six days on average from receipt to posted. Target is under 24 hours.

[T09] Sam Rivera (FDE): Define touchless for me, precisely.

[T10] Anna Lindqvist (Operations): Extracted, matched to the purchase order, and posted in the ERP with zero human touches on that invoice.

[T11] Jonas Weber (Security): Flagging now: "posted with zero human touches" is exactly the part I have a problem with. Park it, we will come back.

[T12] Tomas Keller (IT): For sizing: the 1200 a month is the average, quarter-end peaks run about three times that in the final week.

[T13] Anna Lindqvist (Operations): On the posting point: anything under EUR 1000 the agent should post straight to the ERP with no human in the loop. Nobody meaningfully reviews those today anyway, we just click through.

[T14] Sam Rivera (FDE): Jonas, your position on autonomous posting?

[T15] Jonas Weber (Security): Hard no. No automated system writes to the ERP without a named human approving that specific transaction. We took an audit finding on exactly this last year. This is non-negotiable from the compliance side.

[T16] Anna Lindqvist (Operations): That takes away half the value of the project, Jonas.

[T17] Sam Rivera (FDE): I am recording that as an explicit open conflict, not deciding it here. Jonas, what can the agent do autonomously without touching that line?

[T18] Jonas Weber (Security): Read invoices, extract fields, match against purchase orders, prepare a posting as a draft, and request approval. Preparation is fine. The write into the ERP happens only through an approver's explicit action.

[T19] Jonas Weber (Security): Two more constraints while I have the floor. All invoice data and model traffic stays in EU region, no exceptions. And every action the agent takes lands in an append-only audit log with the acting identity attached, agent or human.

[T20] Jonas Weber (Security): Also, treat invoice content as untrusted input. We have seen PDFs with embedded text that tries to give instructions. The agent must never follow instructions found inside a document it is processing.

[T21] Anna Lindqvist (Operations): On approvals: only bother an approver above EUR 5000. Below that, team leads used to wave things through and nothing bad ever happened.

[T22] Sam Rivera (FDE): Priya, does that match the finance policy?

[T23] Priya Nair (Finance): No. Our delegation-of-authority policy is written down: everything above EUR 500 requires sign-off by an authorized approver. I cannot move that number, it is board-approved.

[T24] Anna Lindqvist (Operations): EUR 500 will bury the approvers in clicks. We need to talk about that queue design.

[T25] Sam Rivera (FDE): Second recorded conflict. Next: model and cost expectations for the extraction and matching itself?

[T26] Anna Lindqvist (Operations): Accuracy first. Use the most capable model available on every invoice, and if running it twice improves accuracy, run it twice. Errors cost us supplier goodwill.

[T27] Sam Rivera (FDE): Priya, is there a budget envelope for that?

[T28] Priya Nair (Finance): A hard one. Ceiling of EUR 0.08 per processed invoice, all model calls included, retries included. The CFO signed that unit economics line and I report against it.

[T29] Sam Rivera (FDE): Third recorded conflict. Latency requirements?

[T30] Tomas Keller (IT): Two modes. The clerk-facing validation step is interactive, p95 under 2 seconds per step or people fall back to typing. The bulk of extraction can run as overnight batch, no latency requirement there.

[T31] Anna Lindqvist (Operations): Roles, so you have them: ap_clerk reviews and corrects extractions and can send an invoice for approval. Clerks never release anything.

[T32] Priya Nair (Finance): And ap_approver approves and releases postings. Segregation of duties between those two roles is mandatory, same person cannot hold both on one invoice.

[T33] Priya Nair (Finance): Escalation case for you: duplicate suspicion. Same vendor, same amount, close dates, the agent stops and a human decides. We pay duplicates today and it is embarrassing.

[T34] Jonas Weber (Security): Related, and this one is fraud-critical: any change in vendor bank details means the payment is blocked and it escalates to a security review. No agent, no clerk, nobody pays against fresh bank details without that review.

[T35] Sam Rivera (FDE): Data governance question I need answered for the contract: who owns the extracted invoice data, and what is the retention period for source documents and model outputs?

[T36] Jonas Weber (Security): That is Legal's call, not mine. I cannot answer it today.

[T37] Anna Lindqvist (Operations): Honestly, we have never defined it.

[T38] Sam Rivera (FDE): Then it goes into the contract as a blocking open question with Legal as the owner. Nothing runs against production data until it is answered.

[T39] Tomas Keller (IT): For acceptance testing: use our seeded staging environment, seed 4711. Deterministic fixtures, clean resets, so your runs are reproducible and comparable.

[T40] Sam Rivera (FDE): Recap of what I am taking away. KPIs: eighty percent touchless and under 24 hours cycle time. Three open conflicts: autonomous posting under EUR 1000 versus named-human approval on every write, approver threshold EUR 5000 versus EUR 500, best-model-regardless versus the EUR 0.08 ceiling. One blocking open question on data ownership and retention, owner Legal. Fixed constraints: EU residency, append-only audit log, untrusted document content, p95 2 seconds interactive, roles ap_clerk and ap_approver with segregation of duties, duplicate and bank-change escalations, staging seed 4711.

[T41] Anna Lindqvist (Operations): Correct. Send the draft contract, we will review it Thursday.
