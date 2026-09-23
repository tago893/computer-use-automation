# Implementation Plan — Computer-Use Automation System

**Assignment:** interface.ai take-home (Assignment A — Computer-Use Automation System)
**Submission:** public GitHub repo, link emailed to assignments@interface.ai from the address applied with. Repo URL on its own line. No zip.
**Plan written:** 2026-09-22

---

## 1. What is actually being asked

One vertical slice that runs end to end:

```
goal (natural language)
  -> real LLM-driven run against a live UI
  -> typed capability artifact saved
  -> deterministic replay (no LLM) with typed inputs + outputs + error handling
  -> human takes over the SAME live session, then hands control back
  -> evidence for both runs
```

Required deliverables, exact paths:

- `/README.md` — setup, keys/config, how to run without live services, and the **demo path** (exact commands to run the agent on a goal, then replay the artifact).
- `/REPORT.md` — 1–3 pages under these **seven exact headings**:
  1. Architecture
  2. Artifact schema
  3. Determinism & error handling
  4. Heterogeneity & multi-tenant
  5. Escalation & handoff
  6. Safety
  7. Cuts
- `/evidence/` — a saved example artifact plus logs from a discovery run and a replay run. **Include at least one replay that hits an error or exceptional state.**

### Stated evaluation weighting (in their order)

1. System design — artifact schema and replay contract are central
2. Correctness of the core loop
3. Robustness & error handling
4. Human-in-the-loop escalation
5. Generalization to the real environment
6. Safety & data handling
7. Code quality
8. Communication

Breadth is explicitly **not** rewarded. Building scaling infrastructure (queues, clusters, multi-tenant plumbing) is explicitly **not** rewarded. A small, correct, well-argued system is the goal.

### The one non-negotiable

> "the discovery run has to be real. At least one genuine LLM-driven run against a live surface, with the evidence in `/evidence/`."

Everything else may be stubbed or mocked **at a clean seam**, as long as it is intentional, documented, and the seam is real.

### The line to keep in mind

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.

---

## 2. Decisions made up front

Each of these must be defensible under questioning — the ground rules say you own everything you submit and must explain any part of it in detail.

### 2.1 Stack: Python 3.10 + Playwright + Pydantic + a provider-agnostic LLM client

Python because it is the language you can defend line by line under interview pressure. Pydantic because the artifact schema is the focal point of the evaluation and Pydantic gives you a typed model **and** free JSON Schema export — which is exactly the "capability an AI agent can call" contract they ask for in 3.2.

### 2.2 Perception: accessibility tree, not the DOM

Section 3.1 says "bias toward an approach that would still work when the surface has *no* clean DOM." The glossary drops the hint: the accessibility tree is "often more stable than raw markup, and available on desktop apps too."

So: every observation is an **AX snapshot where each interactive node is assigned an ordinal**. The model picks an ordinal. **The model never emits a CSS selector.** The harness resolves ordinal -> element and records a *locator ladder* into the artifact.

This is the single most important design move in the project. It is the seam between "how we perceive and act on a surface" and "the recorded flow" that Section 3.7 asks you to articulate, and it is what makes the desktop-surface story credible rather than hand-waving.

### 2.3 Target app: a local hostile legacy app, built by us

Not a public demo site. A Flask app that stands in for a credit-union servicing console.

Why local:
- The thing being graded is how replay handles **runtime exceptional states**. Only a local app lets you inject "record not found," validation errors, session timeout, a surprise interstitial, a slow load, and a 500 on demand.
- No terms-of-service or rate-limit risk (Section 9 ground rule).
- A reviewer can run the whole repo offline.
- Section 4 explicitly blesses "an intentionally hostile surface (iframes/framesets, table-based layouts, no test IDs)."

Bonus: a second tenant config of the same app makes the cross-tenant stretch goal nearly free.

### 2.4 Model: any tool-calling LLM, behind a provider seam

Brief Section 4 is explicit that the LLM provider and model are **"explicitly your call"** and
not prescribed. So the decision recorded here is deliberately *not* a vendor: the discovery agent
depends on a tool-calling chat interface, never on an SDK.

- **Default provider: Gemini 2.5 Flash** via Google AI Studio's free tier. No credit card, native
  function calling, strong enough to navigate an AX tree. Backup: Groq (Llama 3.3 70B).
- **One adapter covers almost everything.** Gemini, Groq, GitHub Models, Cerebras, OpenRouter and
  local Ollama all speak the OpenAI-compatible protocol, so they differ only by `base_url` and
  model id. Anthropic would be a second adapter. Config lives in `config.py::llm_settings`.
- **Not coordinate-based computer use.** Coordinates do not record into a stable artifact; AX
  ordinals do. Tool-use gives structured, validated actions instead of parsed prose.

Why this is better than naming one vendor, not merely cheaper: it sharpens the central thesis.
Discovery is the expensive, non-deterministic, vendor-coupled step -- so you do it **once** and
replay deterministically forever. A free model discovering a flow that then replays perfectly with
no LLM in the loop is the architecture proving itself.

**Data-handling consequence, to be stated in REPORT.md Safety.** Free tiers may retain inputs for
product improvement. That is incompatible with real member data, which is precisely why the target
app carries only synthetic records -- and why the seam matters: swapping to a zero-retention
endpoint is a config change.

### 2.5 Architecture: single process, synchronous, file-backed artifacts

No queue, no service split, no database. Section 7 says prematurely building scaling infrastructure is not rewarded. Artifacts are JSON on disk. The operator console is the one separate process, because it genuinely has to be.

---

## 3. Phases

### Phase 0 — Research & scaffold

- GitHub code search for prior art (browser-use, Skyvern, Playwright AX-snapshot patterns) to steal proven shapes. This is to inform design, **not** to shop for a framework to depend on.
- `git init`, package layout, Pydantic base models, `.env.example`, `.gitignore` (keys out of the repo — Section 9 ground rule).

Proposed layout:

```
/target_app/        Flask hostile legacy app (+ two tenant variants)
/surfaces/          Surface protocol; WebSurface (Playwright); DesktopSurface (stub)
/agent/             LLM discovery loop (observe -> decide -> act)
/artifact/          Pydantic capability schema, save/load, parameterization
/replay/            Deterministic executor, locator ladder, error taxonomy
/policy/            Allowlist, risk classification, redaction
/handoff/           SessionController, intervention requests, operator console
/evidence/          Committed run evidence
/tests/
README.md
REPORT.md
```

### Phase 1 — The target app (`target_app/`)

Flask, server-rendered. Two tenant variants (`meridian`, `northgate`) from one template set with different branding, labels, and slightly different layout — the stand-in for two tenants running the same vendor product.

Flow: login -> member search -> member detail -> open sub-account -> confirmation screen.

Deliberate hostility: frameset or nested iframes, deeply nested table layout, no test IDs, obfuscated/generated class names. Correct ARIA labels only — which is the point: the AX tree is the reliable surface when the markup is not.

Fault injection via query flags or a control endpoint:
`?inject=timeout | notfound | validation | interstitial | slow | 500`

Seeded synthetic data only. No real PII, no real credentials.

### Phase 2 — Surface abstraction (`surfaces/`)

The seam. Protocol:

```python
class Surface(Protocol):
    def observe(self) -> Observation: ...
    def act(self, action: Action) -> ActResult: ...
    def screenshot(self) -> bytes: ...
    def close(self) -> None: ...
```

- `Observation` = ordinal-indexed AX nodes + url + title + frame context.
- `WebSurface` implements it over Playwright + CDP `Accessibility.getFullAXTree`.
- `DesktopSurface` is a documented stub with the same signature (Windows UI Automation would slot in here) — a real seam, not a TODO.

The **locator ladder** is built in this layer. Ordered rungs, most stable first:

1. role + accessible name
2. associated label / visible text
3. anchored relative path (nearest stable ancestor + role + index)
4. DOM path
5. coordinates (last resort, recorded but flagged as fragile)

### Phase 3 — Discovery agent loop (`agent/`)

observe -> decide -> act, using Anthropic tool-use.

Tools exposed to the model:
`click(ordinal)`, `type(ordinal, text)`, `navigate(url)`, `read(ordinal)`, `finish(outputs)`, `escalate(reason)`

Stopping conditions: max steps, wall-clock timeout, no-progress detection (repeated identical observations).

Every step emits a `StepRecord` carrying the ordinal **and** the fully resolved locator ladder for that element at that moment.

Guardrails are enforced **inside** this loop (before each act), not wrapped around it.

### Phase 4 — Artifact schema (`artifact/`) — GO DEEP

Versioned Pydantic model. Sketch:

```
schema_version          str
capability_version      str
id / name / description
target:
  app_id, tenant_variant, entry_point, surface_kind
inputs                  JSON Schema (typed params the agent supplies)
outputs                 JSON Schema (typed shape the agent gets back)
preconditions           list[Condition]
steps: [
  { index, action, target: LocatorLadder, value_binding,
    checkpoint: Condition, timeout_ms, retry_policy, risk_class }
]
success_condition       Condition
known_outcomes          list[BusinessOutcomeDetector]   # e.g. "no such member"
provenance              { discovery_run_id, model, timestamp }  # NOT the raw transcript
approval_state          draft | approved
```

Parameterization: concrete values observed during discovery are lifted into bindings (`{{member_id}}`), and concrete routes canonicalize (`/member/12345` -> `/member/:id`). This is what makes the artifact reusable rather than a one-shot recording, and it feeds directly into the multi-tenant section of the report.

Explicitly decoupled from the raw model transcript — 3.2 requires this.

### Phase 5 — Deterministic replay (`replay/`) — GO DEEP

No LLM anywhere in the decision path.

Walk the locator ladder rung by rung; **record which rung matched**. A step that used to resolve at rung 1 and now resolves at rung 3 is a drift signal worth reporting.

Result contract as a discriminated union:

- `Success(outputs)`
- `BusinessOutcome(code, detail)` — a declared, expected answer the caller needs. "No such member" is an answer, not a crash. The brief names conflating these as **the most common design mistake on this project**.
- `Recoverable` — handled internally and logged: dismiss a known interstitial, bounded wait/retry on a transient slow load, single re-resolve of a locator.
- `HardFailure(step_index, expected, observed, evidence_refs)` — stops and surfaces a debuggable error.

Every checkpoint is asserted, never assumed. Waits are condition-based, never `sleep`.

### Phase 6 — Safety (`policy/`)

- **Allowlist**: permitted origins/routes and permitted action types. Checked before every act, in both discovery and replay. The agent cannot act outside it.
- **Risk classification**: read/navigate = safe and reversible. Form submission / state change / anything irreversible = risky. Risky steps in unattended replay are blocked unless `approval_state == approved`; otherwise they escalate to a human. Justify this choice in the report (it is the conservative option and the brief says the risky class must be handled conservatively).
- **Redaction**: a single layer every disk write passes through — artifacts, logs, AX snapshots, screenshots metadata. Patterns for account numbers, SSN, card numbers, credentials, tokens. Never persist raw sensitive data (3.4).

### Phase 7 — Escalation & handoff (`handoff/`) — GO DEEP

`SessionController` with an explicit owner state (`automation | operator | none`), a pause barrier, and a lease.

On stuck / risky / unrecoverable:
1. Write an `InterventionRequest` carrying enough context to act on: capability + goal, current step index, current state (screenshot + redacted AX snapshot), and **why** it stopped.
2. Park the automation at that step. Cede control.
3. Minimal operator console (FastAPI, two actions: **Claim control** / **Release control**) operating **the same live headed Playwright session** — not a fresh one. This is explicit in 3.6.
4. Capture what the human did via an injected DOM event recorder, appended to the run log.
5. On release, automation resumes from the parked step with context and evidence preserved across the handoff.

Real CDP-screencast co-browsing is documented as the next step — 3.6 says a full real-time operator console is out of scope, but the handoff mechanism and control-transfer model must be real and well-reasoned.

### Phase 8 — Evidence & write-up

`/evidence/` must contain:
- discovery run: structured log + the artifact it produced
- clean replay: log + structured success result
- replay hitting a **business outcome** (`notfound`)
- replay hitting a **hard failure** (injected)
- one escalation -> human takeover -> resume transcript

`/README.md`: setup, config/keys, how to run without live services, exact demo commands.

`/REPORT.md`: the seven headings, verbatim, in order.

Tests where they count (not coverage theatre):
- artifact schema round-trip and version handling
- locator ladder resolution and fallback ordering
- error taxonomy classification (business outcome vs recoverable vs hard failure)
- policy allowlist enforcement and redaction

### Phase 9 — Defense pass

Reread every decision and confirm you can defend it cold, without notes. If a piece cannot be defended, simplify it until it can. This is not optional polish — Section 9 makes it a condition of submission.

---

## 4. Stretch goals

Do **zero** of these until Phases 1–8 are complete. Then at most one.

First pick if there is time: **canonicalization / cross-tenant reuse** — one artifact recorded against `meridian` applied to `northgate` with per-variant overrides. It is nearly free given Phase 1 and it directly strengthens the weakest-by-default section of the report (Heterogeneity & multi-tenant).

---

## 5. Risks

| Level | Risk | Mitigation |
|---|---|---|
| HIGH | No LLM API key yet. Phase 3 is blocked and the real run is mandatory. | Gemini/Groq free tier needs no card; get one before Phase 3. Phases 0-2 build and test without any key. |
| HIGH | Scope creep into Section 8 stretch goals. | Hard rule: nothing from Section 8 until Phases 1–8 ship. |
| MEDIUM | Handoff on the same live session is the fiddliest engineering. | Headed browser + claim/release lease. Not co-browsing. Document the seam. |
| MEDIUM | Over-engineering the artifact schema. | It must fit on one screen in REPORT.md. If it does not, cut it. |
| MEDIUM | Building a polished subset instead of a thin complete slice. | Section 5 is explicit: cut depth, not whole capabilities. |
| LOW | Playwright first-run browser download on Windows (~1–2 min). | Note it in README setup. |

**Complexity: MEDIUM-HIGH.** Roughly 3–5 focused days. Phases 4, 5 and 7 deserve about half of that. Phase 1 is the cheap enabler that makes Phase 5 gradeable at all.

---

## 6. Pickup checklist for the next session

- [x] ~~Confirm an LLM key is available~~ -> deferred to Phase 3; see `.env.example`
- [x] Phase 0: research pass + scaffold + `git init`
- [x] Phase 1: target app with fault injection and two tenant variants
- [x] Phase 2: `Surface` protocol, `WebSurface`, locator ladder
- [ ] Phase 3: discovery loop with tool-use + guardrails inline
- [ ] Phase 4: artifact schema (deep)
- [ ] Phase 5: deterministic replay + error taxonomy (deep)
- [ ] Phase 6: allowlist, risk classes, redaction
- [ ] Phase 7: SessionController + operator console + resume (deep)
- [ ] Phase 8: evidence, README, REPORT (seven headings)
- [ ] Phase 9: defense pass
- [ ] Push to public GitHub repo, email link to assignments@interface.ai
