"""Print what the agent would see. No LLM, no API key.

    python scripts/observe.py                       # meridian login page
    python scripts/observe.py --tenant northgate
    python scripts/observe.py --walk                # drive the full flow
    python scripts/observe.py --walk --inject 500   # ...with a fault armed

This is the cheapest way to check the Phase 2 claim by eye: every line of output is
a node the model could address by ordinal, on a surface with no test hooks at all.
``--walk`` also prints the *validated* locator ladder recorded for each action,
which is what a Phase 4 artifact will store.

Assumes the target app is already running (``python -m target_app``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import app_settings  # noqa: E402
from surfaces import ActionKind, WebSurface  # noqa: E402
from surfaces.models import Action, Observation  # noqa: E402
from target_app.tenants import get_tenant  # noqa: E402


def _show(observation: Observation, heading: str) -> None:
    print(f"\n{'=' * 78}\n{heading}\n{'=' * 78}")
    print(observation.render())


def _pick(observation: Observation, role: str, name: str) -> int:
    for node in observation.nodes:
        if node.role == role and node.name == name:
            return node.ordinal
    raise SystemExit(
        f"could not find {role} {name!r}. Visible nodes:\n" + observation.render()
    )


def _step(surface: WebSurface, action: Action, label: str) -> None:
    """Perform one action and report the ladder it recorded."""
    result = surface.act(action)
    status = "ok" if result.ok else f"FAILED: {result.detail}"
    print(f"\n-- {label}: {action.describe()} -> {status}")
    if result.ladder is not None:
        print(f"   ladder:     {result.ladder.describe()}")
    if result.resolution is not None:
        print(f"   resolution: {result.resolution.describe()}")


def walk(surface: WebSurface, tenant_id: str, inject: str | None) -> None:
    """Drive login -> search -> member -> sub-account by ordinal alone."""
    tenant = get_tenant(tenant_id)

    observation = surface.observe()
    _show(observation, f"1. LOGIN ({tenant.brand})")
    _step(
        surface,
        Action(
            kind=ActionKind.TYPE,
            ordinal=_pick(observation, "textbox", "Operator ID"),
            text=tenant.username,
        ),
        "type operator id",
    )
    observation = surface.observe()
    _step(
        surface,
        Action(
            kind=ActionKind.TYPE,
            ordinal=_pick(observation, "textbox", "Passphrase"),
            text=tenant.password,
        ),
        "type passphrase",
    )
    observation = surface.observe()
    _step(
        surface,
        Action(
            kind=ActionKind.CLICK,
            ordinal=_pick(observation, "button", "Sign In"),
        ),
        "submit login",
    )

    observation = surface.observe()
    _show(observation, "2. CONSOLE (nav frame + main frame)")

    if inject:
        # Arm a fault the way the replay tests will, then continue regardless.
        surface.page.request.post(
            f"{surface.page.url.split('/t/')[0]}/__control__/inject",
            data={"mode": inject, "count": 1},
        )
        print(f"\n-- armed fault {inject!r} for the next pane render")

    _step(
        surface,
        Action(
            kind=ActionKind.TYPE,
            ordinal=_pick(observation, "textbox", tenant.label_member_id),
            text="M-10001",
        ),
        "type member id",
    )
    observation = surface.observe()
    _step(
        surface,
        Action(
            kind=ActionKind.CLICK,
            ordinal=_pick(observation, "button", tenant.label_submit_search),
        ),
        "run search",
    )

    observation = surface.observe()
    _show(observation, "3. RESULTS")

    try:
        open_member = _pick(observation, "link", "Open Member")
    except SystemExit:
        print("\n(no member row -- a fault or business outcome intervened; stopping)")
        return

    _step(
        surface,
        Action(kind=ActionKind.CLICK, ordinal=open_member),
        "open member",
    )
    observation = surface.observe()
    _show(observation, "4. MEMBER DETAIL (products table is a third frame level)")

    deep = [n for n in observation.nodes if len(n.frame_path) > 1]
    print(f"\nnodes in the nested frame: {len(deep)}")
    for node in deep[:6]:
        print(f"   {node.render()}")


def main() -> None:
    settings = app_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=settings.tenant)
    parser.add_argument("--walk", action="store_true", help="drive the whole flow")
    parser.add_argument("--inject", help="arm a fault mid-flow (with --walk)")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()

    url = f"{settings.tenant_url(args.tenant)}/login"
    print(f"opening {url}")

    with WebSurface(headless=not args.headed) as surface:
        try:
            surface.start(url)
        except Exception as exc:
            raise SystemExit(
                f"could not open {url}: {exc}\n"
                "Is the target app running? Start it with: python -m target_app"
            ) from exc

        if args.walk:
            walk(surface, args.tenant, args.inject)
        else:
            _show(surface.observe(), f"LOGIN PAGE ({args.tenant})")


if __name__ == "__main__":
    main()
