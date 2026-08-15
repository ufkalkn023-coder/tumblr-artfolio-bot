import logging
from museum_api import MuseumAPIClient

logging.basicConfig(level=logging.INFO)
client = MuseumAPIClient()

print("\n--- TEST: The Met ---")
met = client.fetch_met_artwork(set())
print(f"The Met result: {'SUCCESS' if met else 'FAILURE'}")

print("\n--- TEST: AIC (Chicago) ---")
aic = client.fetch_aic_artwork(set())
print(f"AIC result: {'SUCCESS' if aic else 'FAILURE'}")

print("\n--- TEST: CMA (Cleveland) ---")
cma = client.fetch_cma_artwork(set())
print(f"CMA result: {'SUCCESS' if cma else 'FAILURE'}")

print("\n--- TEST: SMK (Denmark) ---")
smk = client.fetch_smk_artwork(set())
print(f"SMK result: {'SUCCESS' if smk else 'FAILURE'}")

print("\n--- TEST: Harvard ---")
harvard = client.fetch_harvard_artwork(set())
print(f"Harvard result: {'SUCCESS' if harvard else 'FAILURE (Expected if no API key)'}")
