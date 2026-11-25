# Original course requirement coverage

Source: `evo-agent-platform-latest/cl修改记录/requirements.md` in the sibling project. This document describes implementation and evidence, not an assertion that the final course submission is complete.

## Human/agent communication and autonomous work

**Implemented:** Group and one-to-one channels, selectable participants, editable agent roles/tools, a model-generated task dependency graph, peer messages and bounded dynamic specialist delegation. Group/private model context and memory are separated. The UI exposes actual steps and receipts.

**Evidence:** Runtime tests cover channel isolation, graph validation, specialist delegation, continuity and exact artifact grants. A real DeepSeek task autonomously selected developer and QA work and passed an independent Markdown review. A real browser test completed an agent's actual question and the operator's UI response.

**Limit:** The scheduler is sequential within a run. There is no claim that a fixed script or stock CrewAI alone provides autonomous negotiation. Complex cooperation quality must be assessed on full tasks.

## Human-in-the-loop

**Implemented:** Blocking clarification, exact-argument operation approvals, approve/reject, safe-boundary pause/resume/cancel, in-run supplements and explicit restart reconciliation. The app does not simulate user replies. Human waiting counts toward wall time.

**Evidence:** Actual UI question/answer; tests cover clarification, cancellation, pause, stale-hash overwrite rejection and uncertain-effect recovery. Remote push has not been performed in this adaptation.

## Independent memory and evolution

**Implemented:** Per-owner, channel-scoped persistent factual episodes, candidate lessons, relevance recall, explicit adoption, candidate trials, confirmation and outcome feedback. Execution failures are retained as failure experiences. Private-channel memory is excluded from group model context.

**Evidence:** Tests cover failure retention, ownership/channel isolation and trying a candidate without incorrectly promoting it to verified. The UI shows candidate status and adoption records.

**Remaining evaluation:** Measure completion/time on comparable held-out tasks with memory enabled and disabled, inspect memory changes and failure cases, and distinguish execution success from product acceptance. No causal learning improvement has been established by the current smoke test.

## Private sandbox and filesystem

**Implemented:** Per-agent/run private paths; immutable, explicitly granted peer imports; offline resource-limited Docker execution for Python/JS/browser. The host orchestration process is trusted; agent-controlled source does not execute directly on the host.

**Evidence:** Tests cover forbidden import writes, unauthorized reads and immutable snapshot bytes. A real ToolGateway Python Docker check confirmed private paths, no model credentials or host project mount, read-only imports and disabled network (`evidence/docker-isolation-check.json`). Docker Chromium exercised the real local application. The browser UI test harness bridges Docker to localhost; this operator-owned UI test is separate from the network-disabled agent execution sandbox.

## Required action space

**Implemented:** `web.search`, `web.fetch`, `file.read/list/write/patch/delete`, `code.run/test`, `git.status/commit/push`, plus collaboration, artifact, review and memory operations. Tool permissions are editable per role. Local Git commit is verified by a real commit hash.

**Remaining verification:** Run and inspect the full live-mode six-case products, including actual research and generated-code behavior. Perform an approved push to the team's chosen GitHub repository with real credentials. No remote repository or human contribution history was created by this work.

## Document formats and model service

**Implemented:** Agent prompts require Markdown/TXT for documents; bounded plain-text code/data suffixes are supported by the gateway. No PDF/Word/PowerPoint generation/parser was added. Explicit HTML/JS Mid-Test software and interactive simulations remain code artifacts.

The live adapter uses the configured DeepSeek endpoint and model with native non-thinking mode. Only DeepSeek/HKGAI providers and their HTTPS domains pass configuration validation. Default CrewAI memory, embedding calls, tracing and telemetry are disabled. SDK packages present as transitive dependencies do not mean their model services are used.

**Evidence:** The funded DeepSeek smoke made 8 real model calls, published a 498-byte Markdown artifact, and recorded an independent read/hash review in 19.808 seconds (`evidence/live-deepseek-smoke.json`).

## Practical interface

**Implemented:** Evo-inspired team workbench, private/group chats, task state/timer, actual questions/approvals, artifacts/download ZIP, subtask/call/memory/collaboration panels, role editor, histories/JSON export and evaluation page. Desktop and 390px layouts are verified. The implementation keeps backend orchestration and evidence visible instead of adding decorative product features.

**Evidence:** `evidence/browser-ui-check.json`, `workspace.png`, `human-clarification.png`, `evaluation.png`, `mobile.png`. These screenshots show the new application, not copied Evo outputs.

## Completion and timing metrics

**Implemented:** Subtask completion, whole-task execution completion, product pass rate, model call/token counts and wall elapsed time. Original Mid-Test suite ordering and fresh role initialization are implemented, and all six cases remain in the denominator. Same-team task 2 inherits only its own group's context/grants. Historical parallel experiments are labeled separately.

**Evidence:** The sequential six-case protocol and continuity pass an offline injected-executor integration test. This validates the protocol, not model performance. UI connectivity and small-task real DeepSeek timing are measured separately.

**Measured attempt:** The real-model sequential suite on 2026-11-23 took 1281.804 seconds, including 262.217 seconds of clarification waits and 89.577 seconds of approvals. All six cases ended partial/unassessed; five hit the local per-case token cap, and the investment review never published its required output. Exact timestamps and assumptions are in `evidence/live-six-20261123-01/timing-report.json`. This does not establish successful-delivery latency.

**Remaining evaluation:** Resolve the measured budget/review-output failures, complete and independently check all six products in a fresh frozen-version run, and use a matched baseline if claiming a speed improvement. The released final test set must also be run when available. Previous fixed-stage parallel times are not the new application's sequential completion time.

## Course submission materials

**Prepared:** Working local source, architecture document, coverage document, automated checks, real live-model and browser evidence, preserved historical experiment notes and data.

**Still needed:** The team's GitHub repository with genuine continuous contributions by every human teammate; screenshots of that authentic history; a formal Mid-Test/final-set performance and failure/learning report based on full runs; and the required project presentation. These need real team activity and measured results. They cannot be satisfied by inventing commit authors, backdating commits, or treating smoke tests as official product acceptance.
