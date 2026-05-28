import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';
import sitemap from '@astrojs/sitemap';

// Update `site` to your real Vercel URL once deployed (used for sitemap + canonical URLs).
export default defineConfig({
  site: 'https://your-research-vault.vercel.app',
  integrations: [mdx(), sitemap()],
  markdown: {
    shikiConfig: { theme: 'github-light', wrap: true },
  },
});
