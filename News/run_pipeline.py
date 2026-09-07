"""
Zolotel — Main pipeline runner for AI Healthcare Intelligence.

Executes the full pipeline end to end:
  1. Scrape healthcare-AI news from Google News RSS   (scraper_production.py)
  2. Process with Gemini: score, extract, fingerprint  (llm_processor_production.py)
  3. Cluster into events + pick representatives          (cluster_engine.py)

Intended to run on a schedule (e.g. a GitHub Actions daily cron).
Requires env vars: DATABASE_URL (Neon), GEMINI_API_KEY.
The schema (schema.sql) must already be applied to the database.
"""

import logging
import sys

from scraper_production import main as scraper_main
from llm_processor_production import main as llm_main
from cluster_engine import main as cluster_main
from ranking import main as ranking_main

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def main():
    """Run the complete Zolotel pipeline."""
    try:
        logging.info("🚀 Starting Zolotel — Healthcare AI Intelligence Pipeline")
        logging.info("")

        # Step 1: Scrape news
        logging.info("📰 STEP 1/4: Scraping healthcare-AI news...")
        scraper_main()
        logging.info("")

        # Step 2: Process with the LLM (score, extract, fingerprint)
        logging.info("🤖 STEP 2/4: Processing with Gemini...")
        llm_main()
        logging.info("")

        # Step 3: Cluster into events and select representatives
        logging.info("🔗 STEP 3/4: Clustering into events...")
        cluster_main()
        logging.info("")

        # Step 4: Rank articles and clusters
        logging.info("🏆 STEP 4/4: Ranking articles and clusters...")
        ranking_main()
        logging.info("")

        logging.info("✅ Pipeline completed successfully!")

    except Exception as e:
        logging.error(f"❌ Pipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
