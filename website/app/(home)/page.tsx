import Link from 'next/link';
import { ArrowRight } from 'lucide-react';
import { InstallCommand } from '@/components/home/install-command';
import { Film } from '@/components/film';
import { githubUrl } from '@/lib/shared';
import { groups, inGroup, pipelines } from '@/lib/catalog';
import { TasksmithFeature } from '@/components/pipeline-catalog';
import { absoluteUrl, jsonLd, softwareJsonLd } from '@/lib/seo';
import { version } from '@/lib/version';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  alternates: { canonical: absoluteUrl('/') },
  openGraph: { url: absoluteUrl('/'), images: absoluteUrl('/og/introduction/image.png') },
  twitter: { images: absoluteUrl('/og/introduction/image.png') },
};

const collection =
  'https://huggingface.co/collections/FineEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d';

const steps = [
  {
    n: '01',
    title: 'Generate',
    body: 'Point a pipeline at a repository, a merged PR or a task seed.',
    cmd: 'repo2rlenv generate --repo org/service --pipeline pr_runtime',
  },
  {
    n: '02',
    title: 'Verify',
    body: 'Check every task statically, then prove it: the oracle scores 1, a no-op scores 0.',
    cmd: 'harbor run -p ./tasks -a oracle',
  },
  {
    n: '03',
    title: 'Train and evaluate',
    body: 'Publish to the Hugging Face Hub and run any Harbor agent against it.',
    cmd: 'repo2rlenv push ./tasks org/my-envs',
  },
];

const taskTree = [
  ['task.toml', 'metadata, resources, lineage'],
  ['instruction.md', 'what the agent sees'],
  ['environment/', 'Dockerfile for the starting state'],
  ['tests/', 'private verifier → reward'],
  ['solution/', 'reference solution (the oracle)'],
];

export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: jsonLd(softwareJsonLd(version)) }}
      />
      {/* ---------- hero ---------- */}
      <section className="relative overflow-hidden border-b">
        <div className="hero-grid pointer-events-none absolute inset-0" />
        <div className="relative mx-auto flex max-w-5xl flex-col items-center px-6 pt-20 pb-14 text-center sm:pt-28">
          <Link
            href="/release_notes/HISTORY/"
            className="mb-6 inline-flex items-center gap-2 rounded-full border bg-fd-background px-3 py-1 text-xs text-fd-muted-foreground transition-colors hover:text-fd-foreground"
          >
            <span className="size-1.5 rounded-full bg-brand" />
            v{version}: FrontierSmith optimization synthesis
            <ArrowRight className="size-3" aria-hidden />
          </Link>
          <h1 className="max-w-3xl text-4xl font-semibold tracking-[-0.035em] text-balance sm:text-6xl">
            Turn any repository into <span className="text-brand">verifiable RL environments.</span>
          </h1>
          <p className="mt-6 max-w-2xl text-base leading-relaxed text-fd-muted-foreground sm:text-lg">
            Repo2RLEnv generates coding, terminal and reasoning tasks in the Harbor format. Each one has an
            instruction, a starting environment, a private verifier and a reference solution, ready
            to train and evaluate agents.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
            <Link
              href="/quickstart/"
              className="inline-flex items-center gap-2 rounded-lg bg-fd-foreground px-5 py-2.5 text-sm font-medium text-fd-background transition-opacity hover:opacity-90"
            >
              Get started <ArrowRight className="size-4" aria-hidden />
            </Link>
            <a
              href={githubUrl}
              className="inline-flex items-center gap-2 rounded-lg border bg-fd-background px-5 py-2.5 text-sm font-medium transition-colors hover:bg-fd-accent"
            >
              View on GitHub
            </a>
          </div>
          <div className="mt-6">
            <InstallCommand command="pip install repo2rlenv" />
          </div>
        </div>
        <div className="relative mx-auto max-w-5xl px-6 pb-16">
          <Film id="launch" />
        </div>
      </section>

      {/* ---------- tasksmith, the flagship ---------- */}
      <section className="border-b">
        <div className="mx-auto max-w-5xl px-6 py-20">
          <p className="eyebrow">tasksmith</p>
          <h2 className="mt-2 max-w-2xl text-2xl font-semibold tracking-tight sm:text-3xl">
            An agent that turns pull requests into environments.
          </h2>
          <p className="mt-3 max-w-2xl text-fd-muted-foreground">
            Mining pipelines keep only the pull requests that pass their filters. Tasksmith adapts
            to each one instead: it reads the change, builds the repository, writes the task and its
            private verifier, and repairs its own work until the controls pass.
          </p>
          <div className="mt-10">
            <TasksmithFeature />
          </div>
        </div>
      </section>

      {/* ---------- five kinds of tasks ---------- */}
      <section className="border-b">
        <div className="mx-auto max-w-5xl px-6 py-20">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="eyebrow">pipelines</p>
              <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
                Five kinds of tasks, from the material you already have.
              </h2>
            </div>
            <Link
              href="/pipelines/"
              className="inline-flex items-center gap-1 text-sm text-fd-muted-foreground transition-colors hover:text-fd-foreground"
            >
              All {pipelines.length} pipelines <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </div>
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {groups.map((g) => (
              <div key={g.id} className="flex flex-col rounded-xl border bg-fd-card p-5">
                <Link href={`/pipelines/#${g.id}`} className="font-medium hover:underline">
                  {g.title}
                </Link>
                <p className="mt-2 text-sm leading-relaxed text-fd-muted-foreground">{g.summary}</p>
                <div className="mt-auto flex flex-wrap gap-1.5 pt-5">
                  {inGroup(g.id).map((p) => (
                    <Link
                      key={p.id}
                      href={`/pipelines/${p.page}/`}
                      className={
                        p.id === 'tasksmith'
                          ? 'rounded-md bg-fd-foreground px-2 py-0.5 font-mono text-[11px] text-fd-background'
                          : 'rounded-md border px-2 py-0.5 font-mono text-[11px] text-fd-muted-foreground transition-colors hover:border-fd-foreground/20 hover:text-fd-foreground'
                      }
                    >
                      {p.id}
                    </Link>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ---------- how it works ---------- */}
      <section className="border-b">
        <div className="mx-auto max-w-5xl px-6 py-20">
          <p className="eyebrow">how it works</p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
            From source to a scored environment in three commands.
          </h2>
          <div className="mt-10 grid gap-px overflow-hidden rounded-xl border bg-fd-border sm:grid-cols-3">
            {steps.map((s) => (
              <div key={s.n} className="flex flex-col bg-fd-background p-6">
                <span className="font-mono text-xs text-fd-muted-foreground">{s.n}</span>
                <h3 className="mt-3 font-medium">{s.title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-fd-muted-foreground">{s.body}</p>
                <code className="mt-5 block truncate rounded-md bg-fd-muted px-3 py-2 font-mono text-xs text-fd-foreground">
                  {s.cmd}
                </code>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ---------- the output ---------- */}
      <section className="border-b">
        <div className="mx-auto grid max-w-5xl items-center gap-12 px-6 py-20 lg:grid-cols-2">
          <div>
            <p className="eyebrow">the output</p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              A standard Harbor task, not a bespoke format.
            </h2>
            <p className="mt-4 leading-relaxed text-fd-muted-foreground">
              Every pipeline emits the same directory. Harbor runs it in a sandbox with any of its
              agent harnesses (Claude Code, Codex, OpenHands and more), and the verifier writes the
              reward.
            </p>
            <Link
              href="/reference/SPEC/"
              className="mt-6 inline-flex items-center gap-1 text-sm font-medium text-brand"
            >
              Read the task spec <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </div>
          <div className="rounded-xl border bg-fd-card p-5 font-mono text-sm">
            <div className="text-fd-muted-foreground">org__service-412/</div>
            <ul className="mt-2 space-y-2">
              {taskTree.map(([name, note]) => (
                <li key={name} className="flex items-baseline justify-between gap-4 pl-4">
                  <span>{name}</span>
                  <span className="truncate text-right text-xs text-fd-muted-foreground">{note}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* ---------- datasets + CTA ---------- */}
      <section>
        <div className="mx-auto flex max-w-5xl flex-col items-center px-6 py-20 text-center">
          <h2 className="max-w-2xl text-2xl font-semibold tracking-tight text-balance sm:text-3xl">
            Generate your first environment in minutes.
          </h2>
          <p className="mt-3 max-w-xl text-fd-muted-foreground">
            Or start from ours: 21 published datasets on the Hugging Face Hub, each with its
            generation evidence and evaluation labels.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <Link
              href="/quickstart/"
              className="inline-flex items-center gap-2 rounded-lg bg-fd-foreground px-5 py-2.5 text-sm font-medium text-fd-background transition-opacity hover:opacity-90"
            >
              Start the quickstart <ArrowRight className="size-4" aria-hidden />
            </Link>
            <a
              href={collection}
              className="inline-flex items-center gap-2 rounded-lg border px-5 py-2.5 text-sm font-medium transition-colors hover:bg-fd-accent"
            >
              Browse the datasets
            </a>
          </div>
        </div>
      </section>

      <footer className="border-t">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-4 px-6 py-8 text-xs text-fd-muted-foreground">
          <span>© 2026 Hugging Face · Apache-2.0</span>
          <div className="flex gap-5">
            <a href={githubUrl} className="hover:text-fd-foreground">GitHub</a>
            <a href="https://pypi.org/project/repo2rlenv/" className="hover:text-fd-foreground">PyPI</a>
            <a href={collection} className="hover:text-fd-foreground">Datasets</a>
          </div>
        </div>
      </footer>
    </main>
  );
}
