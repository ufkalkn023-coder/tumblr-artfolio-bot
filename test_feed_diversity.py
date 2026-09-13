"""Persistent artist/source/medium feed-diversity policy tests.

All history is in memory. Source fetches are deterministic fakes; no network
or Tumblr calls are made.
"""

import importlib
import os
import random
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import config
from artfolio.curation.diversity import (
    ARTIST_COOLDOWN,
    MEDIUM_CONSECUTIVE_LIMIT,
    SOURCE_CONSECUTIVE_LIMIT,
    FeedDiversityPolicy,
    normalize_artist_name,
)
from artfolio.curation.museum_client import MuseumAPIClient
from artfolio.sources.base import SourceAttemptStatus, SourceFetchResult
from artfolio.domain.artwork import Artwork
from publication_state import PublicationRecord


def make_artwork(
    artwork_id="candidate",
    *,
    museum="met",
    artist="Claude Monet",
    medium_type="Painting",
    score=95,
    is_public_domain=True,
):
    return Artwork(
        museum=museum,
        id=artwork_id,
        title="Test Artwork",
        artist=artist,
        artist_bio=artist,
        date="1900",
        image_url="https://example.com/image.jpg",
        original_source_url="https://example.com/artwork",
        museum_name="Test Museum",
        location_info="Gallery",
        dimensions="10 cm",
        medium_type=medium_type,
        raw_medium="Oil on canvas",
        score=score,
        is_public_domain=is_public_domain,
    )


def published(
    index,
    *,
    museum="met",
    artist="Claude Monet",
    medium_type="Painting",
):
    published_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    return PublicationRecord(
        museum=museum,
        artwork_id=str(index),
        status="published",
        reserved_at=published_at.isoformat(),
        published_at=published_at.isoformat(),
        artist_name=artist,
        artist_key=normalize_artist_name(artist),
        medium_type=medium_type,
    )


class TestDiversityConfig(unittest.TestCase):
    _KEYS = {
        "ARTIST_COOLDOWN_POSTS",
        "SOURCE_MAX_CONSECUTIVE_POSTS",
        "MEDIUM_MAX_CONSECUTIVE_POSTS",
    }

    def test_defaults_match_feed_policy(self):
        clean_env = {key: value for key, value in os.environ.items() if key not in self._KEYS}
        try:
            with patch.dict(os.environ, clean_env, clear=True):
                reloaded = importlib.reload(config)
            self.assertEqual(reloaded.ARTIST_COOLDOWN_POSTS, 12)
            self.assertEqual(reloaded.SOURCE_MAX_CONSECUTIVE_POSTS, 3)
            self.assertEqual(reloaded.MEDIUM_MAX_CONSECUTIVE_POSTS, 3)
        finally:
            importlib.reload(config)

    def test_invalid_limits_fail_at_startup(self):
        invalid_cases = (
            ("ARTIST_COOLDOWN_POSTS", "-1"),
            ("SOURCE_MAX_CONSECUTIVE_POSTS", "0"),
            ("MEDIUM_MAX_CONSECUTIVE_POSTS", "0"),
        )
        clean_env = {key: value for key, value in os.environ.items() if key not in self._KEYS}
        try:
            for key, value in invalid_cases:
                with self.subTest(key=key), patch.dict(
                    os.environ, {**clean_env, key: value}, clear=True
                ):
                    with self.assertRaises(ValueError):
                        importlib.reload(config)
        finally:
            importlib.reload(config)


class TestArtistNormalization(unittest.TestCase):
    def test_whitespace_case_and_unicode_are_normalized(self):
        self.assertEqual(normalize_artist_name("  Claude   MONET  "), "claude monet")
        self.assertEqual(normalize_artist_name("Ame\u0301lie"), normalize_artist_name("Am\u00e9lie"))

    def test_generic_unknown_identities_have_no_key(self):
        for artist in ("Unknown", "Anonymous", "Unknown Artist", "Unidentified"):
            with self.subTest(artist=artist):
                self.assertEqual(normalize_artist_name(artist), "")

    def test_attribution_qualifiers_remain_distinct(self):
        self.assertNotEqual(
            normalize_artist_name("Workshop of Rembrandt"),
            normalize_artist_name("Rembrandt"),
        )

    def test_different_real_artists_remain_distinct(self):
        self.assertNotEqual(normalize_artist_name("Claude Monet"), normalize_artist_name("Edouard Manet"))


class TestArtistCooldown(unittest.TestCase):
    def policy(self, history, cooldown=12):
        return FeedDiversityPolicy(
            history,
            artist_cooldown_posts=cooldown,
            source_max_consecutive_posts=99,
            medium_max_consecutive_posts=99,
        )

    def test_artist_inside_window_is_rejected(self):
        history = [published(index, artist="Other Artist") for index in range(20, 8, -1)]
        history[5] = published(15, artist="Claude Monet")
        self.assertEqual(self.policy(history).rejection_reason(make_artwork()), ARTIST_COOLDOWN)

    def test_artist_at_exact_window_boundary_is_rejected(self):
        history = [published(index, artist="Other Artist") for index in range(20, 9, -1)]
        history.append(published(9, artist="Claude Monet"))
        self.assertEqual(self.policy(history).rejection_reason(make_artwork()), ARTIST_COOLDOWN)

    def test_artist_outside_window_is_allowed(self):
        history = [published(index, artist="Other Artist") for index in range(20, 8, -1)]
        history.append(published(8, artist="Claude Monet"))
        self.assertIsNone(self.policy(history).rejection_reason(make_artwork()))

    def test_different_artist_is_allowed(self):
        self.assertIsNone(self.policy([published(1)]).rejection_reason(make_artwork(artist="Berthe Morisot")))

    def test_unknown_artist_bypasses_cooldown(self):
        history = [published(1, artist="Unknown"), published(0, artist="Anonymous")]
        self.assertIsNone(self.policy(history).rejection_reason(make_artwork(artist="Unknown Artist")))

    def test_zero_disables_artist_cooldown(self):
        self.assertIsNone(self.policy([published(1)], cooldown=0).rejection_reason(make_artwork()))


class TestConsecutiveLimits(unittest.TestCase):
    def policy(self, history, source_limit=3, medium_limit=3):
        return FeedDiversityPolicy(
            history,
            artist_cooldown_posts=0,
            source_max_consecutive_posts=source_limit,
            medium_max_consecutive_posts=medium_limit,
        )

    def test_source_under_limit_allows(self):
        history = [published(2, museum="met"), published(1, museum="met")]
        self.assertIsNone(self.policy(history).rejection_reason(make_artwork(museum="met")))

    def test_source_limit_rejects_same_source(self):
        history = [published(index, museum="met") for index in (3, 2, 1)]
        self.assertEqual(
            self.policy(history).rejection_reason(make_artwork(museum="met")),
            SOURCE_CONSECUTIVE_LIMIT,
        )

    def test_different_source_is_allowed_and_breaks_sequence(self):
        history = [published(4, museum="met", medium_type="Painting"),
                   published(3, museum="cma", medium_type="Sculpture"),
                   published(2, museum="met", medium_type="Drawing"),
                   published(1, museum="met", medium_type="Object")]
        self.assertIsNone(self.policy(history).rejection_reason(make_artwork(museum="met")))
        self.assertIsNone(self.policy(history[:3]).rejection_reason(make_artwork(museum="smk")))

    def test_medium_under_limit_allows(self):
        history = [published(2, medium_type="Painting"), published(1, medium_type="Painting")]
        self.assertIsNone(self.policy(history).rejection_reason(make_artwork(medium_type="Painting")))

    def test_medium_limit_rejects_same_medium(self):
        history = [published(index, museum=f"source-{index}", medium_type="Painting") for index in (3, 2, 1)]
        self.assertEqual(
            self.policy(history).rejection_reason(make_artwork(medium_type="Painting")),
            MEDIUM_CONSECUTIVE_LIMIT,
        )

    def test_different_medium_is_allowed(self):
        history = [published(index, museum=f"source-{index}", medium_type="Painting") for index in (3, 2, 1)]
        self.assertIsNone(self.policy(history).rejection_reason(make_artwork(medium_type="Sculpture")))


class TestSelectionIntegration(unittest.TestCase):
    @staticmethod
    def _install_fetchers(client, artworks_by_source, calls):
        for source in ("met", "cma", "smk", "harvard"):
            artwork = artworks_by_source.get(source)

            def fetch(_posted_ids, _target_medium, source=source, artwork=artwork):
                calls.append(source)
                client.last_fetch_stats = {
                    "source": source,
                    "candidates": int(artwork is not None),
                    "duplicates": 0,
                    "rejected_image": 0,
                    "rejected_quality": 0,
                    "rejected_other": 0,
                    "rejected_rights": 0,
                    "eligible": int(artwork is not None),
                }
                return artwork

            setattr(client, f"fetch_{source}_artwork", fetch)

    def test_diversity_rejection_has_separate_stats_and_continues(self):
        history = [
            published(3, artist="Claude Monet", museum="old-3", medium_type="Painting"),
            published(2, artist="Other Artist", museum="old-2", medium_type="Sculpture"),
            published(1, artist="Third Artist", museum="old-1", medium_type="Drawing"),
        ]
        client = MuseumAPIClient(recent_published=history)
        calls = []
        self._install_fetchers(client, {
            "met": make_artwork("blocked", museum="met", artist="Claude Monet"),
            "cma": make_artwork("selected", museum="cma", artist="Berthe Morisot"),
        }, calls)
        met_results = iter([
            make_artwork("blocked", museum="met", artist="Claude Monet"),
            None,
        ])

        def fetch_met(_posted_ids, _target_medium):
            calls.append("met")
            artwork = next(met_results)
            client.last_fetch_stats = {
                "source": "met", "candidates": int(artwork is not None),
                "duplicates": 0, "rejected_image": 0, "rejected_quality": 0,
                "rejected_other": 0, "rejected_rights": 0,
                "eligible": int(artwork is not None),
            }
            return artwork

        client.fetch_met_artwork = fetch_met

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False), \
                patch("random.shuffle", side_effect=lambda values: None), \
                self.assertLogs("artfolio_bot.museum_api", level="INFO") as captured:
            result = client.get_random_artwork({})

        self.assertEqual(result.id, "selected")
        self.assertEqual(calls[:3], ["met", "met", "cma"])
        self.assertEqual(client.last_run_stats["rejected_diversity"], 1)
        self.assertEqual(client.last_run_stats[ARTIST_COOLDOWN], 1)
        self.assertEqual(client.last_run_stats["rejected_quality"], 0)
        self.assertEqual(client.last_run_stats["rejected_rights"], 0)
        self.assertTrue(any(
            "diversity_rejection source=met object_id=blocked reason=artist_cooldown" in line
            for line in captured.output
        ))

    def test_artist_rejection_considers_same_source_alternative(self):
        history = [published(1, artist="Claude Monet", museum="old-source")]
        client = MuseumAPIClient(recent_published=history)
        blocked = make_artwork("blocked", museum="met", artist="Claude Monet")
        allowed = make_artwork("allowed", museum="met", artist="Berthe Morisot")
        seen_posted_sets = []

        def fetch_met(posted_ids, _target_medium):
            seen_posted_sets.append(set(posted_ids))
            artwork = allowed if "blocked" in posted_ids else blocked
            client.last_fetch_stats = {
                "source": "met", "candidates": 1, "duplicates": 0,
                "rejected_image": 0, "rejected_quality": 0,
                "rejected_other": 0, "rejected_rights": 0, "eligible": 1,
            }
            return artwork

        client.fetch_met_artwork = fetch_met
        for source in ("cma", "smk", "harvard"):
            setattr(client, f"fetch_{source}_artwork", lambda _ids, _medium: None)

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False), \
                patch("random.shuffle", side_effect=lambda values: None):
            result = client.get_random_artwork({})

        self.assertEqual(result.id, "allowed")
        self.assertEqual(seen_posted_sets, [set(), {"blocked"}])
        self.assertEqual(client.last_run_stats["rejected_diversity"], 1)
        self.assertEqual(client.last_run_stats[ARTIST_COOLDOWN], 1)

    def test_transient_diversity_exclusion_is_not_counted_as_duplicate(self):
        history = [published(1, artist="Claude Monet", museum="old-source")]
        client = MuseumAPIClient(recent_published=history)
        client.fetch_met_artwork = lambda _ids, _medium: None
        for source in ("smk", "harvard"):
            setattr(client, f"fetch_{source}_artwork", lambda _ids, _medium: None)

        def cma_item(artwork_id, artist):
            return {
                "id": artwork_id,
                "title": "Collection Highlight",
                "creators": [{"description": f"{artist} (French, 1840-1926)"}],
                "creation_date": "1900",
                "technique": "Oil on canvas",
                "type": "Painting",
                "department": "European Art",
                "culture": ["French"],
                "images": {"web": {"url": "https://example.com/image.jpg"}},
                "current_location": "Gallery 1",
                "share_license_status": "CC0",
                "url": f"https://example.com/{artwork_id}",
            }

        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [cma_item("blocked", "Claude Monet"),
                     cma_item("allowed", "Berthe Morisot")]
        }
        client.session = Mock()
        client.session.get.return_value = response

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False), \
                patch("random.shuffle", side_effect=lambda values: None):
            result = client.get_random_artwork({})

        self.assertEqual(result.id, "allowed")
        self.assertEqual(client.last_run_stats["duplicates"], 0)
        self.assertEqual(client.scoring_telemetry.sources["cma"]["duplicates"], 0)
        self.assertEqual(client.pool_telemetry.sources["cma"]["duplicates"], 0)

    def test_diversity_guard_runs_after_rights_quality_and_target_medium(self):
        policy = Mock()
        policy.rejection_reason.return_value = ARTIST_COOLDOWN
        client = MuseumAPIClient(diversity_policy=policy)
        calls = []
        self._install_fetchers(client, {
            "met": make_artwork("rights", is_public_domain=False),
            "cma": make_artwork("quality", museum="cma", score=79),
            "smk": make_artwork("medium", museum="smk", medium_type="Sculpture"),
        }, calls)

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False), \
                patch("random.shuffle", side_effect=lambda values: None):
            result = client.get_random_artwork({}, target_medium="Painting")

        self.assertIsNone(result)
        policy.rejection_reason.assert_not_called()
        self.assertEqual(client.last_run_stats["rejected_diversity"], 0)

    def test_diversity_rejection_never_increments_source_failures(self):
        history = [published(index, museum="met", artist=f"Artist {index}") for index in (3, 2, 1)]
        client = MuseumAPIClient(recent_published=history)
        calls = []
        self._install_fetchers(client, {"met": make_artwork("blocked", museum="met")}, calls)
        client.health.record_failure = Mock(wraps=client.health.record_failure)

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False), \
                patch("random.shuffle", side_effect=lambda values: None):
            self.assertIsNone(client.get_random_artwork({}))

        client.health.record_failure.assert_not_called()
        self.assertEqual(client.last_run_stats[SOURCE_CONSECUTIVE_LIMIT], 1)

    def test_healthy_fetch_rejected_by_diversity_remains_source_success(self):
        history = [published(index, museum="met", artist=f"Artist {index}") for index in (3, 2, 1)]
        client = MuseumAPIClient(recent_published=history)
        met = client._sources["met"]

        def fetch(_posted_ids, _target_medium):
            met.last_fetch_stats = {
                "source": "met", "candidates": 1, "duplicates": 0,
                "rejected_image": 0, "rejected_quality": 0,
                "rejected_other": 0, "rejected_rights": 0, "eligible": 1,
            }
            met.last_fetch_health = SourceFetchResult(SourceAttemptStatus.SUCCESS)
            return make_artwork("blocked", museum="met")

        met.fetch_artwork = fetch
        for source in ("cma", "smk", "harvard"):
            setattr(client, f"fetch_{source}_artwork", lambda _ids, _medium: None)

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False), \
                patch("random.shuffle", side_effect=lambda values: None):
            self.assertIsNone(client.get_random_artwork({}))

        health = client.get_source_health()["met"]
        self.assertEqual(health["total_successes"], 1)
        self.assertEqual(health["total_failures"], 0)
        self.assertEqual(health["state"], "closed")
        self.assertEqual(client.last_run_stats["circuit_skips"], 0)

    def test_empty_history_preserves_source_order_and_selection(self):
        no_op_policy = Mock()
        no_op_policy.rejection_reason.return_value = None
        baseline = MuseumAPIClient(diversity_policy=no_op_policy)
        explicit_empty = MuseumAPIClient(recent_published=[])
        baseline_calls = []
        empty_calls = []
        artworks = {"cma": make_artwork("selected", museum="cma")}
        self._install_fetchers(baseline, artworks, baseline_calls)
        self._install_fetchers(explicit_empty, artworks, empty_calls)

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", False):
            random.seed(8675309)
            baseline_result = baseline.get_random_artwork({})
            baseline_random_state = random.getstate()
            random.seed(8675309)
            empty_result = explicit_empty.get_random_artwork({})
            empty_random_state = random.getstate()

        self.assertEqual(baseline_result.id, empty_result.id)
        self.assertEqual(baseline_calls, empty_calls)
        self.assertEqual(baseline.last_run_stats, explicit_empty.last_run_stats)
        self.assertEqual(baseline_random_state, empty_random_state)


if __name__ == "__main__":
    unittest.main()
