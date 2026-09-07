"""
Zolotel — Production LLM Processor for AI Healthcare Intelligence

Reads unprocessed rows from raw_articles and runs a three-stage Gemini pipeline:
  Stage 1 — Relevance + confidence scoring (0-100)
  Stage 2 — Deep extraction (vertical, sub-vertical, entities, impact, evidence,
            source quality, executive summary, why-it-matters)
  Stage 3 — Event-fingerprint extraction (sub-vertical-specific fields, for clustering)

Writes the extracted rows to processed_articles.

Deduplication / event-clustering (step 5) and cluster ranking (step 6) are handled
separately in the backend — this processor intentionally does NOT dedup or rank.

Model strategy: Gemini Flash with a tiered fallback chain so a single-model outage
does not halt the pipeline.
"""

import os
import re
import json
import time
import logging
import requests
import psycopg
from psycopg.rows import dict_row
import pandas as pd
from dotenv import load_dotenv
from typing import Dict, List
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta

from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise Exception("GEMINI_API_KEY environment variable not set")

client = genai.Client(api_key=GEMINI_API_KEY)

# Tiered fallback chain — primary Flash, then Flash-Lite, then prior Flash.
# A transient outage on one model rolls over to the next automatically.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

EXCEL_FILE_PATH = "Healthcare_AI_Taxonomy.xlsx"

# Performance configuration
STAGE1_BATCH_SIZE = 20
STAGE2_BATCH_SIZE = 5
STAGE1_CONCURRENCY = 4
STAGE2_CONCURRENCY = 3
STAGE3_CONCURRENCY = 3
SCRAPE_WORKERS = 10
RATE_LIMIT_DELAY = 0.10

# Relevance threshold — articles scoring >= this go to deep extraction.
# Bands (per taxonomy): 90-100 critical, 70-89 strong, 40-69 moderate,
# 1-39 weak, 0 irrelevant. 40 keeps moderate-and-up for an exhaustive feed;
# raise to 70 for a tighter executive-only feed.
RELEVANCE_THRESHOLD = 40

# Articles processed per run (safety cap)
MAX_ARTICLES_PER_RUN = 5000

# Allowed taxonomy values (mirrors the workbook; fallbacks if Excel unavailable)
VERTICALS = [
    "Drug Discovery & R&D",
    "Diagnostics & Imaging",
    "Clinical Decision Support",
    "Ambient Documentation & Clinical Workflow",
    "Revenue Cycle Management (RCM)",
    "Patient Engagement & Virtual Care",
    "Hospital Operations & Supply Chain",
]
SUB_VERTICALS = [
    "Mergers & Acquisitions",
    "Partnerships & Alliances",
    "Funding & Venture Capital",
    "Product Launch",
    "Regulatory Approval / Clearance",
    "Policy & Governance",
    "Clinical Validation / Evidence",
    "Customer Deployment / Adoption",
    "Commercial Expansion",
    "Technology Breakthrough",
    "Safety, Bias & Ethics",
    "Legal & Disputes",
    "Cybersecurity & Privacy",
    "Leadership & Management",
    "Market Outlook / Industry Trends",
]

# Sub-vertical-specific fingerprint fields (taxonomy Sheet 8 — Dedup Fingerprints)
SUBVERTICAL_FINGERPRINT_FIELDS = {
    "regulatory approval / clearance": ["regulator", "company", "product", "indication", "modality", "clearance_type", "geography", "date"],
    "clinical validation / evidence": ["study_name", "journal", "institution", "product_or_model", "disease_area", "endpoint", "population_size", "result_direction"],
    "funding & venture capital": ["company", "round_type", "amount", "lead_investor", "use_of_funds"],
    "mergers & acquisitions": ["acquirer", "target", "deal_value", "asset_or_product", "rationale"],
    "partnerships & alliances": ["companies", "partner_type", "product_or_platform", "disease_area", "geography"],
    "customer deployment / adoption": ["customer", "vendor", "use_case", "department", "scale", "geography"],
    "product launch": ["company", "product", "use_case", "target_customer", "geography"],
    "policy & governance": ["authority", "policy_name", "affected_technology", "jurisdiction", "effective_date"],
    "safety, bias & ethics": ["company_or_product", "issue_type", "regulator_or_body", "affected_population"],
    "legal & disputes": ["parties", "issue_type", "jurisdiction", "product_or_company", "status"],
    "cybersecurity & privacy": ["organization", "breach_or_risk_type", "affected_system", "data_type", "geography"],
    "market outlook / industry trends": ["topic", "market_segment", "key_stat_or_claim", "geography", "source"],
    "leadership & management": ["company", "person", "role", "change_type", "effective_date"],
}
FINGERPRINT_FALLBACK_FIELDS = ["company", "event_type", "value", "geography"]


# ============================================================================
# DATABASE
# ============================================================================

def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL environment variable not set")
    return psycopg.connect(database_url, row_factory=dict_row)


def load_raw_articles() -> pd.DataFrame:
    """Load unprocessed articles from raw_articles (processing_status = 'new')."""
    conn = get_db_connection()
    query = """
        SELECT id, title, url, canonical_url, source, source_domain,
               published_date, raw_snippet, vertical_seed
        FROM raw_articles
        WHERE processing_status = 'new'
        ORDER BY published_date DESC
        LIMIT %s
    """
    cur = conn.cursor()
    cur.execute(query, (MAX_ARTICLES_PER_RUN,))
    results = cur.fetchall()
    cur.close()
    conn.close()

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results)


def save_to_processed_articles(df: pd.DataFrame):
    """Insert extracted rows into processed_articles and mark raw rows processed."""
    if df.empty:
        logging.info("No articles to save")
        return

    conn = get_db_connection()

    insert_query = """
        INSERT INTO processed_articles (
            raw_article_id, title, url, canonical_url, source, source_domain, published_date,
            primary_vertical, secondary_verticals, sub_vertical, news_type,
            entities, companies, regulators, health_systems, investors, products,
            geography, impact_level, evidence_strength, source_quality,
            confidence_score, relevance_score, rank_score,
            executive_summary, why_it_matters, event_fingerprint,
            cluster_id, duplicate_status, relationship_type
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (raw_article_id) DO NOTHING
    """

    saved_count = 0
    failed_count = 0

    for _, row in df.iterrows():
        try:
            fp = row.get('event_fingerprint')
            fp_json = json.dumps(fp) if isinstance(fp, dict) and fp else None
            ent = row.get('entities')
            ent_json = json.dumps(ent) if isinstance(ent, dict) and ent else None

            cur = conn.cursor()
            cur.execute(insert_query, (
                int(row['id']),
                row.get('title'),
                row.get('url'),
                row.get('canonical_url') or row.get('url'),
                row.get('source', ''),
                row.get('source_domain', ''),
                row.get('published_date'),
                row.get('primary_vertical'),
                row.get('secondary_verticals'),
                row.get('sub_vertical'),
                row.get('news_type'),
                ent_json,
                row.get('companies'),
                row.get('regulators'),
                row.get('health_systems'),
                row.get('investors'),
                row.get('products'),
                row.get('geography'),
                row.get('impact_level'),
                row.get('evidence_strength'),
                row.get('source_quality'),
                row.get('confidence_score'),
                row.get('relevance_score'),
                None,                       # rank_score — filled by step 6 (cluster ranking)
                row.get('executive_summary'),
                row.get('why_it_matters'),
                fp_json,
                None,                       # cluster_id — filled by step 5 (clustering)
                None,                       # duplicate_status — filled by step 5
                None,                       # relationship_type — filled by step 5
            ))
            conn.commit()
            # Mark the raw row as processed (status workflow, not deletion)
            cur.execute(
                "UPDATE raw_articles SET processing_status = 'processed' WHERE id = %s",
                (int(row['id']),)
            )
            conn.commit()
            cur.close()
            saved_count += 1
        except Exception as e:
            conn.rollback()
            failed_count += 1
            logging.error(f"Error saving '{str(row.get('title', 'Unknown'))[:50]}...': {e}")

    conn.close()
    logging.info(f"✅ Saved {saved_count} articles to processed_articles")
    if failed_count > 0:
        logging.warning(f"⚠️  Failed to save {failed_count} articles")


def mark_raw_processed(raw_ids: List[int]):
    """Mark below-threshold raw articles as processed so they are not re-scored."""
    if not raw_ids:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE raw_articles SET processing_status = 'processed' WHERE id = ANY(%s)",
            (raw_ids,)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.warning(f"Failed to mark low-relevance raw rows processed: {e}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# GEMINI HELPER (tiered fallback + retry) + JSON PARSING
# ============================================================================

def gemini_generate(system_prompt: str, user_prompt: str,
                    max_tokens: int, temperature: float = 0.0,
                    force_json: bool = True) -> str:
    """Call Gemini with a tiered model-fallback chain and per-model retry."""
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json" if force_json else "text/plain",
    )
    contents = [types.Content(role="user", parts=[types.Part(text=user_prompt)])]

    last_err = None
    for model_name in GEMINI_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model_name, contents=contents, config=config
                )
                return resp.text or ""
            except Exception as e:
                last_err = e
                msg = str(e).upper()
                transient = any(tok in msg for tok in (
                    '503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED',
                    '500', 'INTERNAL', 'TIMEOUT', 'DEADLINE'
                ))
                if transient and attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                break  # move to next model in the chain
    raise last_err


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'^```\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return raw


def parse_json_array(raw: str):
    raw = _strip_fences(raw)
    m = re.search(r'\[[\s\S]*\]', raw)
    if not m:
        raise ValueError("No JSON array found")
    return json.loads(m.group(0))


def parse_json_object(raw: str):
    raw = _strip_fences(raw)
    m = re.search(r'\{[\s\S]*\}', raw)
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group(0))


def list_to_str(value) -> str:
    """Normalize an entity list/string into a comma-joined string (or '')."""
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


# ============================================================================
# CONTENT SCRAPING
# ============================================================================

def scrape_article(url: str, max_length: int = 3000) -> str:
    """Fetch and clean article body text (best-effort; falls back to '')."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for element in soup(["script", "style", "nav", "footer", "aside", "header", "iframe"]):
            element.decompose()
        text = ' '.join(soup.get_text(separator=' ', strip=True).split())
        return text[:max_length] if text else ""
    except Exception as e:
        logging.warning(f"Scraping failed for {url}: {e}")
        return ""


# ============================================================================
# STAGE 1 — RELEVANCE + CONFIDENCE SCORING
# ============================================================================

STAGE1_SYSTEM_PROMPT = """You are an executive healthcare AI intelligence analyst.

Score each article from 0 to 100 for relevance to AI in healthcare.

The platform tracks these seven verticals:
1. Drug Discovery & R&D
2. Diagnostics & Imaging
3. Clinical Decision Support
4. Ambient Documentation & Clinical Workflow
5. Revenue Cycle Management (RCM)
6. Patient Engagement & Virtual Care
7. Hospital Operations & Supply Chain

Scoring:
90-100 = Critical executive relevance:
- FDA/CMS/HHS/EMA/WHO action on healthcare AI
- major clinical validation or peer-reviewed evidence
- major M&A/funding/partnership involving healthcare AI
- hospital, payer, pharma, or provider-scale deployment
- material safety, bias, privacy, or legal development

70-89 = Strong relevance:
- product launch in one of the seven verticals
- meaningful customer adoption
- credible market or policy analysis
- company strategy with healthcare AI impact

40-69 = Moderate relevance:
- general AI healthcare trend
- early pilot or limited deployment
- commentary with some sector value

1-39 = Weak relevance:
- vague AI healthcare mention
- promotional content with little evidence
- generic technology article

0 = Irrelevant:
- not healthcare
- not AI
- purely unrelated company/business news

Return JSON array only:
[
  {"id": 1, "relevance_score": 87, "confidence_score": 82}
]"""


def batch_relevance_score(articles_batch: List[Dict]) -> Dict[int, Dict]:
    """Score a batch of articles in one Gemini call. Returns {id: {relevance, confidence}}."""
    articles_text = ""
    for a in articles_batch:
        snippet = (a.get('snippet') or '')[:300]
        articles_text += f"\n[{a['id']}] Title: {a['title']}\n    Snippet: {snippet}\n"

    user_prompt = f"""Score these {len(articles_batch)} articles for relevance to AI in healthcare (0-100 each):
{articles_text}

Return ONLY a JSON array like:
[{{"id": 1, "relevance_score": 87, "confidence_score": 82}}, ...]"""

    try:
        raw = gemini_generate(STAGE1_SYSTEM_PROMPT, user_prompt,
                              max_tokens=len(articles_batch) * 40, temperature=0.0)
        scores_list = parse_json_array(raw)
        out = {}
        for item in scores_list:
            aid = item.get('id')
            rel = max(0, min(100, int(item.get('relevance_score', 0) or 0)))
            conf = max(0, min(100, int(item.get('confidence_score', 0) or 0)))
            out[aid] = {'relevance_score': rel, 'confidence_score': conf}
        return out
    except Exception as e:
        logging.warning(f"Batch scoring failed: {e}")
        return {}


def stage1_quick_scoring(df: pd.DataFrame) -> pd.DataFrame:
    logging.info("\n" + "=" * 60)
    logging.info("STAGE 1: RELEVANCE + CONFIDENCE SCORING")
    logging.info(f"  Batch size: {STAGE1_BATCH_SIZE} | Concurrency: {STAGE1_CONCURRENCY}")
    logging.info("=" * 60)

    df = df.reset_index(drop=True)
    relevance = [0] * len(df)
    confidence = [0] * len(df)
    total = len(df)
    total_batches = (total + STAGE1_BATCH_SIZE - 1) // STAGE1_BATCH_SIZE

    all_batches = []
    for i in range(0, total, STAGE1_BATCH_SIZE):
        batch_df = df.iloc[i:i + STAGE1_BATCH_SIZE]
        batch = []
        for local_idx, (_, row) in enumerate(batch_df.iterrows()):
            batch.append({
                'id': local_idx + 1,
                'title': str(row['title']),
                'snippet': str(row.get('raw_snippet', '') or ''),
            })
        all_batches.append((i, batch))

    logging.info(f"📊 Scoring {len(all_batches)} batches...")

    def score_batch(batch_tuple):
        start_idx, batch = batch_tuple
        return start_idx, batch_relevance_score(batch)

    with ThreadPoolExecutor(max_workers=STAGE1_CONCURRENCY) as executor:
        futures = {executor.submit(score_batch, b): b for b in all_batches}
        for future in as_completed(futures):
            start_idx, score_map = future.result()
            for local_id, scores in score_map.items():
                pos = start_idx + (local_id - 1)
                if 0 <= pos < total:
                    relevance[pos] = scores['relevance_score']
                    confidence[pos] = scores['confidence_score']

    df['relevance_score'] = relevance
    df['confidence_score'] = confidence

    high = df[df['relevance_score'] >= RELEVANCE_THRESHOLD]
    logging.info(f"\n📈 Stage 1 Complete:")
    logging.info(f"   Total articles: {total}")
    logging.info(f"   API calls: {total_batches}")
    logging.info(f"   >= threshold ({RELEVANCE_THRESHOLD}): {len(high)} "
                 f"({(len(high)/total*100 if total else 0):.1f}%)")
    return df


# ============================================================================
# STAGE 2 — DEEP EXTRACTION
# ============================================================================

STAGE2_SYSTEM_PROMPT = """You are an executive healthcare AI intelligence analyst.

Analyze the article and return structured JSON.

Allowed verticals:
- Drug Discovery & R&D
- Diagnostics & Imaging
- Clinical Decision Support
- Ambient Documentation & Clinical Workflow
- Revenue Cycle Management (RCM)
- Patient Engagement & Virtual Care
- Hospital Operations & Supply Chain

Allowed sub-verticals:
- Mergers & Acquisitions
- Partnerships & Alliances
- Funding & Venture Capital
- Product Launch
- Regulatory Approval / Clearance
- Policy & Governance
- Clinical Validation / Evidence
- Customer Deployment / Adoption
- Commercial Expansion
- Technology Breakthrough
- Safety, Bias & Ethics
- Legal & Disputes
- Cybersecurity & Privacy
- Leadership & Management
- Market Outlook / Industry Trends

Return only valid JSON:
{
  "primary_vertical": "",
  "secondary_verticals": [],
  "sub_vertical": "",
  "news_type": "",
  "entities": {
    "companies": [],
    "regulators": [],
    "health_systems": [],
    "investors": [],
    "products": [],
    "people": []
  },
  "geography": "",
  "impact_level": "Critical/High/Medium/Low",
  "evidence_strength": "High/Medium/Low",
  "source_quality": "Official/Peer-reviewed/Specialist media/Company PR/Aggregator/Other",
  "relevance_score": 0,
  "confidence_score": 0,
  "executive_summary": "",
  "why_it_matters": "",
  "event_fingerprint": {}
}"""

_REQUIRED_STAGE2 = ["primary_vertical", "secondary_verticals", "sub_vertical", "news_type",
                    "entities", "geography", "impact_level", "evidence_strength",
                    "source_quality", "relevance_score", "confidence_score",
                    "executive_summary", "why_it_matters"]


def _empty_stage2() -> Dict:
    return {
        "primary_vertical": None, "secondary_verticals": [], "sub_vertical": None,
        "news_type": None,
        "entities": {"companies": [], "regulators": [], "health_systems": [],
                     "investors": [], "products": [], "people": []},
        "geography": None, "impact_level": None, "evidence_strength": None,
        "source_quality": None, "relevance_score": 0, "confidence_score": 0,
        "executive_summary": "", "why_it_matters": "", "event_fingerprint": {},
    }


def batch_deep_analysis(articles_batch: List[Dict]) -> List[Dict]:
    """Deep-extract a batch of articles in one Gemini call."""
    articles_text = ""
    for i, a in enumerate(articles_batch):
        content = (a.get('content') or '')[:2000] or a.get('title', '')
        articles_text += (
            f"\n--- ARTICLE {i+1} (source: {a.get('source','')}, "
            f"domain: {a.get('source_domain','')}) ---\n"
            f"Title: {a.get('title','')}\nContent: {content}\n"
        )

    user_prompt = f"""Analyze these {len(articles_batch)} articles. For EACH article, return one JSON object
following exactly the schema in your instructions (same field names and allowed values).

{articles_text}

Return ONLY a JSON array with one object per article, in the same order. No other text."""

    try:
        raw = gemini_generate(STAGE2_SYSTEM_PROMPT, user_prompt,
                              max_tokens=len(articles_batch) * 700, temperature=0.0)
        analyses = parse_json_array(raw)
    except Exception as e:
        logging.error(f"Batch deep analysis failed: {e}")
        return [_empty_stage2() for _ in articles_batch]

    results = []
    for i in range(len(articles_batch)):
        a = analyses[i] if i < len(analyses) and isinstance(analyses[i], dict) else {}
        merged = _empty_stage2()
        merged.update({k: v for k, v in a.items() if k in merged or k == "event_fingerprint"})
        if not isinstance(merged.get("entities"), dict):
            merged["entities"] = _empty_stage2()["entities"]
        try:
            merged["relevance_score"] = max(0, min(100, int(merged.get("relevance_score") or 0)))
        except Exception:
            merged["relevance_score"] = 0
        try:
            merged["confidence_score"] = max(0, min(100, int(merged.get("confidence_score") or 0)))
        except Exception:
            merged["confidence_score"] = 0
        results.append(merged)
    return results


def stage2_deep_analysis(df: pd.DataFrame) -> pd.DataFrame:
    logging.info("\n" + "=" * 60)
    logging.info("STAGE 2: DEEP EXTRACTION")
    logging.info(f"  Batch size: {STAGE2_BATCH_SIZE} | Concurrency: {STAGE2_CONCURRENCY}")
    logging.info("=" * 60)

    # Initialise extraction columns
    for col in ["primary_vertical", "sub_vertical", "news_type", "secondary_verticals",
                "companies", "regulators", "health_systems", "investors", "products",
                "geography", "impact_level", "evidence_strength", "source_quality",
                "executive_summary", "why_it_matters"]:
        df[col] = None
    df["entities"] = None
    df["entities"] = df["entities"].astype(object)

    high = df[df['relevance_score'] >= RELEVANCE_THRESHOLD].copy()
    if high.empty:
        logging.warning("No articles meet relevance threshold. Skipping Stage 2.")
        return df

    indices = list(high.index)
    total = len(indices)

    # Scrape content in parallel (fallback to raw_snippet/title downstream)
    logging.info(f"📥 Scraping {total} articles for content...")
    contents = {}
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as executor:
        fut = {executor.submit(scrape_article, df.loc[idx, 'url']): idx for idx in indices}
        for f in as_completed(fut):
            idx = fut[f]
            contents[idx] = f.result()
    logging.info(f"   ✅ Scraped {sum(1 for v in contents.values() if v)} / {total} with content")

    # Build batches
    all_batches = []
    for i in range(0, total, STAGE2_BATCH_SIZE):
        batch_indices = indices[i:i + STAGE2_BATCH_SIZE]
        batch = []
        for idx in batch_indices:
            row = df.loc[idx]
            body = contents.get(idx, '') or str(row.get('raw_snippet', '') or '')
            batch.append({
                'title': str(row['title']),
                'content': body,
                'source': str(row.get('source', '') or ''),
                'source_domain': str(row.get('source_domain', '') or ''),
            })
        all_batches.append((batch_indices, batch))

    logging.info(f"🔍 Extracting {len(all_batches)} batches...")

    def analyze(batch_tuple):
        batch_indices, batch = batch_tuple
        return batch_indices, batch_deep_analysis(batch)

    with ThreadPoolExecutor(max_workers=STAGE2_CONCURRENCY) as executor:
        futures = {executor.submit(analyze, b): b for b in all_batches}
        for future in as_completed(futures):
            batch_indices, results = future.result()
            for idx, a in zip(batch_indices, results):
                ents = a.get("entities", {}) or {}
                df.at[idx, 'primary_vertical'] = a.get('primary_vertical')
                df.at[idx, 'secondary_verticals'] = list_to_str(a.get('secondary_verticals'))
                df.at[idx, 'sub_vertical'] = a.get('sub_vertical')
                df.at[idx, 'news_type'] = a.get('news_type')
                df.at[idx, 'entities'] = ents
                df.at[idx, 'companies'] = list_to_str(ents.get('companies'))
                df.at[idx, 'regulators'] = list_to_str(ents.get('regulators'))
                df.at[idx, 'health_systems'] = list_to_str(ents.get('health_systems'))
                df.at[idx, 'investors'] = list_to_str(ents.get('investors'))
                df.at[idx, 'products'] = list_to_str(ents.get('products'))
                df.at[idx, 'geography'] = a.get('geography')
                df.at[idx, 'impact_level'] = a.get('impact_level')
                df.at[idx, 'evidence_strength'] = a.get('evidence_strength')
                df.at[idx, 'source_quality'] = a.get('source_quality')
                df.at[idx, 'executive_summary'] = a.get('executive_summary')
                df.at[idx, 'why_it_matters'] = a.get('why_it_matters')
                # Stage 2 re-scores with full content; override Stage 1 when present
                if a.get('relevance_score'):
                    df.at[idx, 'relevance_score'] = a['relevance_score']
                if a.get('confidence_score'):
                    df.at[idx, 'confidence_score'] = a['confidence_score']

    logging.info(f"✅ Stage 2 Complete: {total} articles extracted")
    return df, contents


# ============================================================================
# STAGE 3 — EVENT FINGERPRINT EXTRACTION
# ============================================================================

STAGE3_SYSTEM_PROMPT = """Extract the event fingerprint for deduplication.

Do not summarize. Extract only facts explicitly present.
Use null where unavailable.

Return JSON only."""


def extract_fingerprint(title: str, content: str, sub_vertical: str) -> Dict:
    """Extract a sub-vertical-specific event fingerprint (facts only)."""
    fields = SUBVERTICAL_FINGERPRINT_FIELDS.get(
        (sub_vertical or '').lower().strip(), FINGERPRINT_FALLBACK_FIELDS
    )
    fields_desc = "\n".join([f'  "{f}": <extracted value or null>' for f in fields])

    user_prompt = f"""Article Title: {title}

Article Content: {content[:2000] if content else 'Not available'}

Sub-vertical: {sub_vertical or 'Unknown'}

Extract ONLY the following facts and return as JSON (use null if not explicitly stated):
{{
{fields_desc}
}}

Rules:
- Extract ONLY facts explicitly present in the article
- Do not summarize or infer
- For company/product names use the most common standard form
- Use null for anything not mentioned"""

    try:
        raw = gemini_generate(STAGE3_SYSTEM_PROMPT, user_prompt,
                              max_tokens=400, temperature=0.0)
        return parse_json_object(raw)
    except Exception as e:
        logging.warning(f"Fingerprint extraction failed for '{title[:50]}': {e}")
        return {}


def stage3_fingerprints(df: pd.DataFrame, contents: Dict) -> pd.DataFrame:
    logging.info("\n" + "=" * 60)
    logging.info("STAGE 3: EVENT FINGERPRINT EXTRACTION")
    logging.info(f"  Concurrency: {STAGE3_CONCURRENCY}")
    logging.info("=" * 60)

    df['event_fingerprint'] = None
    df['event_fingerprint'] = df['event_fingerprint'].astype(object)

    high = df[df['relevance_score'] >= RELEVANCE_THRESHOLD]
    indices = list(high.index)
    if not indices:
        return df

    def run(idx):
        row = df.loc[idx]
        title = str(row['title'])
        content = contents.get(idx, '') or str(row.get('raw_snippet', '') or '')
        sub_vertical = str(row.get('sub_vertical') or '')
        fp = extract_fingerprint(title, content, sub_vertical)
        time.sleep(RATE_LIMIT_DELAY)
        return idx, fp

    with ThreadPoolExecutor(max_workers=STAGE3_CONCURRENCY) as executor:
        futures = {executor.submit(run, idx): idx for idx in indices}
        done = 0
        for future in as_completed(futures):
            idx, fp = future.result()
            df.at[idx, 'event_fingerprint'] = fp
            done += 1
            if done % 25 == 0:
                logging.info(f"   ...{done}/{len(indices)} fingerprints")

    logging.info(f"✅ Stage 3 Complete: {len(indices)} fingerprints")
    return df


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def main():
    start_time = time.time()
    logging.info("=" * 60)
    logging.info("ZOLOTEL — HEALTHCARE AI INTELLIGENCE PROCESSOR")
    logging.info("=" * 60)

    logging.info("📥 Loading articles from raw_articles (status='new')...")
    df = load_raw_articles()
    if df.empty:
        logging.info("ℹ️  No new articles to process. Exiting.")
        return
    logging.info(f"📄 Loaded {len(df)} articles")

    # Stage 1 — score all
    df = stage1_quick_scoring(df)

    # Split: below-threshold rows are marked processed (not deep-extracted, not stored)
    below = df[df['relevance_score'] < RELEVANCE_THRESHOLD]
    if not below.empty:
        mark_raw_processed([int(x) for x in below['id'].tolist()])
        logging.info(f"   Marked {len(below)} below-threshold raw rows as processed")

    # Stage 2 — deep extraction (high-relevance only)
    result = stage2_deep_analysis(df)
    if isinstance(result, tuple):
        df, contents = result
    else:
        df, contents = result, {}

    # Stage 3 — fingerprints (high-relevance only)
    df = stage3_fingerprints(df, contents)

    # Persist high-relevance extracted rows
    high_df = df[df['relevance_score'] >= RELEVANCE_THRESHOLD].copy()
    logging.info("\n💾 Saving to processed_articles...")
    save_to_processed_articles(high_df)

    # Stats
    elapsed = time.time() - start_time
    logging.info("\n" + "=" * 60)
    logging.info("📈 PROCESSING COMPLETE")
    logging.info("=" * 60)
    logging.info(f"⏱️  Time: {elapsed/60:.1f} minutes")
    logging.info(f"📄 Total processed: {len(df)}")
    logging.info(f"⭐ Stored (>= {RELEVANCE_THRESHOLD}): {len(high_df)}")
    if not high_df.empty:
        logging.info("\n📁 Top verticals:")
        for v, c in high_df['primary_vertical'].value_counts().head(7).items():
            logging.info(f"   {v}: {c}")
        logging.info("\n📁 Top sub-verticals:")
        for s, c in high_df['sub_vertical'].value_counts().head(8).items():
            logging.info(f"   {s}: {c}")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
