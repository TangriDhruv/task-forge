"""Seed (synthetic, fixed) data for the mock credit-union servicing console.

Everything here is invented for this repo. No real members, people, or account
numbers. The data is intentionally small and fixed so that discovery runs and
replays are reproducible: the same member IDs, account IDs, and field values
exist across process restarts (nothing here is randomized except generated
ticket numbers, which are recorded in-memory per run -- see TICKETS below).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    account_id: str  # e.g. "CHK-10001-01", "SAV-10002-02"
    account_type: str  # "Checking" | "Savings" | "Money Market" | "CD"
    balance: float
    status: str  # "Active" | "Frozen" | "Closed"


@dataclass(frozen=True)
class Member:
    member_id: str  # "M-10001"
    name: str
    status: str  # "Active" | "Restricted" | "Closed"
    member_since: str  # fixed date string, e.g. "2019-03-14"
    branch: str  # fixed branch name, e.g. "Downtown Branch"
    accounts: tuple[Account, ...] = field(default_factory=tuple)


# Account-type -> short code used when generating new sub-account IDs.
ACCOUNT_TYPE_CODES = {
    "Checking": "CHK",
    "Savings": "SAV",
    "Money Market": "MMA",
    "CD": "CD",
}


MEMBERS: dict[str, Member] = {
    "M-10001": Member(
        member_id="M-10001",
        name="Dana Whitfield",
        status="Active",
        member_since="2019-03-14",
        branch="Downtown Branch",
        accounts=(
            Account(account_id="CHK-10001-01", account_type="Checking", balance=2340.55, status="Active"),
            Account(account_id="SAV-10001-02", account_type="Savings", balance=8120.10, status="Active"),
        ),
    ),
    "M-10002": Member(
        member_id="M-10002",
        name="Louis Ferreira",
        status="Active",
        member_since="2020-07-22",
        branch="North Plaza Branch",
        accounts=(
            Account(account_id="CHK-10002-01", account_type="Checking", balance=540.00, status="Active"),
            Account(account_id="SAV-10002-02", account_type="Savings", balance=0.00, status="Frozen"),
        ),
    ),
    "M-10003": Member(
        member_id="M-10003",
        name="Priya Narayan",
        status="Active",
        member_since="2021-01-09",
        branch="Downtown Branch",
        accounts=(
            Account(account_id="CHK-10003-01", account_type="Checking", balance=42.10, status="Active"),
            Account(account_id="SAV-10003-02", account_type="Savings", balance=1250.00, status="Active"),
        ),
    ),
    "M-10004": Member(
        member_id="M-10004",
        name="Omar Jacinto",
        status="Restricted",
        member_since="2018-11-30",
        branch="Eastside Branch",
        accounts=(
            Account(account_id="CHK-10004-01", account_type="Checking", balance=310.00, status="Active"),
        ),
    ),
    "M-10005": Member(
        member_id="M-10005",
        name="Teresa Bloom",
        status="Closed",
        member_since="2017-05-02",
        branch="North Plaza Branch",
        accounts=(),
    ),
}


def find_member(member_id: str) -> Member | None:
    return MEMBERS.get((member_id or "").strip().upper())


def search_members(query: str) -> list[Member]:
    """Case-insensitive search by member ID (exact/prefix) or name substring."""
    q = (query or "").strip().lower()
    if not q:
        return list(MEMBERS.values())
    results = []
    for member in MEMBERS.values():
        if q in member.member_id.lower() or q in member.name.lower():
            results.append(member)
    return results


def find_account(member: Member, account_id: str) -> Account | None:
    for account in member.accounts:
        if account.account_id == account_id:
            return account
    return None


def open_sub_account(member_id: str, account_type: str, initial_deposit: float) -> Account:
    """Create a new sub-account for ``member_id`` and persist it in-memory.

    The new account ID increments the per-member sequence suffix (one counter
    shared across all of a member's accounts, zero-padded to two digits) --
    mirrors the old app's incrementing ticket-number pattern, just applied to
    account IDs instead.
    """
    member = MEMBERS[member_id]
    numeric_part = member_id.split("-", 1)[1] if "-" in member_id else member_id
    next_seq = len(member.accounts) + 1
    type_code = ACCOUNT_TYPE_CODES.get(account_type, "ACC")
    account_id = f"{type_code}-{numeric_part}-{next_seq:02d}"
    account = Account(account_id=account_id, account_type=account_type, balance=initial_deposit, status="Pending Funding")
    MEMBERS[member_id] = dataclasses.replace(member, accounts=member.accounts + (account,))
    return account


# In-memory store of submitted tickets (sub-account openings and transfers),
# keyed by ticket/transfer ID. Reset on process restart -- this app has no
# real persistence layer by design (see CLAUDE.md: "no external services").
TICKETS: dict[str, dict] = {}
