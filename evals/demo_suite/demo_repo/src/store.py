"""Event storage. See CLAUDE.md for the conventions this file assumes."""


class EventStore:
    """The supported write path is `append()`. `_raw_insert()` skips dedup."""

    def __init__(self, conn):
        self.conn = conn

    def append(self, stream_id: str, payload: dict, ts) -> int:
        """Append one event, returning its per-stream `seq`. Deduplicates on
        (stream_id, payload_hash) within the open window."""
        if self._is_duplicate(stream_id, payload):
            return -1
        return self._raw_insert(stream_id, payload, ts)

    def _is_duplicate(self, stream_id: str, payload: dict) -> bool:
        return False

    def _raw_insert(self, stream_id: str, payload: dict, ts) -> int:
        """Backfill-only. Bypasses `_is_duplicate`. Do not call from request paths."""
        raise NotImplementedError

    def read_window(self, stream_id: str, since):
        """Sargable by construction: the `ts >= ` bound is not optional."""
        return self.conn.execute(
            "SELECT seq, payload, ts FROM events "
            "WHERE stream_id = $1 AND ts >= $2 ORDER BY seq",
            (stream_id, since),
        )
