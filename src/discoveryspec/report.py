"""Manager-facing report: one self-contained HTML page per acceptance run.

The audience is a non-technical decision maker. The page answers, in plain
language: can this agent go live, which promise did it break, in whose words,
and did it stay inside the agreed speed and cost limits.

Trust rules, mirroring the rest of the pipeline:
- INPUT BINDING: the report refuses to render unless the contract it is given
  is byte-identical to the contract the run executed (content hash recorded
  in run-report.json), the project matches, and the transcript pin matches.
  A report must never present one contract's promises with another run's
  verdicts.
- CLOSED STYLING: every CSS declaration in the page is emitted by the typed
  serializer below from validated brand tokens; the brand file supplies data,
  never CSS. Policy flags (no shadows, no gradients, corner radius caps, no
  emoji) are enforced by auditing the serialized declarations and refusing to
  write on any violation. A silently broken brand rule in a customer-facing
  document is a defect, not a nuance.
- HONEST VERDICTS: a run with harness or judge errors renders as INCOMPLETE,
  visually distinct from FAIL; an incomplete run proves nothing and the page
  says so. Scenario names come from ``manager_label`` fields carried from the
  scenario templates, never invented at render time.
- The transcript is hash-pinned and the binding hashes are printed; the HTML
  file itself carries no signature and the page does not claim otherwise.
"""

from __future__ import annotations

import re
from typing import Any

from jinja2 import Environment, StrictUndefined
from markupsafe import Markup

from .brand import Brand, validate_brand
from .models import DeploymentContract

Declaration = tuple[str, str]
Rule = tuple[str, list[Declaration]]

EMOJI_RE = re.compile(
    "["
    "🀀-🫿"  # emoticons, symbols, pictographs, extended
    "☀-➿"  # miscellaneous symbols and dingbats
    "⬀-⯿"  # miscellaneous symbols and arrows
    "️"  # emoji variation selector
    "]"
)

VERDICT_TEXT = {
    "PASS": (
        "Cleared to deploy",
        "The agent kept every promise in the signed contract and stayed "
        "inside both agreed operating limits.",
        "Next step: release to the agreed environment; keep this report with "
        "the contract record.",
    ),
    "FAIL": (
        "Do not deploy",
        "The agent broke at least one promise from the signed contract, or "
        "exceeded an agreed operating limit. Details below, in the words of "
        "the people who set each rule.",
        "Next step: fix the agent and rerun the acceptance suite; the "
        "contract itself needs no change.",
    ),
    "INCOMPLETE": (
        "Run incomplete",
        "Part of the acceptance suite could not be executed, so this run "
        "proves nothing about the promises it did not test. Passing rows "
        "below are informational only.",
        "Next step: repair the run setup and execute the full suite before "
        "judging the agent.",
    ),
}


class ReportError(ValueError):
    """The report cannot be rendered; exit 2 (infrastructure/integrity)."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.exit_code = 2
        self.problems = problems or []


# ---------------------------------------------------------------------------
# typed style serializer: brand tokens in, audited declarations out
# ---------------------------------------------------------------------------

def _font_stack(families: list[str]) -> str:
    generic = {"serif", "sans-serif", "monospace", "system-ui", "cursive", "fantasy"}
    rendered = []
    for family in families:
        if family in generic or " " not in family:
            rendered.append(family)
        else:
            rendered.append(f'"{family}"')
    return ", ".join(rendered)


def brand_stylesheet(brand: Brand) -> list[Rule]:
    palette = brand.palette
    radius = f"{min(2, brand.policy.max_corner_radius_px)}px"
    display = _font_stack(brand.typography.display_stack)
    body = _font_stack(brand.typography.body_stack)
    mono = _font_stack(brand.typography.mono_stack)
    return [
        ("body", [
            ("background", palette.paper), ("color", palette.ink),
            ("font-family", body), ("font-size", "16px"),
            ("line-height", "1.6"), ("margin", "0"),
            ("padding", "3rem 1.25rem 4rem"),
        ]),
        (".page", [("max-width", "860px"), ("margin", "0 auto")]),
        (".eyebrow", [
            ("font-size", ".72rem"), ("letter-spacing", ".14em"),
            ("text-transform", "uppercase"), ("color", palette.muted_ink),
            ("margin", "0 0 .5rem"),
        ]),
        ("h1, h2, .verdict-word", [
            ("font-family", display), ("line-height", "1.2"), ("margin", "0"),
        ]),
        ("h1", [("font-size", "2rem"), ("font-weight", "600"), ("text-wrap", "balance")]),
        ("h2", [("font-size", "1.3rem"), ("font-weight", "600"), ("margin", "0 0 1rem")]),
        (".masthead", [
            ("border-bottom", f"2px solid {palette.ink}"),
            ("padding-bottom", "1.4rem"),
        ]),
        (".masthead-meta", [
            ("display", "flex"), ("flex-wrap", "wrap"),
            ("gap", ".35rem 2.5rem"), ("margin-top", "1rem"),
            ("font-size", ".88rem"), ("color", palette.muted_ink),
        ]),
        (".masthead-meta strong", [("color", palette.ink), ("font-weight", "600")]),
        ("section", [("margin-top", "2.6rem")]),
        (".band", [
            ("display", "flex"), ("gap", "1.1rem"),
            ("align-items", "flex-start"), ("padding", "1.1rem 1.3rem"),
            ("margin-top", "2rem"),
        ]),
        (".band p", [("margin", ".15rem 0 0"), ("max-width", "62ch")]),
        (".band .next", [("font-size", ".88rem"), ("color", palette.muted_ink)]),
        (".band-pass", [
            ("background", palette.pass_background),
            ("border-left", f"4px solid {palette.pass_ink}"),
        ]),
        (".band-fail", [
            ("background", palette.fail_background),
            ("border-left", f"4px solid {palette.fail_ink}"),
        ]),
        (".band-incomplete", [
            ("background", palette.paper),
            ("border", f"1px solid {palette.muted_ink}"),
            ("border-left", f"4px solid {palette.accent}"),
        ]),
        (".verdict-word", [("font-size", "1.35rem"), ("font-weight", "700"),
                           ("white-space", "nowrap")]),
        (".band-pass .verdict-word", [("color", palette.pass_ink)]),
        (".band-fail .verdict-word", [("color", palette.fail_ink)]),
        (".band-incomplete .verdict-word", [("color", palette.accent)]),
        (".chip", [
            ("display", "inline-block"), ("font-size", ".7rem"),
            ("font-weight", "700"), ("letter-spacing", ".08em"),
            ("padding", ".12rem .55rem"), ("border-radius", radius),
        ]),
        (".chip-pass", [("color", palette.pass_ink),
                        ("background", palette.pass_background)]),
        (".chip-fail", [("color", palette.fail_ink),
                        ("background", palette.fail_background)]),
        (".chip-notrun", [("color", palette.muted_ink),
                          ("background", palette.paper),
                          ("border", f"1px solid {palette.muted_ink}")]),
        (".tablewrap", [("overflow-x", "auto")]),
        ("table", [
            ("border-collapse", "collapse"), ("width", "100%"),
            ("font-size", ".92rem"), ("font-variant-numeric", "tabular-nums"),
        ]),
        ("th", [
            ("text-align", "left"), ("font-size", ".72rem"),
            ("letter-spacing", ".1em"), ("text-transform", "uppercase"),
            ("color", palette.muted_ink), ("font-weight", "600"),
            ("padding", "0 1rem .5rem 0"),
            ("border-bottom", f"1px solid {palette.ink}"),
        ]),
        ("td", [
            ("padding", ".55rem 1rem .55rem 0"),
            ("border-bottom", f"1px solid {palette.muted_ink}"),
            ("vertical-align", "top"),
        ]),
        ("td.num", [("white-space", "nowrap")]),
        (".who", [("color", palette.muted_ink), ("font-size", ".85rem")]),
        (".evidence", [
            ("border", f"1px solid {palette.muted_ink}"),
            ("padding", "1.4rem 1.5rem"), ("margin-top", "1rem"),
        ]),
        (".evidence h3", [("margin", "0 0 .3rem"), ("font-size", "1.02rem")]),
        (".evidence .what", [("margin", ".2rem 0 1.2rem"), ("max-width", "68ch")]),
        (".evidence ul", [("margin", ".2rem 0 1.2rem"), ("padding-left", "1.2rem"),
                          ("max-width", "68ch")]),
        ("blockquote", [
            ("margin", "0 0 1rem"), ("padding", ".7rem 1.1rem"),
            ("border-left", f"3px solid {palette.accent}"),
            ("border-top", f"1px solid {palette.muted_ink}"),
            ("border-right", f"1px solid {palette.muted_ink}"),
            ("border-bottom", f"1px solid {palette.muted_ink}"),
            ("max-width", "68ch"),
        ]),
        ("blockquote p", [("margin", "0"), ("font-style", "italic")]),
        ("blockquote footer", [
            ("margin-top", ".4rem"), ("font-style", "normal"),
            ("font-size", ".82rem"), ("color", palette.muted_ink),
        ]),
        (".turn", [("font-family", mono), ("font-size", ".78rem"),
                   ("color", palette.accent), ("font-style", "normal")]),
        (".note", [("color", palette.muted_ink), ("font-size", ".85rem"),
                   ("max-width", "70ch")]),
        (".seal", [
            ("margin-top", "3rem"),
            ("border-top", f"1px solid {palette.muted_ink}"),
            ("padding-top", "1.2rem"), ("font-size", ".82rem"),
            ("color", palette.muted_ink), ("max-width", "74ch"),
        ]),
        (".seal code", [("font-family", mono), ("font-size", ".78rem")]),
    ]


ALWAYS_FORBIDDEN = ("url(", "expression(", "@import", "javascript:")


def audit_stylesheet(rules: list[Rule], brand: Brand) -> list[str]:
    """The brand policy is a hard constraint on every serialized declaration."""
    problems: list[str] = []
    policy = brand.policy
    for selector, declarations in rules:
        for prop, value in declarations:
            lowered = f"{prop}:{value}".lower()
            for marker in ALWAYS_FORBIDDEN:
                if marker in lowered:
                    problems.append(f"{selector} {{ {prop} }}: forbidden {marker!r}")
            if not policy.allow_shadows and (
                prop in ("box-shadow", "text-shadow") or "shadow" in value.lower()
            ):
                problems.append(
                    f"{selector} {{ {prop}: {value} }}: brand forbids shadows"
                )
            if not policy.allow_gradients and "gradient(" in value.lower():
                problems.append(
                    f"{selector} {{ {prop}: {value} }}: brand forbids gradients"
                )
            if prop == "border-radius":
                match = re.fullmatch(r"(\d+)px", value)
                if match is None or int(match.group(1)) > policy.max_corner_radius_px:
                    problems.append(
                        f"{selector} {{ border-radius: {value} }}: exceeds the "
                        f"brand's {policy.max_corner_radius_px}px corner cap"
                    )
    return problems


def css_text(rules: list[Rule]) -> str:
    blocks = []
    for selector, declarations in rules:
        body = " ".join(f"{prop}: {value};" for prop, value in declarations)
        blocks.append(f"{selector} {{ {body} }}")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# content policy
# ---------------------------------------------------------------------------

def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _walk_strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _walk_strings(v)]
    return []


def emoji_problems(view: dict) -> list[str]:
    problems = []
    for text in _walk_strings(view):
        if EMOJI_RE.search(text):
            problems.append(f"emoji or symbol in report content: {text[:60]!r}")
    return problems


# ---------------------------------------------------------------------------
# input binding and view model
# ---------------------------------------------------------------------------

def _require_keys(run_report: dict) -> list[str]:
    required = ("source", "scenarios", "slo", "verdict", "passed", "total",
                "mode", "agent_model")
    problems = [f"run-report.json is missing {key!r}" for key in required
                if key not in run_report]
    # the SLO block is indexed by name below; a run report from another tool or
    # an older format must be refused with the pipeline's own integrity error,
    # not surface as a KeyError traceback under a different exit code
    slo = run_report.get("slo")
    if not problems and not isinstance(slo, dict):
        problems.append("run-report.json 'slo' is not an object")
    elif not problems:
        for key in ("p95_latency_ms", "cost_per_task_eur"):
            if not isinstance(slo.get(key), dict):
                problems.append(f"run-report.json slo is missing {key!r}")
    return problems


def build_view(
    contract: DeploymentContract,
    run_report: dict,
    contract_sha256: str,
) -> dict:
    problems = _require_keys(run_report)
    if problems:
        raise ReportError("run report is not renderable", problems=problems)
    source = run_report["source"]

    if source.get("project") != contract.metadata.project:
        problems.append(
            f"run was produced for project {source.get('project')!r}, the "
            f"supplied contract is {contract.metadata.project!r}"
        )
    if source.get("transcript_sha256") != contract.metadata.transcript_sha256:
        problems.append("transcript pin in the run does not match the contract")
    bound_sha = source.get("contract_sha256")
    if bound_sha is None:
        problems.append(
            "run-report.json carries no contract_sha256 binding; rerun the "
            "suite with this version before rendering a report"
        )
    elif bound_sha != contract_sha256:
        problems.append(
            "the supplied contract file is not the contract this run executed "
            f"(run bound {bound_sha[:12]}..., supplied {contract_sha256[:12]}...)"
        )
    if contract.metadata.status != "approved":
        problems.append("contract is not approved; reports render approved runs only")
    if problems:
        raise ReportError(
            "refusing to render: the report inputs do not belong together",
            problems=problems,
        )

    # normalize entries so the strict template can rely on every key, also
    # for run reports written before a field existed
    entries = [
        {**e, "judge_error": bool(e.get("judge_error")), "error": e.get("error")}
        for e in run_report["scenarios"]
    ]
    # a judge outage is infrastructure, exactly like a missing trajectory:
    # it must appear under Not executed, never under Broken promises
    failures = [
        e for e in entries
        if not e["passed"] and not e["error"] and not e["judge_error"]
    ]
    not_run = [e for e in entries if e["error"] or e["judge_error"]]
    verdict = run_report["verdict"]
    if verdict not in VERDICT_TEXT:
        raise ReportError(f"unknown verdict {verdict!r} in run report")

    by_id = contract.requirement_by_id()
    seen_pairs = set()
    conflicts = []
    for req in contract.requirements:
        for other_id in req.conflicts_with:
            pair = tuple(sorted((req.id, other_id)))
            if pair in seen_pairs or other_id not in by_id:
                continue
            seen_pairs.add(pair)
            first, second = by_id[pair[0]], by_id[pair[1]]
            conflicts.append({
                "sides": [
                    {
                        "id": side.id, "title": side.title,
                        "statement": side.statement,
                        "stakeholder": side.stakeholder,
                        "decision": side.resolution.decision if side.resolution else "unresolved",
                        "rationale": side.resolution.rationale if side.resolution else "",
                    }
                    for side in (first, second)
                ],
                "date": first.resolution.date if first.resolution else "",
            })

    title, summary, next_action = VERDICT_TEXT[verdict]
    slo = run_report["slo"]
    return {
        "customer": contract.metadata.customer,
        "project": contract.metadata.project,
        "system": contract.metadata.system,
        "not_covered": list(run_report.get("not_covered", [])),
        "approved_by": contract.metadata.approved_by or "",
        "approved_at": contract.metadata.approved_at or "",
        "run_id": run_report.get("run_id", ""),
        "generated_at": run_report.get("generated_at", ""),
        "mode": run_report["mode"],
        "agent_model": run_report["agent_model"],
        "verdict": verdict,
        "verdict_title": title,
        "verdict_summary": summary,
        "next_action": next_action,
        "passed": run_report["passed"],
        "total": run_report["total"],
        "entries": entries,
        "failures": failures,
        "not_run": not_run,
        "slo": [
            {
                "name": "Response time per interactive step (95th percentile)",
                "agreed": f"{slo['p95_latency_ms']['limit']} ms",
                "observed": (
                    f"{slo['p95_latency_ms']['observed']} ms"
                    if slo["p95_latency_ms"]["observed"] is not None
                    else "not measured"
                ),
                "passed": slo["p95_latency_ms"]["passed"],
            },
            {
                "name": f"Cost per processed {slo['cost_per_task_eur'].get('unit', 'task')}, worst case",
                "agreed": f"EUR {slo['cost_per_task_eur']['limit']}",
                "observed": (
                    f"EUR {slo['cost_per_task_eur']['max_observed']}"
                    if slo["cost_per_task_eur"]["max_observed"] is not None
                    else "not measured"
                ),
                "passed": slo["cost_per_task_eur"]["passed"],
            },
        ],
        "conflicts": conflicts,
        "transcript_sha": (contract.metadata.transcript_sha256 or "")[:12],
        "contract_sha": contract_sha256[:12],
    }


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contract compliance report: {{ view.customer }}</title>
<style>
{{ css }}
</style>
</head>
<body>
<div class="page">
  <header class="masthead">
    <p class="eyebrow">Deployment contract compliance report</p>
    <h1>{{ view.customer }}: {{ view.system }}, acceptance run</h1>
    <div class="masthead-meta">
      <span>Contract signed <strong>{{ view.approved_at }}</strong></span>
      <span>Approved by <strong>{{ view.approved_by }}</strong></span>
      <span>Run <strong>{{ view.run_id }}</strong>, {{ view.mode }} mode{% if view.agent_model != view.mode %}, agent {{ view.agent_model }}{% endif %}</span>
      <span>Generated <strong>{{ view.generated_at }}</strong></span>
    </div>
  </header>

  <div class="band band-{{ view.verdict | lower }}">
    <span class="verdict-word">{{ view.verdict_title }}</span>
    <div>
      <p>{{ view.verdict_summary }}
      {{ view.passed }} of {{ view.total }} promises held in this run.</p>
      <p class="next">{{ view.next_action }}</p>
    </div>
  </div>

  {% if view.failures %}
  <section>
    <h2>{% if view.failures | length == 1 %}The broken promise{% else %}Broken promises{% endif %}</h2>
    {% for failure in view.failures %}
    <div class="evidence">
      <h3>{{ failure.enforces.title }}</h3>
      <p class="what">Test: {{ failure.manager_label }}. The agent failed
      these checks:</p>
      <ul>
        {% for check in failure.failed_checks %}
        <li>{{ check.name }}{% if check.detail %}: {{ check.detail }}{% endif %}</li>
        {% endfor %}
        {% for criterion in failure.failed_criteria %}
        <li>Judge: {{ criterion.criterion }}</li>
        {% endfor %}
      </ul>
      {% for citation in failure.citations %}
      <blockquote>
        <p>"{{ citation.text }}"</p>
        <footer>{{ citation.speaker }}, {{ citation.role }}, discovery call,
        statement <span class="turn">T{{ citation.turn }}</span></footer>
      </blockquote>
      {% endfor %}
      <p class="note">An agent that behaves this way in testing would behave
      this way in production. The failure is in the agent under test, not in
      the contract.</p>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if view.not_run %}
  <section>
    <h2>Not executed</h2>
    <div class="evidence">
      <p class="what">These promises could not be tested in this run; nothing
      here says the agent keeps or breaks them.</p>
      <ul>
        {% for entry in view.not_run %}
        <li>{{ entry.manager_label }}: {{ entry.error or "the LLM judge was unavailable, so the rubric criteria could not be evaluated" }}</li>
        {% endfor %}
      </ul>
    </div>
  </section>
  {% endif %}

  <section>
    <h2>All promises in this run</h2>
    <div class="tablewrap">
      <table>
        <tr><th>Result</th><th>What was tested</th><th>Whose promise it enforces</th></tr>
        {% for entry in view.entries %}
        <tr>
          <td>{% if entry.error or entry.judge_error %}<span class="chip chip-notrun">NOT RUN</span>
          {% elif entry.passed %}<span class="chip chip-pass">PASS</span>
          {% else %}<span class="chip chip-fail">FAIL</span>{% endif %}</td>
          <td>{{ entry.manager_label }}</td>
          <td class="who">{{ entry.enforces.stakeholder }}</td>
        </tr>
        {% endfor %}
      </table>
    </div>
  </section>

  <section>
    <h2>Agreed operating limits</h2>
    <div class="tablewrap">
      <table>
        <tr><th>Limit</th><th>Agreed</th><th>Observed</th><th>Result</th></tr>
        {% for limit in view.slo %}
        <tr>
          <td>{{ limit.name }}</td>
          <td class="num">{{ limit.agreed }}</td>
          <td class="num">{{ limit.observed }}</td>
          <td>{% if limit.passed %}<span class="chip chip-pass">PASS</span>
          {% else %}<span class="chip chip-fail">FAIL</span>{% endif %}</td>
        </tr>
        {% endfor %}
      </table>
    </div>
  </section>

  {% if view.not_covered %}
  <section>
    <h2>What this suite does not check</h2>
    <div class="evidence">
      <p class="what">These commitments cannot be observed in an agent's
      actions, so no test above covers them. They were agreed and remain
      binding; each is checked by the named party instead.</p>
      <ul>
        {% for entry in view.not_covered %}
        <li>{{ entry.title }} ({{ entry.stakeholder }}): {{ entry.reason }}
        Verified by {{ entry.verified_by }}.</li>
        {% endfor %}
      </ul>
    </div>
  </section>
  {% endif %}

  {% if view.conflicts %}
  <section>
    <h2>Appendix: how disagreements were settled</h2>
    <p class="note">These positions contradicted each other in the discovery
    call. Each was resolved by the named people before signing; the adopted
    side is what the tests above enforce.</p>
    {% for conflict in view.conflicts %}
    <div class="evidence">
      {% for side in conflict.sides %}
      <blockquote>
        <p>"{{ side.statement }}"</p>
        <footer>{{ side.stakeholder }} ({{ side.id }}):
        <strong>{{ side.decision }}</strong>{% if side.rationale %}.
        {{ side.rationale }}{% endif %}</footer>
      </blockquote>
      {% endfor %}
    </div>
    {% endfor %}
  </section>
  {% endif %}

  <div class="seal">
    <p>Every verdict in this report traces to a numbered statement in the
    recorded discovery call. The call record is hash-pinned at contract
    signing (fingerprint <code>{{ view.transcript_sha }}&hellip;</code>) and
    this run is bound to the exact signed contract
    (<code>{{ view.contract_sha }}&hellip;</code>); altering either input
    makes the pipeline refuse to run. This HTML page itself carries no
    signature: the machine-readable run record, not this rendering, is the
    authoritative artifact.</p>
    <p>Report style: {{ brand_company }}. Generated by discoveryspec report.</p>
  </div>
</div>
</body>
</html>
"""


def render_report(
    contract: DeploymentContract,
    run_report: dict,
    brand: Brand,
    contract_sha256: str,
) -> str:
    validate_brand(brand)
    view = build_view(contract, run_report, contract_sha256)

    if not brand.policy.allow_emoji:
        # the brand's own identity strings render too and obey the same rule
        scan_target = {
            "view": view,
            "brand_identity": [
                brand.identity.company, brand.identity.product_line or "",
            ],
        }
        problems = emoji_problems(scan_target)
        if problems:
            raise ReportError(
                "brand forbids emoji and the report content contains some; "
                "refusing to render",
                problems=problems,
            )

    rules = brand_stylesheet(brand)
    style_problems = audit_stylesheet(rules, brand)
    if style_problems:
        raise ReportError(
            "serialized styles violate the brand policy; refusing to render",
            problems=style_problems,
        )

    environment = Environment(autoescape=True, undefined=StrictUndefined)
    html = environment.from_string(TEMPLATE).render(
        view=view,
        css=Markup(css_text(rules)),
        brand_company=brand.identity.company,
    )

    # belt and braces on top of the serializer boundary: the only style
    # source is css_text above, so these markers cannot legitimately appear
    if not brand.policy.allow_shadows and "box-shadow" in html:
        raise ReportError("emitted document contains a shadow; refusing to write")
    if not brand.policy.allow_gradients and "gradient(" in html:
        raise ReportError("emitted document contains a gradient; refusing to write")
    return html
