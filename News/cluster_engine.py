"""
Zolotel — Event Clustering Engine (step 5)

Groups rows in processed_articles into event_clusters. Per the spec, this REPLACES
blind deduplication with event clustering: near-identical and related articles are
linked into one cluster rather than deleted, and the most authoritative article is
chosen as the cluster representative.

Pipeline position: runs AFTER llm_processor_production.py (Option A — invoked by
run_pipeline.py). Reads only unclustered rows (cluster_id IS NULL), matches them
against recent existing clusters and against each other, then writes:
  processed_articles.cluster_id, relationship_type, duplicate_status,
                     primary_source_in_cluster
  event_clusters     (created/updated with representative + metadata)

Fingerprints are already produced by the processor (Stage 3) and read from
processed_articles.event_fingerprint, so matching is deterministic and needs no LLM.
A single LLM call per MULTI-article cluster (Stage 4) synthesises an executive
cluster summary; single-article clusters reuse the representative's own summary.

cluster_rank_score is intentionally left NULL — it is computed in step 6 (ranking).
"""

import os
import re
import json
import time
import logging
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

CLUSTER_LOOKBACK_DAYS = 30     # match new articles against clusters seen in this window
SAME_EVENT_DAYS = 7            # within this gap -> same_event; beyond -> follow_on_update
RELATED_THRESHOLD = 0.5        # partial fingerprint match ratio to attach as related_context
EXACT_TITLE_SIM = 0.92         # title similarity -> exact_duplicate
JOIN_TITLE_SIM = 0.85          # title similarity alone is enough to join a cluster
USE_LLM_CLUSTER_SUMMARY = True # Stage 4 synthesis for multi-article clusters
MAX_UNCLUSTERED_PER_RUN = 5000

# Source authority ordering (taxonomy Sheet 5) — higher wins the representative slot
SOURCE_AUTHORITY = {
    "official": 5,
    "peer-reviewed": 4,
    "specialist media": 3,
    "company pr": 2,
    "aggregator": 1,
    "other": 0,
}

COMMENTARY_SIGNALS = {
    "opinion", "analysis", "explainer", "explained", "what it means", "why ",
    "perspective", "viewpoint", "commentary", "op-ed", "column", "takeaways",
    "deep dive", "q&a", "interview",
}

# Identifying fingerprint fields per sub-vertical (subset that defines the event)
SUBVERTICAL_PRIMARY_KEYS = {
    "regulatory approval / clearance": ["regulator", "company", "product"],
    "clinical validation / evidence": ["study_name", "product_or_model", "disease_area"],
    "funding & venture capital": ["company", "round_type"],
    "mergers & acquisitions": ["acquirer", "target"],
    "partnerships & alliances": ["companies"],
    "customer deployment / adoption": ["customer", "vendor"],
    "product launch": ["company", "product"],
    "policy & governance": ["authority", "policy_name"],
    "safety, bias & ethics": ["company_or_product", "issue_type"],
    "legal & disputes": ["parties", "issue_type"],
    "cybersecurity & privacy": ["organization", "breach_or_risk_type"],
    "market outlook / industry trends": ["topic"],
    "leadership & management": ["company", "person"],
}
PRIMARY_KEYS_FALLBACK = ["company"]

NUMERIC_HINT_FIELDS = {"amount", "deal_value", "population_size", "value"}


# ============================================================================
# DATABASE
# ============================================================================

def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL environment variable not set")
    return psycopg.connect(database_url, row_factory=dict_row)


def load_unclustered_articles(conn) -> List[Dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, url, canonical_url, source, source_quality,
               primary_vertical, sub_vertical, news_type,
               impact_level, evidence_strength, relevance_score,
               executive_summary, why_it_matters, event_fingerprint,
               published_date, companies
        FROM processed_articles
        WHERE cluster_id IS NULL
        ORDER BY published_date ASC NULLS LAST, id ASC
        LIMIT %s
    """, (MAX_UNCLUSTERED_PER_RUN,))
    rows = cur.fetchall()
    cur.close()
    return rows


def load_recent_clusters(conn) -> List[Dict]:
    cutoff = datetime.now() - timedelta(days=CLUSTER_LOOKBACK_DAYS)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, cluster_title, primary_vertical, sub_vertical, event_type,
               canonical_event_fingerprint, source_quality, last_seen
        FROM event_clusters
        WHERE last_seen >= %s OR last_seen IS NULL
    """, (cutoff,))
    rows = cur.fetchall()
    cur.close()
    return rows


def insert_new_cluster(conn, article: Dict) -> int:
    """Create a placeholder cluster seeded by an article; returns its id."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO event_clusters
            (cluster_title, primary_vertical, sub_vertical, event_type,
             canonical_event_fingerprint, source_quality, article_count,
             first_seen, last_seen, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, NOW(), NOW())
        RETURNING id
    """, (
        article.get('title'),
        article.get('primary_vertical'),
        article.get('sub_vertical'),
        article.get('sub_vertical'),
        json.dumps(article.get('event_fingerprint')) if isinstance(article.get('event_fingerprint'), dict) else None,
        article.get('source_quality'),
        article.get('published_date'),
        article.get('published_date'),
    ))
    cid = cur.fetchone()['id']
    conn.commit()
    cur.close()
    return cid


# ============================================================================
# GEMINI (Stage 4 cluster summary) — tiered fallback
# ============================================================================

def gemini_generate(system_prompt: str, user_prompt: str, max_tokens: int,
                    temperature: float = 0.2) -> str:
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not configured")
    config = types.GenerateContentConfig(
        system_instruction=system_prompt, temperature=temperature,
        max_output_tokens=max_tokens, response_mime_type="application/json",
    )
    contents = [types.Content(role="user", parts=[types.Part(text=user_prompt)])]
    last_err = None
    for model_name in GEMINI_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(model=model_name, contents=contents, config=config)
                return resp.text or ""
            except Exception as e:
                last_err = e
                if any(t in str(e).upper() for t in ('503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED', '500', 'INTERNAL', 'TIMEOUT', 'DEADLINE')) and attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                break
    raise last_err


def parse_json_object(raw: str) -> Dict:
    raw = raw.strip()
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'^```\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    m = re.search(r'\{[\s\S]*\}', raw)
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group(0))


STAGE4_SYSTEM_PROMPT = """You are writing for senior executives.

Given a cluster of related articles, produce:
1. One concise headline
2. One 2-sentence executive summary
3. One "why it matters" paragraph
4. Impact level
5. Evidence strength
6. Recommended primary source

Rules:
- Be concise and factual.
- Do not overstate.
- Do not invent numbers.
- If evidence is weak, say so.
- Prefer official and peer-reviewed sources over company PR.
- Make the business/clinical/regulatory implication clear.

Return ONLY valid JSON:
{
  "headline": "",
  "executive_summary": "",
  "why_it_matters": "",
  "impact_level": "Critical/High/Medium/Low",
  "evidence_strength": "High/Medium/Low",
  "recommended_primary_source": ""
}"""


def stage4_cluster_summary(members: List[Dict]) -> Optional[Dict]:
    """Synthesise an executive summary across a multi-article cluster (LLM)."""
    if not USE_LLM_CLUSTER_SUMMARY or client is None:
        return None
    lines = ""
    for i, m in enumerate(members[:12]):
        lines += (
            f"\n--- ARTICLE {i+1} ---\n"
            f"Source: {m.get('source','')} ({m.get('source_quality','')})\n"
            f"Title: {m.get('title','')}\n"
            f"Summary: {m.get('executive_summary') or ''}\n"
        )
    user_prompt = f"""These {len(members)} articles report on the same underlying event.
Synthesise ONE executive view across them following your instructions.

{lines}

Return ONLY the JSON object."""
    try:
        raw = gemini_generate(STAGE4_SYSTEM_PROMPT, user_prompt, max_tokens=600)
        return parse_json_object(raw)
    except Exception as e:
        logging.warning(f"Stage 4 cluster summary failed: {e}")
        return None


# ============================================================================
# MATCHING HELPERS
# ============================================================================

def norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        v = " ".join(str(x) for x in v)
    return re.sub(r'\s+', ' ', str(v).lower().strip())


def normalize_title(title: str, source: str = "") -> str:
    t = norm(title)
    if source:
        s = norm(source)
        for sep in (' - ', ' | ', ' – '):
            if t.endswith(sep + s):
                t = t[: -len(sep + s)]
    return re.sub(r'[^a-z0-9 ]', '', t).strip()


def title_similarity(a: str, b: str) -> float:
    a, b = normalize_title(a), normalize_title(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def has_commentary_signal(title: str) -> bool:
    t = norm(title)
    return any(sig in t for sig in COMMENTARY_SIGNALS)


def _string_match(a, b) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() > 0.80


def _numeric_match(a, b, tol: float = 0.10) -> Optional[bool]:
    try:
        na = float(re.sub(r'[^0-9.]', '', str(a)))
        nb = float(re.sub(r'[^0-9.]', '', str(b)))
    except (ValueError, TypeError):
        return None
    if na == 0 and nb == 0:
        return True
    if na == 0 or nb == 0:
        return False
    return abs(na - nb) / max(na, nb) <= tol


def fingerprint_match(fp1: Dict, fp2: Dict, sub_vertical: str) -> Tuple[bool, float]:
    """Compare two fingerprints on the sub-vertical's identifying fields.
    Returns (all_match, partial_ratio) over the fields present in BOTH."""
    if not isinstance(fp1, dict) or not isinstance(fp2, dict) or not fp1 or not fp2:
        return (False, 0.0)
    keys = SUBVERTICAL_PRIMARY_KEYS.get((sub_vertical or '').lower().strip(), PRIMARY_KEYS_FALLBACK)
    comparable = 0
    matched = 0
    for k in keys:
        v1, v2 = fp1.get(k), fp2.get(k)
        if (v1 in (None, "", [])) or (v2 in (None, "", [])):
            continue
        comparable += 1
        if k in NUMERIC_HINT_FIELDS:
            nm = _numeric_match(v1, v2)
            ok = nm if nm is not None else _string_match(v1, v2)
        else:
            ok = _string_match(v1, v2)
        if ok:
            matched += 1
    if comparable == 0:
        return (False, 0.0)
    return (matched == comparable, matched / comparable)


def classify_relationship(article: Dict, cluster_title: str, cluster_last_seen,
                          all_match: bool, partial: float, tsim: float) -> str:
    """Decide how an article relates to a candidate cluster it is joining."""
    if tsim >= EXACT_TITLE_SIM:
        return "exact_duplicate"
    if all_match:
        if has_commentary_signal(article.get('title', '')):
            return "commentary"
        try:
            gap = abs((article['published_date'] - cluster_last_seen).days) if cluster_last_seen and article.get('published_date') else 0
        except Exception:
            gap = 0
        if gap > SAME_EVENT_DAYS:
            return "follow_on_update"
        return "same_event"
    if partial >= RELATED_THRESHOLD:
        return "related_context"
    return "related_context"


def source_rank(source_quality: Optional[str]) -> int:
    return SOURCE_AUTHORITY.get(norm(source_quality), 0)


# ============================================================================
# CLUSTERING
# ============================================================================

class Cluster:
    """In-memory handle to a cluster during a run (wraps a DB cluster id)."""
    __slots__ = ('id', 'title', 'sub_vertical', 'fingerprint', 'last_seen', 'is_new')

    def __init__(self, cid, title, sub_vertical, fingerprint, last_seen, is_new):
        self.id = cid
        self.title = title
        self.sub_vertical = sub_vertical
        self.fingerprint = fingerprint if isinstance(fingerprint, dict) else _as_dict(fingerprint)
        self.last_seen = last_seen
        self.is_new = is_new


def _as_dict(v) -> Dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def best_cluster_for(article: Dict, clusters: List[Cluster]) -> Optional[Tuple[Cluster, str, float]]:
    """Find the best-matching cluster (same sub_vertical) for an article."""
    sv = (article.get('sub_vertical') or '').lower().strip()
    afp = _as_dict(article.get('event_fingerprint'))
    best = None
    best_score = 0.0
    best_rel = None
    for c in clusters:
        if (c.sub_vertical or '').lower().strip() != sv:
            continue
        all_match, partial = fingerprint_match(afp, c.fingerprint, sv)
        tsim = title_similarity(article.get('title', ''), c.title or '')
        # Decide if this article can join this cluster at all
        joins = all_match or partial >= RELATED_THRESHOLD or tsim >= JOIN_TITLE_SIM
        if not joins:
            continue
        score = (1.0 if all_match else partial) + tsim  # rank candidate clusters
        if score > best_score:
            best_score = score
            best = c
            best_rel = classify_relationship(article, c.title, c.last_seen, all_match, partial, tsim)
    if best is None:
        return None
    return (best, best_rel, best_score)


def run_clustering():
    start = time.time()
    logging.info("=" * 60)
    logging.info("ZOLOTEL — EVENT CLUSTERING ENGINE")
    logging.info("=" * 60)

    conn = get_db_connection()

    articles = load_unclustered_articles(conn)
    if not articles:
        logging.info("ℹ️  No unclustered articles. Exiting.")
        conn.close()
        return
    logging.info(f"📄 Unclustered articles: {len(articles)}")

    existing = load_recent_clusters(conn)
    clusters: List[Cluster] = [
        Cluster(c['id'], c['cluster_title'], c['sub_vertical'],
                c['canonical_event_fingerprint'], c['last_seen'], is_new=False)
        for c in existing
    ]
    logging.info(f"🔗 Existing clusters in window: {len(clusters)}")

    assignments: Dict[int, Tuple[int, str]] = {}   # article_id -> (cluster_id, relationship_type)
    touched_clusters = set()
    new_count = 0

    for art in articles:
        match = best_cluster_for(art, clusters)
        if match:
            cluster, rel, _ = match
            assignments[art['id']] = (cluster.id, rel)
            cluster.last_seen = max(
                [d for d in (cluster.last_seen, art.get('published_date')) if d],
                default=cluster.last_seen
            )
            touched_clusters.add(cluster.id)
        else:
            cid = insert_new_cluster(conn, art)
            assignments[art['id']] = (cid, "separate_event")
            clusters.append(Cluster(cid, art.get('title'), art.get('sub_vertical'),
                                    art.get('event_fingerprint'), art.get('published_date'), is_new=True))
            touched_clusters.add(cid)
            new_count += 1

    logging.info(f"   New clusters created: {new_count}")
    logging.info(f"   Articles attached to existing/new clusters: {len(assignments)}")

    # Persist article -> cluster assignments
    cur = conn.cursor()
    for aid, (cid, rel) in assignments.items():
        dup_status = "duplicate" if rel == "exact_duplicate" else "unique"
        cur.execute("""
            UPDATE processed_articles
            SET cluster_id = %s, relationship_type = %s, duplicate_status = %s
            WHERE id = %s
        """, (cid, rel, dup_status, aid))
    conn.commit()
    cur.close()

    # Recompute each touched cluster: representative, metadata, Stage 4 summary
    logging.info(f"\n🧮 Finalising {len(touched_clusters)} clusters...")
    finalise_clusters(conn, touched_clusters)

    conn.close()
    logging.info("\n" + "=" * 60)
    logging.info(f"✅ CLUSTERING COMPLETE in {(time.time()-start)/60:.1f} min")
    logging.info(f"   Clusters touched: {len(touched_clusters)} | New: {new_count}")
    logging.info("=" * 60)


def finalise_clusters(conn, cluster_ids):
    """For each cluster: pick representative, set flags, write metadata + summary."""
    summarised = 0
    for cid in cluster_ids:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, source, source_quality, primary_vertical, sub_vertical,
                   impact_level, evidence_strength, relevance_score,
                   executive_summary, why_it_matters, event_fingerprint,
                   published_date, url
            FROM processed_articles
            WHERE cluster_id = %s
        """, (cid,))
        members = cur.fetchall()
        cur.close()
        if not members:
            continue

        # Representative = highest source authority, then relevance, then most recent
        def rep_key(m):
            return (
                source_rank(m.get('source_quality')),
                int(m.get('relevance_score') or 0),
                m.get('published_date') or datetime.min,
            )
        rep = max(members, key=rep_key)

        dates = [m['published_date'] for m in members if m.get('published_date')]
        first_seen = min(dates) if dates else None
        last_seen = max(dates) if dates else None
        article_count = len(members)

        # Cluster narrative: Stage 4 synthesis for multi-article clusters, else representative's own
        cluster_title = rep.get('title')
        cluster_summary = rep.get('executive_summary')
        why = rep.get('why_it_matters')
        impact = rep.get('impact_level')
        evidence = rep.get('evidence_strength')

        if article_count > 1:
            s4 = stage4_cluster_summary(members)
            if s4:
                cluster_title = s4.get('headline') or cluster_title
                cluster_summary = s4.get('executive_summary') or cluster_summary
                why = s4.get('why_it_matters') or why
                impact = s4.get('impact_level') or impact
                evidence = s4.get('evidence_strength') or evidence
                summarised += 1

        canonical_fp = rep.get('event_fingerprint')
        canonical_fp_json = json.dumps(canonical_fp) if isinstance(canonical_fp, dict) else (
            canonical_fp if isinstance(canonical_fp, str) else None
        )

        cur = conn.cursor()
        cur.execute("""
            UPDATE event_clusters
            SET cluster_title = %s, primary_vertical = %s, sub_vertical = %s,
                event_type = %s, canonical_event_fingerprint = %s,
                representative_article_id = %s, cluster_summary = %s, why_it_matters = %s,
                impact_level = %s, evidence_strength = %s, source_quality = %s,
                article_count = %s, first_seen = %s, last_seen = %s, updated_at = NOW()
            WHERE id = %s
        """, (
            cluster_title, rep.get('primary_vertical'), rep.get('sub_vertical'),
            rep.get('sub_vertical'), canonical_fp_json,
            rep['id'], cluster_summary, why,
            impact, evidence, rep.get('source_quality'),
            article_count, first_seen, last_seen, cid,
        ))
        # Representative flag across all members
        cur.execute(
            "UPDATE processed_articles SET primary_source_in_cluster = (id = %s) WHERE cluster_id = %s",
            (rep['id'], cid)
        )
        conn.commit()
        cur.close()

    logging.info(f"   Stage 4 summaries generated for {summarised} multi-article clusters")


def main():
    run_clustering()


if __name__ == "__main__":
    main()
