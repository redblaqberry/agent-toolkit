# BlastRadius

Assert what your agent must and must not change in the world.

Text and trajectory evals grade what an agent *says* and which tools it *called*. Neither sees the thing the agent was hired to change: the state of your system. An agent can report "payment released successfully," pass an answer check and a tool-call check, and still have written two payment rows where one was audited. The words are right. The world is wrong.

BlastRadius grades the world. Capture your store before and after a run, declare a contract of expected, allowed, and forbidden effects, and it tells you whether the change was correct and nothing else happened.

Two properties are the whole point:

- **Default-deny.** Every changed field, added row, and appended audit event must be justified by an effect in the contract. A change nothing explains fails the verdict. The negative space, "nothing else changed," is asserted, not assumed.
- **Fail-closed.** When the evidence is malformed, a hash does not recompute, or a selector is ill-typed, BlastRadius returns `error`, not a guess. An unjudgeable run blocks a release gate exactly like a failure. An oracle that guesses when the evidence is broken can be talked into a false pass; this one refuses.

It is store-agnostic. An adapter (a live SQL database, a JSON snapshot, or SiloBench) declares its schema and hands the engine a normalized before/after pair. The engine, the seven effect rules, and the default-deny sweep never know which store they came from.

## Install

Not on PyPI. The name `blastradius` there belongs to an unrelated Terraform
graph tool, and it occupies the same import package, so `pip install
blastradius` gets you that project and not this one. Install from source:

```bash
pip install "git+https://github.com/redblaqberry/blastradius"            # core library and pytest plugin
pip install "blastradius[cli] @ git+https://github.com/redblaqberry/blastradius"   # adds the `blast` command
pip install "blastradius[sql] @ git+https://github.com/redblaqberry/blastradius"   # adds the SQL adapter (SQLAlchemy)
```

Requires Python >= 3.10. Working on the project itself:

```bash
git clone https://github.com/redblaqberry/blastradius && cd blastradius
pip install -e ".[dev]"   # pytest, the CLI, and the SQL adapter
pytest                    # 21 tests, no skips, no database, no network
```

## Grade a captured pair

Given a before and after capture and a contract, `blast check` prints a verdict and exits 0 (pass), 1 (fail), or 2 (error), so a CI step gates on it directly. This runs from a fresh clone, no database and no other checkout, using the committed JSON snapshots and the generic adapter:

```bash
blast check --adapter snapshot \
  --contract examples/contracts/order-ships.yaml \
  --before examples/quickstart/orders-before.json \
  --after  examples/quickstart/orders-after-correct.json
```

```
GO: ORD-SHIP-01 (The order ships, and nothing else moves)
  verdict:  PASS
  checks:   4 passed, 0 failed
```

Point `--after` at `orders-after-broken.json`, where the order shipped correctly but a second order row appeared that nobody asked for, and the answer looks fine while the verdict does not:

```
BLOCKED: ORD-SHIP-01 (The order ships, and nothing else moves)
  verdict:  FAIL
  checks:   3 passed, 1 failed (unexplained)
  unexplained: 1 change(s) no expected or allowed effect justifies
```

The same command over SiloBench captures (the reference accounts-payable environment) grades the flagship case, where an agent pays a vendor twice and the answer is byte-identical to the correct run. The captures ship with the repository, so this runs from a fresh clone too:

```bash
blast check --adapter silobench \
  --contract examples/contracts/ap-payment-release.yaml \
  --before tests/fixtures/silobench/baseline/payment/before-snapshot.json \
  --after  tests/fixtures/silobench/defects/duplicate-payment/after-snapshot.json \
  --before-events tests/fixtures/silobench/baseline/payment/before-events.jsonl \
  --after-events  tests/fixtures/silobench/defects/duplicate-payment/after-events.jsonl
```

```
BLOCKED: AP-PAY-01 (An approver releases exactly the payment policy permits)
  verdict:  FAIL
  checks:   6 passed, 5 failed (one-payment, payment-audited, at-most-one-payment-per-invoice, no-second-payment, unexplained)
  unexplained: 2 change(s) no expected or allowed effect justifies
```

The subtle one there is `payment-audited`. A cheap check confirms a `PAYMENT_RELEASED` event exists (it does) and that the payment count is allowed; both can pass while a second, unaudited payment sits in the table. The `correlated` rule asks the harder question, does the audit event actually name the rows that appeared, one for one, and reports the payment nobody audited.

## Grade a live database in a test

The pytest plugin captures your store around the agent, grades what changed, and fails the test if the world is wrong:

```python
from blastradius import parse_scenario
from blastradius.adapters.sql import SqlAdapter

def test_release_pays_once(side_effects):
    adapter = SqlAdapter(engine, events_table="events")
    contract = parse_scenario(...)  # or load_scenario("contract.yaml")
    with side_effects(adapter, contract):
        my_agent.run("release the approved payment for INV-1")
    # the block asserts on exit: a wrong or unjudgeable world fails the test
```

Or drive it yourself with the `capture` context manager:

```python
from blastradius import capture, load_scenario

contract = load_scenario("contract.yaml")
with capture(adapter) as run:
    my_agent.do_the_task()
verdict = run.grade(contract)
assert verdict.ok, verdict.report()
```

## Contracts

A contract is a `blastradius.contract.v1` file of expected / allowed / forbidden effects over the state. The seven rules: `transition` (a field moved from X to Y), `count_delta` (N rows added or removed, optionally matching a selector), `unchanged`, `event_exists`, `correlated` (rows and audit events name each other), `idempotent` (at most one effect per key), and `compensated` (an open and close net out). See `examples/contracts/` and `blast explain -c <contract>`.

## Adapters

- **`snapshot`** (no extra dependencies): a self-describing JSON capture format, for any store you can dump to JSON.
- **`sql`** (needs the `sql` extra): reflect a SQL database with SQLAlchemy and capture its tables before and after, in process.
- **`silobench`**: read SiloBench `snapshot.v1` captures, the reference deterministic environment used in the tests.

Writing your own is one small class: declare a `Schema` and return a `NormalizedSnapshot`.

## Where it comes from

The engine is extracted from [StateDiff](https://github.com/redblaqberry/statediff), a state oracle wired to one environment, and generalized behind the pluggable adapter so it can grade any store. [SiloBench](https://github.com/redblaqberry/silobench) is the deterministic AP environment used as the reference test bed.

## Status

v0.1, early but real: the engine, the SiloBench and SQL and snapshot adapters, the CLI, and the pytest plugin work, with the accounts-payable example graded end to end (correct passes, a duplicate payment is blocked) on real captures. The moat is thin and the ideas are simple by design; the value is being the neutral, self-hostable, fail-closed standard, not a platform.

## License

[MIT](LICENSE)
