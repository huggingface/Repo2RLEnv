import type { MetadataRoute } from 'next';
import { source } from '@/lib/source';
import { absoluteUrl, pageUrl } from '@/lib/seo';

export const dynamic = 'force-static';

// The generated prompt reference is verbatim source; list it, but below the guides.
export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: absoluteUrl('/'), priority: 1 },
    ...source.getPages().map((page) => ({
      url: pageUrl(page.url),
      ...(page.data.tutorial
        ? { lastModified: page.data.tutorial.updated ?? page.data.tutorial.published }
        : {}),
      priority:
        page.path.startsWith('pipelines/prompts/') || page.path.startsWith('rfcs/') ? 0.3 : 0.7,
    })),
  ];
}
