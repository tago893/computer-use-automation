"""``WebSurface`` -- the Playwright implementation of the surface seam.

This is the only module in the project that knows what a browser is. The discovery
agent and the replay executor both talk to ``Surface``, so neither can develop a
dependency on Playwright without that showing up as an import here.

Two behaviors are worth reading closely, because they are where the design earns
its keep.

**Ladder validation at record time.** When an action is performed, the ladder for
the element acted upon is checked rung by rung against the live element -- the
element that carries the perception stamp. Rungs that do not resolve back to that
exact element, or that resolve ambiguously, are *dropped* before the ladder is
returned for recording. An artifact therefore contains only rungs that were
observed to work on the element they claim to address. Replay is walking a ladder
that was true at least once, rather than a set of plausible guesses.

**Bounded, condition-based waits.** There is no ``sleep`` anywhere in this module.
Every wait is for a condition with a deadline, which is what makes replay both
deterministic and honest about slow surfaces.
"""

from __future__ import annotations

import logging
from typing import Final

from playwright.sync_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Frame,
    Locator,
    Page,
    Playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from . import ax
from .locators import LocatorLadder, Resolution, Rung, RungKind
from .models import (
    Action,
    ActionKind,
    ActResult,
    Observation,
    OrdinalNotFound,
    SurfaceError,
)

logger = logging.getLogger(__name__)

#: Per-action ceiling. Long enough to absorb the injected ``slow`` fault,
#: short enough that a genuinely stuck surface fails rather than hangs.
ACTION_TIMEOUT_MS: Final[int] = 8_000

#: Extra grace for the page to settle after a navigating action.
SETTLE_TIMEOUT_MS: Final[int] = 6_000


class WebSurface:
    """A Chromium page, perceived through its accessibility tree.

    Headed by default. The human-takeover path in Phase 7 requires the operator to
    work in *this* session rather than a fresh one, which is only possible if there
    is a visible window to take over.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        viewport: tuple[int, int] = (1280, 900),
    ) -> None:
        self._headless = headless
        self._viewport = viewport
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._session: CDPSession | None = None
        self._snapshot: ax.Snapshot | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, url: str) -> WebSurface:
        """Launch the browser and open ``url``."""
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]}
        )
        self._page = self._context.new_page()
        self._session = self._context.new_cdp_session(self._page)
        for domain in ("DOM", "Accessibility", "Page"):
            self._session.send(f"{domain}.enable")
        self._page.goto(url, wait_until="domcontentloaded")
        return self

    def close(self) -> None:
        """Tear down in reverse order. Idempotent, and never raises."""
        if self._closed:
            return
        self._closed = True
        for shutdown in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                shutdown()
            except Exception as exc:
                # Teardown must not raise, but a browser that will not close is
                # a leaked process -- worth a line in the log.
                logger.debug("surface teardown step failed: %s", exc)

    def __enter__(self) -> WebSurface:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- accessors ---------------------------------------------------------

    @property
    def page(self) -> Page:
        """The live page.

        Exposed for the Phase 7 operator handoff, which by definition needs the
        same session a human can see, and for evidence capture. Deliberately not
        used by the agent or replay layers.
        """
        if self._page is None or self._closed:
            raise SurfaceError("surface is not started")
        return self._page

    # -- perception --------------------------------------------------------

    def observe(self) -> Observation:
        """Snapshot the accessibility tree across every frame."""
        page = self.page
        snapshot = ax.capture(self._session, page)
        self._snapshot = snapshot
        return Observation(
            url=page.url,
            title=self._safe_title(page),
            nodes=snapshot.nodes,
            truncated=snapshot.truncated,
        )

    @staticmethod
    def _safe_title(page: Page) -> str:
        """A title is decoration; never fail an observation over one."""
        try:
            return page.title()
        except PlaywrightError:
            return ""

    # -- action ------------------------------------------------------------

    def act(self, action: Action) -> ActResult:
        """Perform an action addressed by an ordinal from the last observation."""
        page = self.page

        if action.kind in (ActionKind.FINISH, ActionKind.ESCALATE):
            # Control-flow actions; the surface is untouched.
            return ActResult(ok=True, action=action, url_after=page.url)

        if action.kind is ActionKind.NAVIGATE:
            if not action.url:
                return ActResult(ok=False, action=action, detail="no url given")
            try:
                page.goto(action.url, wait_until="domcontentloaded")
            except PlaywrightTimeout as exc:
                return ActResult(ok=False, action=action, detail=f"navigate: {exc}")
            # Settle exactly as a click that navigated would. Returning at
            # domcontentloaded made the observation after a navigate less
            # reliable than the one after a click causing the same navigation --
            # an asymmetry with no justification behind it.
            self._settle()
            return ActResult(ok=True, action=action, url_after=page.url)

        detail = self._detail_for(action.ordinal)
        locator = self._stamped(detail.frame_path, detail.ordinal)
        if locator is None:
            return ActResult(
                ok=False,
                action=action,
                detail=f"ordinal {action.ordinal} no longer present on the surface",
            )

        # Validate the ladder while the element is still identified by its stamp.
        # Doing it here rather than in observe() keeps the cost proportional to
        # actions taken, not to elements seen.
        ladder, resolution = self._validated_ladder(detail)

        try:
            outcome = self._perform(action, locator)
        except PlaywrightTimeout as exc:
            return ActResult(
                ok=False,
                action=action,
                detail=f"timed out performing {action.kind.value}: {exc}",
                ladder=ladder,
                resolution=resolution,
            )
        except Exception as exc:  # detached node, obscured element, ...
            return ActResult(
                ok=False,
                action=action,
                detail=f"{type(exc).__name__}: {exc}",
                ladder=ladder,
                resolution=resolution,
            )

        self._settle()
        return ActResult(
            ok=True,
            action=action,
            read_value=outcome,
            url_after=page.url,
            ladder=ladder,
            resolution=resolution,
        )

    def _perform(self, action: Action, locator: Locator) -> str:
        """Carry out one action. Returns any value read."""
        if action.kind is ActionKind.CLICK:
            locator.click(timeout=ACTION_TIMEOUT_MS)
            return ""
        if action.kind is ActionKind.TYPE:
            locator.fill(action.text, timeout=ACTION_TIMEOUT_MS)
            return ""
        if action.kind is ActionKind.READ:
            return (locator.inner_text(timeout=ACTION_TIMEOUT_MS) or "").strip()
        raise SurfaceError(f"unsupported action kind {action.kind!r}")

    def _settle(self) -> None:
        """Wait for the page to go quiet, tolerating a surface that never does.

        A surface that keeps polling in the background is normal in legacy apps, so
        failing to reach ``networkidle`` is not an error -- it just means the next
        observation happens now.
        """
        try:
            self.page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except PlaywrightTimeout:
            pass

    def _detail_for(self, ordinal: int | None) -> ax.ElementDetail:
        if self._snapshot is None:
            raise OrdinalNotFound("no observation has been taken yet")
        if ordinal is None:
            raise OrdinalNotFound("action requires an ordinal but none was given")
        detail = self._snapshot.details.get(ordinal)
        if detail is None and any(n.ordinal == ordinal for n in self._snapshot.nodes):
            raise OrdinalNotFound(
                f"ordinal {ordinal} is visible text with no addressable element; "
                "act on or read an enclosing node instead"
            )
        if detail is None:
            known = sorted(self._snapshot.details)
            span = f"{known[0]}-{known[-1]}" if known else "none"
            raise OrdinalNotFound(
                f"ordinal {ordinal} is not in the last observation (valid: {span})"
            )
        return detail

    # -- resolution --------------------------------------------------------

    def resolve(self, ladder: LocatorLadder) -> Resolution:
        """Walk a ladder against the live surface, reporting the matching rung."""
        resolution, _ = self.resolve_locator(ladder)
        return resolution

    def resolve_locator(
        self, ladder: LocatorLadder
    ) -> tuple[Resolution, Locator | None]:
        """Walk a ladder, returning both the verdict and the element found.

        Replay needs the element as well as the verdict; the protocol's ``resolve``
        exposes only the verdict so that nothing above this layer can start passing
        Playwright locators around.
        """
        frame = self._frame_for(ladder.frame_path)
        attempted: list[tuple[RungKind, str]] = []

        if frame is None:
            return (
                Resolution(
                    matched=None,
                    attempted=(
                        (
                            RungKind.ROLE_NAME,
                            f"frame {'/'.join(ladder.frame_path) or '<main>'} absent",
                        ),
                    ),
                ),
                None,
            )

        for rung in ladder.ordered:
            locator, why = self._try_rung(frame, rung)
            if locator is not None:
                return Resolution(matched=rung.kind, attempted=tuple(attempted)), locator
            attempted.append((rung.kind, why))

        return Resolution(matched=None, attempted=tuple(attempted)), None

    def _try_rung(self, frame: Frame, rung: Rung) -> tuple[Locator | None, str]:
        """Attempt one rung. Returns the locator, or the reason it declined.

        An ambiguous match is a refusal, not a success. Silently taking the first
        of several matches is how automation ends up acting on the wrong row.
        """
        try:
            locator = self._locator_for(frame, rung)
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

        if locator is None:
            return None, "rung not applicable to this surface"

        try:
            count = locator.count()
        except Exception as exc:
            return None, f"count failed: {type(exc).__name__}"

        if count == 0:
            return None, "no match"
        if count > 1 and rung.kind is not RungKind.ANCHORED:
            return None, f"ambiguous ({count} matches)"
        return locator.first, "matched"

    def _locator_for(self, frame: Frame, rung: Rung) -> Locator | None:
        """Translate one rung into a Playwright locator."""
        params = rung.params

        if rung.kind is RungKind.ROLE_NAME:
            return frame.get_by_role(
                str(params["role"]),  # type: ignore[arg-type]
                name=str(params["name"]),
                exact=True,
            )

        if rung.kind is RungKind.LABEL:
            if "label" in params:
                return frame.get_by_label(str(params["label"]), exact=True)
            return frame.get_by_text(str(params["text"]), exact=True)

        if rung.kind is RungKind.ANCHORED:
            anchor = frame.locator(f"#{params['anchor_id']}")
            return anchor.get_by_role(
                str(params["role"])  # type: ignore[arg-type]
            ).nth(int(params["index"]))

        if rung.kind is RungKind.DOM_PATH:
            return frame.locator(str(params["css"]))

        # Coordinates address a point, not an element. Replay handles them as a
        # positional click; there is no locator to return.
        return None

    def _frame_for(self, path: tuple[str, ...]) -> Frame | None:
        """Walk a recorded frame path back to a live frame."""
        frame: Frame = self.page.main_frame
        for name in path:
            # Skip detached frames: after a navigation the dead frame can still
            # be listed, under the same name, ahead of its live replacement.
            match = next(
                (
                    child
                    for child in frame.child_frames
                    if (child.name or "") == name and not child.is_detached()
                ),
                None,
            )
            if match is None:
                return None
            frame = match
        return frame

    # -- ladder construction ----------------------------------------------

    def _validated_ladder(
        self, detail: ax.ElementDetail
    ) -> tuple[LocatorLadder | None, Resolution | None]:
        """Build a ladder and keep only the rungs that hit the right element.

        The stamp is the ground truth: a rung is retained only if resolving it
        lands on the element carrying this ordinal's stamp. Coordinate rungs cannot
        be checked this way and are retained as-recorded, already flagged fragile.
        """
        try:
            candidate = detail.to_ladder()
        except ValueError:
            return None, None

        frame = self._frame_for(detail.frame_path)
        if frame is None:
            return candidate, None

        kept: list[Rung] = []
        attempted: list[tuple[RungKind, str]] = []
        for rung in candidate.ordered:
            if rung.kind is RungKind.COORDINATES:
                kept.append(rung)
                continue
            locator, why = self._try_rung(frame, rung)
            if locator is None:
                attempted.append((rung.kind, why))
                continue
            if self._is_stamped(locator, detail.ordinal):
                kept.append(rung)
            else:
                attempted.append((rung.kind, "resolved to a different element"))

        if not kept:
            return None, Resolution(matched=None, attempted=tuple(attempted))

        validated = LocatorLadder(
            frame_path=candidate.frame_path,
            rungs=tuple(kept),
            recorded_role=candidate.recorded_role,
            recorded_name=candidate.recorded_name,
        )
        return validated, Resolution(
            matched=validated.best.kind, attempted=tuple(attempted)
        )

    @staticmethod
    def _is_stamped(locator: Locator, ordinal: int) -> bool:
        try:
            return locator.get_attribute(ax.STAMP_ATTR, timeout=1_000) == str(ordinal)
        except Exception:
            return False

    def _stamped(self, frame_path: tuple[str, ...], ordinal: int) -> Locator | None:
        """The exact element behind an ordinal, via its perception stamp."""
        frame = self._frame_for(frame_path)
        if frame is None:
            return None
        locator = frame.locator(f'[{ax.STAMP_ATTR}="{ordinal}"]')
        try:
            if locator.count() != 1:
                return None
        except Exception:
            return None
        return locator.first

    # -- evidence ----------------------------------------------------------

    def screenshot(self) -> bytes:
        """PNG of the current viewport.

        Evidence only. The agent never sees pixels, so a screenshot can never
        become a perception channel by accident.
        """
        try:
            return self.page.screenshot(full_page=False)
        except Exception as exc:
            raise SurfaceError(f"screenshot failed: {exc}") from exc
