"""Content Team: draft-only research/create/track roles for 6Frame Studio.

Nothing in this package may publish or send a reply. Outputs land in a
review queue. X-native posts arrive through the ingest API (Grok Bot
routines have free X reads). This package never calls the X API.
"""

PACK = "content-team-v1"
VERSION = "2026-11-16.2"

from content_team.guard import assert_no_publish_hooks  # noqa: F401

def health() -> dict:
    from content_team import scheduler, store
    from content_team.llm import provider_status
    return {
        "pack": PACK,
        "version": VERSION,
        "publish_blocked": True,
        "store": store.status(),
        "scheduler": scheduler.status(),
        "llm": provider_status(),
    }
