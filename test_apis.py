"""Manual live museum API checks.

This module is intentionally import-safe and is not part of the normal unit
test suite. Run it directly when live API verification is needed:

    ./venv/bin/python test_apis.py
"""

import logging
from museum_api import MuseumAPIClient


def run_live_checks():
    logging.basicConfig(level=logging.INFO)
    client = MuseumAPIClient()
    checks = [
        ("The Met", client.fetch_met_artwork),
        ("AIC (Chicago)", client.fetch_aic_artwork),
        ("CMA (Cleveland)", client.fetch_cma_artwork),
        ("SMK (Denmark)", client.fetch_smk_artwork),
        ("Harvard", client.fetch_harvard_artwork),
    ]

    failures = []
    for name, fetcher in checks:
        print(f"\n--- TEST: {name} ---")
        artwork = fetcher(set())
        if artwork is None:
            failures.append(name)
            print(f"{name} result: FAILURE")
            continue
        print(f"{name} result: SUCCESS ({artwork.id})")

    if failures:
        raise RuntimeError(f"Live museum API checks failed: {', '.join(failures)}")


if __name__ == "__main__":
    run_live_checks()
