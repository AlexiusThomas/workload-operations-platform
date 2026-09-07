"""Reporting helpers — pure logic (no I/O).

Provides :func:`compute_week_key`, which maps an ISO-8601 UTC timestamp string to the
ISO week key ``YYYY-WNN`` used to partition ProductionEvents and MaterialEvents for the
weekly report (design: ProductionEvent week_key computation).

Pure function library: no I/O, no AWS calls, no environment reads. ``datetime`` parsing
operates entirely on the supplied string.
"""

from __future__ import annotations

from datetime import datetime


def compute_week_key(timestamp_iso: str) -> str:
    """Compute the ISO 8601 week key for an ISO-8601 timestamp.

    The week key is ``YYYY-WNN`` where ``YYYY`` is the ISO week-numbering year and
    ``NN`` is the zero-padded ISO week number (design: week_key Computation).

    Example: ``"2024-01-17T14:30:00Z"`` -> ``"2024-W03"``.

    Args:
        timestamp_iso: An ISO-8601 timestamp string. A trailing ``Z`` (UTC) is accepted.

    Returns:
        The ISO week key string, e.g. ``"2024-W03"``.
    """
    dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"
