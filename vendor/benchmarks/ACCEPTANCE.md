# Automated Acceptance and Performance Evaluation

## First-round demo

From the repository root, pull the team's Docker image and evaluate the handwritten run records.

```bash
docker pull python:3.11-alpine
python3 benchmarks/acceptance.py \
  --suite benchmarks/fixtures/acceptance/suite.json \
  --runs benchmarks/fixtures/acceptance/runs.json \
  --data-dir .runtime/acceptance-demo \
  --output .runtime/acceptance-demo/result.json
```

Every case appears in the console summary, JSON report, and SQLite evaluation table. Exit code `0` means all goals passed. Exit code `1` means results were written and at least one goal failed or remains unassessed. Exit code `2` signals a manifest or storage error.

The [example report](fixtures/acceptance/example_result.json) records one passing run, one completed run rejected for treating zero as invalid, one timeout, and one planning crash. The results are 1/4 overall goals, 5/9 business tasks, and 5/16 fixed checks. The records model 13,400 ms of elapsed run time, including 6,000 ms of human wait. Actual Docker verifier time varies. The fixture reports and tool receipts are handwritten. The evaluator's script reruns use real Docker.

## Scoring

- **Overall goals.** Count one goal per case. An eligible terminal run passes when all declared checks pass. Failed, timed-out, crashed, and cancelled runs fail the goal. A completed or partial run with unresolved evidence remains unassessed unless a check already proves failure.
- **Business tasks.** Count each non-planning task ID once per run at its highest revision. Checks bind to the tasks that produced the evidence. A linked failure fails the task. All linked checks must pass for the task to pass. A task with no independent verdict is unassessed and stays in the denominator. A planning crash with no business tasks contributes zero tasks.
- **Fixed checks.** Every case contributes all checks declared by its verifier. Failed, timed-out, and crashed cases can still receive credit for independently verified partial work. Unassessed checks stay in the denominator.
- **Timing.** Measure assignment to final output, or to the recorded termination or deadline when output is absent. Human wait is the union of recorded wait intervals clipped to that time window. It is already included in elapsed time. Verifier time is measured separately while the checks execute.
- **Failure and memory evidence.** The report preserves case, task, and check reasons, task revisions, memory IDs, and recorded changes. The failure-mode counts include each recorded error occurrence at those three levels. Feedback candidates carry the run ID, task ID, revision, owner, memory IDs, evidence IDs, reason, verdict, and stage. Only evidenced pass or fail on a closed revision can reach the final stage. Module C owns settlement, including deduplication by owner, run, task, revision, and memory ID.

## CSV order case

The test operator owns all answer files. Send the model only the task prompt and primary CSV. Give the independent CSV directly to the Reviewer through a read-only grant. Keep the evaluator's case directory and expected values outside agent workspaces. The standard CSV prompt asks for a public source, a general-purpose Python script, a five-field JSON summary, and a Markdown report. Leave task decomposition and delegation to the platform.

- **S1 Research.** Bind a successful `web.search` or `web.fetch` call, a returned public CSV source, and a CSV finding present in the Research task output. The recorded source must contain topical text.
- **S2 Executor.** Bind `summarize_orders.py` and `summary.json` from the same current revision. The evaluator checks both the registered summary and a fresh Docker rerun. The script contract is `python summarize_orders.py INPUT_CSV OUTPUT_JSON`. Summaries contain exactly the five integer keys in the expected answer.
- **S3 Reviewer.** Bind `code.test` in the Reviewer sandbox to the current script ID, SHA-256, and revision, plus the independent input and a registered `review_summary.json`. The evaluator compares the Reviewer output and reruns the script in a separate sandbox using the independent input.
- **S4 Coordinator.** Register `report.md` with headings **Method**, **Results**, **Sources**, and **Review**. Method must cover the case rules, Results gives each field on its own `field: integer` line, Sources includes the verified source URL, and Review begins with the verdict `Pass`, `Fail`, or `Unassessed` and explains the outcome.

Both verifier reruns reuse the existing sandbox manager with fresh workspaces, a read-only script, and network isolation. Only the input CSV and registered script are provided to the container. The evaluator compares the output on the host after Docker exits. An unavailable Docker service or verifier timeout yields an unassessed check with a reason and supplies no final strategy settlement for that check.

## Collect and evaluate platform evidence

Use another directory for handwritten demo evaluations. For real runs, point `--platform-data-dir` at the saved platform runtime. Set `--data-dir` to the same runtime directory when the team wants real acceptance rows to appear in the evaluation view. Use the image configured by Module B through `--docker-image` when it differs from the default.

```bash
python3 benchmarks/acceptance.py \
  --suite /path/to/evaluator/suite.json \
  --runs /path/to/evaluator/platform_bindings.json \
  --platform-data-dir .runtime \
  --data-dir .runtime \
  --output .runtime/acceptance/report.json
```

Both manifests use `schemaVersion: 1`. The suite contains `suiteId` and a `cases` array. The run manifest contains a `runs` array with one unique `runId` per `caseId`. Record an explicit timeout or crash for each case that starts without producing an output. The command requires the entire case roster to avoid partial reporting. The example suite and handwritten records show the full structure.

For platform bindings, supply `caseId`, `runId`, and the applicable entries in `evidence`. The adapter reads status, business tasks, timestamps, events, and calls from SQLite and artifact bytes from the immutable registry. For an interrupted or in-flight snapshot, supply the observed `terminatedAtMs` or `deadlineAtMs` and the reason. A crash before a run was stored can be recorded through the external record format with no tasks. Review history in the platform log remains historical evidence. Module A can provide a revision on tool calls and memory hits to associate them after rework. The shared baseline's unversioned calls and memory hits are only used at revision zero.

Each artifact binding supplies `taskId`, `revision`, `artifactId`, and `sha256`. For CSV, set the `script`, `summary`, and `report` bindings. Set `research` with its task and revision, `callId`, `sourceUrl`, and `conclusion`. Set `review` with its task and revision, `callId`, the script binding as `target`, the reviewer-only `inputArtifactId`, and the `result` artifact binding. Module A/B should produce these distinct audit records for S3.

1. Register the script and the evaluator-provided `review_orders.csv` as separate artifacts, with the Reviewer as owner of the input snapshot, then grant both to the Reviewer before execution. Only the Reviewer receives the independent input.
2. In the Reviewer task call `code.test` with path `imports/<script-id>/summarize_orders.py` and arguments `["/imports/<input-id>/review_orders.csv", "review_summary.json"]`.
3. Register `review_summary.json` after the tool call. The evaluator checks the grants, version snapshot, exact call arguments, sandbox receipt, registration ordering, and hashes.
4. Pass the requested binding IDs to D. Keep expected values in the evaluator directory throughout.

The external format can also be used with trusted records exported by the test operator, including `source: "recorded"` for observed runs. The handwritten example documents the shape. Agent-authored claims alone cannot supply the tool evidence or lifecycle record. Real course-model success, private review input injection, and live A/B/C integration still require a team run.

## Use another suite

For CSV cases, set `verifier` to `orders-csv` and point to the main input and expected summary, independent input and expected summary, and report method patterns. Resolve all file references relative to their manifest directory. The `exact-json` verifier supports a case with an evaluator-owned `expectedPath` and an `outputName` bound as `evidence.output`. For a new deliverable type, add a trusted verifier to the registry in `acceptance.py`. It declares ordered `check_ids` and evaluates the normalized evidence to return `Check` objects with task bindings.

The [follow-up case manifest](fixtures/acceptance/followup_suite.json) includes the supplied clarification and memory holdout inputs. For clarification, send the ambiguous prompt and wait for the run to ask about negative amounts. Reply that refunds count toward the net amount and resume the same run. Verify the question and response in its approval log. The signed review answer counts the independent `-2` as valid, yielding a total of five cents.

For the memory comparison, use isolated runtime directories with matched model, budget, prompt, CSV, and rubric. Capture the memory-off and memory-on runs under distinct case IDs. Use a one-case suite for each platform runtime, or combine trusted exported records under the follow-up manifest. Keep the answers out of agent context and memory. Set `memoryCondition` and `memoryBoundary` in the bindings, then compare correctness, elapsed time, and adopted evidence across repeated runs. One pair illustrates the mechanism. Official scores require the instructor's official inputs and scoring rules.
