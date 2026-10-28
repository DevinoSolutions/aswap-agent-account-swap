"""Live settings for a running auto-switch engine (Claude Code and Codex).

``aswap auto`` and ``aswap codex auto`` are processes people leave running,
so an ``aswap config set`` must reach them without a restart. Both engines
hold one :class:`LiveSettings`: each tick it compares the settings document's
fingerprint with the last one seen (one indexed lookup) and, after a write,
re-reads the ``autoswitch`` section strictly and works out which keys move.
:func:`sliced_wait` lets the sleep between ticks end on such a write.

The engines own what happens next -- assigning the new settings, carrying
them into derived state and reporting them (a ``settings-reloaded`` event,
a ``config-warning`` per refused value) -- because only they know what is
derived from which key.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from claude_swap.endpoints import scaled
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    load_settings,
    reload_autoswitch_settings,
    settings_fingerprint,
)

_logger = logging.getLogger("claude-swap")

# How often a sleeping engine looks for a settings change (one indexed
# lookup of the settings document's ``updated_at``). A change ends the sleep,
# so ``aswap config set`` reaches a running engine within this long even
# while it waits out an exhausted account's reset.
SETTINGS_WATCH_S = scaled(10.0)
AUTOSWITCH_SPECS = tuple(
    spec for spec in SETTING_SPECS.values() if spec.section == "autoswitch"
)

# (dotted key, old, new) per applied change; (dotted key, stored, kept) per
# pinned key whose stored value moved.
Change = tuple[str, object, object]


@dataclass(frozen=True)
class SettingsReload:
    """What one settings write means for a running engine.

    ``settings`` is what the engine should run on from now on (the current
    settings with ``changes`` applied); ``problems`` holds one message per
    stored value that failed validation and was not applied.
    """

    settings: AutoSwitchSettings
    changes: tuple[Change, ...] = ()
    pinned: tuple[Change, ...] = ()
    problems: tuple[str, ...] = ()


class LiveSettings:
    """Tracks the settings document for one running engine.

    Holds the document's fingerprint and ``autoswitch`` section as last read,
    and the fields this process keeps whatever the document says: ``pinned``
    (command-line flags) plus any field the caller passed differently from
    the document (a TUI session threshold, a caller's own settings), plus any
    pinned later with :meth:`pin`.
    """

    def __init__(
        self,
        backup_dir: Path,
        settings: AutoSwitchSettings,
        pinned: Iterable[str] = (),
    ) -> None:
        self._backup_dir = backup_dir
        # Fingerprint first: a write racing these reads is then seen again
        # on the next poll rather than missed.
        self._fp = settings_fingerprint(backup_dir)
        self._stored, _problems = reload_autoswitch_settings(
            backup_dir, load_settings(backup_dir)
        )
        self._pinned: set[str] = set(pinned) | {
            spec.field
            for spec in AUTOSWITCH_SPECS
            if getattr(settings, spec.field) != getattr(self._stored, spec.field)
        }

    @property
    def pinned(self) -> frozenset[str]:
        return frozenset(self._pinned)

    def pin(self, field: str) -> None:
        """Keep ``field`` at the engine's value for the rest of this process."""
        self._pinned.add(field)

    def changed(self) -> bool:
        """Whether the settings document was written since the last poll."""
        return settings_fingerprint(self._backup_dir) != self._fp

    def poll(self, current: AutoSwitchSettings) -> SettingsReload | None:
        """Re-read the document if it was written; None when nothing to do.

        Costs one indexed lookup unless the document was written since the
        last poll; then its ``autoswitch`` section is re-read strictly (see
        ``reload_autoswitch_settings``) and every key whose stored value
        changed is applied to ``current``, except the pinned ones, which are
        reported instead. Never raises: a failed re-read returns None and
        the engine keeps the settings it has.
        """
        try:
            fp = settings_fingerprint(self._backup_dir)
            if fp == self._fp:
                return None
            stored, problems = reload_autoswitch_settings(self._backup_dir, self._stored)
            self._fp = fp
        except Exception:
            _logger.debug("settings reload failed", exc_info=True)
            return None
        previous_stored, self._stored = self._stored, stored
        updates: dict = {}
        changes: list[Change] = []
        pinned: list[Change] = []
        for spec in AUTOSWITCH_SPECS:
            new = getattr(stored, spec.field)
            if new == getattr(previous_stored, spec.field):
                continue
            kept = getattr(current, spec.field)
            if spec.field in self._pinned:
                pinned.append((spec.dotted, new, kept))
            elif new != kept:
                updates[spec.field] = new
                changes.append((spec.dotted, kept, new))
        return SettingsReload(
            settings=replace(current, **updates) if updates else current,
            changes=tuple(changes),
            pinned=tuple(pinned),
            problems=tuple(problems),
        )


def sliced_wait(
    delay: float,
    wake: threading.Event,
    stop: threading.Event,
    changed: Callable[[], bool],
) -> None:
    """Wait ``delay`` seconds, ending early on wake/stop or a settings change.

    Sliced by count, not against a deadline, so a test that stubs the wait
    still finishes in a bounded number of slices. ``changed`` failing is
    treated as "no change": the full delay is always safe.
    """
    slices = max(1, math.ceil(delay / SETTINGS_WATCH_S))
    step = delay / slices
    for _ in range(slices):
        if wake.wait(step) or stop.is_set():
            return
        try:
            if changed():
                return  # the next tick applies it
        except Exception:
            pass
