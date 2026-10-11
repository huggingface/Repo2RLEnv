import { tutorialPages } from '@/lib/tutorials';
import { absoluteUrl, pageUrl } from '@/lib/seo';

export const dynamic = 'force-static';

const xml = (value: string) =>
  value.replace(
    /[<>&"']/g,
    (char) =>
      ({
        '<': '&lt;',
        '>': '&gt;',
        '&': '&amp;',
        '"': '&quot;',
        "'": '&apos;',
      })[char]!,
  );

export function GET() {
  const items = tutorialPages()
    .map((page) => {
      const tutorial = page.data.tutorial!;
      return `<item>
<title>${xml(page.data.title)}</title>
<link>${xml(pageUrl(page.url))}</link>
<guid isPermaLink="true">${xml(pageUrl(page.url))}</guid>
<description>${xml(page.data.description ?? '')}</description>
<dc:creator>${xml(tutorial.author.name)}</dc:creator>
<pubDate>${new Date(`${tutorial.published}T00:00:00Z`).toUTCString()}</pubDate>
</item>`;
    })
    .join('\n');
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
<title>Repo2RLEnv Tutorials</title>
<link>${xml(absoluteUrl('/tutorials/'))}</link>
<description>Build reproducible coding-agent evaluations for your codebase.</description>
<language>en</language>
<atom:link href="${xml(absoluteUrl('/tutorials/feed.xml'))}" rel="self" type="application/rss+xml" />
${items}
</channel>
</rss>`,
    { headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' } },
  );
}
