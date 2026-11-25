# Live platform architecture

## Request routing and execution

`server.py` serves a local, dependency-free web client and a JSON API. Each team has a persisted roster and a group channel plus a private channel per agent. Agent role prompts and allowed tools are editable while no task is active. A task snapshots its participants and permissions, so later changes cannot silently alter active execution.

A group task first calls a real CrewAI planner. It returns a bounded dependency graph with agent IDs, task instructions, expected output, artifact requirements and independent review links. The host validates IDs, participant membership, dependency order and independent producer/reviewer assignment before scheduling. A ready subtask runs as one real CrewAI `Agent`/`Task`/sequential `Crew`. There is no fixed four-stage requirement in this live path, no Flow, and no stock CrewAI hierarchical manager. This is a host scheduler around CrewAI, not a claim that stock CrewAI implements the course's entire platform.

An agent can call `collab.message` to share channel context and `collab.delegate` to synchronously invoke another participating specialist. Delegation is limited to depth 3 and 20 total tasks. Peer messages are delivered as bounded context at subsequent model calls. Existing tasks within one run execute sequentially; the server allows at most three active runs across separate channels. Private responses use their selected agent directly rather than publishing to the group.

`live/model.py` uses a persistent direct HTTPS session for each Crew, with certificate verification, no environment proxy inheritance, no redirects, bounded retries and output/context limits. A DeepSeek-specific adapter normalizes tool-response syntax; fabricated model observations are discarded at the ReAct tool boundary. Only gateway receipts establish that tools ran. Raw model responses stay in private run records; normal UI snapshots omit them. Telemetry and default CrewAI embedding memory are disabled.

## Human interaction, timing and recovery

`ask_user` creates a durable clarification and blocks that task until an actual answer arrives or its original deadline expires. The app never invents answers. Existing-file replacement/patch/deletion and remote Git push go through the inherited gateway's approval path. The approval binds exact arguments, including expected file hashes or target commit/remote/branch. Changed state invalidates the old approval.

Pause/cancel takes effect at the next safe boundary; an already running bounded HTTP/tool operation may finish first. Pauses and human waiting do not replenish the task deadline. Runs are bounded by wall time, 320 model calls and 2,500,000 reported tokens, reserving 80 calls / 750,000 tokens for independent review. Tool execution receives the remaining monotonic deadline. Model calls have bounded socket timeouts; safe-boundary cancellation is not instantaneous preemption of a request already in progress.

A server restart marks active records interrupted, without automatically repeating operations. Non-model calls still marked running become uncertain. The operator inspects their actual effects and records reconciliation through the UI before resume. A reconciliation is explicitly marked `machineVerified: false`; it does not become a successful machine receipt. Cancelling an interrupted run preserves its uncertain-operation list. Resume uses the original deadline and saved task checkpoints; it does not promise instruction-level continuation inside a previous Crew. An expired task must be started as a new measured attempt. Interrupted suites are retained rather than automatically replayed.

## Workspace and tool boundaries

The vendored `ToolGateway` handles file, web, execution and Git operations. Each agent/run has its own private workspace; relative path validation, no-follow filesystem access and read-only import rules restrict access. Agents cannot read configuration files or arbitrary host paths through these tools.

Python, JavaScript and browser checks execute in resource-limited Docker containers with network disabled, a read-only root filesystem, dropped capabilities, bounded output/time, and only the current agent's writable workspace plus authorized read-only imports mounted. Credentials are not placed into container environments. The Python and browser images are prepared before execution; there is no host-code fallback. The model orchestration process remains a trusted local server process; the isolation boundary applies to agent-controlled code and files, not one separate model-server OS process per agent.

Web retrieval runs through the host gateway with URL/address validation and bounded content. Retrieved pages are evidence/data, not authorization to bypass tool permissions. Documents are prompted as Markdown/TXT; the file gateway accepts bounded plain-text source/data suffixes, not PDF/DOCX/PPT. HTML and JS are supported as code artifacts for the explicit Mid-Test software/simulation tasks. There is no unrestricted shell tool.

Git status and local commit run in the agent workspace. GitHub push uses a configured HTTPS remote and locally stored credentials; its exact target needs human approval. Agent-produced commits do not substitute for the course's required genuine contributions by each human teammate.

## Artifact handoff and acceptance

`ArtifactStore.register` snapshots published bytes and records their SHA256. The runtime grants named recipients access through read-only imports. A later edit requires a new publication/revision; superseded artifacts and their reviews cannot pass as the new product. The same team's next group task can inherit snapshots through explicit grants. Private-channel output is not automatically inherited by the group.

The runtime closes missing acceptance coverage after the model-generated plan, and schedules repair only when an actual independent verdict fails. Review records are durable evidence, not recursively reviewed product documents. Unchanged same-task publications reuse their immutable version and review; changed bytes require another independent verdict.

A product reviewer must be a separate review-capable participant, have access to every current producer artifact, and have actually read the matching hashes. Review cannot approve the reviewer's own delegated output. HTML/JS acceptance additionally needs current hash-bound successful browser execution. Python acceptance requires a successful executed test; its generic test receipt is not as strong as the HTML/JS exact-file binding, so reviewers must still establish that their test exercises the requested Python source. These are minimum evidence gates, not a formal proof that all semantic requirements are satisfied.

Execution completion, product verdict and human acceptance remain separate concepts. A task whose Crews return may still be partial/unassessed. An agent review is not course-instructor official acceptance. ZIP export preserves agent ownership and original relative file layout and includes a manifest and run record. Individual artifacts are sent as downloads, never executed as active content on the workbench origin.

## Independent memory and learning evidence

Memory belongs to an agent and a channel, persisted in its team's SQLite repository. Successful/partial subtask outcomes and failed execution episodes are retained with source run/task/call references. Recall uses local text relevance, without external embedding APIs. Private-channel memories are not inserted into group context.

An agent can propose a procedural candidate and explicitly mark a recalled item adopted with `memory.use`. A candidate can be tried in a later task while still labeled unverified. Trials record actual adoption and execution outcome; they do not automatically establish product correctness. Human confirmation is recorded separately. Confirmed strategies receive feedback, and the inherited memory service can lower confidence/archive ineffective strategies. This supports learning experiments without equating persisted text or self-reported adoption with proven improvement. Controlled held-out, memory-enabled/disabled runs remain necessary to measure benefit.

## Storage, evaluation and local access

Each team persists channels, run checkpoints, memories and events independently under `.platform/teams/`. Original Mid-Test suites use fresh scenario teams and exact role/task Markdown, with fixed software 1→2, newsroom 1→2, fund 1→2 order. The suite timer includes all waits, and all six cases remain in completion denominators. The app stores the current suite and archived suite JSON. Team metrics cover up to the latest 60 task records; use archived suite records for full benchmark denominators.

The HTTP service binds only to 127.0.0.1, validates Host and Origin, requires a custom JSON mutation header, limits request size, whitelists static files and applies a restrictive content security policy. This is a trusted single-user local application, not an authenticated multi-tenant service. API keys and Git tokens remain in ignored local configuration; known credential strings are scrubbed from receipts and rejected from published artifacts. Public deployment would require a separate authentication/authorization design.

## Measured redundancy fixes

The original CrewAI Task prompt asked for the complete product in its final answer. Live execution now defines that final answer as a short receipt, while its actual artifact requirements remain in the task description. Model-facing file-write and browser receipts are compact views; complete unmodified evidence stays in storage. History compaction preserves the actual Observation appended to a proposed Action by CrewAI, rather than accidentally deleting the successful write receipt along with a large source body.

Every model turn receives a checkpoint derived only from actual calls: written paths, current hashes, prior tests, actual human decisions, unread review artifacts and pending verdicts. Reviewer control points prioritize remaining file reads, original acceptance criteria and submission of the real pass/fail verdict. Minimum read/test gate readiness never becomes an automatic product pass. Final current-byte coverage also includes revisions made after a planned review.

Ordinary requests use DeepSeek non-thinking mode. Review and repeated tool errors use native enabled thinking with low effort; the visible ReAct Action is stopped at the Observation boundary, avoiding generation of invented tool receipts. Such invented text is never treated as execution. The adapter does not send native tools in its HTTPS body, so previous reasoning_content is not replayed. This policy follows the official DeepSeek thinking guide: https://api-docs.deepseek.com/guides/thinking_mode/ . Diagnostic recovery and source revisions are recorded, with original wall deadlines retained.
