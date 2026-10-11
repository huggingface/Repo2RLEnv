import Link from 'next/link';
import { ArrowUpRight } from 'lucide-react';
import type { ReactNode } from 'react';
import { groups, kindLabel, type Pipeline } from '@/lib/catalog';
import { cn } from '@/lib/cn';

export function Badge({
  children,
  href,
  className,
}: {
  children: ReactNode;
  href?: string;
  className?: string;
}) {
  const classes = cn(
    'inline-flex items-center gap-1.5 rounded-md border bg-fd-card px-2 py-0.5 text-xs text-fd-muted-foreground',
    href && 'transition-colors hover:border-fd-foreground/20 hover:text-fd-foreground',
    className,
  );
  if (!href) return <span className={classes}>{children}</span>;
  if (href.startsWith('http'))
    return (
      <a href={href} className={classes}>
        {children}
        <ArrowUpRight className="size-3" aria-hidden />
      </a>
    );
  return (
    <Link href={href} className={classes}>
      {children}
    </Link>
  );
}

export function StatusBadge({ status }: { status: Pipeline['status'] }) {
  return (
    <Badge>
      <span
        className={cn(
          'size-1.5 rounded-full',
          status === 'stable' ? 'bg-brand' : 'bg-fd-muted-foreground/50',
        )}
      />
      {status === 'stable' ? 'Stable' : 'Experimental'}
    </Badge>
  );
}

/** The facts strip under a pipeline page's title. */
export function PipelineHeader({ pipeline: p }: { pipeline: Pipeline }) {
  const group = groups.find((g) => g.id === p.group)!;
  return (
    <div className="not-prose flex flex-wrap items-center gap-1.5">
      <StatusBadge status={p.status} />
      <Badge href={`/pipelines/#${p.group}`}>{group.title}</Badge>
      <Badge>{p.kind === 'recipe' && p.method ? `${p.method} recipe` : kindLabel[p.kind]}</Badge>
      <Badge>{p.runs === 'local' ? 'Runs locally' : 'Runs on Modal or Daytona'}</Badge>
      {p.dataset ? <Badge href={p.dataset.href}>Dataset · {p.dataset.label}</Badge> : null}
    </div>
  );
}
