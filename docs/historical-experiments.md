# CrewAI Mid-Test pilot

Archive: the CrewAI runner scripts referenced below have been removed. See the [current startup instructions](../README.md) to run EvoAgent.

This isolated experiment runs the original three four-agent teams and their six
original Mid-Test requests using CrewAI 1.15.44. It does not modify EvoMAC.

## Run

```sh
.venv/bin/python run.py --output runs/pilot-01 --seconds 900
# Adapted course-API ReAct integration, with verified proxy bypass:
.venv/bin/python run.py --output runs/pilot-03 --seconds 900 --transport standard --direct --react-boundary --native-stop
```

The runner reads the course model configuration from the existing local EvoMAC
`.env`; it never copies credentials into this experiment. Telemetry and tracing
are disabled, and CrewAI memory is disabled to avoid default embedding services.
The existing provider and real file/web/Docker tools are snapshotted in `vendor/`.
`provenance.json` identifies their source commit. No other model provider is used.

Three team processes execute concurrently. Each team's second task follows its
first task in the same private workspaces and receives its first result as
context. Every role has a 12-iteration limit. The 900-second team deadline includes
both tasks; the parent allows a 15-second process-start/cleanup grace period.
The completion cap is 8,000 tokens per call; no model-output fixtures are used.

The four stage descriptions are experiment-authored orchestration prompts.
Original role identities and original task text are unchanged. Delegation is
disabled because all four roles are explicitly scheduled. Explicit `publish`
copies source bytes to unique peer handoff paths and records SHA256 receipts.
This simplified handoff is not EvoMAC's complete immutable grant mechanism.

The operator authorized simulated user responses. Assumptions are recorded in
each team report and each actual clarification receipt. Existing-file revisions
inside the isolated experiment are approved with exact gateway-provided hashes
and logged. Remote pushes and file deletion are unavailable.

## Evidence and comparison limits

Each team writes `events.jsonl`, `report.json`, `console.log`, a file manifest,
and actual artifacts below `runtime/workspaces/`. Events preserve model latency,
reported usage, returned model identity, tool arguments and real tool receipts.
`crew_returned` means execution returned, not that the product passed acceptance.
The summary never promotes that status to a quality verdict.

This pilot changes concurrency and orchestration as well as framework. Historical
EvoMAC timings therefore provide context only. A causal speedup claim requires a
fresh matched baseline with the same model, transport, concurrency, budgets,
assumptions and independent product checks. Framework installation time is
separate from the measured execution window. Failed and unstarted cases must
remain in the six-case denominator.

## Integration diagnostics

Pilot 01 uses the unchanged existing provider configuration. Pilot 02 switches
to standard writes and removes proxy environment variables, but macOS system
proxies still apply; its reports explicitly correct the initial direct-route
label. Neither is an accepted product result.

Pilot 03 uses an explicit empty urllib proxy handler for model requests and
`NO_PROXY=*`. A minimal custom course-API adapter sends CrewAI's ReAct stop
sequence (`\nObservation:`) to the endpoint. It uses the same course credentials,
model, temperature, disabled-thinking setting and completion cap, standard HTTPS
with certificate verification and no redirects. Unlike the original provider,
this adapter does not use a per-call subprocess; socket timeout and the parent
team deadline bound it. If the server still returns an invented observation,
the local parser boundary keeps only the preceding tool action. Raw model
responses and every applied local boundary remain in the event ledger. These
integration changes are confounders and must not be called framework-only gains.

## Delivery continuation

`run_delivery.py` adds a persistent direct `requests.Session`, bounded generation
and stage retries, saved stage checkpoints and resumable private workspaces.
Four fixed role stages run as sequential single-agent Crews, with the host
carrying exact stage results forward. No CrewAI Flow is used. Current budgets
are 2,000 output tokens for planning, 3,000 for research, 6,000 for other roles,
and 20 iterations per role. Context history is bounded by `context_budget.py`,
retaining current file observations rather than repeatedly dropping them.

The host explicitly publishes completed developer runtime bytes before QA.
A forced final response at the iteration boundary may contain an unexecuted
local artifact action. At most one valid pending write/patch/publication is
recovered through the real permission gateway and logged separately; model
proposals never count as executed tool receipts by themselves. A task-specific
check rejects a wrong-topic fund slide deck. These host interventions are part
of the experimental harness, not stock CrewAI behavior.

```sh
.venv/bin/python run_delivery.py --output runs/a-new-run --seconds 1800 --transport standard --direct --react-boundary --native-stop
# Resume only incomplete saved stages with a new bounded execution allowance:
.venv/bin/python run_delivery.py --output runs/a-new-run --seconds 1800 --transport standard --direct --react-boundary --native-stop --resume
```

`delivery-01` completed six actual product collections after adaptive debugging
and rework. `verify_delivery.py` independently verified date-range statistics,
cancellations and persistence (8 assertions), plus correct-topic semiconductor
slide navigation (10 assertions), against exact published bytes in real Docker
Chromium. The experiment's software release stage also recorded 12 passed
dashboard assertions. This is not complete official product acceptance.

`delivery-results.json` records approximately 40m09s as the union of observed
worker execution windows, including retries and rework within those windows.
The wall span to final generation is approximately 1h53m10s; gaps between worker
lifetimes are excluded from the observed-window metric. Interrupted silent
tails may be missing. The harness changed during this run, so neither number
is a clean continuous fixed-version benchmark or a framework speedup estimate.
Per-case timings accumulate observed windows across all attempts and cannot be
summed to obtain parallel suite wall time.

`crewai-midtest-deliverables.zip` contains the six model-created products,
agent reports, file hashes and limited independent browser receipts. Host
collection preserves product bytes. News link placeholders and research source
limitations remain disclosed; the software is an assumed single-browser pilot.

## Isolated DeepSeek run

Fill `config-deepseek/.env` with a DeepSeek API key locally. The file is private
and ignored; the HKGAI key is never copied or sent to DeepSeek. The configuration
uses the documented `deepseek-flash` model and explicit non-thinking mode via
DeepSeek's `thinking: {type: disabled}` parameter, rather than HKGAI's template
parameter. Missing credentials stop preflight before any model request.

```sh
.venv/bin/python run_delivery.py --config-root config-deepseek --output runs/deepseek-01 --seconds 1800 --transport standard --direct --react-boundary --native-stop
```

Use a fresh output directory so previous HKGAI artifacts cannot be treated as
DeepSeek products. Cross-provider/model resume is rejected. Keep the same role
prompts, tasks, tool capabilities, stage structure and token/iteration budgets;
record the actual returned model identity. Live connectivity and six-task
completion still require a valid funded DeepSeek credential.
