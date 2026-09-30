// Check the deployed artifacts, including the GitHub Pages path prefix.
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const out = join(import.meta.dirname, '..', 'out');
const base = (
  process.env.NEXT_PUBLIC_SITE_URL ?? 'https://huggingface.github.io/Repo2RLEnv'
).replace(/\/$/, '');
const sitemap = readFileSync(join(out, 'sitemap.xml'), 'utf8');
const feed = readFileSync(join(out, 'tutorials/feed.xml'), 'utf8');
const index = readFileSync(join(out, 'tutorials/index.html'), 'utf8');
const search = readFileSync(join(out, 'api/search.json'), 'utf8');
const xml = (value) =>
  value.replace(
    /[<>&"']/g,
    (char) =>
      ({
        '<': '&lt;',
        '>': '&gt;',
        '&': '&amp;',
        '"': '&quot;',
        "'": '&apos;',
      })[char],
  );
let checked = 0;

for (const entry of readdirSync(join(out, 'tutorials'), {
  withFileTypes: true,
})) {
  if (!entry.isDirectory()) continue;
  const html = readFileSync(join(out, 'tutorials', entry.name, 'index.html'), 'utf8');
  const data = [...html.matchAll(/<script type="application\/ld\+json">(.*?)<\/script>/gs)].flatMap(
    (match) => JSON.parse(match[1]),
  );
  const article = data.find((item) => item['@type'] === 'Article');
  assert(article, `${entry.name}: missing Article structured data`);
  const url = `${base}/tutorials/${entry.name}/`;
  assert.equal(article.url, url, 'Article URL must match its deployed path');
  assert.equal(article.mainEntityOfPage['@id'], url);
  assert(html.includes(`<link rel="canonical" href="${url}"`), 'Missing canonical URL');
  assert(
    !/<meta[^>]+name="robots"[^>]+content="[^"]*noindex/.test(html),
    'Tutorial must be indexable',
  );
  assert(article.author?.name && article.author?.url, 'Author identity is required');
  assert(html.includes(article.author.name), 'Author must also be visible');
  assert(Number.isFinite(Date.parse(article.datePublished)), 'Invalid publication date');
  assert(
    Date.parse(article.dateModified) >= Date.parse(article.datePublished),
    'Update predates publication',
  );
  assert(
    new RegExp(`<time[^>]+datetime="${article.datePublished.slice(0, 10)}"`, 'i').test(html),
    'Publication date must be visible',
  );
  const imagePath = article.image.slice(base.length);
  assert(
    article.image.startsWith(`${base}/og/tutorials/`) ||
      article.image.startsWith(`${base}/images/tutorials/`),
    'Missing tutorial social image',
  );
  assert(existsSync(join(out, imagePath)), 'Social image file was not exported');
  assert(
    html.includes(`<meta property="og:image" content="${article.image}"`),
    'Social image disagrees',
  );
  if (imagePath.startsWith('/images/tutorials/')) {
    assert(html.includes(imagePath), 'Thumbnail absent from article');
    assert(index.includes(imagePath), 'Thumbnail absent from tutorial index');
  }
  assert(
    data.some(
      (item) =>
        item['@type'] === 'BreadcrumbList' &&
        item.itemListElement.some((crumb) => crumb.item === `${base}/tutorials/`),
    ),
    'Missing tutorial breadcrumb',
  );
  assert(sitemap.includes(`<loc>${xml(url)}</loc>`), 'Tutorial absent from sitemap');
  assert(feed.includes(`<guid isPermaLink="true">${xml(url)}</guid>`), 'Tutorial absent from feed');
  assert(feed.includes(`<title>${xml(article.headline)}</title>`), 'Feed title disagrees');
  assert(index.includes(article.headline), 'Tutorial absent from index');
  assert(search.includes(article.headline), 'Tutorial absent from search');
  assert(html.includes('application/rss+xml'), 'Missing feed discovery link');
  checked++;
}
assert(checked > 0, 'No tutorial articles were built');
assert.equal(
  (feed.match(/<item>/g) ?? []).length,
  checked,
  'Feed count disagrees with published tutorials',
);
console.log(
  `${checked} tutorial(s): canonical URLs, bylines, Article data, sitemap, search and RSS checked`,
);
