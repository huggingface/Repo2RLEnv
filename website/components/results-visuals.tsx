import measurements from '../../docs/data/pipelines.json';
import { basePath } from '@/lib/shared';
import { BarChart, RecipeComparison, type RecipeMeasurement } from './result-charts';

const guideUrl = (guide: string) => `${basePath}/pipelines/${guide.replace(/\.md$/, '')}/`;

function Stat({ value, label, detail }: { value: string; label: string; detail: string }) {
  return (
    <div className="result-stat">
      <strong>{value}</strong>
      <span>{label}</span>
      <p>{detail}</p>
    </div>
  );
}

export function ResultsVisuals({ kind }: { kind: 'economics' | 'native' }) {
  if (kind === 'native') return <NativeResults />;
  const recipes: RecipeMeasurement[] = measurements.pipelines
    .filter((r) => r.recipe !== 'tasksmith')
    .map((r) => ({
      name: r.recipe,
      href: guideUrl(r.guide),
      exports: r.sample_exports,
      api: Number(r.model_usd),
      compute: r.compute_usd === undefined ? null : Number(r.compute_usd),
      candidates: r.attempted_candidates ?? null,
    }));
  const shared = measurements.shared_compute;
  return (
    <div className="results-visuals not-prose">
      <div className="result-stats">
        <Stat
          value={recipes.reduce((sum, r) => sum + r.exports, 0).toLocaleString('en-US')}
          label="new recipe exports"
          detail="14 recipes · generation sample"
        />
        <Stat
          value={`$${(Number(shared.compute_usd) + recipes.reduce((sum, r) => sum + r.api + (r.compute ?? 0), 0)).toFixed(2)}`}
          label="generation accounted"
          detail="API + compute · excludes held charges"
        />
        <Stat
          value={`${recipes.filter((r) => r.candidates !== null).length} / ${recipes.length}`}
          label="recipes with measured yield"
          detail="A missing denominator stays unknown"
        />
      </div>
      <RecipeComparison
        recipes={recipes}
        shared={{
          name: 'Repository pool · 6 recipes',
          href: '#research-recipes-and-tasksmith-generation',
          exports: shared.sample_exports,
          api: Number(shared.model_usd),
          compute: Number(shared.compute_usd),
          candidates: null,
        }}
      />
      <div className="result-reading-guide">
        <strong>Keep the denominator in view.</strong> These charts cover generation only. Tasksmith
        acceptance and the CodeMidas / FrontierSmith campaigns include different review and rollout
        work; their separate cost tables follow below.{' '}
        <a href={guideUrl('experiment_accounting.md')}>Explore models, tokens and stage costs →</a>
      </div>
    </div>
  );
}

function NativeResults() {
  const history = measurements.native_history;
  const inventory = history.pipelines;
  const gate = history.pr_runtime_validation;
  const generation = history.generation_reports.find(
    (r) => r.source === 'cinst-100-gen.log',
  )!.reports;
  const exports = generation.reduce((sum, row) => sum + row.emitted, 0);
  const candidates = generation.reduce((sum, row) => sum + row.candidates, 0);
  return (
    <div className="results-visuals not-prose">
      <div className="result-stats">
        <Stat
          value={inventory.reduce((sum, r) => sum + r.tasks, 0).toLocaleString('en-US')}
          label="retained task entries"
          detail="Six inventories · not cross-pipeline deduplicated"
        />
        <Stat
          value={`${gate.eval_grade} / 100`}
          label="PR runtime regression gate"
          detail="Historical oracle criteria · not independent quality"
        />
        <Stat
          value={`${((exports / candidates) * 100).toFixed(1)}%`}
          label="code-instruct export yield"
          detail={`${exports} exports / ${candidates} candidates`}
        />
      </div>
      <section className="result-panel">
        <p className="eyebrow">Historical inventory · May–July 2026</p>
        <h2>How much data do we have?</h2>
        <p className="result-note">
          Bar length shows retained entries only. Evidence coverage differs by pipeline; a larger
          dataset does not imply more verified tasks.
        </p>
        <BarChart
          label="Native pipeline inventory"
          rows={inventory.map((r) => ({
            label: r.pipeline,
            href: `#${r.pipeline.replaceAll('_', '-')}`,
            value: r.tasks,
            display: `${r.tasks} tasks`,
          }))}
        />
      </section>
      <section className="result-panel">
        <p className="eyebrow">PR runtime · one 100-task cohort</p>
        <h2>What survives a stronger oracle check?</h2>
        <p className="result-note">
          Each row adds a requirement to the row above. These are nested counts, so they must not be
          added together.
        </p>
        <BarChart
          label="Nested PR runtime oracle checks"
          max={100}
          rows={[
            {
              label: 'Tracked tests pass',
              value: gate.resolved,
              display: `${gate.resolved} / 100`,
              detail: 'Gold patch scores 1.0 on the recorded verifier.',
            },
            {
              label: '+ clean test command',
              value: gate.command_resolved,
              display: `${gate.command_resolved} / 100`,
              detail: 'Also exits successfully, with no untracked failures.',
            },
            {
              label: '+ regression guard',
              value: gate.eval_grade,
              display: `${gate.eval_grade} / 100`,
              detail: 'Also includes a non-empty regression-test set.',
            },
          ]}
        />
        <p className="result-footnote">
          Commit runtime’s earlier 52-task oracle cohort is separate from its later 100-task
          inventory. Other inventories do not have equivalent full-cohort oracle receipts; unknown
          coverage is not a failed gate.
        </p>
      </section>
      <section className="result-panel">
        <p className="eyebrow">Code instruct · same generation campaign</p>
        <h2>Yield varies across source repositories</h2>
        <p className="result-note">
          Every repository produced 20 exports, but needed a different number of candidates. Retries
          stay within their candidate.
        </p>
        <BarChart
          label="Code instruct candidate yield by repository"
          max={100}
          rows={generation.map((r) => ({
            label: r.run.split(' -> ')[0],
            value: (r.emitted / r.candidates) * 100,
            display: `${((r.emitted / r.candidates) * 100).toFixed(1)}%`,
            detail: `${r.emitted} exports / ${r.candidates} candidates`,
          }))}
        />
        <p className="result-footnote">
          Equivalence tests have an incomplete attempt denominator, so no comparable yield is shown.
          Solver pilots use small, different samples and are documented separately below.
        </p>
      </section>
    </div>
  );
}
