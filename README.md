# ResearchVault

A living research site that drills from **sector → sub-sector → company**. Built with [Astro](https://astro.build), content written in Markdown, hosted free on Vercel + GitHub.

You add new analysis by dropping a Markdown file into the right folder. The site regenerates itself — new pages, index listings, and cross-links appear automatically.

---

## 1. Run it locally

You need [Node.js](https://nodejs.org) 18.20+ (Node 20+ recommended).

```bash
npm install      # install dependencies (first time only)
npm run dev      # start local server at http://localhost:4321
```

Edit any file and the browser reloads live. When you're happy:

```bash
npm run build    # outputs a static site to /dist
npm run preview  # preview the production build locally
```

---

## 2. Put it on GitHub

```bash
git init
git add .
git commit -m "Initial research vault"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/research-vault.git
git push -u origin main
```

(Create the empty repo first at github.com — it can be free/public or free/private.)

---

## 3. Deploy to Vercel (free)

1. Go to [vercel.com](https://vercel.com) and sign in with GitHub.
2. **Add New → Project**, import your `research-vault` repo.
3. Vercel auto-detects Astro. Leave the defaults (build `astro build`, output `dist`). Click **Deploy**.
4. You get a live URL like `https://research-vault.vercel.app`.

After this, **every `git push` to `main` auto-deploys.** Edit Markdown → commit → push → live in ~30 seconds.

> Update the `site:` field in `astro.config.mjs` to your real Vercel URL so the sitemap and canonical links are correct.

---

## 4. How content works — the only thing you'll touch day-to-day

All content lives in `src/content/` in three folders:

```
src/content/
├── sectors/        ← top-level themes        (e.g. ai-healthcare.md)
├── subsectors/     ← segments within sectors (e.g. clinical-documentation.md)
└── companies/      ← individual deep-dives    (e.g. tempus-ai.md)
```

The **filename becomes the URL slug**. `companies/tempus-ai.md` → `/companies/tempus-ai`.

Each file has a **frontmatter** block (the `--- ... ---` at the top) with structured fields, then Markdown body below it. The frontmatter is validated on build — if you miss a required field, the build tells you exactly what's wrong.

### To add a new SECTOR

Create `src/content/sectors/my-sector.md`:

```markdown
---
title: "My New Sector"
tagline: "One-line hook shown under the title."
summary: "One sentence shown on cards and indexes."
status: active            # active | draft | watching
order: 2                  # controls ordering on the home page
updated: 2026-06-01
convictionOneLiner: "Your one-breath argument (optional, shows in the hero box)."
tags: [thing, another]
---

## Sector Overview — The Setup
Write freely in Markdown here. Tables, lists, quotes all work.
```

### To add a SUB-SECTOR

Create `src/content/subsectors/my-segment.md`. The `sector:` field must match the parent sector's filename slug:

```markdown
---
title: "My Segment"
sector: "my-sector"       # ← must match sectors/my-sector.md
summary: "One-line summary."
economicCharacter: "Margins / moat / dynamics (optional, shows in a callout)."
order: 1
updated: 2026-06-01
tags: [segment]
---

Body in Markdown.
```

### To add a COMPANY

Create `src/content/companies/my-company.md`:

```markdown
---
title: "My Company"
sector: "my-sector"            # ← required, matches a sector slug
subsector: "my-segment"        # ← optional, matches a subsector slug
ticker: "NYSE: XYZ"            # optional
stage: "public"                # public | private | seed | venture | growth | late-stage ...
valuation: "~$2B"              # optional, free text
revenue: "$300M TTM"           # optional
multiple: "8x fwd rev"         # optional
conviction: "high"             # high | medium | watch | avoid (optional, shows as a badge)
thesisFit: "One line: which thesis this expresses (optional, shows in a callout)."
summary: "One sentence for cards."
updated: 2026-06-01
tags: [tag1, tag2]
sources:
  - label: "Source name"
    url: "https://example.com/article"
---

## Why it expresses the thesis
Body in Markdown — the deep-dive goes here.

## What to diligence
- Bullet one
- Bullet two
```

That's it. Commit, push, and the company appears on the home count, its sector page, its sub-sector page, and the companies table — all automatically.

### Reusable visual blocks inside any body

Because content is MDX-capable, you can drop these HTML snippets straight into a Markdown body:

```html
<!-- stat row -->
<div class="stat-row">
  <div class="stat"><div class="big">$14.2B</div><div class="cap">Caption</div></div>
</div>

<!-- insight callout -->
<div class="callout"><div class="h">Core Thesis</div><p>...</p></div>

<!-- risk callout -->
<div class="callout risk"><div class="h">Risk name</div><p>...</p></div>
```

Standard Markdown tables, `> blockquotes` (rendered as teal insight bars), lists, and headings all style automatically.

---

## Project structure

```
src/
├── content.config.ts      ← the schema (what fields each type allows)
├── content/               ← YOUR CONTENT (the only folder you edit often)
├── layouts/Base.astro     ← header, footer, global wrapper
├── pages/                 ← routes (auto-generate from content; rarely touched)
└── styles/global.css      ← design system / all styling
```

To restyle the whole site, edit `src/styles/global.css` (colors are CSS variables at the top).

---

*Independent analysis, not investment advice.*
