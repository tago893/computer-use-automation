"""Tenant variants of the same vendor product.

Two credit unions running one servicing console, configured differently. This is
the cheap enabler for the heterogeneity story: an artifact discovered against
``meridian`` must survive being pointed at ``northgate``, where the same field is
labelled differently and the flow has an extra step.

What varies deliberately:

* **Accessible names.** ``"Member ID"`` vs ``"Account Holder Number"``. This is
  what breaks rung 1 of the locator ladder and forces canonicalization.
* **Layout depth.** ``northgate`` wraps the main pane in one more nested table,
  which moves DOM paths without moving roles.
* **Flow length.** ``northgate`` interposes a disclosure interstitial before the
  member detail pane, so replay must tolerate a step the artifact never saw.
* **Credentials and branding.** Cosmetic, but it proves the entry point is
  config, not a constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Tenant:
    """One tenant's configuration of the shared console."""

    tenant_id: str
    brand: str
    accent: str
    username: str
    password: str

    # Label overrides -- the accessible names the AX tree will expose.
    label_member_id: str
    label_search: str
    label_submit_search: str
    label_subaccount: str
    label_confirm: str

    # Structural variation.
    extra_table_nesting: int = 0
    disclosure_interstitial: bool = False

    # Obfuscated class prefix, so generated class names differ per tenant too.
    class_prefix: str = "q"


MERIDIAN: Final[Tenant] = Tenant(
    tenant_id="meridian",
    brand="Meridian Credit Union",
    accent="#1f3a5f",
    username="svc.agent",
    password="demo-pass-1",
    label_member_id="Member ID",
    label_search="Member Search",
    label_submit_search="Search",
    label_subaccount="Open Sub-Account",
    label_confirm="Confirm Transfer",
    extra_table_nesting=0,
    disclosure_interstitial=False,
    class_prefix="q",
)

NORTHGATE: Final[Tenant] = Tenant(
    tenant_id="northgate",
    brand="Northgate Federal",
    accent="#5f1f2a",
    username="ops.user",
    password="demo-pass-2",
    label_member_id="Account Holder Number",
    label_search="Find Account Holder",
    label_submit_search="Run Lookup",
    label_subaccount="View Linked Account",
    label_confirm="Authorize Transfer",
    extra_table_nesting=1,
    disclosure_interstitial=True,
    class_prefix="zx",
)

TENANTS: Final[dict[str, Tenant]] = {
    MERIDIAN.tenant_id: MERIDIAN,
    NORTHGATE.tenant_id: NORTHGATE,
}

DEFAULT_TENANT: Final[str] = MERIDIAN.tenant_id


class UnknownTenant(KeyError):
    """Raised when a request names a tenant that does not exist."""


def get_tenant(tenant_id: str) -> Tenant:
    """Look up a tenant, failing loudly on an unknown id.

    Validating at this boundary keeps every route handler free of tenant checks.
    """
    try:
        return TENANTS[tenant_id]
    except KeyError as exc:
        raise UnknownTenant(
            f"unknown tenant {tenant_id!r}; known: {sorted(TENANTS)}"
        ) from exc
