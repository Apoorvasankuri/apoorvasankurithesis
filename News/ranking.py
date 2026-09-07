"""
Zolotel — Ranking Engine (step 6)

Computes the executive rank score defined in the taxonomy:

  rank_score = category_impact + source_authority + evidence_strength
             + regulatory_significance + commercial_significance + adoption_scale
             + novelty + recency + confidence
             - duplicate_penalty - low_quality_penalty

Writes:
  processed_articles.rank_score   (per article)
  event_clusters.cluster_rank_score (representative article rank + multi-source corroboration)

Pipeline position: runs AFTER cluster_engine.py (it uses relationship_type and the
representative flag). Reads the factor weights from Healthcare_AI_Taxonomy.xlsx
(Sheets 5–7) with hardcoded fallbacks, so it runs even if the workbook is absent.
Pure arithmetic — no LLM calls. Re-running is idempotent and refreshes recency.

NOTE ON TUNABLES: the spec fixes the FACTORS and their MAX points (Sheet 6) plus the
category base scores (Sheet 7) and source-authority scores (Sheet 5). The intermediate
band mappings (e.g. evidence High->50 / Medium->30 / Low->10, recency decay) are sensible
defaults within those caps and are grouped below for easy tuning.
"""

import os
import re
import logging
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from datetime import datetime
from typing import Dict, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()

EXCEL_FILE_PATH = "Healthcare_AI_Taxonomy.xlsx"
MAX_ROWS_PER_RUN = 20000

# ─── Fallback weights (mirror the workbook; used if the Excel file is unavailable) ──

# Sheet 7 — Category Impact Scores (keyed by sub-vertical), max 100
CATEGORY_IMPACT_FALLBACK = {
    "regulatory approval / clearance": 100,
    "clinical validation / evidence": 95,
    "mergers & acquisitions": 90,
    "customer deployment / adoption": 85,
    "policy & governance": 85,
    "partnerships & alliances": 80,
    "funding & venture capital": 80,
    "safety, bias & ethics": 75,
    "legal & disputes": 75,
    "product launch": 65,
    "commercial expansion": 60,
    "technology breakthrough": 60,
    "cybersecurity & privacy": 60,
    "market outlook / industry trends": 45,
    "leadership & management": 35,
}
CATEGORY_IMPACT_DEFAULT = 25  # "generic commentary"

# Sheet 5 — Source Priority (keyed by source-quality label), max 60
SOURCE_AUTHORITY_FALLBACK = {
    "official": 60,
    "peer-reviewed": 55,
    "specialist media": 45,
    "investment source": 35,
    "business media": 30,
    "company pr": 30,
    "aggregator": 15,
    "low authority": 5,
}
SOURCE_AUTHORITY_DEFAULT = 10  # "Other"/unknown

# ─── Tunable band mappings (within the Sheet 6 caps) ───────────────────────────
EVIDENCE_SCORE = {"high": 50, "medium": 30, "low": 10}          # max 50
REGULATORY_SUBVERTICALS = {"regulatory approval / clearance", "policy & governance"}
COMMERCIAL_DEAL = {"mergers & acquisitions", "funding & venture capital"}            # 40
COMMERCIAL_PARTNERSHIP = {"partnerships & alliances"}                                # 30
COMMERCIAL_EXPANSION = {"commercial expansion", "customer deployment / adoption"}    # 25
NOVELTY_KEYWORDS = {
    "first-in-class", "first in class", "first-of-its-kind", "world's first",
    "first ", "novel", "breakthrough", "agentic", "foundation model",
    "new modality", "new class", "unprecedented",
}

# Penalties (Sheet 6): duplicate -30, low-quality -20
DUPLICATE_PENALTY = 30
LOW_QUALITY_PENALTY = 20


# ============================================================================
# WEIGHT LOADING
# ============================================================================

def load_weights() -> Dict[str, Dict]:
    """Load category-impact (Sheet 7) and source-authority (Sheet 5) maps from the
    workbook; fall back to the hardcoded tables if the file is missing/unreadable."""
    category = dict(CATEGORY_IMPACT_FALLBACK)
    source = dict(SOURCE_AUTHORITY_FALLBACK)
    try:
        import pandas as pd
        cat_df = pd.read_excel(EXCEL_FILE_PATH, sheet_name="Category Impact Scores", header=1)
        for _, r in cat_df.iterrows():
            sv, score = r.get("Sub-Vertical"), r.get("Base Impact Score")
            if pd.notna(sv) and pd.notna(score):
                category[str(sv).lower().strip()] = int(score)
        src_df = pd.read_excel(EXCEL_FILE_PATH, sheet_name="Source Priority", header=1)
        for _, r in src_df.iterrows():
            label, score = r.get("Source Quality Label"), r.get("Score")
            if pd.notna(label) and pd.notna(score):
                source[str(label).lower().strip()] = int(score)
        logging.info(f"📂 Loaded weights from {EXCEL_FILE_PATH} "
                     f"({len(category)} categories, {len(source)} source tiers)")
    except Exception as e:
        logging.warning(f"Using fallback weights (could not read {EXCEL_FILE_PATH}: {e})")
    return {"category": category, "source": source}


# ============================================================================
# DATABASE
# ============================================================================

def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL environment variable not set")
    return psycopg.connect(database_url, row_factory=dict_row)


def load_processed_articles(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, sub_vertical, source_quality, evidence_strength,
               regulators, health_systems, confidence_score, published_date,
               title, executive_summary, relationship_type, event_fingerprint,
               cluster_id, primary_source_in_cluster, source
        FROM processed_articles
        ORDER BY id
        LIMIT %s
    """, (MAX_ROWS_PER_RUN,))
    rows = cur.fetchall()
    cur.close()
    return rows


# ============================================================================
# SCORE COMPONENTS
# ============================================================================

def _nonempty(v) -> bool:
    return bool(v) and str(v).strip() not in ("", "-", "none", "null")


def _sub(row) -> str:
    return (row.get("sub_vertical") or "").lower().strip()


def category_impact_score(row, weights) -> int:
    return weights["category"].get(_sub(row), CATEGORY_IMPACT_DEFAULT)


def source_authority_score(row, weights) -> int:
    return weights["source"].get((row.get("source_quality") or "").lower().strip(),
                                 SOURCE_AUTHORITY_DEFAULT)


def evidence_strength_score(row) -> int:
    return EVIDENCE_SCORE.get((row.get("evidence_strength") or "").lower().strip(), 10)


def regulatory_significance_score(row) -> int:
    if _sub(row) in REGULATORY_SUBVERTICALS:
        return 50
    if _nonempty(row.get("regulators")):
        return 30
    return 0


def commercial_significance_score(row) -> int:
    sv = _sub(row)
    if sv in COMMERCIAL_DEAL:
        return 40
    if sv in COMMERCIAL_PARTNERSHIP:
        return 30
    if sv in COMMERCIAL_EXPANSION:
        return 25
    return 0


def adoption_scale_score(row) -> int:
    sv = _sub(row)
    if sv == "customer deployment / adoption":
        return 30 if _nonempty(row.get("health_systems")) else 20
    if _nonempty(row.get("health_systems")):
        return 10
    return 0


def novelty_score(row) -> int:
    if _sub(row) == "technology breakthrough":
        return 20
    text = f"{row.get('title') or ''} {row.get('executive_summary') or ''}".lower()
    if any(kw in text for kw in NOVELTY_KEYWORDS):
        return 15
    return 0


def recency_score(row) -> int:
    pd = row.get("published_date")
    if not pd:
        return 0
    try:
        days = (datetime.now() - pd).days
    except Exception:
        return 0
    if days <= 2:
        return 20
    if days <= 7:
        return 15
    if days <= 14:
        return 10
    if days <= 30:
        return 5
    return 0


def confidence_component(row) -> int:
    try:
        return round(min(100, max(0, int(row.get("confidence_score") or 0))) / 100 * 20)
    except Exception:
        return 0


def duplicate_penalty(row) -> int:
    return DUPLICATE_PENALTY if (row.get("relationship_type") or "") == "exact_duplicate" else 0


def low_quality_penalty(row, weights) -> int:
    auth = source_authority_score(row, weights)
    conf = int(row.get("confidence_score") or 0)
    if auth <= 15 and conf < 40:
        return LOW_QUALITY_PENALTY
    if (row.get("evidence_strength") or "").lower().strip() == "low" and \
       (row.get("source_quality") or "").lower().strip() == "company pr":
        return 10
    return 0


def compute_article_rank(row, weights) -> int:
    total = (
        category_impact_score(row, weights)
        + source_authority_score(row, weights)
        + evidence_strength_score(row)
        + regulatory_significance_score(row)
        + commercial_significance_score(row)
        + adoption_scale_score(row)
        + novelty_score(row)
        + recency_score(row)
        + confidence_component(row)
        - duplicate_penalty(row)
        - low_quality_penalty(row, weights)
    )
    return max(0, int(total))


# ============================================================================
# MAIN
# ============================================================================

def run_ranking():
    start = datetime.now()
    logging.info("=" * 60)
    logging.info("ZOLOTEL — RANKING ENGINE")
    logging.info("=" * 60)

    weights = load_weights()
    conn = get_db_connection()

    rows = load_processed_articles(conn)
    if not rows:
        logging.info("ℹ️  No processed articles to rank. Exiting.")
        conn.close()
        return
    logging.info(f"📄 Ranking {len(rows)} articles...")

    # 1) Per-article rank_score
    ranks: Dict[int, int] = {}
    cur = conn.cursor()
    for row in rows:
        r = compute_article_rank(row, weights)
        ranks[row["id"]] = r
        cur.execute("UPDATE processed_articles SET rank_score = %s WHERE id = %s", (r, row["id"]))
    conn.commit()
    cur.close()
    logging.info("   ✅ Article rank_score written")

    # 2) Cluster-level cluster_rank_score = representative rank + multi-source corroboration
    clusters: Dict[int, Dict] = {}
    for row in rows:
        cid = row.get("cluster_id")
        if cid is None:
            continue
        c = clusters.setdefault(cid, {"rep_rank": None, "max_rank": 0, "sources": set()})
        rk = ranks[row["id"]]
        c["max_rank"] = max(c["max_rank"], rk)
        if row.get("primary_source_in_cluster"):
            c["rep_rank"] = rk
        if (row.get("relationship_type") or "") != "exact_duplicate" and _nonempty(row.get("source")):
            c["sources"].add(str(row.get("source")).lower().strip())

    cur = conn.cursor()
    ranked_clusters = 0
    for cid, c in clusters.items():
        base = c["rep_rank"] if c["rep_rank"] is not None else c["max_rank"]
        distinct_sources = len(c["sources"])
        corroboration = min(max(distinct_sources - 1, 0), 5) * 4   # 0..20
        cluster_rank = base + corroboration
        cur.execute("UPDATE event_clusters SET cluster_rank_score = %s, updated_at = NOW() WHERE id = %s",
                    (cluster_rank, cid))
        ranked_clusters += 1
    conn.commit()
    cur.close()
    logging.info(f"   ✅ Cluster_rank_score written for {ranked_clusters} clusters")

    # Stats: top of the feed
    cur = conn.cursor()
    cur.execute("""
        SELECT ec.cluster_rank_score, ec.article_count, ec.primary_vertical, ec.cluster_title
        FROM event_clusters ec
        WHERE ec.cluster_rank_score IS NOT NULL
        ORDER BY ec.cluster_rank_score DESC NULLS LAST
        LIMIT 10
    """)
    top = cur.fetchall()
    cur.close()
    conn.close()

    logging.info("\n🏆 Top clusters by rank:")
    for t in top:
        logging.info(f"   [{t['cluster_rank_score']:>3}] ({t['article_count']}x) "
                     f"{t['primary_vertical']} — {str(t['cluster_title'])[:55]}")

    logging.info("\n" + "=" * 60)
    logging.info(f"✅ RANKING COMPLETE in {(datetime.now()-start).total_seconds()/60:.1f} min")
    logging.info("=" * 60)


def main():
    run_ranking()


if __name__ == "__main__":
    main()
