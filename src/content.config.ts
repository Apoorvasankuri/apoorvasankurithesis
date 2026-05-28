import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

/* ------------------------------------------------------------------ *
 *  CONTENT MODEL
 *  Three collections, linked by slugs:
 *    sectors      → top-level themes (e.g. ai-healthcare)
 *    subsectors   → segments within a sector (e.g. clinical-documentation)
 *    companies    → individual deep-dives, tagged to a sector + subsector
 *
 *  To ADD content later you only ever create a new .md/.mdx file in the
 *  matching folder and fill in the frontmatter. Pages regenerate on build.
 * ------------------------------------------------------------------ */

const sectors = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/sectors' }),
  schema: z.object({
    title: z.string(),
    tagline: z.string().optional(),
    summary: z.string(),                       // one-paragraph thesis hook
    status: z.enum(['active', 'draft', 'watching']).default('active'),
    order: z.number().default(99),             // controls home-page ordering
    updated: z.coerce.date(),
    convictionOneLiner: z.string().optional(), // the "argument in one breath"
    tags: z.array(z.string()).default([]),
  }),
});

const subsectors = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/subsectors' }),
  schema: z.object({
    title: z.string(),
    sector: z.string(),                        // slug of parent sector
    summary: z.string(),
    economicCharacter: z.string().optional(),  // margins / moat / dynamics
    order: z.number().default(99),
    updated: z.coerce.date(),
    tags: z.array(z.string()).default([]),
  }),
});

const companies = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/companies' }),
  schema: z.object({
    title: z.string(),
    sector: z.string(),                        // slug of parent sector
    subsector: z.string().optional(),          // slug of parent subsector
    ticker: z.string().optional(),
    stage: z.enum(['public', 'private', 'pre-seed', 'seed', 'venture', 'growth', 'late-stage']).optional(),
    valuation: z.string().optional(),          // free text, e.g. "~$5.3B"
    revenue: z.string().optional(),
    multiple: z.string().optional(),
    conviction: z.enum(['high', 'medium', 'watch', 'avoid']).optional(),
    thesisFit: z.string().optional(),          // one line: which thesis it expresses
    summary: z.string(),
    updated: z.coerce.date(),
    tags: z.array(z.string()).default([]),
    sources: z.array(z.object({
      label: z.string(),
      url: z.string().url(),
    })).default([]),
  }),
});

export const collections = { sectors, subsectors, companies };
