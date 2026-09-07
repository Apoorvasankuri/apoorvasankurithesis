-- ============================================================================
-- Zolotel — AI Healthcare Intelligence Platform
-- PostgreSQL schema (Neon)
--
-- Three-table pipeline:
--   raw_articles        <- scraper_production.py writes here
--   processed_articles  <- llm_processor_production.py writes here
--   event_clusters      <- backend clustering (step 5) + cluster ranking (step 6)
--
-- Apply with:  psql "$DATABASE_URL" -f schema.sql
--          or: paste into the Neon SQL editor
-- Safe to re-run: uses IF NOT EXISTS / idempotent constraint guards.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. raw_articles — scraped Google News items awaiting processing
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_articles (
    id                  SERIAL PRIMARY KEY,
    title               TEXT,
    url                 TEXT UNIQUE,                 -- scraper ON CONFLICT (url)
    canonical_url       TEXT,                        -- UTM/tracking stripped
    source              TEXT,
    source_domain       TEXT,
    published_date      TIMESTAMP,
    scraped_date        TIMESTAMP DEFAULT NOW(),
    raw_snippet         TEXT,
    raw_content         TEXT,
    search_keyword      TEXT,                        -- keyword that found it
    vertical_seed       TEXT,                        -- seed vertical (LLM decides final)
    source_type         TEXT,
    processing_status   TEXT DEFAULT 'new'           -- new -> processed
);

CREATE INDEX IF NOT EXISTS idx_raw_status
    ON raw_articles (processing_status);
CREATE INDEX IF NOT EXISTS idx_raw_status_new
    ON raw_articles (published_date DESC)
    WHERE processing_status = 'new';
CREATE INDEX IF NOT EXISTS idx_raw_published
    ON raw_articles (published_date DESC);


-- ----------------------------------------------------------------------------
-- 2. processed_articles — LLM-extracted, structured intelligence rows
--    (cluster_id / duplicate_status / relationship_type / rank_score /
--     primary_source_in_cluster are filled by steps 5 & 6)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processed_articles (
    id                          SERIAL PRIMARY KEY,
    raw_article_id              INT UNIQUE REFERENCES raw_articles(id),  -- processor ON CONFLICT
    title                       TEXT,
    url                         TEXT,                -- clickable link for the UI
    canonical_url               TEXT,
    source                      TEXT,
    source_domain               TEXT,
    published_date              TIMESTAMP,

    -- classification
    primary_vertical            TEXT,
    secondary_verticals         TEXT,
    sub_vertical                TEXT,
    news_type                   TEXT,

    -- entities (full JSON in `entities`; flattened lists in the columns below)
    entities                    TEXT,
    companies                   TEXT,
    regulators                  TEXT,
    health_systems              TEXT,
    investors                   TEXT,
    products                    TEXT,

    geography                   TEXT,

    -- scoring / assessment
    impact_level                TEXT,                -- Critical / High / Medium / Low
    evidence_strength           TEXT,                -- High / Medium / Low
    source_quality              TEXT,                -- Official / Peer-reviewed / ...
    confidence_score            INT,                 -- 0-100
    relevance_score             INT,                 -- 0-100
    rank_score                  INT,                 -- step 6 (cluster ranking)

    -- narrative
    executive_summary           TEXT,
    why_it_matters              TEXT,

    -- dedup / clustering
    event_fingerprint           JSONB,
    cluster_id                  INT,                 -- FK added below (step 5)
    duplicate_status            TEXT,                -- exact_duplicate / same_event / ...
    relationship_type           TEXT,                -- same as duplicate_status family
    primary_source_in_cluster   BOOLEAN DEFAULT FALSE,

    created_at                  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_proc_published
    ON processed_articles (published_date DESC);
CREATE INDEX IF NOT EXISTS idx_proc_primary_vertical
    ON processed_articles (primary_vertical);
CREATE INDEX IF NOT EXISTS idx_proc_sub_vertical
    ON processed_articles (sub_vertical);
CREATE INDEX IF NOT EXISTS idx_proc_source_quality
    ON processed_articles (source_quality);
CREATE INDEX IF NOT EXISTS idx_proc_relevance
    ON processed_articles (relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_proc_rank
    ON processed_articles (rank_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_proc_cluster
    ON processed_articles (cluster_id);
CREATE INDEX IF NOT EXISTS idx_proc_rank_published
    ON processed_articles (rank_score DESC NULLS LAST, published_date DESC);

-- Full-text search over title + summary + why-it-matters
-- ('english'::regconfig keeps the expression IMMUTABLE so it is index-safe)
CREATE INDEX IF NOT EXISTS idx_proc_fts
    ON processed_articles
    USING GIN (
        to_tsvector(
            'english'::regconfig,
            COALESCE(title, '') || ' ' ||
            COALESCE(executive_summary, '') || ' ' ||
            COALESCE(why_it_matters, '')
        )
    );


-- ----------------------------------------------------------------------------
-- 3. event_clusters — grouped events (one cluster = one underlying news event)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_clusters (
    id                          SERIAL PRIMARY KEY,
    cluster_title               TEXT,
    primary_vertical            TEXT,
    sub_vertical                TEXT,
    event_type                  TEXT,
    canonical_event_fingerprint JSONB,
    representative_article_id   INT,                 -- FK added below
    cluster_summary             TEXT,
    why_it_matters              TEXT,
    impact_level                TEXT,
    evidence_strength           TEXT,
    source_quality              TEXT,
    cluster_rank_score          INT,
    article_count               INT,
    first_seen                  TIMESTAMP,
    last_seen                   TIMESTAMP,
    created_at                  TIMESTAMP DEFAULT NOW(),
    updated_at                  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cluster_primary_vertical
    ON event_clusters (primary_vertical);
CREATE INDEX IF NOT EXISTS idx_cluster_sub_vertical
    ON event_clusters (sub_vertical);
CREATE INDEX IF NOT EXISTS idx_cluster_rank
    ON event_clusters (cluster_rank_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_cluster_last_seen
    ON event_clusters (last_seen DESC);


-- ----------------------------------------------------------------------------
-- 4. Cross-table foreign keys (added after both tables exist — circular ref)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_proc_cluster'
    ) THEN
        ALTER TABLE processed_articles
            ADD CONSTRAINT fk_proc_cluster
            FOREIGN KEY (cluster_id) REFERENCES event_clusters(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_cluster_rep_article'
    ) THEN
        ALTER TABLE event_clusters
            ADD CONSTRAINT fk_cluster_rep_article
            FOREIGN KEY (representative_article_id) REFERENCES processed_articles(id)
            ON DELETE SET NULL;
    END IF;
END $$;
