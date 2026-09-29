import { ImageResponse } from 'next/og';
import { notFound } from 'next/navigation';
import { source } from '@/lib/source';
import { getPageImageUrl } from '@/lib/shared';
import { describe, pipelineFor } from '@/lib/seo';
import { groups } from '@/lib/catalog';

export const revalidate = false;

// Social share image: the site's minimal look — white, ink, one emerald label.
export async function GET(_req: Request, { params }: { params: Promise<{ slug: string[] }> }) {
  const { slug } = await params;
  const page = source.getPage(slug.slice(0, -1));
  if (!page) notFound();

  const pipeline = pipelineFor(page);
  const section = pipeline
    ? groups.find((g) => g.id === pipeline.group)!.title
    : page.slugs[0]?.replace(/[_-]/g, ' ') ?? 'docs';
  const description = describe(page);

  return new ImageResponse(
    (
      <div
        style={{
          width: '100%',
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
          padding: '72px 80px',
          background: '#ffffff',
          backgroundImage:
            'linear-gradient(#f0f0f0 1px, transparent 1px), linear-gradient(90deg, #f0f0f0 1px, transparent 1px)',
          backgroundSize: '36px 36px',
          color: '#171717',
          fontFamily: 'sans-serif',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <div style={{ fontSize: 26, color: '#047857', letterSpacing: 1, textTransform: 'lowercase' }}>
            {section}
          </div>
          <div
            style={{
              marginTop: 24,
              fontSize: page.data.title.length > 40 ? 60 : 76,
              fontWeight: 700,
              letterSpacing: -2,
              lineHeight: 1.05,
            }}
          >
            {page.data.title}
          </div>
          <div style={{ marginTop: 28, fontSize: 30, lineHeight: 1.4, color: '#6b6b6b', maxWidth: 980 }}>
            {description.length > 150 ? `${description.slice(0, 147)}…` : description}
          </div>
        </div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            borderTop: '1px solid #ebebeb',
            paddingTop: 28,
            fontSize: 26,
          }}
        >
          <div style={{ fontWeight: 700, letterSpacing: -0.5 }}>Repo2RLEnv</div>
          <div style={{ color: '#6b6b6b' }}>huggingface.github.io/Repo2RLEnv</div>
        </div>
      </div>
    ),
    { width: 1200, height: 630 },
  );
}

export function generateStaticParams() {
  return source.getPages().map((page) => ({
    slug: getPageImageUrl(page).segments,
  }));
}
