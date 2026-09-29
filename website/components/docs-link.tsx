import type { ComponentProps, FC } from 'react';
import defaultMdxComponents from 'fumadocs-ui/mdx';
import { Card } from 'fumadocs-ui/components/card';
import { posix } from 'node:path';
import { source } from '@/lib/source';
import { githubUrl, gitConfig } from '@/lib/shared';

type Page = NonNullable<ReturnType<typeof source.getPage>>;

const external = /^([a-z][a-z0-9+.-]*:|\/\/|#|\/)/i;

/**
 * Resolve an href the way MkDocs did, so pages keep working on GitHub too:
 * - `other.md` / `dir/other.mdx` are relative to the current page's file;
 * - relative links to non-page files (`../data/pipelines.json`) open on GitHub.
 */
export function resolveDocsHref(page: Page, href: string | undefined): string | undefined {
  if (!href || external.test(href)) return href;

  const dir = posix.dirname(page.path);
  const [path, hash] = href.split('#');
  const resolved = posix.normalize(posix.join(dir, path));

  if (/\.mdx?$/.test(path)) {
    const target = source.getPageByHref(`./${path}`, { dir });
    if (!target) return href; // left unresolved so the link check reports it
    const anchor = hash ?? target.hash;
    return target.page.url + (anchor ? `#${anchor}` : '');
  }

  if (/\.[a-z0-9]+$/i.test(path) && !resolved.startsWith('..')) {
    return `${githubUrl}/blob/${gitConfig.branch}/docs/${resolved}` + (hash ? `#${hash}` : '');
  }

  return source.resolveHref(href, page);
}

export function createDocsLink(page: Page): FC<ComponentProps<'a'>> {
  const Link = defaultMdxComponents.a;
  return function DocsLink({ href, ...props }) {
    return <Link href={resolveDocsHref(page, href)} {...props} />;
  };
}

export function createDocsCard(page: Page): FC<ComponentProps<typeof Card>> {
  return function DocsCard({ href, ...props }) {
    return <Card href={resolveDocsHref(page, href)} {...props} />;
  };
}
