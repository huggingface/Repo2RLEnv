import Link from 'next/link';
import Image from 'next/image';
import { ArrowRight } from 'lucide-react';
import { tutorialPages, dateLabel, readingMinutes } from '@/lib/tutorials';
import { basePath } from '@/lib/shared';

type Tutorial = {
  author: { name: string; url: string };
  published: string;
  updated?: string;
  testedVersion: string;
  thumbnail?: { src: string; alt: string; width: number; height: number };
};

export function TutorialThumbnail({ tutorial }: { tutorial: Tutorial }) {
  const thumbnail = tutorial.thumbnail;
  if (!thumbnail) return null;
  return (
    <Image
      src={`${basePath}${thumbnail.src}`}
      alt={thumbnail.alt}
      width={thumbnail.width}
      height={thumbnail.height}
      className="h-auto w-full rounded-xl border"
    />
  );
}

export function TutorialByline({ tutorial, minutes }: { tutorial: Tutorial; minutes: number }) {
  return (
    <div className="tutorial-byline not-prose">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-fd-muted-foreground">
        <a href={tutorial.author.url} className="font-medium text-fd-foreground hover:underline">
          {tutorial.author.name}
        </a>
        <time dateTime={tutorial.published}>{dateLabel(tutorial.published)}</time>
        <span>{minutes} min read</span>
        {tutorial.updated && tutorial.updated !== tutorial.published && (
          <span>
            Updated <time dateTime={tutorial.updated}>{dateLabel(tutorial.updated)}</time>
          </span>
        )}
      </div>
      <p className="mt-2 text-xs text-fd-muted-foreground">
        Commands and configuration checked against Repo2RLEnv {tutorial.testedVersion}. Cloud
        execution is billed separately.
      </p>
    </div>
  );
}

export function TutorialIndex() {
  return (
    <div className="not-prose my-8 grid gap-4">
      {tutorialPages().map((page) => {
        const meta = page.data.tutorial!;
        const text = page.data.structuredData.contents.map((part) => part.content).join(' ');
        return (
          <Link
            key={page.url}
            href={page.url}
            className="group rounded-xl border bg-fd-card p-5 transition-colors hover:border-fd-primary/50 sm:p-7"
          >
            {meta.thumbnail ? (
              <div className="mb-5">
                <TutorialThumbnail tutorial={meta} />
              </div>
            ) : null}
            <p className="eyebrow">Hands-on tutorial</p>
            <h2 className="mt-3 text-xl font-semibold tracking-tight">{page.data.title}</h2>
            <p className="mt-3 text-sm leading-relaxed text-fd-muted-foreground">
              {page.data.description}
            </p>
            <div className="mt-5 flex flex-wrap items-center justify-between gap-3 text-xs text-fd-muted-foreground">
              <span>
                {meta.author.name} · {dateLabel(meta.published)} · {readingMinutes(text)} min read
              </span>
              <span className="inline-flex items-center gap-1 font-medium text-brand">
                Follow the tutorial <ArrowRight className="size-4" aria-hidden="true" />
              </span>
            </div>
          </Link>
        );
      })}
    </div>
  );
}
