# Bundled examples

Two contracts from different businesses, so the pipeline can be exercised end
to end from a clone and so the compiler's domain independence is demonstrable
rather than asserted.

| | `invoice_automation/` | `refund_handling/` |
|---|---|---|
| Customer | Nordlicht GmbH (furniture retailer) | Voltbay (online electronics retailer) |
| System | supplier-invoice agent | customer-refund agent |
| Unit of work | invoice | refund request |
| Actions | read_invoice, extract_fields, match_purchase_order, prepare_posting_draft, request_approval, post_invoice_to_erp | read_order, check_return_window, check_fraud_signals, issue_refund, request_approval, send_customer_message |
| Shows | the full draft to approved walkthrough, with three seeded conflicts and a blocking question | that a second contract compiles with no change to the compiler |

Both compile to ten scenarios from their own `acceptance_rules`. Nothing in
`src/discoveryspec/scenarios.py` mentions either domain; `tests/test_second_domain.py`
fails if a word from the first example ever appears in the second one's export.

## Running them

```bash
discoveryspec run --contract invoice_automation/approved-contract.json \
  --mode replay --fixtures invoice_automation/fixtures --prices prices.json \
  --out gate-run
discoveryspec report --contract invoice_automation/approved-contract.json \
  --run gate-run --brand brands/nordlicht-strict.json --out report.html
```

Substitute `refund_handling/` for the second domain.

## What the fixtures are, and are not

`*/fixtures/*.json` are **synthetic** recorded trajectories. They were written
to satisfy each scenario's checks so that `run` and `report` are executable from
a clone and exercised in CI. They are not recordings of a live model, and they
therefore prove that the harness works, not that any particular agent behaves.
Replacing them with real recordings from an agent under test is a roadmap item,
and nothing in the pipeline changes when that happens: the file format is
agent-eval-gate's own `Trajectory`, which its `record` command emits.

`prices.json` is different: the rates are real EUR approximations of published
list prices, so the per-task cost arithmetic and the ceiling check are genuine.
A run refuses outright if a model in a trajectory has no entry there, because a
cost SLO that cannot be computed must never be silently skipped.
