# Evals

The eval harness runs an agent against a dataset of cases and scores each
output. It is offline (no engine, no database), uses the same cost-aware
`LLMClient` the engine uses, and exits non-zero when the pass rate drops below a
threshold — so it drops straight into CI.

```bash
export AGENTFORGE_ANTHROPIC_API_KEY=...        # a real key: evals call a model
uv run agentforge eval evals/suites/sales_scoring.yaml
uv run agentforge eval evals/suites/planner_agent.yaml --json report.json --threshold 0.9
```

## Suite format

```yaml
name: sales-scoring
target: sales_scoring_agent      # an agent_type in the StepRegistry
threshold: 0.8                    # min mean weighted score across cases
cases:
  - name: strong-fit-hot
    inputs:                       # becomes StepContext.inputs
      research:
        lead: { company_name: Northwind, contact_title: VP Engineering }
        dossier: { industry: software, headcount_estimate: 400 }
    checks:
      - scorer: one_of
        args: { path: tier, options: [hot, warm] }
      - scorer: in_range
        args: { path: fit_score, min: 55 }
      - scorer: llm_judge
        args: { path: rationale, rubric: "cites concrete ICP evidence", threshold: 0.6 }
        weight: 2
```

`path` is a dotted lookup into the agent's output dict (`""` / omitted = whole
output); list indices work too (`plan.0.step`).

## Scorers

| scorer | args | passes when |
|---|---|---|
| `equals` | `path`, `value` | `output[path] == value` |
| `one_of` | `path`, `options` | value is in `options` |
| `contains` | `path`, `substring` or `substrings` | all substrings present (case-insensitive) |
| `regex` | `path`, `pattern` | `re.search` hits |
| `in_range` | `path`, `min?`, `max?` | numeric value within bounds |
| `json_keys` | `path?`, `required` | value is an object with every required key |
| `non_empty` | `path` | value present and not `""`/`[]`/`{}`/`None` |
| `llm_judge` | `path?`, `rubric`, `threshold?`, `tier?` | a judge model scores it ≥ threshold (0–1) |

A case passes only if **every** check passes; the suite's `pass_rate` is the mean
of each case's weighted score, so partial credit still shows up in the report.

## Report

`render_text` prints a per-case / per-check breakdown with cost and token totals;
`--json` writes the full `SuiteReport` (every `CaseResult`, every `ScoreResult`,
the agent output) for diffing across runs.
