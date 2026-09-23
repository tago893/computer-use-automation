"""Accessibility-tree perception over CDP.

How an ordinal becomes a real element, and why it is done this way.

The browser's own accessibility tree is the source of truth, read via CDP
``Accessibility.getFullAXTree``. That is the same tree a screen reader consumes, so
it reflects computed ARIA semantics rather than our guess at them, and it is the
one perception model that also exists on desktop surfaces (UI Automation, AX API).

Three findings from probing Chromium shaped this implementation, each of which
would otherwise have been a silent bug:

1. ``getFullAXTree`` **does not pierce iframes.** Called plainly on the target
   app's console it returns 13 nodes; the search form inside ``mainframe`` is not
   among them. It must be called once per frame with an explicit ``frameId``.
2. **Same-origin iframes have no separate CDP session.** ``new_cdp_session(frame)``
   fails with "this frame does not have a separate CDP session". So one session on
   the page drives every frame, and frames are enumerated via ``Page.getFrameTree``.
3. **CDP node ids are not reachable from Playwright's public API.** There is no
   supported way to turn a ``backendDOMNodeId`` into an ``ElementHandle``.

Point 3 is the interesting one. The bridge used here is to stamp a transient
``data-ax-ord`` attribute onto each observed element via ``DOM.setAttributeValue``,
which Playwright can then locate exactly. The trade-off is explicit: it mutates
the live DOM, which a page with a MutationObserver could in principle notice. The
alternative -- reimplementing accessible-name computation in injected JavaScript --
trades a real browser AX tree for our approximation of one, which is a worse trade
for a system whose whole argument rests on perceiving what the platform perceives.

The stamp is a **perception-time device only**. It is cleared before every
observation and never appears in an artifact; artifacts address elements solely
through the locator ladder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from playwright.sync_api import CDPSession, Error as PlaywrightError, Frame, Page

from .locators import LocatorLadder, build_ladder
from .models import (
    ACTIONABLE_ROLES,
    MAX_NAME_CHARS,
    MAX_NODES,
    READABLE_ROLES,
    AXNode,
)

logger = logging.getLogger(__name__)

#: Perception degrades rather than crashing, so every swallowed failure is logged.
#: A discovery run that quietly saw half a page is worse than one that says so.
_PERCEPTION_FAULTS = (PlaywrightError, KeyError, TypeError)

#: The transient perception stamp. Never recorded into an artifact.
STAMP_ATTR: Final[str] = "data-ax-ord"

#: Roles worth keeping. Everything else on a legacy page is layout scaffolding
#: (``LayoutTable``, ``InlineTextBox``, ``generic``, ``none``) and only wastes
#: context window.
KEEP_ROLES: Final[frozenset[str]] = ACTIONABLE_ROLES | READABLE_ROLES

#: Roles whose DOM node is a bare text node. Such a node cannot carry an
#: attribute, so it is stamped through its host element instead.
TEXT_ROLES: Final[frozenset[str]] = frozenset({"StaticText", "InlineTextBox"})

_CLEAR_STAMPS_JS: Final[str] = f"""
() => {{
  for (const el of document.querySelectorAll('[{STAMP_ATTR}]')) {{
    el.removeAttribute('{STAMP_ATTR}');
  }}
}}
"""

#: Collects everything the locator ladder needs, for every stamped element in one
#: frame, in a single round trip.
_DETAILS_JS: Final[str] = f"""
() => {{
  const out = {{}};
  for (const el of document.querySelectorAll('[{STAMP_ATTR}]')) {{
    const ord = el.getAttribute('{STAMP_ATTR}');

    let label = '';
    if (el.labels && el.labels.length) {{
      label = (el.labels[0].innerText || '').trim();
    }}
    if (!label) {{
      label = (el.getAttribute('aria-label') || '').trim();
    }}

    const text = ((el.innerText || el.value || '') + '').trim().slice(0, 120);

    let anchor = el.parentElement;
    let anchorId = '';
    while (anchor) {{
      if (anchor.id) {{ anchorId = anchor.id; break; }}
      anchor = anchor.parentElement;
    }}

    let anchorIndex = null;
    if (anchor) {{
      const same = Array.from(anchor.getElementsByTagName(el.tagName));
      const at = same.indexOf(el);
      if (at >= 0) {{ anchorIndex = at; }}
    }}

    const parts = [];
    let node = el;
    while (node && node.nodeType === 1) {{
      let step = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {{
        const sibs = Array.from(parent.children)
          .filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) {{
          step += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
        }}
      }}
      parts.unshift(step);
      node = node.parentElement;
    }}

    const box = el.getBoundingClientRect();
    out[ord] = {{
      label: label,
      text: text,
      tag: el.tagName.toLowerCase(),
      anchor_id: anchorId,
      anchor_index: anchorIndex,
      dom_path: parts.join(' > '),
      x: box.left + box.width / 2,
      y: box.top + box.height / 2,
      visible: box.width > 0 && box.height > 0,
      disabled: !!el.disabled
    }};
  }}
  return out;
}}
"""


@dataclass(frozen=True)
class FrameRef:
    """One frame, in both CDP's and Playwright's terms.

    Frames must be addressed in both vocabularies at once: CDP holds the
    accessibility tree, Playwright holds the ability to click. ``path`` is the
    durable name recorded into artifacts.
    """

    cdp_id: str
    path: tuple[str, ...]
    url: str
    frame: Frame


@dataclass(frozen=True)
class ElementDetail:
    """Per-element facts gathered from the page, used to build a ladder."""

    ordinal: int
    role: str
    name: str
    frame_path: tuple[str, ...]
    label: str = ""
    text: str = ""
    anchor_id: str = ""
    anchor_index: int | None = None
    dom_path: str = ""
    point: tuple[float, float] | None = None
    visible: bool = True

    def to_ladder(self) -> LocatorLadder:
        """Build this element's locator ladder.

        Unvalidated: rungs are emitted from what the page reported. The surface
        validates a ladder against the live element before recording it into an
        artifact, so a bad rung is dropped rather than replayed blindly.
        """
        return build_ladder(
            role=self.role,
            name=self.name,
            frame_path=self.frame_path,
            label=self.label,
            text=self.text,
            anchor_id=self.anchor_id,
            anchor_role=self.role,
            anchor_index=self.anchor_index,
            dom_path=self.dom_path,
            point=self.point,
        )


@dataclass(frozen=True)
class Snapshot:
    """One observation's nodes plus the detail needed to act on them."""

    nodes: tuple[AXNode, ...]
    details: dict[int, ElementDetail] = field(default_factory=dict)
    truncated: bool = False


def _pair_siblings(
    cdp_children: list[dict[str, Any]], pw_children: list[Frame]
) -> list[tuple[dict[str, Any], Frame | None]]:
    """Pair one level of the CDP frame tree with the Playwright frame tree.

    Named frames are paired by name; anything left over is paired positionally in
    document order, which both trees agree on.

    Pairing on ``(name, url)`` was the obvious first implementation and it was
    wrong: mid-navigation, CDP and Playwright briefly disagree about a frame's URL,
    the key misses, and the frame is dropped from perception entirely. That
    presented as an agent that intermittently could not see the pane it had just
    navigated -- a silent blindness far worse than a loud failure. Identity here
    must not depend on a value that changes while we are reading it.
    """
    remaining = list(pw_children)
    paired: list[tuple[dict[str, Any], Frame | None]] = []

    by_name: dict[str, list[Frame]] = {}
    for frame in remaining:
        by_name.setdefault(frame.name or "", []).append(frame)

    for child in cdp_children:
        name = child["frame"].get("name") or ""
        candidates = by_name.get(name, []) if name else []
        if len(candidates) == 1 and candidates[0] in remaining:
            match = candidates[0]
            remaining.remove(match)
            paired.append((child, match))
        else:
            paired.append((child, None))

    # Positional fallback for whatever names could not settle.
    leftovers = iter(remaining)
    paired = [
        (child, match if match is not None else next(leftovers, None))
        for child, match in paired
    ]
    return paired


def frame_refs(session: CDPSession, page: Page) -> tuple[FrameRef, ...]:
    """Enumerate frames, pairing CDP frame ids with Playwright frames.

    The two trees are walked in lockstep. An unpaired CDP frame is skipped rather
    than guessed at, so a frame we could observe but not act in is never presented
    to the model as actionable.
    """
    refs: list[FrameRef] = []

    def walk(node: dict[str, Any], pw_frame: Frame, path: tuple[str, ...]) -> None:
        info = node["frame"]
        refs.append(
            FrameRef(
                cdp_id=info["id"],
                path=path,
                url=info.get("url") or "",
                frame=pw_frame,
            )
        )
        children = node.get("childFrames", [])
        if not children:
            return
        # Detached frames linger in ``child_frames`` for a while after a
        # navigation. Left in, they share names with their live replacements,
        # name pairing gives up, and the positional fallback can hand back a
        # dead frame -- the whole pane then silently vanishes from perception.
        live = [f for f in pw_frame.child_frames if not f.is_detached()]
        for child, match in _pair_siblings(children, live):
            if match is None:
                continue
            name = child["frame"].get("name") or ""
            walk(child, match, path + (name,) if name else path)

    tree = session.send("Page.getFrameTree")["frameTree"]
    walk(tree, page.main_frame, ())
    return tuple(refs)


def _node_name(node: dict[str, Any]) -> str:
    return str(node.get("name", {}).get("value", "") or "").strip()


def _node_role(node: dict[str, Any]) -> str:
    return str(node.get("role", {}).get("value", "") or "")


def _properties(node: dict[str, Any]) -> dict[str, Any]:
    return {
        prop.get("name"): prop.get("value", {}).get("value")
        for prop in node.get("properties", [])
    }


def _repeats_an_ancestor(
    node: dict[str, Any], name: str, ax_by_id: dict[str, dict[str, Any]]
) -> bool:
    """True when ``name`` is already carried by an enclosing node's name.

    A link, cell, or heading takes its accessible name from its text, so the
    text node under it says nothing new. On a table-heavy legacy page that is
    most of the tree.
    """
    parent_id = node.get("parentId")
    while parent_id:
        parent = ax_by_id.get(parent_id)
        if parent is None:
            return False
        if _node_name(parent) == name:
            return True
        parent_id = parent.get("parentId")
    return False


def _keep(node: dict[str, Any], ax_by_id: dict[str, dict[str, Any]]) -> bool:
    """Decide whether an AX node is worth showing the model.

    Drops ignored nodes, layout scaffolding, nameless nodes, and text that
    merely repeats an ancestor's accessible name.
    """
    if node.get("ignored"):
        return False
    if not node.get("backendDOMNodeId"):
        return False

    role = _node_role(node)
    if role not in KEEP_ROLES:
        return False

    name = _node_name(node)
    if not name and role not in ACTIONABLE_ROLES:
        return False
    if role in TEXT_ROLES and _repeats_an_ancestor(node, name, ax_by_id):
        return False
    return True


def _document_order(
    ax_nodes: list[dict[str, Any]], ax_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Depth-first, following ``childIds`` -- i.e. the order a reader meets them.

    ``getFullAXTree`` does not promise document order and in practice returns
    text nodes after their siblings' subtrees, so "No such member." rendered
    *after* the sentence that follows it on the page. Walking the tree explicitly
    is the only order worth trusting.
    """
    roots = [n for n in ax_nodes if str(n.get("parentId", "")) not in ax_by_id]
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        node_id = str(node.get("nodeId"))
        if node_id in seen:  # defensive: a malformed tree must not loop forever
            continue
        seen.add(node_id)
        ordered.append(node)
        children = [ax_by_id[c] for c in node.get("childIds", []) if c in ax_by_id]
        stack.extend(reversed(children))
    return ordered


def _select_nodes(
    ax_nodes: list[dict[str, Any]],
    ax_by_id: dict[str, dict[str, Any]],
    first_ordinal: int,
    budget: int,
) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
    """Choose the nodes worth keeping from one frame's AX tree.

    Returns the kept ``(ordinal, node)`` pairs and whether the node budget ran
    out. Ordinals are assigned here so that numbering follows document order.
    """
    kept: list[tuple[int, dict[str, Any]]] = []
    for node in _document_order(ax_nodes, ax_by_id):
        if len(kept) >= budget:
            return kept, True
        if _keep(node, ax_by_id):
            kept.append((first_ordinal + len(kept), node))
    return kept, False


def _host_backend_id(
    node: dict[str, Any], ax_by_id: dict[str, dict[str, Any]]
) -> int | None:
    """The nearest ancestor of a text node that is a real element.

    A bare text node cannot carry a DOM attribute, so it cannot be stamped. Its
    host element can, and reading the host yields the text.
    """
    parent_id = node.get("parentId")
    while parent_id:
        parent = ax_by_id.get(parent_id)
        if parent is None:
            return None
        if _node_role(parent) not in TEXT_ROLES and parent.get("backendDOMNodeId"):
            return int(parent["backendDOMNodeId"])
        parent_id = parent.get("parentId")
    return None


def _try_stamp(session: CDPSession, backend_id: int, ordinal: int) -> bool:
    """Stamp one element. False if the node is gone or cannot take an attribute."""
    try:
        node_id = session.send(
            "DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": [backend_id]}
        )["nodeIds"][0]
        if not node_id:
            return False
        session.send(
            "DOM.setAttributeValue",
            {"nodeId": node_id, "name": STAMP_ATTR, "value": str(ordinal)},
        )
    except (*_PERCEPTION_FAULTS, IndexError) as exc:
        logger.debug("could not stamp ordinal %d: %s", ordinal, exc)
        return False
    return True


def _stamp(
    session: CDPSession,
    kept: list[tuple[int, dict[str, Any]]],
    ax_by_id: dict[str, dict[str, Any]],
) -> set[int]:
    """Mark each kept node's element with its ordinal. Returns the stamped ordinals.

    Text nodes are stamped through their host element, unless that host already
    carries another ordinal's stamp -- one element can answer to one ordinal only,
    or a stamp lookup would become ambiguous. Such a node stays *visible* but is
    not addressable.

    Every CDP call here is guarded. A frame can detach, or a node id can go stale,
    between reading the accessibility tree and writing the stamp -- routine on a
    surface with iframes that navigate. Losing a frame from one observation is
    survivable; crashing the discovery loop is not.
    """
    stamped: set[int] = set()
    used_hosts: set[int] = set()
    for ordinal, node in kept:
        backend_id = int(node["backendDOMNodeId"])
        if _node_role(node) in TEXT_ROLES:
            host = _host_backend_id(node, ax_by_id)
            if host is None or host in used_hosts:
                continue
            backend_id = host
        if backend_id in used_hosts:
            continue
        if _try_stamp(session, backend_id, ordinal):
            stamped.add(ordinal)
            used_hosts.add(backend_id)
    return stamped


def _describe(
    ordinal: int,
    node: dict[str, Any],
    ref: FrameRef,
    extra: dict[str, Any],
) -> tuple[AXNode, ElementDetail]:
    """Turn one stamped AX node into what the model sees and what replay records."""
    role = _node_role(node)
    name = _node_name(node)[:MAX_NAME_CHARS]
    props = _properties(node)

    ax_node = AXNode(
        ordinal=ordinal,
        role=role,
        name=name,
        value=str(node.get("value", {}).get("value", "") or "")[:MAX_NAME_CHARS],
        description=str(node.get("description", {}).get("value", "") or "")[
            :MAX_NAME_CHARS
        ],
        frame_path=ref.path,
        focusable=bool(props.get("focusable")),
        disabled=bool(props.get("disabled")) or bool(extra.get("disabled")),
    )

    visible = bool(extra.get("visible", True))
    point: tuple[float, float] | None = None
    # A coordinate for a zero-sized element is a point at the document origin,
    # which would resolve to whatever happens to be there. An unusable rung is
    # worse than an absent one, so it is not recorded at all.
    if visible and extra.get("x") is not None and extra.get("y") is not None:
        point = (float(extra["x"]), float(extra["y"]))

    detail = ElementDetail(
        ordinal=ordinal,
        role=role,
        name=name,
        frame_path=ref.path,
        label=str(extra.get("label", "") or ""),
        text=str(extra.get("text", "") or ""),
        anchor_id=str(extra.get("anchor_id", "") or ""),
        anchor_index=extra.get("anchor_index"),
        dom_path=str(extra.get("dom_path", "") or ""),
        point=point,
        visible=visible,
    )
    return ax_node, detail


def _capture_frame(
    session: CDPSession, ref: FrameRef, first_ordinal: int, budget: int
) -> tuple[list[AXNode], dict[int, ElementDetail], int, bool]:
    """Observe one frame. Never raises: an unreadable frame yields nothing.

    Returns ``(nodes, details, next_ordinal, truncated)``. Every kept node is
    *visible*; only nodes whose element took a stamp get a detail and so are
    addressable. ``next_ordinal`` is where the next frame must start numbering --
    it counts every ordinal handed out here, visible or not.
    """
    # Clear stale stamps first: a leftover stamp from a previous observation
    # would let an old ordinal resolve to the wrong element.
    try:
        ref.frame.evaluate(_CLEAR_STAMPS_JS)
    except _PERCEPTION_FAULTS as exc:  # frame detached mid-observation
        logger.debug("frame %s unreadable, skipping: %s", ref.path, exc)
        return [], {}, first_ordinal, False

    try:
        ax_nodes = session.send(
            "Accessibility.getFullAXTree", {"frameId": ref.cdp_id}
        )["nodes"]
    except _PERCEPTION_FAULTS as exc:
        logger.debug("no AX tree for frame %s: %s", ref.path, exc)
        return [], {}, first_ordinal, False

    ax_by_id = {str(node.get("nodeId")): node for node in ax_nodes}
    kept, truncated = _select_nodes(ax_nodes, ax_by_id, first_ordinal, budget)
    next_ordinal = first_ordinal + len(kept)
    if not kept:
        return [], {}, next_ordinal, truncated

    stamped = _stamp(session, kept, ax_by_id)

    extras: dict[str, Any] = {}
    if stamped:
        try:
            extras = ref.frame.evaluate(_DETAILS_JS)
        except _PERCEPTION_FAULTS as exc:
            # Ladders will be poorer for this frame but nodes stay addressable.
            logger.debug("detail probe failed in frame %s: %s", ref.path, exc)

    nodes: list[AXNode] = []
    details: dict[int, ElementDetail] = {}
    for ordinal, node in kept:
        ax_node, detail = _describe(ordinal, node, ref, extras.get(str(ordinal), {}))
        nodes.append(ax_node)
        if ordinal in stamped:
            details[ordinal] = detail
    return nodes, details, next_ordinal, truncated


def capture(session: CDPSession, page: Page) -> Snapshot:
    """Take one observation: AX nodes across all frames, stamped and detailed.

    Ordinals are assigned in frame-tree order, so numbering is stable for an
    unchanged page and meaningless across changes -- exactly the lifetime an
    ordinal is supposed to have.
    """
    try:
        session.send("DOM.getDocument", {"depth": -1, "pierce": True})
    except _PERCEPTION_FAULTS as exc:
        logger.warning("DOM.getDocument failed; nothing is addressable: %s", exc)
        # Without a retrieved document, node ids cannot be pushed to the
        # frontend, so nothing can be stamped. An empty observation is the
        # honest answer; the caller sees a surface it cannot address.
        return Snapshot(nodes=(), details={}, truncated=False)

    nodes: list[AXNode] = []
    details: dict[int, ElementDetail] = {}
    truncated = False
    # Numbering continues from the last ordinal *assigned*, never from
    # ``len(nodes)``: the two diverge whenever a frame yields fewer nodes than it
    # numbered, and then the next frame reuses ordinals already handed out.
    next_ordinal = 0

    for ref in frame_refs(session, page):
        budget = MAX_NODES - len(nodes)
        if budget <= 0:
            truncated = True
            break

        frame_nodes, frame_details, next_ordinal, hit_limit = _capture_frame(
            session, ref, next_ordinal, budget
        )
        nodes.extend(frame_nodes)
        details.update(frame_details)
        truncated = truncated or hit_limit

    return Snapshot(nodes=tuple(nodes), details=details, truncated=truncated)
