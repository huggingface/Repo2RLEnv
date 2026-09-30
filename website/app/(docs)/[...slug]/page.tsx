import { TutorialByline } from '@/components/tutorials';
import { readingMinutes } from '@/lib/tutorials';
import { source } from '@/lib/source';
import {
  DocsBody,
  DocsDescription,
  DocsPage,
  DocsTitle,
  EditOnGitHub,
  MarkdownCopyButton,
  ViewOptionsPopover,
} from 'fumadocs-ui/layouts/docs/page';
import { notFound } from 'next/navigation';
import { getMDXComponents } from '@/components/mdx';
import { ResultsVisuals } from '@/components/results-visuals';
import { Film } from '@/components/film';
import { PipelineHeader } from '@/components/pipeline-header';
import {
  absoluteUrl,
  pageUrl,
  articleJsonLd,
  breadcrumbJsonLd,
  describe,
  displayDescription,
  jsonLd,
  pipelineFor,
} from '@/lib/seo';
import type { Metadata } from 'next';
import { createDocsCard, createDocsLink } from '@/components/docs-link';
import { getPageImageUrl, getPageMarkdownUrl, githubEditUrl } from '@/lib/shared';

type Props = { params: Promise<{ slug: string[] }> };

export default async function Page(props: Props) {
  const { slug } = await props.params;
  const page = source.getPage(slug);
  if (!page) notFound();

  const MDX = page.data.body;
  const markdownUrl = getPageMarkdownUrl(page).url;
  const pipeline = pipelineFor(page);
  const description = displayDescription(page);

  return (
    <DocsPage toc={page.data.toc} full={page.data.full}>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: jsonLd([articleJsonLd(page), breadcrumbJsonLd(page)]) }}
      />
      <DocsTitle>{page.data.title}</DocsTitle>
      {description ? <DocsDescription className="mb-0">{description}</DocsDescription> : null}
      {page.data.tutorial ? (
        <TutorialByline
          tutorial={page.data.tutorial}
          minutes={readingMinutes(
            page.data.structuredData.contents.map((part) => part.content).join(' '),
          )}
        />
      ) : null}
      {pipeline ? <PipelineHeader pipeline={pipeline} /> : null}
      <div className="docs-page-actions flex flex-wrap items-center gap-2 border-b pb-6">
        <MarkdownCopyButton markdownUrl={markdownUrl} />
        <ViewOptionsPopover markdownUrl={markdownUrl} githubUrl={githubEditUrl(page.path)} />
        <EditOnGitHub href={githubEditUrl(page.path)} className="ms-auto" />
      </div>
      {page.data.film ? <Film id={page.data.film} /> : null}
      <DocsBody>
        {page.data.resultsVisual ? <ResultsVisuals kind={page.data.resultsVisual} /> : null}
        <MDX
          components={getMDXComponents({
            // Resolve relative .md links (the MkDocs convention) to page URLs.
            a: createDocsLink(page),
            Card: createDocsCard(page),
          })}
        />
      </DocsBody>
    </DocsPage>
  );
}

export async function generateStaticParams() {
  return source.generateParams().filter((params) => params.slug.length > 0);
}

export async function generateMetadata(props: Props): Promise<Metadata> {
  const { slug } = await props.params;
  const page = source.getPage(slug);
  if (!page) notFound();

  const description = describe(page);
  const image = absoluteUrl(getPageImageUrl(page).url);
  // Recipe pages are titled by their id; keep the method's name in search results.
  const recipe = pipelineFor(page);
  const title = recipe?.method ? `${page.data.title} (${recipe.method})` : page.data.title;
  return {
    title,
    description,
    ...(page.data.tutorial
      ? { authors: [{ name: page.data.tutorial.author.name, url: page.data.tutorial.author.url }] }
      : {}),
    alternates: {
      canonical: pageUrl(page.url),
      ...(page.slugs[0] === 'tutorials'
        ? {
            types: {
              'application/rss+xml': [
                { url: absoluteUrl('/tutorials/feed.xml'), title: 'Repo2RLEnv Tutorials' },
              ],
            },
          }
        : {}),
    },
    openGraph: {
      type: 'article',
      siteName: 'Repo2RLEnv',
      url: pageUrl(page.url),
      title,
      description,
      images: image,
      ...(page.data.tutorial
        ? {
            publishedTime: `${page.data.tutorial.published}T00:00:00Z`,
            modifiedTime: `${page.data.tutorial.updated ?? page.data.tutorial.published}T00:00:00Z`,
            authors: [page.data.tutorial.author.url],
          }
        : {}),
    },
    twitter: { card: 'summary_large_image', title, description, images: image },
  };
}
