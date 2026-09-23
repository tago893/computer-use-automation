"""Prompt construction.

Kept apart from the loop so it can be read -- and argued with -- on its own. The
system prompt is fixed; the per-step prompt is a pure function of the goal, the
bindings, the step history, and the current observation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from surfaces.models import Observation

from .bindings import Bindings
from .records import StepRecord

#: Steps of history shown to the model. Older steps are summarized by count only:
#: the current observation carries the state, the history only the trajectory.
HISTORY_WINDOW: Final[int] = 12

SYSTEM_PROMPT: Final[str] = """\
You operate a legacy web console on behalf of a credit-union servicing team.
You see the page as a list of accessibility-tree elements, each with a numeric
ordinal, e.g. [7] textbox 'Member ID'. You act by calling exactly one tool per
turn, addressing elements by ordinal.

Rules:
- Ordinals are valid ONLY for the observation shown this turn. They are
  renumbered after every action. Never reuse an ordinal from an earlier turn.
- The page may be built from frames; you do not need to care which frame an
  element is in -- the ordinal is enough.
- To enter a task input or credential, type its placeholder exactly as listed
  under BINDINGS (e.g. {{member_id}}). You never know credential values and must
  not guess them.
- Every value the goal asks you to report must be captured with `read`, giving
  it a snake_case output_name. Do not put values in the finish summary instead.
- When the goal is done, call finish with outcome=success.
- If the application gives a definitive answer that makes the goal impossible
  (for example: no such record exists, or the input was rejected), that is a
  valid business outcome, not an error. Read the message that says so, then call
  finish with outcome=business_outcome and an UPPER_SNAKE outcome_code -- reuse
  a code the page shows if it shows one.
- If an action is BLOCKED by policy, do not try to work around it. Escalate.
- If you are stuck, the page is in a state you do not understand, or an
  interstitial asks for something you cannot judge, call escalate with a reason
  a human operator can act on. Escalating is always better than guessing.
- Take the most direct path. Do not explore unrelated screens.
"""


def render_step_prompt(
    *,
    goal: str,
    bindings: Bindings,
    history: Sequence[StepRecord],
    observation: Observation,
    outputs: dict[str, str],
    step_index: int,
    max_steps: int,
    note: str = "",
) -> str:
    """The user message for one step."""
    shown = history[-HISTORY_WINDOW:]
    earlier = len(history) - len(shown)
    lines = [
        f"GOAL: {goal}",
        "",
        "BINDINGS (type the placeholder, not the value):",
        bindings.describe_for_model(),
        "",
        f"STEP {step_index + 1} of at most {max_steps}.",
        "",
        "HISTORY:",
    ]
    if earlier:
        lines.append(f"  ({earlier} earlier steps omitted)")
    if shown:
        lines.extend(f"  {record.index + 1}. {record.summary()}" for record in shown)
    else:
        lines.append("  (none yet)")

    lines += ["", "OUTPUTS CAPTURED SO FAR:"]
    if outputs:
        lines.extend(f"  {name} = {value!r}" for name, value in outputs.items())
    else:
        lines.append("  (none)")

    if note:
        lines += ["", f"NOTE: {note}"]

    lines += ["", "CURRENT OBSERVATION:", observation.render()]
    # Scrub the whole prompt, not just the observation: a read value in the
    # history could echo a secret just as easily as the page can.
    return bindings.scrub("\n".join(lines))
