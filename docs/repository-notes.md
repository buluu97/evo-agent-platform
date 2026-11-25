# Repository snapshot

The `crewai-midtest-experiment` branch adds this directory to the repository without replacing the original project. It includes the Crew Studio workbench, real CrewAI adapter, pinned dependencies, vendored tools, original six-task Markdown fixtures, Docker browser image definition, offline tests, measurement scripts and historical runners.

## Local setup and data

Follow the application README from this directory. Copy `config-deepseek/.env.example` to `config-deepseek/.env` once and add your own DeepSeek key. Install Python 3.12 dependencies and prepare the Python and Chromium Docker images. The workbench starts with empty local teams; archived conversations and products are not seeded into a new checkout.

The branch excludes real environment files, credentials, Python environments, `.platform/` databases and workspaces, raw `runs/` and `evidence/` ledgers, generated products, ZIP archives and logs. Referenced historical evidence paths describe files retained on the operator workstation. They are not downloadable artifacts in this source snapshot. The vendored benchmark fixtures are source-controlled test inputs, not live Mid-Test answers. Copied vendor benchmark reports are excluded.

Historical runners retain their previous execution design. Their published defaults use bundled `fixtures/mid_test/` inputs and `config-deepseek/` configuration. Historical experiment notes describe earlier workstation runs; use the current README for setup.

## Runtime and acceptance status

The application snapshot is `delivery-v8`. Production and independent-review budgets are separate, and current-version artifact reads and execution evidence remain required. The current six-case benchmark is still in progress at publication preparation; this branch does not claim six accepted deliveries or a measured speedup over EvoMAC.

A known acceptance limitation remains for published Node-only JavaScript test scripts: the current gate requires browser-bound hashes for every `.js` artifact, even when an independent reviewer successfully executed a Node test. This can leave a dashboard delivery unassessed. The gate has not been relaxed or its records manually promoted to pass for publication. A follow-up needs trustworthy hash-bound Node execution receipts and another independent review.

## Verification

Run `.venv/bin/python -m unittest discover -s tests -v` and `node --check web/app.js` for the offline checks. These tests do not spend provider credit. Real model/browser integration and a full six-case benchmark require a configured local key, Docker images and separately recorded human replies. Model-call, clarification-wait, operation-approval and wall-clock times must remain distinguishable.

The publication copy was checked on 2026-11-23: all 35 offline tests passed; the UI JavaScript syntax check passed; the server and three historical runner help commands returned successfully. Known-credential and credential-format scans found no matches in the publication files. All 62 application/core tool source files checked against the running local snapshot were byte-identical. Git ignore checks excluded real environment files, databases, runtime ledgers and generated products. These checks validate the uploaded source snapshot; they do not establish six-case product acceptance.
