# Project: interface.ai take-home — Computer-Use Automation System

Read `PLAN.md` first. It contains the full brief summary, the decisions already made, and the phase breakdown. The original assignment PDF is in `assignment/`.

## The one-line goal

Build a system where an LLM discovers how to do a task in a legacy UI once, saves it as a typed reusable capability artifact, and then replays that artifact deterministically with no LLM in the loop — with real error handling, safety guardrails, and a human-takeover path on the same live session.

## Hard rules

1. **The discovery run must be real.** At least one genuine LLM-driven run against a live surface, with evidence committed to `/evidence/`. Everything else may be stubbed at a clean, documented seam.
2. **Thin and complete beats polished and partial.** Every core requirement in Section 3 of the brief gets a real version. Cut depth, not capabilities.
3. **No stretch goals** (brief Section 8) until the full vertical slice works end to end.
4. **Business outcome != failure.** "No such member" is an answer the caller needs, not a crash. The brief names conflating these as the most common design mistake on this project.
5. **Defend everything.** Every decision must be explainable cold in an interview. If it cannot be defended, simplify it until it can.
6. **No secrets in the repo.** No real PII, no real credentials, no automating real bank systems.

## Deliverables, exact paths

- `/README.md` — setup, config, how to run without live services, exact demo commands
- `/REPORT.md` — seven headings verbatim: Architecture / Artifact schema / Determinism & error handling / Heterogeneity & multi-tenant / Escalation & handoff / Safety / Cuts
- `/evidence/` — artifact + discovery run log + replay log, including at least one replay that hits an error or exceptional state

## Stack decisions already made

Python 3.10, Playwright, Pydantic, `openai` SDK pointed at **any** OpenAI-compatible provider.
The LLM sits behind a **provider seam** (`config.py::llm_settings`) — the agent depends on a
tool-calling interface, never a vendor SDK. Default is Gemini 2.5 Flash on the free tier; Groq,
GitHub Models, Cerebras, OpenRouter and local Ollama need only a different `base_url`. Brief
Section 4 puts provider choice explicitly in our hands. Rationale in `PLAN.md` §2.4.
Perception is the **accessibility tree with ordinal-indexed nodes** — the model picks an ordinal, never a CSS selector. The harness resolves the ordinal and records a locator ladder. Rationale in `PLAN.md` §2.2.
Target app is a **locally built hostile legacy Flask app**, not a public demo site. Rationale in `PLAN.md` §2.3.

## Where the build is

Phases 0-2 are **done and tested** (41 tests green): scaffold + config seam, the hostile Flask app
with two tenants and six injectable faults, and the `Surface` seam with `WebSurface` (CDP
accessibility tree, ordinal stamping, validated locator ladder) plus the documented
`DesktopSurface` stub. Next: Phase 3, the discovery agent — which is the first thing needing a key.

Use the venv: `.venv/Scripts/python.exe`. Run the app with `python -m target_app`; inspect
perception with `python scripts/observe.py --walk`.
