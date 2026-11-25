# EvoAgent multi-agent workspace

A local multi-agent workbench with a model-driven planner, dependency scheduler and agent execution loop.

This branch contains the experiment in `crewai-midtest-experiment/`. Start all commands below from that directory. Credentials, local databases, generated products and raw benchmark ledgers are excluded from Git. See [repository notes](docs/repository-notes.md) for the snapshot scope and known acceptance limitation.

## Start the application

Use Python 3.12 and Docker Desktop. Install the dependencies, prepare both execution images, and add your own DeepSeek key in the local configuration file.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# First setup only: create the private configuration, then fill LLM_API_KEY.
cp config-deepseek/.env.example config-deepseek/.env
# Never commit the key or paste it into messages.
docker pull python:3.11-alpine
docker build -t evo-agent-browser:1.64.1 docker/browser
.venv/bin/python server.py --port 8100
```

Open http://127.0.0.1:8100. Keep this terminal and Docker Desktop running. The server is local only, without multi-user authentication; do not expose it through a public proxy. Model calls and supported file/web tools can work without Docker, but code execution and browser acceptance require the prepared images and have no host execution fallback.

The local configuration uses `LLM_PROVIDER=deepseek`, `LLM_MODEL=deepseek-flash` and `https://api.deepseek.com/v1`. The live adapter uses non-thinking mode for ordinary work, and low-effort thinking for independent review or repeated tool errors. Every request records its actual mode. This delivery policy differs from the historical non-thinking benchmarks. `LLM_THINKING_MODE` stays `default` because the inherited configuration parser reserves its `disabled` flag for HKGAI. API keys are read locally; they are not sent to the UI, agent tools or Docker. Provider and endpoint validation permit only DeepSeek/HKGAI.

## Use the workbench

- Choose a software, newsroom or investment team, or create a separate team from a template. Edit role prompts, names and allowed tools in **Agent 与记忆**.
- Use **群聊** for team work and a named agent channel for private conversation. Select participants before a group task. Private messages and memory are excluded from group model context.
- Send a high-level goal in **执行任务** mode. The planner generates a task graph; agents can communicate and delegate additional specialist work. A normal chat uses a single responder.
- Answer actual questions and approve or reject exact operations in the workbench. No preset simulated user replies are supplied by the app. Supplements during a run reach subsequent model calls.
- Inspect actual subtasks, model/tool receipts, recalled memory and independent review. Download individually published artifacts, a ZIP preserving relative runtime paths, or the run's JSON record.
- Pause at safe model/tool boundaries, resume, cancel, or inspect archived runs. Human waiting and pauses count toward the original wall deadline. After a restart, uncertain operations require an explicit reconciliation note before recovery; an expired run must become a new timed task. Cancelling an interrupted run preserves uncertain-operation records.

The live application uses model-generated plans, bounded delegation and a local execution loop with direct model and tool calls.

## Run the original Mid-Test

In **评测记录**, choose **运行原始 Mid-Test** and a whole-suite wall budget. The application creates fresh teams using the original role prompts, then runs software 1 → 2, newsroom 1 → 2, fund 1 → 2. Task 2 receives its own team's prior artifacts through explicit read-only grants. Open the current case to answer questions and approvals. The timer continues while waiting for people.

Original Markdown fixtures are under `fixtures/mid_test/`; no answer fixtures are supplied. All six cases remain in the denominator, including unstarted cases. Execution completion and independent product acceptance are recorded separately. The UI shows recent team metrics; `/api/state` also includes the subtask completion rate. Suite records are retained under `.platform/suites/` and can be used in a formal performance report.

A real-model sequential six-case attempt was measured on 2026-11-23: **21m21.804s** total, including **4m22.217s** of clarification waiting and **1m29.577s** of operation-approval waiting. All six attempts ended partial/unassessed (execution completion and product pass rate both 0/6). Five cases reached the existing per-case 500,000-token budget; the investment case produced repetitive unexecuted review text and did not publish a review, blocking its remaining work. This is measured attempt time, not the time to successfully deliver all six products. There were 156 real DeepSeek calls and no core source-hash changes during the run. The authorized operator acted as a simulated requester and recorded each actual question, answer, assumption and approval. See `evidence/live-six-20261123-01/timing-report.json` and `human-decisions.jsonl`. Historical parallel experiment times remain separate and cannot establish a speedup for this new application. Memory persistence and reuse are implemented; a controlled memory-on/off evaluation is still needed to claim improved performance.

The evidence paths above refer to operator-local measurement files. They are not included in this source branch; the results stated here remain historical measurements, not a claim that the current six-case suite has passed.

## Delivery recovery after the first live benchmark

`delivery-v8` keeps products in real files and returns short handoff receipts. It preserves real observations when compacting combined Action/Observation messages, carries an authoritative per-agent execution checkpoint, and prioritizes unread current artifacts and the actual review verdict. Local handoff and verdict tools end their steps after the actual publication or review receipt, avoiding an extra response that repeats the product. Current linked source is retained while it fits; duplicate historical reads can be omitted. Re-publishing unchanged bytes in the same task reuses the artifact/version and its valid review. Only an actual failed verdict schedules repair, followed by independent review of the new hashes. Packaging does not need another copy of unchanged source files.

The task budget is 2,500,000 reported tokens / 320 model requests, with a production allowance that preserves 750,000 tokens / 80 requests for independent acceptance. Context is bounded around 115,000 characters including the execution checkpoint. Complete raw evidence remains in the ledger. Higher limits do not replace evidence: no completed file or verdict means the step remains partial, and no successful current-byte test means executable artifacts cannot pass.

The original booking task passed a diagnostic delivery/QA/final-acceptance run after recovery. That run included a pause, runtime revision and operator guidance; its elapsed time is not a clean speed benchmark. The subsequent full frozen-version six-case measurement is kept separately from the original failed run and cancelled diagnostics. See `evidence/target-booking-v4/` and the corresponding new suite evidence.

## Verification evidence

- 35 automated runtime/HTTP/protocol tests cover isolation, immutable publication, stale approvals, review versions, memory, delegation, local Git commit, failure retention and restart reconciliation, plus the measured context/receipt failures, reserved QA budget, real repair/review closure and non-executed promises remaining incomplete.
- A real DeepSeek task autonomously assigned a Markdown checklist to a developer and an independent review to QA: **8 model calls, 19.808 seconds, product verdict pass**. See `evidence/live-deepseek-smoke.json`. This is a connectivity/interaction check, not a Mid-Test result.
- Real Docker Chromium exercised the live UI, including a real DeepSeek clarification, its actual UI answer, role editor, private/group isolation, evaluation dialog and 390px layout. See `evidence/browser-ui-check.json` and the screenshots in `evidence/`.
- A real ToolGateway Docker Python check confirmed no model credentials or host project mount, private per-agent/run paths, read-only imports and disabled network; see `evidence/docker-isolation-check.json`.
- A local Git commit is tested. Remote GitHub push is implemented with exact commit/remote/branch approval, but has not been exercised against a remote repository in this adaptation.

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check web/app.js
# Requires the running local server and uses the funded API:
.venv/bin/python scripts/live_smoke.py
# Docker Desktop browser check; also makes a real private-channel model request:
docker run --rm -v "$PWD/tests:/tests:ro" -v "$PWD/evidence:/evidence" evo-agent-browser:1.64.1 node /tests/ui_smoke.cjs
```

## Architecture and course coverage

See [architecture](docs/architecture.md) for routing, memory, persistence and sandbox behavior, and [requirement coverage](docs/requirements.md) for implementation/evidence boundaries and remaining course submissions. New source comments and technical documents use English; the UI uses Chinese.

- `server.py`: localhost HTTP API and static serving.
- `live/model.py`: local agent execution loop and direct model-provider integration.
- `live/platform.py`: team/task runtime, HITL, delegation, versioned handoffs, review and memory.
- `live/suite.py`: original sequential six-case protocol.
- `web/`: workbench, agent editor and evaluation UI.
- `vendor/backend/`: snapshotted Evo tool, sandbox, artifact, memory and storage implementations; `provenance.json` records their origin.
- `.platform/`: ignored local team databases, workspaces, event ledgers and evaluation records.

## Earlier experiment runners

The original CrewAI runners have been removed. The [historical experiment notes](docs/historical-experiments.md) document the earlier configuration and timings.
