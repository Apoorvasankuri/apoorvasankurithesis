"""
Zolotel — AI Healthcare Intelligence API (FastAPI)

Serves the clustered, ranked intelligence from Neon to the dashboard.
Read-only data endpoints + an Ask-AI assistant (Gemini with Google Search grounding).

Endpoints
  GET  /                        health/info
  GET  /api/health              DB connectivity
  GET  /api/stats               headline counts
  GET  /api/verticals           the 7 verticals with counts
  GET  /api/clusters            clusters (filterable, paginated, ranked)
  GET  /api/clusters/top        Executive Brief — top clusters grouped by vertical
  GET  /api/clusters/{id}       cluster detail + member articles
  GET  /api/articles            All Intelligence — articles (filterable, paginated)
  GET  /api/regulatory-watch    lens tab (grouped by vertical)
  GET  /api/clinical-evidence   lens tab
  GET  /api/companies-deals     lens tab
  GET  /api/adoption-tracker    lens tab
  GET  /api/risk-governance     lens tab
  GET  /api/search              full-text search
  POST /api/chat                Ask AI over the database
  GET  /api/export-csv          CSV export

Requires env: DATABASE_URL (Neon), GEMINI_API_KEY (for /api/chat).
"""

import os
import io
import csv
import json
import math
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row

try:
    from google import genai
    from google.genai import types
except Exception:  # google-genai optional at import time (only needed for /api/chat)
    genai = None
    types = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ─── Taxonomy constants ─────────────────────────────────────────────────────
VERTICALS = [
    "Drug Discovery & R&D",
    "Diagnostics & Imaging",
    "Clinical Decision Support",
    "Ambient Documentation & Clinical Workflow",
    "Revenue Cycle Management (RCM)",
    "Patient Engagement & Virtual Care",
    "Hospital Operations & Supply Chain",
]

# Dashboard tab -> included sub-verticals (taxonomy Sheet 12)
TAB_SUBVERTICALS = {
    "regulatory-watch": ["Regulatory Approval / Clearance", "Policy & Governance"],
    "clinical-evidence": ["Clinical Validation / Evidence", "Technology Breakthrough"],
    "companies-deals": ["Mergers & Acquisitions", "Partnerships & Alliances",
                        "Funding & Venture Capital", "Product Launch", "Commercial Expansion"],
    "adoption-tracker": ["Customer Deployment / Adoption", "Commercial Expansion"],
    "risk-governance": ["Safety, Bias & Ethics", "Legal & Disputes",
                       "Cybersecurity & Privacy", "Policy & Governance"],
}

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
_gemini_client = None


def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or genai is None:
            raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


app = FastAPI(title="Zolotel — AI Healthcare Intelligence API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# HELPERS
# ============================================================================

def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL environment variable not set")
    return psycopg.connect(database_url, row_factory=dict_row)


def safe_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def safe_float(value):
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def iso(value):
    return value.isoformat() if isinstance(value, (datetime, date)) else None


def split_list(value) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in str(value).split(',') if x.strip() and x.strip() != '-']


def serialize_cluster(row: dict) -> dict:
    return {
        "id": safe_int(row.get("id")),
        "cluster_title": row.get("cluster_title"),
        "primary_vertical": row.get("primary_vertical"),
        "sub_vertical": row.get("sub_vertical"),
        "event_type": row.get("event_type"),
        "cluster_summary": row.get("cluster_summary"),
        "why_it_matters": row.get("why_it_matters"),
        "impact_level": row.get("impact_level"),
        "evidence_strength": row.get("evidence_strength"),
        "source_quality": row.get("source_quality"),
        "cluster_rank_score": safe_int(row.get("cluster_rank_score")),
        "article_count": safe_int(row.get("article_count")) or 1,
        "first_seen": iso(row.get("first_seen")),
        "last_seen": iso(row.get("last_seen")),
        "primary_source": {
            "url": row.get("rep_url"),
            "canonical_url": row.get("rep_canonical_url"),
            "source": row.get("rep_source"),
        },
    }


def serialize_article(row: dict) -> dict:
    return {
        "id": safe_int(row.get("id")),
        "title": row.get("title"),
        "url": row.get("url") or row.get("canonical_url"),
        "source": row.get("source"),
        "source_domain": row.get("source_domain"),
        "published_date": iso(row.get("published_date")),
        "primary_vertical": row.get("primary_vertical"),
        "secondary_verticals": split_list(row.get("secondary_verticals")),
        "sub_vertical": row.get("sub_vertical"),
        "news_type": row.get("news_type"),
        "companies": split_list(row.get("companies")),
        "regulators": split_list(row.get("regulators")),
        "health_systems": split_list(row.get("health_systems")),
        "investors": split_list(row.get("investors")),
        "products": split_list(row.get("products")),
        "geography": row.get("geography"),
        "impact_level": row.get("impact_level"),
        "evidence_strength": row.get("evidence_strength"),
        "source_quality": row.get("source_quality"),
        "confidence_score": safe_int(row.get("confidence_score")),
        "relevance_score": safe_int(row.get("relevance_score")),
        "rank_score": safe_int(row.get("rank_score")),
        "executive_summary": row.get("executive_summary"),
        "why_it_matters": row.get("why_it_matters"),
        "cluster_id": safe_int(row.get("cluster_id")),
        "relationship_type": row.get("relationship_type"),
        "primary_source_in_cluster": bool(row.get("primary_source_in_cluster")),
    }


CLUSTER_SELECT = """
    SELECT ec.*, pa.url AS rep_url, pa.canonical_url AS rep_canonical_url,
           pa.source AS rep_source
    FROM event_clusters ec
    LEFT JOIN processed_articles pa ON pa.id = ec.representative_article_id
"""


# ============================================================================
# HEALTH / INFO
# ============================================================================

@app.get("/")
def read_root():
    return {"status": "healthy", "service": "Zolotel — AI Healthcare Intelligence API",
            "timestamp": datetime.now().isoformat()}


@app.get("/api/health")
def health_check():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return {"status": "healthy", "database": "connected",
                "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}


@app.get("/api/stats")
def get_statistics():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM event_clusters")
        total_clusters = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM processed_articles")
        total_articles = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM event_clusters WHERE last_seen >= CURRENT_DATE - INTERVAL '7 days'")
        recent = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM event_clusters WHERE impact_level IN ('Critical','High')")
        high_impact = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM event_clusters WHERE sub_vertical = 'Regulatory Approval / Clearance'")
        regulatory = cur.fetchone()["c"]
        cur.execute("""SELECT primary_vertical AS v, COUNT(*) AS c FROM event_clusters
                       WHERE primary_vertical IS NOT NULL GROUP BY primary_vertical""")
        by_vertical = {r["v"]: r["c"] for r in cur.fetchall()}
        cur.close()
        conn.close()
        return {"status": "success", "stats": {
            "total_clusters": int(total_clusters),
            "total_articles": int(total_articles),
            "recent_clusters_7d": int(recent),
            "high_impact_clusters": int(high_impact),
            "regulatory_clearances": int(regulatory),
            "clusters_by_vertical": by_vertical,
        }}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/api/verticals")
def get_verticals():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""SELECT primary_vertical AS v, COUNT(*) AS clusters,
                              COALESCE(SUM(article_count),0) AS articles
                       FROM event_clusters WHERE primary_vertical IS NOT NULL
                       GROUP BY primary_vertical""")
        stats = {r["v"]: {"clusters": int(r["clusters"]), "articles": int(r["articles"])}
                 for r in cur.fetchall()}
        cur.close()
        conn.close()
        return {"status": "success", "verticals": [
            {"name": v, "clusters": stats.get(v, {}).get("clusters", 0),
             "articles": stats.get(v, {}).get("articles", 0)}
            for v in VERTICALS
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# ============================================================================
# CLUSTERS
# ============================================================================

@app.get("/api/clusters")
def get_clusters(vertical: str = "", sub_vertical: str = "", impact: str = "",
                 days: int = 0, limit: int = 50, offset: int = 0):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        where, params = [], []
        if vertical:
            where.append("ec.primary_vertical = %s"); params.append(vertical)
        if sub_vertical:
            where.append("ec.sub_vertical = %s"); params.append(sub_vertical)
        if impact:
            where.append("ec.impact_level = %s"); params.append(impact)
        if days and days > 0:
            where.append("ec.last_seen >= CURRENT_DATE - (%s * INTERVAL '1 day')"); params.append(days)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        query = f"""{CLUSTER_SELECT} {where_sql}
                    ORDER BY ec.cluster_rank_score DESC NULLS LAST, ec.last_seen DESC
                    LIMIT %s OFFSET %s"""
        cur.execute(query, params + [limit, offset])
        clusters = [serialize_cluster(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {"status": "success", "count": len(clusters), "clusters": clusters}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/api/clusters/top")
def get_top_clusters(limit_per_vertical: int = 5):
    """Executive Brief: top clusters grouped by the 7 verticals (taxonomy ES01-ES03)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        result = []
        for v in VERTICALS:
            cur.execute(f"""{CLUSTER_SELECT}
                            WHERE ec.primary_vertical = %s
                            ORDER BY ec.cluster_rank_score DESC NULLS LAST, ec.last_seen DESC
                            LIMIT %s""", [v, limit_per_vertical])
            clusters = [serialize_cluster(r) for r in cur.fetchall()]
            result.append({"vertical": v, "clusters": clusters})
        cur.close()
        conn.close()
        return {"status": "success", "tab": "Executive Brief", "verticals": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/api/clusters/{cluster_id}")
def get_cluster_detail(cluster_id: int):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"{CLUSTER_SELECT} WHERE ec.id = %s", [cluster_id])
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="Cluster not found")
        cluster = serialize_cluster(row)
        cur.execute("""SELECT * FROM processed_articles WHERE cluster_id = %s
                       ORDER BY primary_source_in_cluster DESC, rank_score DESC NULLS LAST,
                                published_date DESC""", [cluster_id])
        cluster["articles"] = [serialize_article(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {"status": "success", "cluster": cluster}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# ============================================================================
# LENS TABS (grouped by vertical)
# ============================================================================

def _clusters_grouped_by_vertical(sub_verticals: List[str], limit_per_vertical: int = 8,
                                   days: int = 0) -> List[dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    out = []
    for v in VERTICALS:
        where = ["ec.primary_vertical = %s", "ec.sub_vertical = ANY(%s)"]
        params = [v, sub_verticals]
        if days and days > 0:
            where.append("ec.last_seen >= CURRENT_DATE - (%s * INTERVAL '1 day')"); params.append(days)
        cur.execute(f"""{CLUSTER_SELECT} WHERE {" AND ".join(where)}
                        ORDER BY ec.cluster_rank_score DESC NULLS LAST, ec.last_seen DESC
                        LIMIT %s""", params + [limit_per_vertical])
        out.append({"vertical": v, "clusters": [serialize_cluster(r) for r in cur.fetchall()]})
    cur.close()
    conn.close()
    return out


def _lens_endpoint(tab_key: str, tab_name: str, limit_per_vertical: int, days: int):
    try:
        groups = _clusters_grouped_by_vertical(TAB_SUBVERTICALS[tab_key], limit_per_vertical, days)
        return {"status": "success", "tab": tab_name,
                "sub_verticals": TAB_SUBVERTICALS[tab_key], "verticals": groups}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/api/regulatory-watch")
def regulatory_watch(limit_per_vertical: int = 8, days: int = 0):
    return _lens_endpoint("regulatory-watch", "Regulatory Watch", limit_per_vertical, days)


@app.get("/api/clinical-evidence")
def clinical_evidence(limit_per_vertical: int = 8, days: int = 0):
    return _lens_endpoint("clinical-evidence", "Clinical Evidence", limit_per_vertical, days)


@app.get("/api/companies-deals")
def companies_deals(limit_per_vertical: int = 8, days: int = 0):
    return _lens_endpoint("companies-deals", "Companies & Deals", limit_per_vertical, days)


@app.get("/api/adoption-tracker")
def adoption_tracker(limit_per_vertical: int = 8, days: int = 0):
    return _lens_endpoint("adoption-tracker", "Adoption Tracker", limit_per_vertical, days)


@app.get("/api/risk-governance")
def risk_governance(limit_per_vertical: int = 8, days: int = 0):
    return _lens_endpoint("risk-governance", "Risk & Governance", limit_per_vertical, days)


# ============================================================================
# ARTICLES (All Intelligence)
# ============================================================================

@app.get("/api/articles")
def get_articles(vertical: str = "", sub_vertical: str = "", source_quality: str = "",
                 impact: str = "", days: int = 0, search: str = "",
                 include_duplicates: bool = False, limit: int = 50, offset: int = 0):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        where, params = [], []
        if not include_duplicates:
            where.append("(relationship_type IS DISTINCT FROM 'exact_duplicate')")
        if vertical:
            where.append("primary_vertical = %s"); params.append(vertical)
        if sub_vertical:
            where.append("sub_vertical = %s"); params.append(sub_vertical)
        if source_quality:
            where.append("source_quality = %s"); params.append(source_quality)
        if impact:
            where.append("impact_level = %s"); params.append(impact)
        if days and days > 0:
            where.append("published_date >= CURRENT_DATE - (%s * INTERVAL '1 day')"); params.append(days)
        if search:
            where.append("""to_tsvector('english',
                COALESCE(title,'')||' '||COALESCE(executive_summary,'')||' '||COALESCE(why_it_matters,''))
                @@ plainto_tsquery('english', %s)""")
            params.append(search)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(f"""SELECT * FROM processed_articles {where_sql}
                        ORDER BY rank_score DESC NULLS LAST, published_date DESC
                        LIMIT %s OFFSET %s""", params + [limit, offset])
        articles = [serialize_article(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {"status": "success", "count": len(articles), "articles": articles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# ============================================================================
# SEARCH
# ============================================================================

@app.get("/api/search")
def search(q: str = "", limit: int = 20):
    if not q.strip():
        return {"status": "success", "count": 0, "results": []}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # One representative per event (exclude non-representative cluster members)
        cur.execute("""
            SELECT * FROM processed_articles
            WHERE (primary_source_in_cluster = TRUE OR cluster_id IS NULL)
            AND to_tsvector('english',
                COALESCE(title,'')||' '||COALESCE(executive_summary,'')||' '||COALESCE(why_it_matters,''))
                @@ plainto_tsquery('english', %s)
            ORDER BY rank_score DESC NULLS LAST, published_date DESC
            LIMIT %s
        """, [q, limit])
        results = [serialize_article(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {"status": "success", "count": len(results), "query": q, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# ============================================================================
# ASK AI (chat)
# ============================================================================

def preprocess_chat_query(message: str):
    """Extract keywords + optional vertical/sub-vertical/date filters from NL."""
    msg = message.lower().strip()

    days = None
    for phrase, d in {
        'today': 1, 'yesterday': 2, 'this week': 7, 'past week': 7, 'last week': 14,
        'this month': 30, 'past month': 30, 'this quarter': 90, 'recent': 7, 'latest': 7,
    }.items():
        if phrase in msg:
            days = d
            break

    sub_vertical = None
    for phrase, sv in sorted({
        'fda': 'Regulatory Approval / Clearance', 'clearance': 'Regulatory Approval / Clearance',
        'approval': 'Regulatory Approval / Clearance', '510(k)': 'Regulatory Approval / Clearance',
        'policy': 'Policy & Governance', 'governance': 'Policy & Governance', 'cms': 'Policy & Governance',
        'merger': 'Mergers & Acquisitions', 'acquisition': 'Mergers & Acquisitions', 'm&a': 'Mergers & Acquisitions',
        'acquires': 'Mergers & Acquisitions', 'funding': 'Funding & Venture Capital', 'raise': 'Funding & Venture Capital',
        'series a': 'Funding & Venture Capital', 'series b': 'Funding & Venture Capital', 'venture': 'Funding & Venture Capital',
        'partnership': 'Partnerships & Alliances', 'alliance': 'Partnerships & Alliances',
        'trial': 'Clinical Validation / Evidence', 'study': 'Clinical Validation / Evidence',
        'validation': 'Clinical Validation / Evidence', 'evidence': 'Clinical Validation / Evidence',
        'deployment': 'Customer Deployment / Adoption', 'adoption': 'Customer Deployment / Adoption',
        'launch': 'Product Launch', 'breach': 'Cybersecurity & Privacy', 'privacy': 'Cybersecurity & Privacy',
        'lawsuit': 'Legal & Disputes', 'bias': 'Safety, Bias & Ethics', 'safety': 'Safety, Bias & Ethics',
    }.items(), key=lambda x: -len(x[0])):
        if phrase in msg:
            sub_vertical = sv
            break

    vertical = None
    for phrase, v in {
        'drug discovery': 'Drug Discovery & R&D', 'molecule': 'Drug Discovery & R&D', 'pharma': 'Drug Discovery & R&D',
        'radiology': 'Diagnostics & Imaging', 'imaging': 'Diagnostics & Imaging', 'pathology': 'Diagnostics & Imaging',
        'decision support': 'Clinical Decision Support', 'triage': 'Clinical Decision Support',
        'scribe': 'Ambient Documentation & Clinical Workflow', 'documentation': 'Ambient Documentation & Clinical Workflow',
        'revenue cycle': 'Revenue Cycle Management (RCM)', 'rcm': 'Revenue Cycle Management (RCM)',
        'coding': 'Revenue Cycle Management (RCM)', 'claims': 'Revenue Cycle Management (RCM)',
        'patient engagement': 'Patient Engagement & Virtual Care', 'virtual care': 'Patient Engagement & Virtual Care',
        'chatbot': 'Patient Engagement & Virtual Care', 'hospital operations': 'Hospital Operations & Supply Chain',
        'supply chain': 'Hospital Operations & Supply Chain', 'staffing': 'Hospital Operations & Supply Chain',
    }.items():
        if phrase in msg:
            vertical = v
            break

    filler = {'what', 'are', 'the', 'is', 'any', 'show', 'me', 'tell', 'about', 'find', 'get',
              'give', 'list', 'all', 'of', 'for', 'in', 'by', 'from', 'to', 'a', 'an', 'do',
              'does', 'has', 'have', 'been', 'their', 'there', 'how', 'many', 'can', 'you', 'please'}
    keywords = ' '.join(w for w in msg.split() if w not in filler and len(w) > 2).strip()
    return {"keywords": keywords, "sub_vertical": sub_vertical, "vertical": vertical, "days": days}


class ChatRequest(BaseModel):
    message: str
    conversation_history: list = []


@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        parsed = preprocess_chat_query(req.message)

        conn = get_db_connection()
        cur = conn.cursor()
        where = ["(primary_source_in_cluster = TRUE OR cluster_id IS NULL)"]
        params = []
        if parsed["vertical"]:
            where.append("primary_vertical = %s"); params.append(parsed["vertical"])
        if parsed["sub_vertical"]:
            where.append("sub_vertical = %s"); params.append(parsed["sub_vertical"])
        if parsed["days"]:
            where.append("published_date >= CURRENT_DATE - (%s * INTERVAL '1 day')"); params.append(parsed["days"])
        if parsed["keywords"]:
            where.append("""to_tsvector('english',
                COALESCE(title,'')||' '||COALESCE(executive_summary,'')||' '||COALESCE(why_it_matters,''))
                @@ plainto_tsquery('english', %s)""")
            params.append(parsed["keywords"])
        where_sql = " AND ".join(where) if where else "TRUE"
        cur.execute(f"""SELECT title, executive_summary, why_it_matters, primary_vertical,
                               sub_vertical, source, source_quality, published_date, url,
                               impact_level, companies, regulators
                        FROM processed_articles WHERE {where_sql}
                        ORDER BY rank_score DESC NULLS LAST, published_date DESC
                        LIMIT 8""", params)
        rows = cur.fetchall()

        # Fallback: if keyword search found nothing, retry without the FTS clause
        # (keep vertical/sub-vertical/date filters) so the assistant still has context.
        if not rows and parsed["keywords"]:
            fb_where, fb_params = ["(primary_source_in_cluster = TRUE OR cluster_id IS NULL)"], []
            if parsed["vertical"]:
                fb_where.append("primary_vertical = %s"); fb_params.append(parsed["vertical"])
            if parsed["sub_vertical"]:
                fb_where.append("sub_vertical = %s"); fb_params.append(parsed["sub_vertical"])
            if parsed["days"]:
                fb_where.append("published_date >= CURRENT_DATE - (%s * INTERVAL '1 day')"); fb_params.append(parsed["days"])
            cur.execute(f"""SELECT title, executive_summary, why_it_matters, primary_vertical,
                                   sub_vertical, source, source_quality, published_date, url,
                                   impact_level, companies, regulators
                            FROM processed_articles WHERE {" AND ".join(fb_where)}
                            ORDER BY rank_score DESC NULLS LAST, published_date DESC
                            LIMIT 8""", fb_params)
            rows = cur.fetchall()
        cur.close()
        conn.close()

        db_context = ""
        sources = []
        for i, r in enumerate(rows):
            d = iso(r.get("published_date")) or ""
            db_context += (
                f"\n[Article {i+1}]\nTitle: {r.get('title')}\n"
                f"Vertical: {r.get('primary_vertical')} | Sub-vertical: {r.get('sub_vertical')}\n"
                f"Impact: {r.get('impact_level')} | Source: {r.get('source')} ({r.get('source_quality')})\n"
                f"Summary: {r.get('executive_summary')}\nWhy it matters: {r.get('why_it_matters')}\n"
                f"Date: {d}\nLink: {r.get('url')}\n"
            )
            sources.append({"title": r.get("title"), "link": r.get("url"),
                            "date": d, "type": "database"})

        system_prompt = f"""You are Zolotel — an AI healthcare intelligence assistant.
You help executives track AI developments across seven verticals: Drug Discovery & R&D,
Diagnostics & Imaging, Clinical Decision Support, Ambient Documentation & Clinical Workflow,
Revenue Cycle Management (RCM), Patient Engagement & Virtual Care, and Hospital Operations & Supply Chain.

Use the database articles below to answer. When information is available, mention the
company/regulator, the source and its quality, and the date. Cite database-sourced facts
with [DB] and anything from general knowledge or web search with [AI]. Be concise (3-5
sentences) and focus on the strategic, clinical, or regulatory implication. Never invent
numbers or facts not present in the sources.

{db_context}"""

        client = get_gemini_client()
        contents = []
        for m in req.conversation_history[-6:]:
            role = "model" if m.get("role") == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=m.get("content", ""))]))
        contents.append(types.Content(role="user", parts=[types.Part(text=req.message)]))

        tools = [types.Tool(google_search=types.GoogleSearch())]
        config = types.GenerateContentConfig(
            system_instruction=system_prompt, tools=tools,
            temperature=0.3, max_output_tokens=1200,
        )

        last_err = None
        response = None
        for model_name in GEMINI_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name, contents=contents, config=config)
                break
            except Exception as e:
                last_err = e
                if any(t in str(e).upper() for t in ('503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED', '500', 'INTERNAL')):
                    continue
                raise
        if response is None:
            raise last_err

        # Append web grounding sources if present
        try:
            cand = response.candidates[0]
            gm = getattr(cand, "grounding_metadata", None)
            if gm and getattr(gm, "grounding_chunks", None):
                for ch in gm.grounding_chunks:
                    if getattr(ch, "web", None):
                        sources.append({"title": ch.web.title, "link": ch.web.uri,
                                        "date": "", "type": "web"})
        except Exception:
            pass

        return {"status": "success", "answer": response.text, "sources": sources}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# CSV EXPORT
# ============================================================================

@app.get("/api/export-csv")
def export_csv(days: int = 30):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""SELECT published_date, title, source, source_quality, primary_vertical,
                              sub_vertical, news_type, impact_level, evidence_strength,
                              relevance_score, rank_score, geography, companies, regulators, url
                       FROM processed_articles
                       WHERE published_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
                       AND (relationship_type IS DISTINCT FROM 'exact_duplicate')
                       ORDER BY rank_score DESC NULLS LAST, published_date DESC""", [days])
        rows = cur.fetchall()
        cur.close()
        conn.close()
        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for r in rows:
                writer.writerow({k: (v.isoformat() if isinstance(v, (datetime, date)) else v)
                                 for k, v in r.items()})
        output.seek(0)
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=zolotel_export_{days}d.csv"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export error: {str(e)}")
