# Zolotel — Deployment Guide

This takes you from the files on your machine to a live site. Four free services do the work:

- **Neon** — the PostgreSQL database (stores all intelligence)
- **Google AI Studio** — gives you the Gemini API key (powers extraction + Ask AI)
- **GitHub** — holds your code and runs the daily pipeline
- **Vercel** — hosts the website and the API

You only set this up once. After that it runs itself daily.

---

## How the pieces fit together

```
   GitHub Actions (daily cron)              Vercel
   run_pipeline.py                          ┌─────────────────────┐
   scrape → process → cluster → rank  ───►  │  Neon  ◄── main.py   │  ◄── visitors
        writes to Neon                      │  (DB)      (API)     │
                                            │         index.html   │
                                            └─────────────────────┘
```

The **pipeline** runs on GitHub (it takes several minutes and makes many API calls, so it can't run on Vercel's short-lived functions). The **website + API** run on Vercel and read from Neon. Both talk to the same Neon database.

---

## The files

Put all of these in one folder (this becomes your GitHub repo):

```
zolotel/
├── Healthcare_AI_Taxonomy.xlsx     ← the taxonomy (pipeline reads this)
├── scraper_production.py
├── llm_processor_production.py
├── cluster_engine.py
├── ranking.py
├── run_pipeline.py
├── main.py                         ← the API
├── index.html                      ← the dashboard
├── schema.sql                      ← database tables
├── requirements.txt                ← API dependencies (Vercel)
├── requirements-pipeline.txt       ← pipeline dependencies (GitHub Actions)
├── vercel.json                     ← Vercel routing
├── .env.example
└── .github/workflows/pipeline.yml  ← the daily cron
```

Two important notes:
- **Replace the old KEC files** — `main.py`, `index.html`, `scraper_production.py`, `llm_processor_production.py`, and `run_pipeline.py` are the new versions and take the place of your old ones.
- **Commit `Healthcare_AI_Taxonomy.xlsx`** to the repo — the scraper and ranking read it at runtime.

---

## Step 1 — Create the database (Neon)

1. Go to **neon.tech**, sign in, create a project (any name).
2. Open the **SQL Editor** (left sidebar).
3. Open `schema.sql`, copy **all** of it, paste it in, click **Run**. This creates the three tables. (Safe to run more than once.)
4. Open **Connection Details** and copy the connection string. It looks like:
   `postgresql://user:password@host.neon.tech/dbname?sslmode=require`
   This is your **`DATABASE_URL`** — keep it handy.

---

## Step 2 — Get the Gemini key (Google AI Studio)

1. Go to **aistudio.google.com/apikey**.
2. Create an API key.
3. Copy it. This is your **`GEMINI_API_KEY`** — keep it handy.

---

## Step 3 — Put the code on GitHub

1. Create a new repository at **github.com** (private is fine).
2. Upload the whole `zolotel/` folder (or push it with git).
3. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add **two** secrets:
   - `DATABASE_URL` → your Neon string from Step 1
   - `GEMINI_API_KEY` → your key from Step 2

These secrets let the daily pipeline reach the database and Gemini.

---

## Step 4 — Load data for the first time

The site is empty until the pipeline runs once. Two options:

**Option A — run it from GitHub (no setup on your computer):**
1. In your repo, open the **Actions** tab.
2. Pick **Zolotel Pipeline** → **Run workflow**.
3. Wait a few minutes; it scrapes, processes, clusters, and ranks into Neon.

> First-run tip: the scraper saves only the **last 1 day** of news by default (good for daily runs). For a richer first load, open `scraper_production.py` and temporarily set `SAVE_WINDOW_DAYS = 7`, run once, then set it back to `1`.

**Option B — run it on your computer:**
```bash
pip install -r requirements-pipeline.txt
# set the two variables (Mac/Linux):
export DATABASE_URL="...your neon string..."
export GEMINI_API_KEY="...your key..."
python run_pipeline.py
```

After it finishes, you can confirm rows exist in Neon's SQL Editor:
`SELECT count(*) FROM event_clusters;`

---

## Step 5 — Deploy the site + API (Vercel)

1. Go to **vercel.com**, sign in with GitHub.
2. **Add New → Project**, import your `zolotel` repo.
3. Before deploying, open **Environment Variables** and add the same two:
   - `DATABASE_URL`
   - `GEMINI_API_KEY`
4. Click **Deploy**.

Vercel reads `vercel.json`: it serves `index.html` as the site and runs `main.py` as the API at `/api/*`. Because they share one domain, the dashboard's `API_BASE = ""` (same origin) just works — you don't change anything.

When it finishes, open the Vercel URL. Check `https://your-app.vercel.app/api/health` — it should say `"database": "connected"`. Then open the site; your intelligence should be there.

---

## Step 6 — Point your domain (Namecheap)

1. In your Vercel project: **Settings → Domains → Add**, enter your domain (e.g. `zolotel.com` or a subdomain like `intel.yourdomain.com`).
2. Vercel shows you the DNS records to set.
3. In **Namecheap → Domain List → Manage → Advanced DNS**, add the records Vercel gave you (usually an `A` record for the root and/or a `CNAME` for a subdomain). 
4. Wait for it to propagate (minutes to a couple of hours). Vercel will mark the domain **Valid** when it's ready.

---

## Step 7 — Confirm it's all running

- The **daily cron** is already configured (`.github/workflows/pipeline.yml`, 06:00 UTC). It will keep adding fresh intelligence every day. Change the `cron:` line to adjust the time.
- Open the site → click through the tabs, run a search, and try **Ask AI**.
- Each morning after the cron runs, new events appear automatically.

---

## Settings you might tune later

- **Region of news** — `scraper_production.py` is set to US sources (FDA/CMS/HHS-led). Switch the `GOOGLE_NEWS_*` constants to India if you prefer.
- **How much to keep** — `RELEVANCE_THRESHOLD` in `llm_processor_production.py` (40 = exhaustive; raise to 70 for an executive-only feed).
- **Ranking weights** — edit the `Healthcare_AI_Taxonomy.xlsx` sheets (Category Impact Scores, Source Priority) or the tunables at the top of `ranking.py`.
- **Cluster summaries cost** — `USE_LLM_CLUSTER_SUMMARY` in `cluster_engine.py` (set `False` to make clustering fully free).

---

## If something doesn't work

- **Site loads but says "Couldn't reach the intelligence service"** → the API can't reach Neon. Re-check `DATABASE_URL` in Vercel's environment variables, then redeploy.
- **"No events yet"** → the pipeline hasn't populated data. Run it (Step 4).
- **Ask AI errors** → `GEMINI_API_KEY` missing or wrong in Vercel.
- **Pipeline fails in GitHub Actions** → open the failed run's logs; usually a missing secret (`DATABASE_URL` / `GEMINI_API_KEY`) or the schema wasn't applied (Step 1).
