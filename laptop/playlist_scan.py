"""Safe, newest-first playlist history scan for Trinity downloaders.

The YouTube playlist's returned order is authoritative: index 1 is the first
item displayed by YouTube, not necessarily the newest upload date. Keep the
existing 30-consecutive-history-matches optimization, but only terminate if
no unprocessed playlist ID occurs later in that same ordered playlist.
"""


def last_unprocessed_position(entries, history):
    """Return last 1-based playlist position without a download-history ID.

    This preflight examines IDs only, not media files or video downloads.
    History is the union of online Google Sheets and the pending local journal,
    as supplied by shared_history.load_set(). Unknown/empty IDs are ignored.
    """
    known = set(history)
    return max(
        (position for position, entry in enumerate(entries, start=1)
         if entry and entry.get("id") and entry["id"] not in known),
        default=0,
    )


def safe_to_stop(streak, threshold, position, last_unprocessed):
    """Require 30 consecutive matches AND no future unseen playlist entries."""
    return streak >= threshold and position >= last_unprocessed
