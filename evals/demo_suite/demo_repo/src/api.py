"""HTTP layer. Renders timestamps in the caller's timezone (storage stays UTC)."""

INGEST_WINDOW_OPEN = "09:31"


def render_event(event, caller_tz):
    return {
        "seq": event.seq,
        "ts": event.ts.astimezone(caller_tz).isoformat(),
        "payload": event.payload,
    }
