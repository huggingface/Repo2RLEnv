'use client';

import { useId, useState } from 'react';

export type RecipeMeasurement = {
  name: string;
  href: string;
  exports: number;
  api: number;
  compute: number | null;
  candidates: number | null;
};

type Bar = {
  label: string;
  value: number;
  display: string;
  detail?: string;
  secondary?: number;
  href?: string;
};

/** Values remain readable without colour, hover, animation or JavaScript. */
export function BarChart({ rows, max, label }: { rows: Bar[]; max?: number; label: string }) {
  const ceiling = max ?? Math.max(...rows.map((row) => row.value + (row.secondary ?? 0)), 1);
  return (
    <ul className="result-bars" aria-label={label}>
      {rows.map((row) => (
        <li key={row.label}>
          <div className="result-bars__label">
            {row.href ? <a href={row.href}>{row.label}</a> : <span>{row.label}</span>}
            <strong>{row.display}</strong>
          </div>
          <div className="result-bars__track" aria-hidden="true">
            <span style={{ width: `${(row.value / ceiling) * 100}%` }} />
            {row.secondary !== undefined && (
              <span
                className="result-bars__compute"
                style={{ width: `${(row.secondary / ceiling) * 100}%` }}
              />
            )}
          </div>
          {row.detail && <p className="result-bars__detail">{row.detail}</p>}
        </li>
      ))}
    </ul>
  );
}

export function RecipeComparison({
  recipes,
  shared,
}: {
  recipes: RecipeMeasurement[];
  shared: RecipeMeasurement;
}) {
  const [metric, setMetric] = useState('combined');
  const id = useId();
  const money = (n: number) => `$${n.toFixed(3)}`;
  const costRows = (items: RecipeMeasurement[], combined: boolean): Bar[] =>
    items.map((r) => ({
      label: r.name,
      href: r.href,
      value: r.api / r.exports,
      secondary: combined ? (r.compute ?? 0) / r.exports : undefined,
      display: money((r.api + (combined ? (r.compute ?? 0) : 0)) / r.exports),
      detail: `${r.exports} new exports${combined ? ` · API ${money(r.api / r.exports)} + compute ${money((r.compute ?? 0) / r.exports)}` : r.compute === null ? ' · compute shared, not allocated' : ' · compute excluded'}`,
    }));
  const rows =
    metric === 'yield'
      ? recipes
          .filter((r) => r.candidates !== null)
          .map((r) => ({
            label: r.name,
            href: r.href,
            value: (r.exports / r.candidates!) * 100,
            display: `${((r.exports / r.candidates!) * 100).toFixed(1)}%`,
            detail: `${r.exports} exports / ${r.candidates!.toLocaleString('en-US')} candidates${r.name === 'terminalworld' ? ' · before suitability screening' : ''}`,
          }))
      : costRows(
          metric === 'combined' ? [shared, ...recipes.filter((r) => r.compute !== null)] : recipes,
          metric === 'combined',
        );

  return (
    <section className="result-panel" aria-labelledby={`${id}-title`}>
      <div className="result-panel__heading">
        <div>
          <p className="eyebrow">Research recipes · September 14, 2026</p>
          <h2 id={`${id}-title`}>
            {metric === 'yield' ? 'Candidate-to-export yield' : 'Generation cost per export'}
          </h2>
        </div>
        <div className="result-select">
          <label htmlFor={id}>Compare</label>
          <select id={id} value={metric} onChange={(event) => setMetric(event.target.value)}>
            <option value="combined">API + compute / export</option>
            <option value="api">API only / export</option>
            <option value="yield">Candidate → export yield</option>
          </select>
        </div>
      </div>
      <p className="result-note">
        Same accounting scope, different tasks and difficulty. These are observed generation
        results, not a quality or price ranking. Independent evaluation and unresolved charges are
        excluded.
      </p>
      {metric === 'combined' && (
        <p className="result-legend">
          <span>
            <i /> Model API
          </span>
          <span>
            <i className="result-legend__compute" /> Estimated compute
          </span>
          <span>USD per new export · bars start at zero</span>
        </p>
      )}
      {metric === 'api' && (
        <p className="result-note">
          All 14 recipes. SCALER made no generation-model calls; running its environments still
          costs compute.
        </p>
      )}
      {metric === 'yield' && (
        <p className="result-note">
          Only six recipes have a recovered candidate denominator. The other eight have unknown
          yield, not zero yield. TerminalWorld reaches 54.1% after screening (80/148), versus 6.2%
          before screening (80/1,293).
        </p>
      )}
      <BarChart
        rows={rows}
        max={metric === 'yield' ? 100 : undefined}
        label={`Recipe comparison: ${metric}`}
      />
      {metric === 'combined' && (
        <p className="result-footnote">
          The repository pool groups swe-smith, r2e, swe-gen, swe-next, r2e-gym and scaler. Its
          shared worker bill cannot be allocated reliably to individual recipes. Use “API only” to
          compare their recorded model costs.
        </p>
      )}
    </section>
  );
}
