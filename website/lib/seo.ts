import { byPage, groups } from './catalog';
import type { source } from './source';

type Page = NonNullable<ReturnType<typeof source.getPage>>;

/** Where the site is served; canonical URLs, the sitemap and JSON-LD use it. */
export const siteUrl = (
  process.env.NEXT_PUBLIC_SITE_URL ?? 'https://huggingface.github.io/Repo2RLEnv'
).replace(/\/$/, '');

export const absoluteUrl = (path: string) => `${siteUrl}${path.startsWith('/') ? '' : '/'}${path}`;

/** A page's canonical URL: the site serves pages with a trailing slash. */
export const pageUrl = (url: string) => {
  const [path, hash] = url.split('#');
  const slashed = path.endsWith('/') ? path : `${path}/`;
  return absoluteUrl(slashed) + (hash ? `#${hash}` : '');
};

export const siteDescription =
  'Turn repositories, pull requests and task seeds into verifiable RL environments for coding agents, in the Harbor task format.';

/** The catalogue entry for a pipeline page, if it is one. */
export function pipelineFor(page: Page) {
  return page.slugs.length === 2 && page.slugs[0] === 'pipelines'
    ? byPage.get(page.slugs[1])
    : undefined;
}

function clip(text: string, max = 158) {
  const clean = text.replace(/\s+/g, ' ').trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  return `${cut.slice(0, cut.lastIndexOf(' '))}…`;
}

/**
 * The page's description: front matter first, then the pipeline catalogue's
 * one-line task shape, then the page's lead paragraph.
 */
export function describe(page: Page): string {
  if (page.data.description) return page.data.description;
  const p = pipelineFor(page);
  if (p) return `${p.method ? `${p.method} recipe: ` : ''}${p.shape}.`;
  const lead = page.data.structuredData.contents.find((c) => !c.heading && c.content.length > 40);
  return lead ? clip(lead.content) : siteDescription;
}

/** A shown description for pages without one: pipeline pages use the catalogue. */
export function displayDescription(page: Page): string | undefined {
  if (page.data.description) return page.data.description;
  const p = pipelineFor(page);
  return p ? `${p.shape}.` : undefined;
}

export function breadcrumbJsonLd(page: Page) {
  const items = [{ name: 'Docs', url: absoluteUrl('/introduction/') }];
  const p = pipelineFor(page);
  if (page.slugs[0] === 'pipelines' && page.slugs.length > 1) {
    items.push({ name: 'Pipelines', url: absoluteUrl('/pipelines/') });
    if (p) {
      const g = groups.find((x) => x.id === p.group)!;
      items.push({ name: g.title, url: absoluteUrl(`/pipelines/#${g.id}`) });
    }
  }
  items.push({ name: page.data.title, url: pageUrl(page.url) });
  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: items.map((item, i) => ({
      '@type': 'ListItem',
      position: i + 1,
      name: item.name,
      item: item.url,
    })),
  };
}

export function articleJsonLd(page: Page) {
  return {
    '@context': 'https://schema.org',
    '@type': 'TechArticle',
    headline: page.data.title,
    description: describe(page),
    url: pageUrl(page.url),
    inLanguage: 'en',
    isPartOf: { '@type': 'WebSite', name: 'Repo2RLEnv', url: absoluteUrl('/') },
    publisher: { '@type': 'Organization', name: 'Hugging Face', url: 'https://huggingface.co' },
  };
}

export function softwareJsonLd(version: string) {
  return {
    '@context': 'https://schema.org',
    '@type': 'SoftwareApplication',
    name: 'Repo2RLEnv',
    description: siteDescription,
    url: absoluteUrl('/'),
    applicationCategory: 'DeveloperApplication',
    operatingSystem: 'Linux, macOS, Windows',
    softwareVersion: version,
    license: 'https://www.apache.org/licenses/LICENSE-2.0',
    offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
    downloadUrl: 'https://pypi.org/project/repo2rlenv/',
    codeRepository: 'https://github.com/huggingface/Repo2RLEnv',
    programmingLanguage: 'Python',
    author: { '@type': 'Organization', name: 'Hugging Face', url: 'https://huggingface.co' },
  };
}

/** Render JSON-LD safely inside a <script> tag. */
export const jsonLd = (data: unknown) =>
  JSON.stringify(data).replace(/</g, '\\u003c');
