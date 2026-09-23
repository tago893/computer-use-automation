# Computer-Use Automation System

Many businesses still run on old web applications with no API: back-office consoles, servicing screens, admin panels. The only way to automate them is to click through the UI the way a person would.

LLM agents can do that, but they're slow and expensive, and they don't always make the same choice twice. You wouldn't want one making fresh decisions every time it touches a bank's member records.

This project splits the job in two:

1. **Learn once.** An LLM agent is given a task ("look up member M-10003 and read their balance") and works out how to do it in the live app, step by step.
2. **Save it.** The steps it took are saved as a typed, reusable **capability artifact**. The artifact is a file that says which fields to fill, which buttons to press, how to find each element again, what counts as success, and which step is risky.
3. **Replay without the LLM.** Every run after that replays the artifact deterministically, with no model involved. It's fast and free, and it does the same thing every time.

Around that core are the parts that make it safe to use for real:

- **Error handling** that tells "the member doesn't exist" (a valid answer) apart from "the server crashed" (a failure).
- **Guardrails** that keep the agent inside the app and stop risky actions like transfers.
- **A human-takeover path.** When automation gets stuck, a person takes over the *same live browser session* and hands it back.

> **Status: in progress.** The target app, the perception layer, the locator system, the safety guard and the discovery loop are built and tested (128 tests passing). A first real LLM run was attempted but stopped before step 1 because the API account was out of credit, so there's no real run yet. The artifact format, replay and handoff are still to do. See [What's left](#whats-left).

---

## How it works

```text
            DISCOVERY (LLM, once)                           REPLAY (no LLM, every time)

  task + inputs ──► agent loop                   artifact + inputs ──► replay executor
                    observe ─► decide ─► act                           find element ─► act ─► check
                       ▲          │                                        │
                       └──────────┘                                        ├─► Success(outputs)
                          │                                                ├─► BusinessOutcome("MEMBER_NOT_FOUND")
                          ▼                                                ├─► Recoverable (retry / dismiss, then continue)
                 capability artifact  ───────────────────────────────►     └─► HardFailure(step, evidence) ─► human takeover
                 (typed JSON: steps, locators,
                  inputs, outputs, risk per step)

  Policy guard (allowlist + risk check) runs before every action, in both phases.
```

The main pieces, and where they live:

| Part | Folder | What it does |
|---|---|---|
| Target app | `target_app/` | A deliberately awkward legacy web app to automate. Built locally so it can be made to fail on demand. |
| Surface | `surfaces/` | How the system sees and acts on a UI. It reads the accessibility tree, performs clicks and typing, and records durable locators. |
| Agent | `agent/` | The LLM side: the discovery loop, provider adapters, the tools the model may call, and secret placeholders. |
| Policy | `policy/` | Checks every action before it runs: is it inside the app, and is it risky? |
| Artifact | `artifact/` | The saved capability format. *(not started)* |
| Replay | `replay/` | Runs an artifact with no LLM and classifies the result. *(not started)* |
| Handoff | `handoff/` | Pauses automation, lets a person take over the live session, then resumes. *(not started)* |

---

## Quick start

Python 3.10+. From the repo root:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS / Linux

.venv/Scripts/python.exe -m playwright install chromium        # ~1-2 min on first run
```

**Nothing that runs today needs an API key or the internet.** The target app, the perception layer and the tests all run locally.

**1. Start the target app**

```bash
.venv/Scripts/python.exe -m target_app
# -> http://127.0.0.1:5099/t/meridian/login
```

Sign in with the demo credentials shown on the login page: `svc.agent` / `demo-pass-1` for the meridian tenant, or `ops.user` / `demo-pass-2` for northgate. Member ids `M-10001` to `M-10005` exist. `M-99999` returns "no such member".

**2. See what the agent sees** (no LLM needed)

```bash
.venv/Scripts/python.exe scripts/observe.py                    # the login page, as the agent sees it
.venv/Scripts/python.exe scripts/observe.py --walk             # drive the whole lookup flow
.venv/Scripts/python.exe scripts/observe.py --walk --headed    # ...and watch it in a browser window
.venv/Scripts/python.exe scripts/observe.py --tenant northgate
```

**3. Run the tests**

```bash
.venv/Scripts/python.exe -m pytest -q                       # all 128
.venv/Scripts/python.exe -m pytest -m "not integration" -q  # skip the browser tests
```

### Configuring the LLM (for the discovery agent)

Copy `.env.example` to `.env`. `.env` is git-ignored.

The agent code never imports a vendor SDK directly. It talks to one small interface ("here are the tools, pick one"), and `config.py` picks the adapter:

| `LLM_PROVIDER` | Default model | Key variable |
|---|---|---|
| `gemini` (default, free tier) | gemini-2.5-flash | `GEMINI_API_KEY` |
| `groq` | llama-3.3-70b-versatile | `GROQ_API_KEY` |
| `github` | gpt-4o-mini | `GITHUB_TOKEN` |
| `anthropic` | claude-sonnet-5 | `ANTHROPIC_API_KEY` |
| `cerebras`, `openrouter`, `openai` | see `config.py` | `LLM_API_KEY` |
| `ollama` (local) | qwen2.5:7b | `LLM_API_KEY` (any value; Ollama ignores it) |

Every provider also accepts `LLM_API_KEY` in place of its own key variable. `LLM_MODEL` and `LLM_BASE_URL` override the defaults.

---

## The target app

`target_app/` is a stand-in for a credit-union servicing console, and it's built to be hard to automate:

- iframes nested three levels deep
- table-based layout
- machine-generated class names
- ASP.NET-WebForms-style field names
- no test ids of any kind

Its accessibility labels are correct, though, so the accessibility tree is the one reliable way to read it.

**Two tenants,** `meridian` and `northgate`, run the same product configured differently. The same field has a different label (`Member ID` vs `Account Holder Number`), northgate adds an extra layer of layout nesting, and it inserts a disclosure step that a meridian-recorded artifact has never seen.

**Six faults you can switch on**, each testing a different branch of replay's error handling:

| Fault | What the app does | Replay should treat it as |
|---|---|---|
| `notfound` | "No such member", HTTP 200 | **BusinessOutcome**: an answer for the caller |
| `validation` | input rejected, with a reason | **BusinessOutcome** |
| `slow` | 4-second stall | **Recoverable**: wait for the page, with a time limit |
| `interstitial` | surprise verification screen | **Recoverable**: dismiss it and carry on |
| `timeout` | session expired, bounced to login | **Recoverable** once, then a hard failure |
| `500` | server error page | **HardFailure**: stop and keep the evidence |

Turn one on for a single request with `?inject=slow`, or for several requests through the control endpoint:

```bash
curl -X POST http://127.0.0.1:5099/__control__/inject \
     -H 'Content-Type: application/json' -d '{"mode":"500","count":1}'
```

The agent isn't allowed to call `/__control__`. It has to cope with faults, not switch them off.

**A business outcome is not a failure.** "No such member" comes back as HTTP 200 with `data-outcome="MEMBER_NOT_FOUND"` and no error marker. A real fault comes back as 500 with `data-error` and no outcome code. You can tell them apart from the page itself, and tests pin that distinction down. Mixing the two up is the most common way automation like this goes wrong.

---

## How the system sees a page

Each observation is a list of accessibility-tree nodes with a number in front of each:

```text
[12] textbox 'Member ID' (frame: mainframe)
[13] button 'Search' (frame: mainframe)
[67] link 'Open Sub-Account SA-4471' (frame: mainframe/detailframe)
```

The model picks a number. It never writes a CSS selector, and it's never shown one. The harness turns the number back into a real element.

Three Chromium behaviors shaped this. Each was found by testing, and each would have been a silent bug if missed:

1. `Accessibility.getFullAXTree` **doesn't look inside iframes.** On the console page it returns 13 nodes, and the search form isn't one of them. It has to be called once per frame.
2. **Same-origin iframes share one CDP session,** so one session on the page reaches every frame, listed with `Page.getFrameTree`.
3. **Playwright can't use CDP node ids.** The bridge is a temporary `data-ax-ord` attribute stamped on the element, which Playwright can then find exactly. The stamp is cleared before every observation and never ends up in an artifact.

### Finding the same element again later: the locator ladder

The numbers only mean something for one snapshot, but an artifact has to work next week too. So every recorded step stores several independent ways to find its element, most durable first:

| # | Method | Survives | Breaks on |
|---|---|---|---|
| 1 | role + accessible name | restyling, reordering, class changes, framework rewrites | relabelling |
| 2 | label / visible text | markup changes | relabelling |
| 3 | path relative to a nearby anchor | page-level changes | rows being reordered |
| 4 | DOM path | little | any structural edit |
| 5 | screen coordinates | nothing | any layout change |

- **Every method is tested when it's recorded.** A method is kept only if it finds exactly the element the agent used. Methods that miss, or that match more than one element, are dropped. Replay only ever tries methods that were seen to work.
- **Replay reports which method matched.** If a step used to match with method 1 and now needs method 3, it still works, but the app has changed, and that's worth knowing before it breaks.
- **Ambiguity is refused, never guessed.** Two rows on the sub-account grid have the same visible text. Silently taking the first match is how automation ends up acting on the wrong account.

Method 1 comes first because it's the only one that also works on desktop apps. `surfaces/desktop.py` maps it to Windows UI Automation.

### Secrets never reach the model

The model never sees a password. It's told a placeholder like `{{secret:operator_passphrase}}` exists and types that. The harness puts the real value in at the moment of typing and logs only the placeholder. Task inputs work the same way (`{{member_id}}`), so a recorded run is already parameterized and replays with any member id.

---

## Guardrails

These are written for this project. There's no guardrails library (such as LangChain's or Guardrails AI), because those check the *text* a model writes, and the risk here is in the *actions* it takes in a live app. Every action goes through the checks below before it runs, in discovery and in replay alike.

| # | Guardrail | What it stops | Where | State |
|---|---|---|---|---|
| 1 | **Allowlist** | The agent can only visit this app's own origin and the current tenant's routes (`/t/<tenant>/...`). Navigating anywhere else is refused, and if a click lands the browser outside the list, the run stops. The fault switch `/__control__` is always blocked: the agent has to cope with faults, not turn them off. | `policy/guard.py` | written |
| 2 | **Risky-action check** | A click whose label contains an irreversible verb (confirm, authorize, approve, transfer, pay, delete, remove, withdraw, refund, reverse, void, close account…) is marked **risky** and refused unless the run was explicitly allowed risky actions. The refusal tells the model to call `escalate`, which hands over to a person. It's deliberately cautious: a wrong block costs one human click, a wrong allow can move money. | `policy/guard.py` | written |
| 3 | **Strict tool calls** | The model must answer with exactly one tool call. Plain text is treated as a protocol error, never guessed at. Every argument is checked: element numbers must be integers, text is capped at 500 characters, and output names and outcome codes must match fixed patterns. A bad call is sent back to the model as an error, so nothing ever runs halfway. | `agent/tools.py` | written |
| 4 | **Secrets never reach the model** | Passwords go in as placeholders (`{{secret:operator_passphrase}}`) and are swapped for the real value only at the moment of typing. If the page ever shows a secret value, it's removed from what the model sees. Logs contain only the placeholder. | `agent/bindings.py` | written |
| 5 | **Run limits** | Discovery stops after 25 steps or 5 minutes (`AGENT_MAX_STEPS`, `AGENT_WALL_CLOCK_S`), or when the page stops changing between steps. | `agent/loop.py` | written, tested |
| 6 | **Approval before replay** | In replay, risk isn't guessed again from labels. Each step in the artifact carries a `risk_class`, and a risky step only runs unattended if a person has marked the artifact `approved`. | `artifact/`, `replay/` | planned |
| 7 | **Redaction** | One layer that every disk write goes through (artifacts, logs, page snapshots), removing account numbers, SSNs, card numbers and tokens. | `policy/` | planned |

The risky-action check matches words in labels, so it's a heuristic, and it's treated as one. It's the safety net for discovery, where no artifact exists yet. In replay the decision comes from the human-approved artifact, not from matching words.

## Human takeover

*Planned, not built yet. The model's `escalate` tool is the only part that exists today.*

**When it happens:**

- The model calls `escalate(reason)` during discovery because it's stuck.
- The guard refuses a risky action the run isn't allowed to take.
- Replay hits a hard failure, or a recoverable problem that didn't clear on retry.

**What happens:**

1. **Stop and explain.** The system writes an intervention request with:
   - which capability was running, and for what goal
   - the step it stopped on
   - why it stopped
   - a screenshot and a redacted snapshot of the page
2. **Park.** Automation pauses at that step and gives up control of the browser. A session controller always knows who owns the browser: `automation`, `operator`, or no one. Only the owner can act, so the two can never click at the same time.
3. **Take over the same session.** The operator opens a small console and clicks **Claim control**. They get the *same live browser*, still logged in and on the same page, not a fresh one. Starting over would lose the state that caused the problem.
4. **Record what the person does.** A small script injected into the page logs the operator's clicks and typing into the run log, so the record shows exactly what they changed.
5. **Hand back.** The operator clicks **Release control**. Automation re-reads the page and resumes from the step it parked on, with the log continuous across the handoff.

Live screen sharing (co-browsing) is out of scope. The console only needs claim and release, but the control transfer itself is real.

---

## What's done

| Area | State |
|---|---|
| Project setup, config, provider switching | done |
| `target_app/`: legacy app, 2 tenants, 6 faults | done, tested |
| `surfaces/`: accessibility-tree perception, actions, validated locator ladder, desktop stub | done, tested |
| `agent/`: the discovery loop, provider adapters (OpenAI-compatible, Anthropic, and a scripted fake for tests), the model's tools (`click`, `type_text`, `read`, `navigate`, `finish`, `escalate`), secret placeholders, run log and step records | done, tested with the scripted model |
| `policy/guard.py`: allowlist of routes, risky-action check | done, tested |
| `scripts/discover.py`: runs one real discovery against the app | written; the first real run stopped at step 0 (API out of credit) |

## What's left

**1. First real discovery run.** The loop is built. What's missing is a successful real run:
   - add API credit, or switch `LLM_PROVIDER` to the free Gemini tier
   - run `scripts/discover.py` against the live app
   - copy the run log and recording from `runs/` into `evidence/`

**2. Artifact format (`artifact/`).** A versioned Pydantic model holding:
   - the target app and tenant
   - typed inputs and outputs
   - preconditions
   - the steps, each with its locator ladder, input binding, checkpoint, timeout, retry rule and risk class
   - a success condition
   - known business outcomes
   - provenance
   - an approval state (`draft` / `approved`)

   Values seen during discovery become placeholders, and URLs become patterns (`/member/12345` → `/member/:id`). It also needs round-trip and version tests.

**3. Replay (`replay/`).** Run an artifact with no LLM:
   - walk each step's locator ladder and report which method matched
   - wait on conditions, never fixed sleeps
   - return exactly one of `Success`, `BusinessOutcome`, `Recoverable` or `HardFailure`
   - tests that drive each of the six faults into the right result

**4. Safety (`policy/`).**
   - Block risky steps in unattended replay unless the artifact is approved.
   - Add a redaction layer that every disk write goes through (artifacts, logs, snapshots), removing account numbers, SSNs, card numbers and tokens.

**5. Handoff (`handoff/`).** A session controller that records who owns the browser (automation, operator, or no one). When automation gets stuck, it:
   - writes an intervention request saying what it was doing and why it stopped, with a screenshot and a redacted snapshot
   - parks at that step
   - lets a person **claim** the same live browser from a small console
   - records what the person did
   - resumes from the parked step when the person **releases** control

**6. Evidence and write-up.** Fill `evidence/` with:
   - a discovery run and the artifact it produced
   - a clean replay
   - a replay that hits "no such member"
   - a replay that hits a hard failure
   - one takeover-and-resume transcript

   Then write `REPORT.md`, covering architecture, the artifact schema, determinism and error handling, multi-tenant support, escalation and handoff, safety, and what was cut.

---

## Project layout

```text
config.py            settings and LLM provider selection, checked at startup
target_app/          the legacy app being automated
surfaces/
  models.py          AXNode, Observation, Action, ActResult (all immutable)
  locators.py        the locator ladder (no browser code, on purpose)
  protocol.py        the Surface interface
  ax.py              accessibility-tree reading over CDP
  web.py             the Playwright implementation, the only module that knows about browsers
  desktop.py         documented stub and UI Automation mapping
agent/
  llm.py             provider adapters behind one tool-calling interface
  tools.py           tool definitions and argument checking
  bindings.py        secret and input placeholders
policy/guard.py      allowlist and risk classification
artifact/  replay/  handoff/     not started
scripts/observe.py   see what the agent sees, with no LLM
scripts/discover.py  run one real LLM discovery against the app
tests/               128 tests
```

## Safety

No real credentials, no real personal data, and no real system is automated. All member data in `target_app/data.py` is made up. Account numbers are formatted to *look* sensitive so the redaction layer has realistic patterns to catch. The app only listens on localhost and runs with the Werkzeug debugger off. `.env` is git-ignored.
