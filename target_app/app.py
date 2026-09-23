"""The hostile legacy servicing console.

A stand-in for a vendor product a credit union would actually be stuck with in
2026: server-rendered, nested iframes, table-based layout, machine-generated
class names, no test hooks of any kind. What it *does* get right is ARIA --
labels and roles are correct.

That combination is the entire point. The markup offers nothing stable to grip,
so the accessibility tree is the only reliable way to perceive the surface. An
agent that leans on CSS selectors here will be brittle by construction; one that
leans on role + accessible name will not.

Run it:

    python -m target_app

Routes are grouped as:

* ``/t/<tenant>/login``   -- unframed login page
* ``/t/<tenant>/console`` -- the frame shell (nav + main iframes)
* ``/t/<tenant>/pane/*``  -- iframe contents, where the real work happens
* ``/__control__/*``      -- test-only fault control plane
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Final

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from werkzeug.wrappers import Response

from . import data, faults, fields
from .faults import FaultMode
from .tenants import DEFAULT_TENANT, TENANTS, Tenant, UnknownTenant, get_tenant

_SESSION_USER: Final[str] = "user"
_SESSION_TENANT: Final[str] = "tenant"
_SESSION_DISCLOSED: Final[str] = "disclosed"


def obfuscated_class(prefix: str, seed: str) -> str:
    """Generate a stable, meaningless class name.

    Stable matters: if class names changed per render, DOM-path locators would
    fail for the wrong reason and the experiment would prove nothing. These are
    meaningless but deterministic, exactly like a real minified build.
    """
    digest = hashlib.md5(
        f"{prefix}:{seed}".encode(), usedforsecurity=False
    ).hexdigest()
    return f"{prefix}{digest[:5]}"


def webforms_id(name: str) -> str:
    """Mirror ASP.NET's name-to-id mangling (``$`` becomes ``_``).

    Real WebForms emits both, so labels can be correctly associated even though
    the identifier itself is generated and unstable across layout changes. Having
    correct ``label for=`` is what makes rung 2 of the locator ladder viable.
    """
    return name.replace("$", "_")


def create_app() -> Flask:
    """Build the Flask app.

    A factory rather than a module-level global so tests can spin up isolated
    instances with their own sessions.
    """
    app = Flask(__name__)

    # No hardcoded secret. Sessions do not need to survive a restart, so a
    # per-process random key is both safer and sufficient.
    app.secret_key = os.getenv("TARGET_APP_SECRET") or secrets.token_hex(32)

    @app.context_processor
    def _inject_helpers() -> dict[str, object]:
        return {
            "cls": obfuscated_class,
            "f": fields.FIELDS,
            "idof": webforms_id,
        }

    @app.errorhandler(UnknownTenant)
    def _unknown_tenant(exc: UnknownTenant) -> tuple[str, int]:
        return render_template("error.html", code=404, detail=str(exc)), 404

    @app.errorhandler(500)
    def _server_error(exc: object) -> tuple[str, int]:
        return (
            render_template(
                "error.html",
                code=500,
                detail="Servicing host unavailable (SVC-0500).",
            ),
            500,
        )

    _register_routes(app)
    _register_control_plane(app)
    return app


# ---------------------------------------------------------------------------
# Request guards
# ---------------------------------------------------------------------------


def _authenticated(tenant: Tenant) -> bool:
    return (
        session.get(_SESSION_USER) == tenant.username
        and session.get(_SESSION_TENANT) == tenant.tenant_id
    )


def _expired(tenant: Tenant) -> Response:
    """Drop the session and bounce to login, the way a real console would."""
    session.clear()
    return redirect(url_for("login", tenant=tenant.tenant_id, expired=1))


def _apply_fault(tenant: Tenant, mode: FaultMode) -> ResponseReturnValue | None:
    """Act on a fault that is not pane-specific.

    Returns a response to short-circuit with, or ``None`` to carry on. Faults
    that only make sense inside one pane (``notfound``, ``validation``) are
    handled by that pane instead.
    """
    if mode is FaultMode.SLOW:
        time.sleep(faults.SLOW_SECONDS)
        return None
    if mode is FaultMode.ERROR500:
        abort(500)
    if mode is FaultMode.TIMEOUT:
        return _expired(tenant)
    if mode is FaultMode.INTERSTITIAL:
        return render_template(
            "interstitial.html",
            tenant=tenant,
            kind="security",
            heading="Additional verification required",
            body=(
                "This session was flagged for periodic re-verification. "
                "Acknowledge to continue to the requested screen."
            ),
            continue_to=request.full_path,
        )
    return None


def _register_routes(app: Flask) -> None:
    @app.route("/")
    def index() -> Response:
        return redirect(url_for("login", tenant=DEFAULT_TENANT))

    # -- login ------------------------------------------------------------

    @app.route("/t/<tenant>/login", methods=["GET", "POST"])
    def login(tenant: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)

        if request.method == "GET":
            return render_template(
                "login.html",
                tenant=cfg,
                expired=bool(request.args.get("expired")),
                error=None,
            )

        username = (request.form.get(fields.LOGIN_USER) or "").strip()
        password = request.form.get(fields.LOGIN_PASS) or ""
        if username != cfg.username or password != cfg.password:
            return (
                render_template(
                    "login.html",
                    tenant=cfg,
                    expired=False,
                    error="Sign-in failed. Check your credentials and retry.",
                ),
                401,
            )

        session.clear()
        session[_SESSION_USER] = cfg.username
        session[_SESSION_TENANT] = cfg.tenant_id
        return redirect(url_for("console", tenant=cfg.tenant_id, view="search"))

    # -- frame shell ------------------------------------------------------

    @app.route("/t/<tenant>/console")
    def console(tenant: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        # The shell always opens on search. ``?view=`` is accepted and ignored:
        # a legacy console would route several views through one shell, but this
        # app only has one entry view, and a parameter that pretends to switch
        # views while doing nothing is worse than no parameter.
        main_src = url_for("pane_search", tenant=cfg.tenant_id)
        return render_template(
            "console.html",
            tenant=cfg,
            nav_src=url_for("pane_nav", tenant=cfg.tenant_id),
            main_src=main_src,
        )

    # -- panes ------------------------------------------------------------

    @app.route("/t/<tenant>/pane/nav")
    def pane_nav(tenant: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)
        return render_template("pane_nav.html", tenant=cfg)

    @app.route("/t/<tenant>/pane/search")
    def pane_search(tenant: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        short_circuit = _apply_fault(cfg, faults.take(request.args.get("inject")))
        if short_circuit is not None:
            return short_circuit

        return render_template("pane_search.html", tenant=cfg, error=None, query="")

    @app.route("/t/<tenant>/pane/results", methods=["GET", "POST"])
    def pane_results(tenant: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        mode = faults.take(request.args.get("inject"))
        short_circuit = _apply_fault(cfg, mode)
        if short_circuit is not None:
            return short_circuit

        source = request.form if request.method == "POST" else request.args
        query = (source.get(fields.MEMBER_ID) or "").strip()

        # Input validation at the boundary. An empty or malformed id is a
        # rejected input the caller needs told about -- not an exception.
        if mode is FaultMode.VALIDATION or not query:
            return render_template(
                "pane_search.html",
                tenant=cfg,
                query=query,
                error=(
                    f"{cfg.label_member_id} is required and must look like "
                    "M-10001."
                ),
            )

        member = None if mode is FaultMode.NOTFOUND else data.find_member(query)
        if member is None:
            # The "no such member" business outcome. Rendered as an answer, with
            # a machine-readable code the artifact can declare and detect.
            return render_template(
                "pane_results.html",
                tenant=cfg,
                query=query,
                member=None,
                outcome_code="MEMBER_NOT_FOUND",
            )

        return render_template(
            "pane_results.html",
            tenant=cfg,
            query=query,
            member=member,
            outcome_code=None,
        )

    @app.route("/t/<tenant>/pane/member/<member_id>")
    def pane_member(tenant: str, member_id: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        short_circuit = _apply_fault(cfg, faults.take(request.args.get("inject")))
        if short_circuit is not None:
            return short_circuit

        # Tenant-specific extra step: northgate demands a disclosure the
        # meridian-recorded artifact has never seen.
        if cfg.disclosure_interstitial and not session.get(_SESSION_DISCLOSED):
            return render_template(
                "interstitial.html",
                tenant=cfg,
                kind="disclosure",
                heading="Member privacy disclosure",
                body=(
                    "Access to member records is logged. Acknowledge the "
                    "disclosure to view this record."
                ),
                continue_to=request.full_path,
            )

        member = data.find_member(member_id)
        if member is None:
            return render_template(
                "pane_results.html",
                tenant=cfg,
                query=member_id,
                member=None,
                outcome_code="MEMBER_NOT_FOUND",
            )

        return render_template(
            "pane_member.html",
            tenant=cfg,
            member=member,
            sub_src=url_for(
                "pane_subaccounts", tenant=cfg.tenant_id, member_id=member.member_id
            ),
        )

    @app.route("/t/<tenant>/pane/subaccounts/<member_id>")
    def pane_subaccounts(tenant: str, member_id: str) -> ResponseReturnValue:
        """Contents of the nested (third-level) iframe."""
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        member = data.find_member(member_id)
        if member is None:
            abort(404)
        return render_template("pane_subaccounts.html", tenant=cfg, member=member)

    @app.route("/t/<tenant>/pane/account/<member_id>/<account_id>")
    def pane_account(tenant: str, member_id: str, account_id: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        short_circuit = _apply_fault(cfg, faults.take(request.args.get("inject")))
        if short_circuit is not None:
            return short_circuit

        member = data.find_member(member_id)
        account = data.find_sub_account(member, account_id) if member else None
        if member is None or account is None:
            abort(404)

        return render_template(
            "pane_account.html", tenant=cfg, member=member, account=account
        )

    @app.route("/t/<tenant>/pane/confirm", methods=["POST"])
    def pane_confirm(tenant: str) -> ResponseReturnValue:
        cfg = get_tenant(tenant)
        if not _authenticated(cfg):
            return _expired(cfg)

        short_circuit = _apply_fault(cfg, faults.take(request.args.get("inject")))
        if short_circuit is not None:
            return short_circuit

        member_id = (request.form.get(fields.MEMBER_ID_HIDDEN) or "").strip()
        account_id = (request.form.get(fields.ACCOUNT_ID_HIDDEN) or "").strip()
        member = data.find_member(member_id)
        account = data.find_sub_account(member, account_id) if member else None
        if member is None or account is None:
            abort(404)

        # A display-only reference. Not a token, not a secret, not security-
        # relevant -- it only has to look like a vendor confirmation code.
        digest = hashlib.md5(
            f"{member_id}{account_id}".encode(), usedforsecurity=False
        ).hexdigest()
        reference = f"CNF-{digest[:8].upper()}"
        return render_template(
            "pane_confirm.html",
            tenant=cfg,
            member=member,
            account=account,
            reference=reference,
        )

    @app.route("/t/<tenant>/pane/acknowledge", methods=["POST"])
    def pane_acknowledge(tenant: str) -> Response:
        """Dismiss an interstitial and continue to the original destination."""
        cfg = get_tenant(tenant)
        # Authenticated like every other pane. Without this check an
        # unauthenticated POST could pre-set the disclosure flag on a fresh
        # cookie, and the subsequent real session would skip northgate's
        # mandatory disclosure entirely -- defeating the one piece of
        # tenant-specific control flow the heterogeneity story rests on.
        if not _authenticated(cfg):
            return _expired(cfg)
        session[_SESSION_DISCLOSED] = True
        target = request.form.get(fields.CONTINUE_TO) or url_for(
            "pane_search", tenant=cfg.tenant_id
        )
        # Only ever redirect within this app -- never follow an absolute URL
        # supplied in a form field.
        if not target.startswith("/"):
            target = url_for("pane_search", tenant=cfg.tenant_id)
        return redirect(target)


def _register_control_plane(app: Flask) -> None:
    """Test-only endpoints for arming faults.

    Kept under an obvious ``__control__`` prefix, and excluded from the Phase 6
    action allowlist so neither the discovery agent nor replay can reach it. The
    agent must cope with faults, not switch them off.
    """

    @app.post("/__control__/inject")
    def control_inject() -> dict[str, object]:
        payload = request.get_json(silent=True) or {}
        mode = FaultMode.parse(str(payload.get("mode", "")))
        count = payload.get("count", 1)
        try:
            count = int(count)
        except (TypeError, ValueError):
            return {"ok": False, "error": "count must be an integer"}
        state = faults.arm(mode, count)
        return {"ok": True, "mode": state.mode.value, "remaining": state.remaining}

    @app.post("/__control__/reset")
    def control_reset() -> dict[str, object]:
        faults.disarm()
        session.pop(_SESSION_DISCLOSED, None)
        return {"ok": True}

    @app.get("/__control__/state")
    def control_state() -> dict[str, object]:
        state = faults.armed()
        return {
            "mode": state.mode.value,
            "remaining": state.remaining,
            "authenticated": bool(session.get(_SESSION_USER)),
            "tenants": sorted(TENANTS),
        }
