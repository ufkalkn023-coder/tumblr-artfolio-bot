"""Final feed-diversity guard for otherwise eligible artwork candidates.

The policy is deliberately deterministic and conservative. It consumes an
already ordered publication history and never reads the journal itself.
"""

import unicodedata

import config

ARTIST_COOLDOWN = "artist_cooldown"
SOURCE_CONSECUTIVE_LIMIT = "source_consecutive_limit"
MEDIUM_CONSECUTIVE_LIMIT = "medium_consecutive_limit"

_GENERIC_ARTIST_KEYS = {
    "anonymous",
    "unidentified",
    "unknown",
    "unknown artist",
}


def normalize_artist_name(value):
    """Return the canonical cooldown key for an artist name."""
    normalized = unicodedata.normalize("NFC", str(value or ""))
    normalized = " ".join(normalized.split()).casefold()
    if normalized in _GENERIC_ARTIST_KEYS:
        return ""
    return normalized


class FeedDiversityPolicy:
    """Evaluate persistent artist, source, and medium publication history."""

    def __init__(
        self,
        recent_published,
        *,
        artist_cooldown_posts=None,
        source_max_consecutive_posts=None,
        medium_max_consecutive_posts=None,
    ):
        self.recent_published = list(recent_published)
        self.artist_cooldown_posts = (
            config.ARTIST_COOLDOWN_POSTS
            if artist_cooldown_posts is None
            else artist_cooldown_posts
        )
        self.source_max_consecutive_posts = (
            config.SOURCE_MAX_CONSECUTIVE_POSTS
            if source_max_consecutive_posts is None
            else source_max_consecutive_posts
        )
        self.medium_max_consecutive_posts = (
            config.MEDIUM_MAX_CONSECUTIVE_POSTS
            if medium_max_consecutive_posts is None
            else medium_max_consecutive_posts
        )

    def rejection_reason(self, artwork):
        artist_key = normalize_artist_name(getattr(artwork, "artist", ""))
        if artist_key and self.artist_cooldown_posts > 0:
            for record in self.recent_published[:self.artist_cooldown_posts]:
                historical_key = getattr(record, "artist_key", "") or normalize_artist_name(
                    getattr(record, "artist_name", "")
                )
                if historical_key == artist_key:
                    return ARTIST_COOLDOWN

        recent_sources = self.recent_published[:self.source_max_consecutive_posts]
        if len(recent_sources) == self.source_max_consecutive_posts and all(
            getattr(record, "museum", "") == getattr(artwork, "museum", "")
            for record in recent_sources
        ):
            return SOURCE_CONSECUTIVE_LIMIT

        recent_media = self.recent_published[:self.medium_max_consecutive_posts]
        if len(recent_media) == self.medium_max_consecutive_posts and all(
            getattr(record, "medium_type", "") == getattr(artwork, "medium_type", "")
            for record in recent_media
        ):
            return MEDIUM_CONSECUTIVE_LIMIT

        return None
