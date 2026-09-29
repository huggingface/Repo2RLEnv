import Link from 'next/link';
import { ArrowRight, Play } from 'lucide-react';
import { groups, inGroup, pipelines, type Pipeline } from '@/lib/catalog';
import { StatusBadge } from '@/components/pipeline-header';

const tasksmith = pipelines.find((p) => p.id === 'tasksmith')!;

const count = (n: number) => `${n} pipeline${n === 1 ? '' : 's'}`;

export const tasksmithStages = [
  { title: 'Investigate', body: 'Reads the PR, its diff and the repository around it.' },
  { title: 'Bootstrap', body: 'Builds the repository’s environment on a remote worker.' },
  { title: 'Design', body: 'Writes the instruction and a private verifier.' },
  { title: 'Construct', body: 'Assembles the Harbor task; the merged code is the oracle.' },
  { title: 'Review and repair', body: 'Runs the controls and a blind solver, then fixes what fails.' },
];

/** The flagship: Tasksmith, shown above the catalogue and on the landing page. */
export function TasksmithFeature() {
  return (
    <div className="not-prose relative overflow-hidden rounded-2xl border bg-fd-card">
      <div className="hero-grid pointer-events-none absolute inset-0 opacity-60" />
      <div className="relative p-6 sm:p-8">
        <div className="flex flex-wrap items-center gap-2">
          <span className="eyebrow">flagship · agentic</span>
          <StatusBadge status={tasksmith.status} />
        </div>
        <h3 className="mt-3 text-2xl font-semibold tracking-tight">Tasksmith</h3>
        <p className="mt-2 max-w-2xl text-fd-muted-foreground">
          Point an agent at a merged pull request and get a verified Harbor environment back.
          Tasksmith investigates the repository, builds its environment, designs the task, then
          reviews and repairs its own work until the controls pass.
        </p>
        <ol className="mt-6 grid gap-px overflow-hidden rounded-xl border bg-fd-border sm:grid-cols-5">
          {tasksmithStages.map((s, i) => (
            <li key={s.title} className="bg-fd-background p-4">
              <span className="font-mono text-xs text-fd-muted-foreground">
                {String(i + 1).padStart(2, '0')}
              </span>
              <p className="mt-2 text-sm font-medium">{s.title}</p>
              <p className="mt-1 text-xs leading-relaxed text-fd-muted-foreground">{s.body}</p>
            </li>
          ))}
        </ol>
        <div className="mt-6 flex flex-wrap items-center justify-between gap-4">
          <a
            href={tasksmith.dataset!.href}
            className="font-mono text-xs text-fd-muted-foreground transition-colors hover:text-fd-foreground"
          >
            HuggingEnvs/HF_ML_Tasksmith · {tasksmith.dataset!.label}
          </a>
          <div className="flex gap-2">
            <Link
              href="/pipelines/tasksmith/#run-one-pr"
              className="inline-flex items-center gap-1.5 rounded-lg border bg-fd-background px-3.5 py-2 text-sm font-medium transition-colors hover:bg-fd-accent"
            >
              Run one PR
            </Link>
            <Link
              href="/pipelines/tasksmith/"
              className="inline-flex items-center gap-1.5 rounded-lg bg-fd-foreground px-3.5 py-2 text-sm font-medium text-fd-background transition-opacity hover:opacity-90"
            >
              How Tasksmith works <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function PipelineCard({ p }: { p: Pipeline }) {
  return (
    <Link
      href={`/pipelines/${p.page}/`}
      className="group flex flex-col rounded-xl border bg-fd-card p-4 transition-colors hover:border-fd-foreground/20"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="font-mono text-sm font-medium">{p.id}</span>
          <span className="mt-0.5 block text-xs text-fd-muted-foreground">
            {p.kind === 'recipe' ? `${p.method} · research recipe` : p.kind === 'agentic' ? 'Agentic' : 'Native'}
          </span>
        </div>
        {p.film ? (
          <span
            title="Has an explainer film"
            className="grid size-6 shrink-0 place-items-center rounded-md border text-fd-muted-foreground"
          >
            <Play className="size-3" aria-hidden />
          </span>
        ) : null}
      </div>
      <p className="mt-3 text-sm leading-relaxed text-fd-muted-foreground">{p.shape}</p>
      <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pt-4 font-mono text-[11px] text-fd-muted-foreground">
        <span className="text-fd-foreground">{p.reward}</span>
        <span>{p.runs === 'local' ? 'local' : 'remote'}</span>
        <span>{p.status}</span>
      </div>
    </Link>
  );
}

/** Every pipeline, grouped by the kind of task it produces. */
export function PipelineCatalog() {
  return (
    <div className="not-prose flex flex-col gap-14">
      <TasksmithFeature />
      {groups.map((g) => {
        const items = inGroup(g.id).filter((p) => p.id !== 'tasksmith');
        return (
          <section key={g.id} id={g.id} className="scroll-mt-24">
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-3">
              <h2 className="text-lg font-semibold tracking-tight">{g.title}</h2>
              <span className="font-mono text-xs text-fd-muted-foreground">
                {count(items.length + (g.id === 'repair' ? 1 : 0))}
              </span>
            </div>
            <p className="mt-3 text-sm text-fd-muted-foreground">
              {g.summary}
              {g.id === 'repair' ? ' Tasksmith, above, is the agentic route.' : ''}
            </p>
            <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {items.map((p) => (
                <PipelineCard key={p.id} p={p} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}
