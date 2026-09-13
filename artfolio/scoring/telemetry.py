"""Skorlama telemetrisi: salt gözlem amaçlı toplamalar; seçim yoluna ASLA katılmaz."""

from typing import Dict

import config

from .scorer import ArtworkScorer

SCORING_BANDS = ((0, 19), (20, 39), (40, 59), (60, 79), (80, 100))


def classify_secondary_telemetry_label(raw_medium: str, classification: str, object_name: str) -> str:
    """Add conservative, telemetry-only labels without affecting selection."""
    text = f"{raw_medium} {classification} {object_name}".casefold()
    matches = set()
    if "manuscript" in text:
        matches.add("manuscript")
    if any(term in text for term in ("print", "engraving", "etching", "woodcut", "lithograph")):
        matches.add("print")
    if any(term in text for term in ("ceramic", "pottery", "porcelain", "earthenware")):
        matches.add("ceramic")
    if any(term in text for term in ("decorative art", "decorative arts", "artifact", "vessel", "object")):
        matches.add("decorative/object")
    return matches.pop() if len(matches) == 1 else "other"


class ScoringTelemetry:
    """In-memory scoring aggregates; never participates in selection."""

    def __init__(self):
        self.sources = {}
        self.categories = {category: self._new_bucket() for category in ("Painting", "Sculpture", "Drawing", "Object")}
        self.secondary_categories = {}
        self.artists = {"known": self._new_bucket(), "unknown": self._new_bucket()}
        self.flags = {flag: self._new_bucket() for flag in ("highlight", "on_view", "additional_images")}

    @staticmethod
    def _new_bucket():
        return {
            "evaluated": 0,
            "scored": 0,
            "eligible": 0,
            "score_sum": 0,
            "min": None,
            "max": None,
            "bands": {f"{low}-{high}": 0 for low, high in SCORING_BANDS},
            "duplicates": 0,
            "attempts": 0,
            "first_eligible": 0,
            "first_eligible_scores": [],
            "selected_scores": [],
        }

    def _source_bucket(self, source):
        if source not in self.sources:
            self.sources[source] = self._new_bucket()
        return self.sources[source]

    def _record_score(self, bucket, score):
        bucket["scored"] += 1
        bucket["score_sum"] += score
        bucket["min"] = score if bucket["min"] is None else min(bucket["min"], score)
        bucket["max"] = score if bucket["max"] is None else max(bucket["max"], score)
        for low, high in SCORING_BANDS:
            if low <= score <= high:
                bucket["bands"][f"{low}-{high}"] += 1
                break
        if score >= config.MINIMUM_QUALITY_SCORE:
            bucket["eligible"] += 1

    def record_duplicate(self, source, count=1):
        self._source_bucket(source)["duplicates"] += count

    def record_evaluated(self, source):
        self._source_bucket(source)["evaluated"] += 1

    def record_attempt(self, source):
        self._source_bucket(source)["attempts"] += 1

    def record_first_eligible(self, source, score):
        bucket = self._source_bucket(source)
        bucket["first_eligible"] += 1
        bucket["first_eligible_scores"].append(score)

    def record_selected(self, source, score):
        self._source_bucket(source)["selected_scores"].append(score)

    def record_scored(self, source, raw_medium, classification, object_name, artist, score, is_highlight, on_view, has_additional_images):
        source_bucket = self._source_bucket(source)
        self._record_score(source_bucket, score)

        category = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
        category_bucket = self.categories[category]
        category_bucket["evaluated"] += 1
        self._record_score(category_bucket, score)

        secondary = classify_secondary_telemetry_label(raw_medium, classification, object_name)
        secondary_bucket = self.secondary_categories.setdefault(secondary, self._new_bucket())
        secondary_bucket["evaluated"] += 1
        self._record_score(secondary_bucket, score)

        artist_key = "unknown" if any(term in (artist or "").casefold() for term in ("unknown", "anonymous", "unidentified", "various", "maker unknown")) else "known"
        artist_bucket = self.artists[artist_key]
        artist_bucket["evaluated"] += 1
        self._record_score(artist_bucket, score)

        for flag, enabled in (("highlight", is_highlight), ("on_view", on_view), ("additional_images", has_additional_images)):
            if enabled:
                flag_bucket = self.flags[flag]
                flag_bucket["evaluated"] += 1
                self._record_score(flag_bucket, score)

    @staticmethod
    def _average(bucket):
        return "n/a" if not bucket["scored"] else f"{bucket['score_sum'] / bucket['scored']:.1f}"

    @staticmethod
    def _export_bucket(bucket):
        return {
            "evaluated": bucket["evaluated"],
            "scored": bucket["scored"],
            "duplicates": bucket["duplicates"],
            "eligible": bucket["eligible"],
            "rejected_score": bucket["scored"] - bucket["eligible"],
            "avg_score": None if not bucket["scored"] else round(bucket["score_sum"] / bucket["scored"], 1),
            "min_score": bucket["min"],
            "max_score": bucket["max"],
            "score_bands": dict(bucket["bands"]),
        }

    def export_selection_path(self):
        sources = {}
        selected_source = None
        selected_score = None
        for source in sorted(self.sources):
            bucket = self.sources[source]
            source_selected_score = bucket["selected_scores"][-1] if bucket["selected_scores"] else None
            sources[source] = {
                "attempts": bucket["attempts"],
                "examined": bucket["evaluated"],
                "rejected_before_first_eligible": bucket["evaluated"] - bucket["first_eligible"],
                "first_eligible": bucket["first_eligible"],
                "selected_score": source_selected_score,
            }
            if source_selected_score is not None:
                selected_source = source
                selected_score = source_selected_score
        return {
            "sources": sources,
            "selected_source": selected_source,
            "selected_score": selected_score,
        }

    def log(self, logger):
        if not self.sources:
            logger.info("selection_path_stats empty=true")
            return

        for source in sorted(self.sources):
            bucket = self.sources[source]
            selected_score = bucket["selected_scores"][-1] if bucket["selected_scores"] else "none"
            logger.info(
                "selection_path_stats source=%s attempts=%d examined=%d rejected=%d first_eligible=%d selected_score=%s",
                source, bucket["attempts"], bucket["evaluated"], bucket["evaluated"] - bucket["first_eligible"],
                bucket["first_eligible"], selected_score,
            )

    def log_pool(self, logger, coverage):
        if not self.sources:
            logger.info("pool_stats empty=true")
            return

        for source in sorted(self.sources):
            bucket = self.sources[source]
            bands = ",".join(f"{low}-{high}:{bucket['bands'][f'{low}-{high}']}" for low, high in SCORING_BANDS)
            source_coverage = coverage.get(source, {"coverage": "partial", "materialized": 0})
            logger.info(
                "pool_stats source=%s coverage=%s materialized=%d evaluated=%d scored=%d duplicates=%d eligible=%d rejected_score=%d avg=%s min=%s max=%s bands=%s",
                source, source_coverage["coverage"], source_coverage["materialized"], bucket["evaluated"], bucket["scored"],
                bucket["duplicates"], bucket["eligible"], bucket["scored"] - bucket["eligible"], self._average(bucket),
                bucket["min"] if bucket["min"] is not None else "n/a", bucket["max"] if bucket["max"] is not None else "n/a", bands,
            )
            category_map = getattr(self, "source_categories", {}).get(source, {})
            for category in ("Painting", "Sculpture", "Drawing", "Object"):
                category_bucket = category_map.get(category, self._new_bucket())
                logger.info(
                    "pool_category_stats source=%s category=%s evaluated=%d eligible=%d avg=%s",
                    source, category, category_bucket["evaluated"], category_bucket["eligible"], self._average(category_bucket),
                )

            secondary_map = getattr(self, "source_secondary_categories", {}).get(source, {})
            for category in sorted(secondary_map):
                category_bucket = secondary_map[category]
                logger.info(
                    "pool_secondary_category_stats source=%s category=%s evaluated=%d eligible=%d avg=%s",
                    source, category, category_bucket["evaluated"], category_bucket["eligible"], self._average(category_bucket),
                )

            artist_map = getattr(self, "source_artists", {}).get(source, {})
            known = artist_map.get("known", self._new_bucket())
            unknown = artist_map.get("unknown", self._new_bucket())
            known_rate = "n/a" if not known["evaluated"] else f"{known['eligible'] / known['evaluated']:.1%}"
            unknown_rate = "n/a" if not unknown["evaluated"] else f"{unknown['eligible'] / unknown['evaluated']:.1%}"
            logger.info(
                "pool_artist_stats source=%s known evaluated=%d eligible=%d eligibility_rate=%s avg=%s unknown evaluated=%d eligible=%d eligibility_rate=%s avg=%s",
                source, known["evaluated"], known["eligible"], known_rate, self._average(known),
                unknown["evaluated"], unknown["eligible"], unknown_rate, self._average(unknown),
            )

            flag_map = getattr(self, "source_flags", {}).get(source, {})
            for flag in ("highlight", "on_view", "additional_images"):
                flag_bucket = flag_map.get(flag, self._new_bucket())
                logger.info(
                    "pool_flag_stats source=%s flag=%s evaluated=%d eligible=%d avg=%s",
                    source, flag, flag_bucket["evaluated"], flag_bucket["eligible"], self._average(flag_bucket),
                )


class CandidatePoolTelemetry(ScoringTelemetry):
    def __init__(self):
        super().__init__()
        self.source_categories = {}
        self.source_secondary_categories = {}
        self.source_artists = {}
        self.source_flags = {}

    @staticmethod
    def _dimension_bucket(collection, source, key):
        source_buckets = collection.setdefault(source, {})
        return source_buckets.setdefault(key, ScoringTelemetry._new_bucket())

    def record_scored(self, source, raw_medium, classification, object_name, artist, score, is_highlight, on_view, has_additional_images):
        self._record_score(self._source_bucket(source), score)

        category = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
        category_bucket = self._dimension_bucket(self.source_categories, source, category)
        category_bucket["evaluated"] += 1
        self._record_score(category_bucket, score)

        secondary = classify_secondary_telemetry_label(raw_medium, classification, object_name)
        secondary_bucket = self._dimension_bucket(self.source_secondary_categories, source, secondary)
        secondary_bucket["evaluated"] += 1
        self._record_score(secondary_bucket, score)

        artist_key = "unknown" if any(term in (artist or "").casefold() for term in ("unknown", "anonymous", "unidentified", "various", "maker unknown")) else "known"
        artist_bucket = self._dimension_bucket(self.source_artists, source, artist_key)
        artist_bucket["evaluated"] += 1
        self._record_score(artist_bucket, score)

        for flag, enabled in (("highlight", is_highlight), ("on_view", on_view), ("additional_images", has_additional_images)):
            if enabled:
                flag_bucket = self._dimension_bucket(self.source_flags, source, flag)
                flag_bucket["evaluated"] += 1
                self._record_score(flag_bucket, score)

    @staticmethod
    def _export_dimension_map(dimension_map: Dict, source):
        return {
            key: ScoringTelemetry._export_bucket(dimension_map[source][key])
            for key in sorted(dimension_map.get(source, {}))
        }

    def export_pool(self, coverage):
        sources = {}
        for source in sorted(self.sources):
            source_coverage = coverage.get(source, {"coverage": "partial", "materialized": 0})
            source_data = ScoringTelemetry._export_bucket(self.sources[source])
            source_data.update({
                "coverage": source_coverage["coverage"],
                "materialized": source_coverage["materialized"],
                "primary_category_stats": self._export_dimension_map(self.source_categories, source),
                "secondary_category_stats": self._export_dimension_map(self.source_secondary_categories, source),
                "artist_stats": self._export_dimension_map(self.source_artists, source),
                "flag_stats": self._export_dimension_map(self.source_flags, source),
            })
            sources[source] = source_data
        return {"sources": sources}
