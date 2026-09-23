"""Run one real, LLM-driven discovery against the target app.

    python scripts/discover.py \\
        --goal "Find member M-10001 and report the balance of their Share Savings account" \\
        --input member_id=M-10001 --provider anthropic

Writes ``runs/<run_id>/``:

* ``discovery.jsonl`` -- every step as it happened (append-only, flushed per line)
* ``recording.json``  -- the finished recording Phase 4 compiles into an artifact
* ``*.png``           -- start, final, and failed-step screenshots

``runs/`` is git-ignored. Evidence is copied into ``/evidence`` deliberately, not
written there by accident.

Credentials come from ``TARGET_OPERATOR_ID`` / ``TARGET_OPERATOR_PASSPHRASE``. If
those are unset, the local target app's own *synthetic* seed credentials are used
and the run says so -- they are demo values for a fake app, not secrets. Either
way the model only ever sees placeholders.

Assumes the target app is already running (``python -m target_app``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import Bindings, DiscoveryAgent, Limits, RunLog, build_llm  # noqa: E402
from config import REPO_ROOT, ConfigError, app_settings, llm_settings  # noqa: E402
from policy.guard import ActionPolicy, Allowlist  # noqa: E402
from surfaces import WebSurface  # noqa: E402
from target_app.tenants import get_tenant  # noqa: E402

logger = logging.getLogger("discover")


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"--input must look like name=value, got {pair!r}")
        inputs[name.strip()] = value.strip()
    return inputs


def _credentials(tenant_id: str) -> dict[str, str]:
    operator_id = os.getenv("TARGET_OPERATOR_ID", "")
    passphrase = os.getenv("TARGET_OPERATOR_PASSPHRASE", "")
    if operator_id and passphrase:
        return {"operator_id": operator_id, "operator_passphrase": passphrase}
    seed = get_tenant(tenant_id)
    logger.warning(
        "TARGET_OPERATOR_ID/PASSPHRASE unset: using the local target app's "
        "synthetic seed credentials for tenant %r",
        tenant_id,
    )
    return {"operator_id": seed.username, "operator_passphrase": seed.password}


def main() -> int:
    app = app_settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--goal", required=True)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--tenant", default=app.tenant)
    parser.add_argument("--provider", help="override LLM_PROVIDER")
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument(
        "--allow-risky",
        action="store_true",
        help="permit irreversible actions (default: blocked, the agent escalates)",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "runs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        settings = llm_settings(provider=args.provider, model=args.model)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from exc

    bindings = Bindings(
        inputs=_parse_inputs(args.input), secrets=_credentials(args.tenant)
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"disc-{args.tenant}-{stamp}"
    start_url = f"{app.tenant_url(args.tenant)}/login"

    with RunLog(args.out / run_id, scrub=bindings.scrub) as log, WebSurface(
        headless=not args.headed
    ) as surface:
        try:
            surface.start(start_url)
        except Exception as exc:
            raise SystemExit(
                f"could not open {start_url}: {exc}\n"
                "Is the target app running? Start it with: python -m target_app"
            ) from exc

        agent = DiscoveryAgent(
            surface=surface,
            llm=build_llm(settings),
            policy=ActionPolicy(
                Allowlist.for_tenant(app.base_url, args.tenant),
                allow_risky=args.allow_risky,
            ),
            bindings=bindings,
            limits=Limits(max_steps=settings.max_steps, wall_clock_s=settings.wall_clock_s),
            log=log,
        )
        result = agent.run(args.goal, run_id=run_id)

        recording = {
            **result.to_dict(),
            "tenant": args.tenant,
            "start_url": start_url,
            "inputs": dict(bindings.inputs),
            "secrets": sorted(bindings.secrets),
        }
        (log.run_dir / "recording.json").write_text(
            bindings.scrub(json.dumps(recording, indent=2)), encoding="utf-8"
        )

    print(f"\nrun      {run_id}")
    print(f"model    {result.model_id}")
    print(f"status   {result.status.value} -- {result.reason}")
    if result.outcome_code:
        print(f"outcome  {result.outcome_code}")
    for name, value in result.outputs.items():
        print(f"output   {name} = {value!r}")
    print(f"steps    {len(result.steps)}  tokens in/out {result.input_tokens}/{result.output_tokens}")
    print(f"log      {log.path}")
    return 0 if result.status.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
