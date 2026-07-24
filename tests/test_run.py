"""Run-command behavior: the suite executes through agent-eval-gate's own
loader, adapters, and checks; verdicts link back to requirements and turns;
SLOs are enforced across the run; failures and gaps fail closed."""

import json

import pytest
from typer.testing import CliRunner

from discoveryspec import RunError, percentile_nearest_rank, scenario_cost_eur
from discoveryspec.cli import app
from tests.conftest import APPROVED_PATH, DRAFT_PATH, TRANSCRIPT_PATH

gate = pytest.importorskip(
    "agent_eval_gate", reason="agent-eval-gate not installed; run-command tests skipped"
)

runner = CliRunner()

PRICES = {"claude-test": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}


def combined(result) -> str:
    return result.output + (result.stderr or "")


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    out = tmp_path_factory.mktemp("export")
    result = runner.invoke(app, [
        "export-gate", "--contract", str(APPROVED_PATH), "--out", str(out),
    ])
    assert result.exit_code == 0, combined(result)
    return gate.load_scenarios(out / "scenarios.yaml")


def passing_trajectory(scenario, latency_s=0.4, output_tokens=200):
    checks = scenario.checks
    tool_calls = [
        gate.ToolCallRecord(name=expected.name, args=dict(expected.args_subset))
        for expected in checks.expected_tools
    ]
    final_text = " ".join(checks.output_must_contain) or "handled per the contract"
    step = gate.StepRecord(
        index=0,
        stop_reason="end_turn",
        text=final_text,
        tool_calls=tool_calls,
        usage=gate.Usage(input_tokens=800, output_tokens=output_tokens),
        latency_s=latency_s,
    )
    return gate.Trajectory(
        scenario_id=scenario.id, model="claude-test", steps=[step], final_text=final_text
    )


def write_fixtures(fixtures_dir, scenarios, overrides=None):
    """Write a passing trajectory fixture per scenario; ``overrides`` maps a
    scenario id to a replacement Trajectory (or None to omit the fixture)."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    overrides = overrides or {}
    for scenario in scenarios:
        if scenario.id in overrides:
            trajectory = overrides[scenario.id]
            if trajectory is None:
                continue
        else:
            trajectory = passing_trajectory(scenario)
        (fixtures_dir / f"{scenario.id}.json").write_text(
            trajectory.model_dump_json(indent=2), encoding="utf-8"
        )


def run_cli(tmp_path, fixtures_dir, *extra, prices=PRICES, contract=APPROVED_PATH):
    args = [
        "run", "--contract", str(contract), "--fixtures", str(fixtures_dir),
        "--out", str(tmp_path / "run-out"),
    ]
    if prices is not None:
        prices_path = tmp_path / "prices.json"
        prices_path.write_text(json.dumps(prices), encoding="utf-8")
        args += ["--prices", str(prices_path)]
    return runner.invoke(app, args + list(extra))


# --- the happy path -----------------------------------------------------------

def test_generated_trajectories_actually_pass_the_gate_checks(scenarios):
    # self-check for the fixture generator: every synthetic trajectory passes
    # agent-eval-gate's own checks, so downstream failures are real failures
    for scenario in scenarios:
        results = gate.run_checks(scenario, passing_trajectory(scenario))
        failed = [c for c in results if not c.passed]
        assert not failed, (scenario.id, failed)


def test_run_replay_all_pass(tmp_path, scenarios, transcript):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 0, combined(result)
    assert "verdict: PASS (10/10 scenarios)" in result.output

    out = tmp_path / "run-out"
    assert (out / "scenarios.yaml").exists()
    assert (out / "gate-config.json").exists()

    # run.json is a valid agent-eval-gate RunReport
    gate_report = gate.RunReport.model_validate(
        json.loads((out / "run.json").read_text(encoding="utf-8"))
    )
    assert gate_report.mode == "replay"
    assert gate_report.pass_rate == 1.0

    # the provenance report links every verdict back to statements
    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    assert report["passed"] == report["total"] == 10
    assert report["slo"]["p95_latency_ms"]["passed"]
    assert report["slo"]["cost_per_invoice_eur"]["passed"]
    for entry in report["scenarios"]:
        assert entry["manager_label"].strip() and entry["manager_label"] != entry["scenario_id"]
        assert entry["enforces"]["requirement_id"].startswith("REQ-")
        assert entry["citations"], entry["scenario_id"]
        first = entry["citations"][0]
        assert first["text"] == transcript.turn(first["turn"]).text
        assert entry["cost_eur"] is not None


def test_run_failing_scenario_quotes_the_broken_promise(tmp_path, scenarios):
    target = next(s for s in scenarios if s.checks.expected_tools)
    empty = gate.Trajectory(
        scenario_id=target.id, model="claude-test",
        steps=[gate.StepRecord(index=0, stop_reason="end_turn", text="done",
                               usage=gate.Usage(input_tokens=10, output_tokens=5),
                               latency_s=0.1)],
        final_text="done",
    )
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={target.id: empty})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 1, combined(result)
    assert "broken promise" in result.output
    report = json.loads(
        (tmp_path / "run-out" / "run-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "FAIL"
    failed = next(e for e in report["scenarios"] if e["scenario_id"] == target.id)
    assert failed["failed_checks"]
    assert failed["enforces"]["statement"]


def test_run_missing_fixture_is_exit_2(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={scenarios[0].id: None})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 2, combined(result)
    assert "proves nothing" in combined(result)


def test_run_latency_slo_breach_is_exit_1(tmp_path, scenarios):
    # inflate a scenario without a per-scenario latency budget: every check
    # passes, but the statistical p95 breaches the contract ceiling
    target = next(s for s in scenarios if s.budgets.max_latency_s is None)
    slow = passing_trajectory(target, latency_s=10.0)
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={target.id: slow})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 1, combined(result)
    report = json.loads(
        (tmp_path / "run-out" / "run-report.json").read_text(encoding="utf-8")
    )
    assert all(e["passed"] for e in report["scenarios"])  # scenarios green...
    assert not report["slo"]["p95_latency_ms"]["passed"]  # ...the SLO is not
    assert report["verdict"] == "FAIL"


def test_latency_slo_is_per_step_not_per_trajectory(tmp_path, scenarios):
    # the customer said "p95 under 2 seconds PER STEP" (T30): a compliant
    # three-step interaction at 1 s per step must not be rejected just
    # because its total runs 3 s
    target = next(
        s for s in scenarios
        if s.budgets.max_latency_s is None
        and (s.budgets.max_steps is None or s.budgets.max_steps >= 3)
    )
    base = passing_trajectory(target)
    first = base.steps[0]
    first.latency_s = 1.0
    extra = [
        gate.StepRecord(
            index=i, stop_reason="tool_use", text="",
            usage=gate.Usage(input_tokens=10, output_tokens=10), latency_s=1.0,
        )
        for i in (1, 2)
    ]
    multi_step = gate.Trajectory(
        scenario_id=target.id, model="claude-test",
        steps=[first] + extra, final_text=base.final_text,
    )
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={target.id: multi_step})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 0, combined(result)
    report = json.loads(
        (tmp_path / "run-out" / "run-report.json").read_text(encoding="utf-8")
    )
    assert report["slo"]["p95_latency_ms"]["passed"]
    assert report["slo"]["p95_latency_ms"]["observed"] == 1000.0


def test_run_rejects_negative_usage_in_fixtures(tmp_path, scenarios):
    # a fixture with negative token counts would lower the computed cost and
    # sneak under the ceiling; malformed recordings refuse the run
    target = scenarios[0]
    bad = passing_trajectory(target)
    bad.steps[0].usage = gate.Usage(input_tokens=800, output_tokens=-500)
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={target.id: bad})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 2
    assert "negative usage" in combined(result)


def test_run_unwritable_out_is_exit_2(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the output directory should go", encoding="utf-8")
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(PRICES), encoding="utf-8")
    result = runner.invoke(app, [
        "run", "--contract", str(APPROVED_PATH), "--fixtures", str(fixtures),
        "--prices", str(prices_path), "--out", str(blocker),
    ])
    assert result.exit_code == 2
    # the refusal may fire at the staging step or the final commit; both name
    # the artifacts and the blocked output path
    assert "artifacts under" in combined(result)


def test_run_cost_slo_breach_is_exit_1(tmp_path, scenarios):
    target = scenarios[0]
    expensive = passing_trajectory(target, output_tokens=10_000_000)
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={target.id: expensive})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 1, combined(result)
    report = json.loads(
        (tmp_path / "run-out" / "run-report.json").read_text(encoding="utf-8")
    )
    assert not report["slo"]["cost_per_invoice_eur"]["passed"]


# --- fail-closed refusals -------------------------------------------------------

def test_run_requires_prices(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    result = run_cli(tmp_path, fixtures, prices=None)
    assert result.exit_code == 2
    assert "cost SLO" in combined(result)


def test_run_unknown_model_in_price_table(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    result = run_cli(tmp_path, fixtures, prices={"other-model": PRICES["claude-test"]})
    assert result.exit_code == 2
    assert "no entry for model" in combined(result)


def test_run_refuses_draft_and_removes_stale_artifacts(tmp_path):
    out = tmp_path / "run-out"
    out.mkdir()
    stale = out / "run.json"
    stale.write_text("stale run", encoding="utf-8")
    result = run_cli(tmp_path, tmp_path / "fixtures", contract=DRAFT_PATH)
    assert result.exit_code == 1
    assert not stale.exists()


def test_run_live_requires_agent(tmp_path, scenarios):
    result = run_cli(tmp_path, tmp_path / "fixtures", "--mode", "live")
    assert result.exit_code == 2
    assert "--agent is required" in combined(result)


def test_run_replay_rejects_live_flags(tmp_path, scenarios):
    result = run_cli(tmp_path, tmp_path / "fixtures", "--agent", "some.module")
    assert result.exit_code == 2
    assert "only apply to live mode" in combined(result)


def test_run_rejects_unknown_mode(tmp_path):
    result = run_cli(tmp_path, tmp_path / "fixtures", "--mode", "dry")
    assert result.exit_code == 2
    assert "--mode must be replay or live" in combined(result)


def test_slow_non_clerk_facing_step_does_not_fail_the_slo(tmp_path, scenarios):
    # the customer scoped the latency SLO to clerk-facing steps; a slow
    # approver release must not contaminate the p95 population
    target = next(s for s in scenarios if s.id == "posting-only-after-named-approval")
    slow = passing_trajectory(target, latency_s=10.0)
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios, overrides={target.id: slow})
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 0, combined(result)
    report = json.loads(
        (tmp_path / "run-out" / "run-report.json").read_text(encoding="utf-8")
    )
    assert report["slo"]["p95_latency_ms"]["passed"]


def test_all_fixtures_missing_still_writes_incomplete_report(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    result = run_cli(tmp_path, fixtures)
    assert result.exit_code == 2
    report = json.loads(
        (tmp_path / "run-out" / "run-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "INCOMPLETE"
    assert report["slo"]["p95_latency_ms"]["observed"] is None
    assert not report["slo"]["p95_latency_ms"]["passed"]


def test_run_never_deletes_an_input_colliding_with_out(tmp_path, scenarios):
    # a contract stored under --out with an artifact name must be refused,
    # not deleted by the stale-artifact cleanup
    import shutil

    out = tmp_path / "run-out"
    out.mkdir()
    contract_as_artifact = out / "run.json"
    shutil.copyfile(APPROVED_PATH, contract_as_artifact)
    shutil.copyfile(TRANSCRIPT_PATH, out / "transcript.md")
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(json.dumps(PRICES), encoding="utf-8")
    result = runner.invoke(app, [
        "run", "--contract", str(contract_as_artifact),
        "--fixtures", str(tmp_path / "fixtures"),
        "--prices", str(prices_path), "--out", str(out),
    ])
    assert result.exit_code == 2
    assert "never overwritten" in combined(result)
    assert contract_as_artifact.exists()


# --- the manager report over a real run ------------------------------------------

def test_report_cli_end_to_end(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    assert run_cli(tmp_path, fixtures).exit_code == 0
    out_html = tmp_path / "report.html"
    result = runner.invoke(app, [
        "report", "--contract", str(APPROVED_PATH),
        "--run", str(tmp_path / "run-out"), "--out", str(out_html),
    ])
    assert result.exit_code == 0, combined(result)
    html = out_html.read_text(encoding="utf-8")
    assert "Cleared to deploy" in html
    assert "Stops on a suspected duplicate invoice" in html  # template label
    assert "box-shadow" not in html and "gradient(" not in html


def test_report_cli_refuses_a_reformatted_contract(tmp_path, scenarios):
    # same contract content, different bytes: the byte-level binding refuses,
    # because "probably the same contract" is not a provenance claim
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    assert run_cli(tmp_path, fixtures).exit_code == 0
    reformatted = tmp_path / "contract.json"
    reformatted.write_text(
        json.dumps(json.loads(APPROVED_PATH.read_text(encoding="utf-8"))),
        encoding="utf-8",
    )
    result = runner.invoke(app, [
        "report", "--contract", str(reformatted),
        "--run", str(tmp_path / "run-out"), "--out", str(tmp_path / "report.html"),
    ])
    assert result.exit_code == 2
    assert "not the contract this run executed" in combined(result)
    assert not (tmp_path / "report.html").exists()


def test_report_cli_requires_a_run(tmp_path):
    result = runner.invoke(app, [
        "report", "--contract", str(APPROVED_PATH),
        "--run", str(tmp_path / "nowhere"), "--out", str(tmp_path / "report.html"),
    ])
    assert result.exit_code == 2
    assert "cannot read" in combined(result)


# --- unit behavior ---------------------------------------------------------------

def test_judge_outage_makes_the_run_incomplete(scenarios, transcript):
    # a judge infrastructure failure must never read as a broken promise
    from discoveryspec import evaluate_slos, load_contract, provenance_report
    from discoveryspec.scenarios import gate_export

    contract = load_contract(APPROVED_PATH)
    _, config_payload = gate_export(contract, transcript)
    results = []
    for scenario in scenarios:
        trajectory = passing_trajectory(scenario)
        results.append(gate.ScenarioResult(
            scenario_id=scenario.id, trajectory=trajectory,
            checks=gate.run_checks(scenario, trajectory),
        ))
    costs = {r.scenario_id: 0.01 for r in results}
    slo_verdict = evaluate_slos(results, costs, config_payload["slo"])
    report = provenance_report(
        contract, transcript, config_payload, results, costs, slo_verdict,
        "live", "claude-test", judge_error_ids=[scenarios[0].id],
    )
    assert report["verdict"] == "INCOMPLETE"
    without = provenance_report(
        contract, transcript, config_payload, results, costs, slo_verdict,
        "live", "claude-test", judge_error_ids=[],
    )
    assert without["verdict"] == "PASS"


def test_percentile_nearest_rank():
    assert percentile_nearest_rank([float(n) for n in range(1, 11)], 95) == 10.0
    assert percentile_nearest_rank([5.0], 95) == 5.0
    assert percentile_nearest_rank([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    with pytest.raises(RunError):
        percentile_nearest_rank([], 95)


def test_scenario_cost_eur_counts_cache_tokens_at_input_rate():
    trajectory = gate.Trajectory(
        scenario_id="s", model="claude-test",
        steps=[gate.StepRecord(
            index=0,
            usage=gate.Usage(
                input_tokens=1_000_000, output_tokens=1_000_000,
                cache_creation_input_tokens=500_000, cache_read_input_tokens=500_000,
            ),
        )],
    )
    cost = scenario_cost_eur(trajectory, PRICES)
    # (1M + 0.5M + 0.5M) * 3/M + 1M * 15/M: cache tokens deliberately at the
    # full input rate, overstating cost so the ceiling can only be stricter
    assert cost == pytest.approx(2.0 * 3.0 + 15.0)


def test_scenario_cost_eur_rejects_negative_and_non_finite_rates():
    trajectory = gate.Trajectory(
        scenario_id="s", model="claude-test",
        steps=[gate.StepRecord(
            index=0, usage=gate.Usage(input_tokens=100, output_tokens=100)
        )],
    )
    for bad in (
        {"input_per_mtok": -3.0, "output_per_mtok": 15.0},   # negative -> false PASS
        {"input_per_mtok": float("nan"), "output_per_mtok": 15.0},
        {"input_per_mtok": 3.0, "output_per_mtok": "inf"},
    ):
        with pytest.raises(RunError):
            scenario_cost_eur(trajectory, {"claude-test": bad})


def test_run_live_adapter_failure_is_exit_2(tmp_path, scenarios, monkeypatch):
    # live mode with a missing SDK or broken credentials must refuse with
    # exit 2, never leak a traceback
    monkeypatch.setattr(
        gate, "load_agent_spec",
        lambda module: gate.AgentSpec(system_prompt="test agent", tools=[]),
    )

    def broken_adapter(spec):
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(gate, "AnthropicToolAgent", broken_adapter)
    result = run_cli(
        tmp_path, tmp_path / "fixtures", "--mode", "live", "--agent", "dummy.module"
    )
    assert result.exit_code == 2
    assert "cannot start the live agent adapter" in combined(result)


def test_run_preflight_refusal_clears_stale_run_artifacts(tmp_path, scenarios):
    # a refused run must not leave a previous run's PASS lying around, even
    # when it is refused before execution starts (here: missing --prices)
    out = tmp_path / "run-out"
    out.mkdir()
    names = ("run.json", "run-report.json", "scenarios.yaml", "gate-config.json")
    for name in names:
        (out / name).write_text("stale artifact from an earlier run", encoding="utf-8")
    result = run_cli(tmp_path, tmp_path / "fixtures", prices=None)
    assert result.exit_code == 2
    for name in names:
        assert not (out / name).exists(), name


def test_scenario_cost_eur_fails_closed_on_gaps():
    trajectory = gate.Trajectory(scenario_id="s", model="mystery-model")
    with pytest.raises(RunError):
        scenario_cost_eur(trajectory, PRICES)
    with pytest.raises(RunError):
        scenario_cost_eur(
            gate.Trajectory(scenario_id="s", model="claude-test"),
            {"claude-test": {"input_per_mtok": "free"}},
        )


def test_run_refuses_boolean_and_string_price_rates(tmp_path, scenarios):
    fixtures = tmp_path / "fixtures"
    write_fixtures(fixtures, scenarios)
    for bad_table in (
        {"claude-opus-4-8": {"input_per_mtok": True, "output_per_mtok": 1}},
        {"claude-opus-4-8": {"input_per_mtok": "4.6", "output_per_mtok": "23"}},
    ):
        prices_path = tmp_path / "prices.json"
        prices_path.write_text(json.dumps(bad_table), encoding="utf-8")
        result = runner.invoke(app, [
            "run", "--contract", str(APPROVED_PATH), "--fixtures", str(fixtures),
            "--prices", str(prices_path), "--out", str(tmp_path / "run-out"),
        ])
        assert result.exit_code == 2, combined(result)
        assert "JSON numbers" in combined(result)
