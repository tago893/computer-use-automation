"""Form field wire names.

Deliberately shaped like ASP.NET WebForms output, because that is what a real
legacy servicing console emits and because it is exactly the kind of "identifier"
that looks stable and is not: the generated prefix shifts when a developer nests
a control one level deeper or renames a content placeholder.

Keeping them in one module rather than inline in templates means the hostility is
a documented property of the target app instead of scattered noise, and the
Phase 2 experiment stays honest: nothing in ``surfaces/`` may reference these
names. An agent that perceives via the accessibility tree never needs them; a
selector-driven agent would be coupled to all of them.
"""

from __future__ import annotations

from typing import Final

_PREFIX: Final[str] = "ctl00$cphMain"

LOGIN_USER: Final[str] = f"{_PREFIX}$txtUsr"
LOGIN_PASS: Final[str] = f"{_PREFIX}$txtPwd"
MEMBER_ID: Final[str] = f"{_PREFIX}$grdSrch$txtMbrId"
MEMBER_ID_HIDDEN: Final[str] = f"{_PREFIX}$hidMbrId"
ACCOUNT_ID_HIDDEN: Final[str] = f"{_PREFIX}$hidAcctId"
CONTINUE_TO: Final[str] = f"{_PREFIX}$hidNextUrl"

#: Exposed to templates so markup and server agree on one source of truth.
FIELDS: Final[dict[str, str]] = {
    "login_user": LOGIN_USER,
    "login_pass": LOGIN_PASS,
    "member_id": MEMBER_ID,
    "member_id_hidden": MEMBER_ID_HIDDEN,
    "account_id_hidden": ACCOUNT_ID_HIDDEN,
    "continue_to": CONTINUE_TO,
}
