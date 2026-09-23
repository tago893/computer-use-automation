"""Seeded synthetic records.

Every value here is invented. No real people, no real account numbers, no real
credentials. That is a hard constraint rather than a convenience: the discovery
agent sends observations to a third-party model whose free tier may retain
inputs, so the surface it reads must contain nothing that matters.

Account numbers are deliberately formatted to *look* sensitive (``****-****-4471``
shapes appear in the rendered pages) so the Phase 6 redaction layer has realistic
patterns to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class SubAccount:
    """A product held by a member."""

    account_id: str
    kind: str
    masked_number: str
    balance_cents: int
    status: str

    @property
    def balance(self) -> str:
        return f"${self.balance_cents / 100:,.2f}"


@dataclass(frozen=True)
class Member:
    """A synthetic member record."""

    member_id: str
    full_name: str
    city: str
    state: str
    joined: str
    standing: str
    sub_accounts: tuple[SubAccount, ...]


MEMBERS: Final[dict[str, Member]] = {
    "M-10001": Member(
        member_id="M-10001",
        full_name="Ada Sable",
        city="Fairhaven",
        state="OR",
        joined="2016-04-11",
        standing="Good",
        sub_accounts=(
            SubAccount("SA-4471", "Share Savings", "****-****-4471", 812_355, "Active"),
            SubAccount("SA-4472", "Draft Checking", "****-****-4472", 204_110, "Active"),
        ),
    ),
    "M-10002": Member(
        member_id="M-10002",
        full_name="Bram Oyelaran",
        city="Kestrel Falls",
        state="WA",
        joined="2019-09-02",
        standing="Good",
        sub_accounts=(
            SubAccount("SA-5510", "Share Savings", "****-****-5510", 43_900, "Active"),
            SubAccount("SA-5511", "Auto Loan", "****-****-5511", -1_288_400, "Current"),
            SubAccount("SA-5512", "Money Market", "****-****-5512", 2_004_775, "Active"),
        ),
    ),
    "M-10003": Member(
        member_id="M-10003",
        full_name="Cyrene Baptiste",
        city="Thornwood",
        state="ID",
        joined="2012-01-23",
        standing="Review",
        sub_accounts=(
            SubAccount("SA-6001", "Share Savings", "****-****-6001", 15_075, "Frozen"),
        ),
    ),
    "M-10004": Member(
        member_id="M-10004",
        full_name="Devrim Halloway",
        city="Fairhaven",
        state="OR",
        joined="2021-07-30",
        standing="Good",
        sub_accounts=(
            SubAccount("SA-7113", "Draft Checking", "****-****-7113", 98_240, "Active"),
            SubAccount("SA-7114", "Certificate", "****-****-7114", 5_000_000, "Matured"),
        ),
    ),
    "M-10005": Member(
        member_id="M-10005",
        full_name="Eun-Ji Marlowe",
        city="Alder Bay",
        state="WA",
        joined="2018-03-14",
        standing="Good",
        sub_accounts=(
            SubAccount("SA-8220", "Share Savings", "****-****-8220", 331_600, "Active"),
        ),
    ),
}

#: A member id guaranteed to be absent. Searching for it produces the
#: "no such member" *business outcome* -- an answer, not a failure.
ABSENT_MEMBER_ID: Final[str] = "M-99999"


def find_member(member_id: str) -> Member | None:
    """Look up a member. Returns ``None`` for an unknown id.

    ``None`` here is an expected, declared result. Callers must render it as an
    answer, never raise on it.
    """
    return MEMBERS.get(member_id.strip().upper())


def find_sub_account(member: Member, account_id: str) -> SubAccount | None:
    """Find one of a member's sub-accounts by id."""
    wanted = account_id.strip().upper()
    for account in member.sub_accounts:
        if account.account_id == wanted:
            return account
    return None
