"""Describe the progress of a long task: the share done and the time left."""

from __future__ import annotations

from dataclasses import dataclass

from portolan_registry_qgis.core.i18n import _, ngettext

# The share a task must gain after its first report before a time left shows.
# DuckDB and GDAL often report a few percent at once, then stall.
MIN_GAIN = 0.1


@dataclass
class Clock:
    """Estimate the time left of one task from its rate since its first report."""

    # When the first share above 0 came, and that share.
    first: float | None = None
    first_share: float = 0.0
    # The largest share so far. A later query of the same task can start again at 0.
    best: float = 0.0

    def share(self, fraction: float) -> float:
        """Return the share to show: the largest so far, from 0 to 1."""
        self.best = max(self.best, min(max(fraction, 0.0), 1.0))
        return self.best

    def left(self, fraction: float, now: float) -> float | None:
        """Return the seconds left at the rate since the first report, or None before it holds."""
        if not 0 < fraction < 1:
            return None
        if self.first is None:
            self.first, self.first_share = now, fraction
            return None
        gained = fraction - self.first_share
        if gained < MIN_GAIN:
            return None
        return (now - self.first) * (1 - fraction) / gained


def left_text(seconds: float) -> str:
    """Return the time left in rounded words, such as "about 15 s left"."""
    if seconds < 60:
        return _("about {n} s left").format(n=max(5, round(seconds / 5) * 5))
    if seconds < 3600:
        minutes = round(seconds / 60)
        return ngettext("about {n} min left", "about {n} min left", minutes).format(n=minutes)
    hours = round(seconds / 3600)
    return ngettext("about {n} hour left", "about {n} hours left", hours).format(n=hours)


def progress_text(message: str, fraction: float | None, left: float | None = None) -> str:
    """Return ``message`` with the percentage done and the seconds ``left``, when they are known."""
    if fraction is None:
        return message
    percent = max(0, min(100, int(fraction * 100)))
    if left is None:
        # Translators: {message} says what runs, and {percent} how much of it is done.
        return _("{message} {percent}%").format(message=message, percent=percent)
    # Translators: {left} is a time left, such as "about 15 s left".
    return _("{message} {percent}%, {left}").format(
        message=message, percent=percent, left=left_text(left)
    )
