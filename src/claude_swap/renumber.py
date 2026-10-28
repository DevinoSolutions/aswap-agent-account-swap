"""Planning for ``aswap renumber`` and slot-number bookkeeping around moves.

Slot numbers are what people type (``aswap switch 3``), so they should read
1..N. They used to only grow: ``add`` took the highest number plus one and
``remove`` left a hole, so a long-lived roster looked like 3, 13-16, 19, ...
and the same account had a different number on every machine.
``aswap renumber`` packs the roster back to 1..N, and new accounts now take
the lowest free number.

This module is the pure part: which account goes to which number, the next
single step towards that layout, and how the engine's slot-keyed state
follows a move. The switcher executes each step with the same relocate/swap
machinery ``aswap move`` uses, so there is one renumber path, not two.

Why one step at a time, recomputed from the roster each time, instead of a
precomputed list of moves: every step is a committed ``move`` that leaves a
consistent roster, so a crash between steps is harmless and re-running
``renumber`` picks up where it stopped. That only works if the plan can be
derived again from any intermediate roster and comes out the same, which
holds for both orders:

* **List order** (ascending slot number). Placing the k-th account at number
  k never overtakes another account: every account still to be placed sits
  at a number above k, so the target is empty (or already the account's
  own) and the step is a plain relocation. The relative order is preserved
  at every step, so the plan computed after a crash is the plan computed
  before it.
* **By email** (lowercase email, then organization uuid). The order depends
  on the accounts, not their numbers. Moving the account that belongs at k
  onto an occupied k swaps it with the occupant (``move`` semantics), which
  puts one more account in its final place each step: at most N-1 steps,
  cycles included, and no free slot is needed to break a cycle.

Ties (two slots for the same email and organization) fall back to the
current number, which keeps the already-placed prefix stable, so the loop
always terminates.
"""

from __future__ import annotations

from dataclasses import dataclass

# The auto-switch engine's persisted state (see autoswitch.py), named here so
# the switcher can keep it in step with a move without importing the engine.
ENGINE_STATE_FILENAME = "autoswitch_state.json"
ENGINE_STATE_LOCKNAME = ".autoswitch_state.lock"


@dataclass(frozen=True)
class Step:
    """One move: the account at ``source`` goes to ``target``.

    ``swapped`` is True when ``target`` was occupied, so its occupant took
    ``source`` (exactly ``aswap move``).
    """

    source: str
    target: str
    swapped: bool


def record_numbers(accounts: dict) -> list[str]:
    """The roster's slot numbers (records only), ascending."""
    return sorted((n for n in accounts if str(n).isdigit()), key=int)


def _email_key(accounts: dict, num: str) -> tuple[str, str, int]:
    record = accounts.get(num) or {}
    email = str(record.get("email") or "").lower()
    org = str(record.get("organizationUuid") or "")
    return (email, org, int(num))


def desired_order(accounts: dict, by_email: bool = False) -> list[str]:
    """Current slot numbers in the order they should end up: index i -> i+1."""
    nums = record_numbers(accounts)
    if by_email:
        nums.sort(key=lambda n: _email_key(accounts, n))
    return nums


def plan(accounts: dict, by_email: bool = False) -> dict[str, str]:
    """Old number -> new number for every account (unchanged ones included)."""
    return {num: str(i) for i, num in enumerate(desired_order(accounts, by_email), 1)}


def is_packed(accounts: dict, by_email: bool = False) -> bool:
    """Whether the roster already reads 1..N in the requested order."""
    return all(old == new for old, new in plan(accounts, by_email).items())


def next_step(accounts: dict, by_email: bool = False) -> Step | None:
    """The first move still needed, or None when the roster is packed."""
    for i, num in enumerate(desired_order(accounts, by_email), 1):
        target = str(i)
        if num != target:
            return Step(num, target, target in accounts)
    return None


def apply_step(accounts: dict, step: Step) -> dict:
    """``accounts`` after ``step``, as ``move`` would leave it (a new dict)."""
    out = dict(accounts)
    moving = out.pop(step.source)
    if step.swapped:
        out[step.source] = out[step.target]
    out[step.target] = moving
    return out


def simulate(accounts: dict, by_email: bool = False) -> list[Step]:
    """Every step ``renumber`` would take, without touching anything.

    Bounded: a correct plan needs at most N-1 steps; anything longer is a bug
    and raises rather than spinning.
    """
    steps: list[Step] = []
    current = dict(accounts)
    limit = len(record_numbers(accounts)) + 1
    while (step := next_step(current, by_email)) is not None:
        if len(steps) >= limit:
            raise RuntimeError("renumber plan did not converge")
        steps.append(step)
        current = apply_step(current, step)
    return steps


def lowest_free_number(accounts: dict) -> int:
    """The lowest slot number >= 1 with no record."""
    taken = {int(n) for n in accounts if str(n).isdigit()}
    n = 1
    while n in taken:
        n += 1
    return n


def rekey_rows(
    rows: dict, moves: dict[str, tuple[str, tuple[str, str]]]
) -> tuple[dict, bool]:
    """A slot-keyed, identity-guarded cache table after a move.

    ``moves`` maps old number -> (new number, (email, organization uuid)) for
    every account that moved. A row follows its account only when it really
    is that account's row (same email and organization); anything else left
    at a moved-from or moved-to number belonged to nobody on the roster and
    is dropped. Returns (new rows, changed).

    The caches would stay correct without this (reads check the identity and
    ignore a mismatched row), but every account would look "never fetched"
    after a renumber and refetch at once; carrying the rows keeps the
    usage, gate and billing history attached to the account.
    """
    if not isinstance(rows, dict) or not moves:
        return rows, False
    touched = set(moves) | {new for new, _identity in moves.values()}
    out = {k: v for k, v in rows.items() if str(k) not in touched}
    for old, (new, (email, org)) in moves.items():
        row = rows.get(old)
        if (
            isinstance(row, dict)
            and row.get("email") == email
            and (row.get("organizationUuid") or "") == (org or "")
        ):
            out[new] = row
    return out, out != rows


# -- the auto-switch engine's slot-keyed state ---------------------------------
#
# ``autoswitch_state.json`` names accounts by slot number in a few places:
# ``quarantine`` (keyed by number, each entry carries the email),
# ``lastSeenActive``, ``lastSwitchTo``, ``lastSwitchFrom`` and
# ``manualHold.account``. A move keeps them pointing at the same accounts;
# a removal drops them, so a number reused by a new account never inherits
# a quarantine, a manual hold or the anti-flap bar of the account it replaced.

_NUMBER_FIELDS = ("lastSeenActive", "lastSwitchTo", "lastSwitchFrom")


def _remap_value(value: object, mapping: dict[str, str]) -> object:
    """``value`` (a slot number as str or int) through ``mapping``, keeping its type."""
    if value is None or isinstance(value, bool):
        return value
    key = str(value)
    if key not in mapping:
        return value
    new = mapping[key]
    return int(new) if isinstance(value, int) else new


def remap_engine_state(state: dict, mapping: dict[str, str]) -> bool:
    """Rewrite the engine state's slot numbers through ``mapping`` (old -> new).

    ``mapping`` lists only the numbers that moved; a swap lists both. Returns
    True when anything changed.
    """
    if not isinstance(state, dict) or not mapping:
        return False
    changed = False
    for field in _NUMBER_FIELDS:
        if field in state:
            new = _remap_value(state[field], mapping)
            if new != state[field]:
                state[field] = new
                changed = True
    hold = state.get("manualHold")
    if isinstance(hold, dict) and "account" in hold:
        new = _remap_value(hold["account"], mapping)
        if new != hold["account"]:
            hold["account"] = new
            changed = True
    quarantine = state.get("quarantine")
    if isinstance(quarantine, dict) and any(str(k) in mapping for k in quarantine):
        moved = {str(k): v for k, v in quarantine.items() if str(k) in mapping}
        kept = {k: v for k, v in quarantine.items() if str(k) not in mapping}
        for old, entry in moved.items():
            kept[mapping[old]] = entry
        state["quarantine"] = kept
        changed = True
    return changed


def forget_engine_slot(state: dict, num: str, email: str | None = None) -> bool:
    """Drop every engine reference to slot ``num`` (its account is gone).

    A quarantine entry is dropped only when it names ``email`` (or ``email``
    is None): an entry for another account at that number is not this
    removal's to clear. Returns True when anything changed.
    """
    if not isinstance(state, dict):
        return False
    num = str(num)
    changed = False
    for field in _NUMBER_FIELDS:
        if field in state and state[field] is not None and str(state[field]) == num:
            del state[field]
            changed = True
    hold = state.get("manualHold")
    if isinstance(hold, dict) and str(hold.get("account")) == num:
        del state["manualHold"]
        changed = True
    quarantine = state.get("quarantine")
    if isinstance(quarantine, dict) and num in quarantine:
        entry = quarantine[num]
        if email is None or not isinstance(entry, dict) or entry.get("email") == email:
            del quarantine[num]
            changed = True
    return changed
