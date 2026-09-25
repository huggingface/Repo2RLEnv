"""Render public cost and dataset tables from a portable, sanitized summary."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def unit(row: dict, key: str) -> str:
    value = row.get(key)
    return "—" if value is None else f"${Decimal(str(value)) / row['sample_exports']:.2f}"


def native_tables(history: dict) -> tuple[list[str], list[str]]:
    """Keep historical inventory and partial costs outside modern quality totals."""
    rows = history["pipelines"]
    if len({row["pipeline"] for row in rows}) != len(rows):
        raise ValueError("Duplicate native pipeline inventory")
    releases = [
        "## Native pipelines",
        "",
        f"**{sum(row['tasks'] for row in rows):,} task entries across {len(rows)} earlier datasets.** Recovered from cached Hub manifests and local publication stagings on **{history['reviewed_on']}**. These are historical snapshots, not a fresh Hub recount. Older revisions and duplicate stagings are excluded; cross-pipeline content is not deduplicated.",
        "",
        "| Pipeline | Tasks | Recovered validation evidence | Dataset and evidence |",
        "|---|---:|---|---|",
    ]
    economics = [
        "## Native pipeline measurements",
        "",
        "These May–July 2026 runs have less complete accounting. **Recorded synthesis cost excludes bootstrap, compute and solver evaluation**; it is not comparable to the total generation costs above. — means unavailable. See [historical results](native_results.md) for the evidence and sample boundaries.",
        "",
        "| Pipeline | Retained tasks | Measured generation yield | Recorded synthesis / task | Scope |",
        "|---|---:|---:|---:|---|",
    ]
    for row in rows:
        if row["tasks"] <= 0 or sum(row["repo_distribution"].values()) != row["tasks"]:
            raise ValueError("Native repository counts disagree with inventory")
        if any(key not in history["sources"] for key in row["sources"]):
            raise ValueError("Missing native evidence source")
        attempted = row["attempted_candidates"]
        if attempted is not None and attempted < row["tasks"]:
            raise ValueError("Invalid native generation denominator")
        yield_text = (
            f"{row['tasks']}/{attempted} ({100 * row['tasks'] / attempted:.1f}%)"
            if attempted
            else "—"
        )
        name = f"[{row['pipeline']}]({row['guide']})"
        evidence = (
            f"[Manifest]({row['manifest_url']})"
            if row["manifest_url"]
            else "[Local evidence](native_results.md#evidence-and-reproduction)"
        )
        releases.append(
            f"| {name} | {row['tasks']} | {row['validation_summary']} | "
            f"[Dataset](https://huggingface.co/datasets/{row['repo_id']}) · {evidence} |"
        )
        value = row["recorded_synthesis_usd"]
        cost = "—" if value is None else f"${Decimal(value) / row['tasks']:.3f}"
        if row["pipeline"] == "equivalence_tests" and value is not None:
            cost = "≥ " + cost
        economics.append(
            f"| {name} | {row['tasks']} | {yield_text} | {cost} | {row['cost_scope']} |"
        )
    releases += [
        "",
        "These tasks have **historical evidence scopes**, not retrospectively assigned `verified` labels. In particular, the earlier 52-task commit-runtime gate does not validate the later 100-task dataset. [Read the native results and solver samples](native_results.md).",
        "",
    ]
    economics += [
        "",
        "Code-instruct's complete generation log records 136 candidates, correcting the earlier 132-candidate claim. Equivalence-test logs contain at least 200 candidates, including zero-output runs, but several runs lack a final summary; its overall yield is unavailable. Its $0.025/task figure is only a lower bound from productive-run counters.",
        "",
    ]
    return releases, economics


def codemidas_tables(row: dict) -> tuple[list[str], list[str]]:
    """Keep the local, reviewed cohort out of published and generation-only totals."""
    n = row["curated_tasks"]
    if (
        row["review_passed"] + row["review_defects"] + row["review_unresolved"]
        != row["generated_tasks"]
        or sum(row["repositories"].values()) != n
        or sum(row["quality_counts"].values()) != n
        or sum(row["curricula"].values()) != n
        or not 0
        < n
        <= row["review_passed"]
        <= row["generated_tasks"]
        <= row["attempted_candidates"]
        or Decimal(row["accounted_usd"])
        != Decimal(row["api_usd"]) + Decimal(row["compute_estimate_usd"])
    ):
        raise ValueError("CodeMidas campaign counts or cost scopes disagree")
    releases = [
        "## Local collections awaiting publication",
        "",
        f"[CodeMidas](codemidas.md) has **{n} staged Harbor tasks**, separate from the published totals above. The {row['measured_on']} campaign generated {row['generated_tasks']} exports; {row['review_passed']} passed ordinary solver review, {row['review_defects']} had demonstrated verifier/instruction defects and {row['review_unresolved']} remained unresolved.",
        "",
        f"The curated collection has **{row['baseline_failures']} baseline failures and {row['oracle_passes']} oracle passes**, plus four reviewed Luna attempts and four Sol screens per task. All {n} retain **blocked** labels because adversarial checks could not run. No full-method acceptance is claimed. See the [release notes and audit](../release_notes/codemidas.md) and [source/difficulty breakdown](codemidas.md#measured-local-campaign).",
        "",
    ]
    costs = [
        "## CodeMidas generation and evaluation",
        "",
        f"Measured **{row['measured_on']}** using GPT-6 Luna/Sol and Daytona. This whole-campaign sample includes historical pilots, failed construction, independent review, rollouts and compute. It is **not comparable to generation-only prices** above; interactive assistant usage is excluded.",
        "",
        "| Measure | Result |",
        "|---|---:|",
        f"| Construction yield | {row['generated_tasks']}/{row['attempted_candidates']} ({100 * row['generated_tasks'] / row['attempted_candidates']:.1f}%) |",
        f"| Ordinary review yield | {row['review_passed']}/{row['generated_tasks']} ({100 * row['review_passed'] / row['generated_tasks']:.1f}%) |",
        f"| Recorded API usage | ${Decimal(row['api_usd']):.2f} |",
        f"| Conservative compute estimate | ${Decimal(row['compute_estimate_usd']):.2f} |",
        f"| Combined accounted | ${Decimal(row['accounted_usd']):.2f} |",
        f"| Unknown API billing reserved | ${Decimal(row['unresolved_usd']):.2f} |",
        f"| Accounted per export | ${Decimal(row['accounted_usd']) / row['generated_tasks']:.2f} |",
        f"| Accounted per reviewed task | ${Decimal(row['accounted_usd']) / row['review_passed']:.2f} |",
        f"| Accounted per curated task | ${Decimal(row['accounted_usd']) / n:.2f} |",
        "",
        "Compute is an estimate, not an invoice. The construction denominator includes two candidates stopped after the goal was met. The 100 curated tasks remain adversarial-blocked; there is no cost per fully accepted task. See [stage costs and limitations](codemidas.md#measured-economics).",
        "",
    ]
    return releases, costs


def render(data: dict) -> dict[str, str]:
    rows = data["pipelines"]
    by_name = {row["recipe"]: row for row in rows}
    if len(by_name) != len(rows):
        raise ValueError("Duplicate pipeline measurement")
    tasksmith = by_name["tasksmith"]
    sample = tasksmith["evaluation_sample"]
    observed = data["tasksmith_observation"]
    native_releases, native_economics = native_tables(data["native_history"])
    local_releases, local_economics = codemidas_tables(data["codemidas_campaign"])
    labels_total = {}
    for row in rows:
        if sum(row["quality_counts"].values()) != row["published_tasks"]:
            raise ValueError("Evaluation labels disagree with dataset count")
        for key, count in row["quality_counts"].items():
            labels_total[key] = labels_total.get(key, 0) + count
        if row.get("generation_usd") is not None and Decimal(row["generation_usd"]) != Decimal(
            row["model_usd"]
        ) + Decimal(row["compute_usd"]):
            raise ValueError("Generation total disagrees with model and compute costs")
    economics = [
        "# Yield and cost per task",
        "",
        f"Observed samples measured on **{data['measured_on']}**. Use these as measured examples, not price guarantees. A task is one exported Harbor environment; an export is not independent quality acceptance.",
        "",
        "## Research recipes and Tasksmith generation",
        "",
        "Costs include unsuccessful attempts and bounded repairs within each sample. Model and estimated compute costs are separate; the total is shown only when both are attributable. **— means unavailable, not zero.**",
        "",
        "| Pipeline | Attempted candidates | New tasks | Yield | Model / task | Compute / task | Total generation / task |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    releases = [
        "# Published Harbor datasets",
        "",
        "Published results cover the six native pipelines, Tasksmith and 14 research recipes. The local CodeMidas collection is reported separately and is not included in published totals.",
        "",
        f"Browse the [HuggingEnvs collection](https://huggingface.co/collections/{data['collection']}). The native datasets retain their existing owners.",
        "",
        *native_releases,
        "## Tasksmith and research recipes",
        "",
        f"**{sum(r['published_tasks'] for r in rows):,} tasks across {len(rows)} datasets.** Each dataset contains complete Harbor task directories, archives and a registry pinned to its artifact revision.",
        "",
        "| Pipeline | Tasks | Evaluation labels | Dataset and pinned manifest |",
        "|---|---:|---|---|",
    ]
    for row in rows:
        n = row["sample_exports"]
        if n <= 0 or row.get("attempted_candidates", n) < n:
            raise ValueError(f"Invalid measurement denominator: {row['recipe']}")
        name = f"[{row['recipe']}]({row['guide']})"
        attempted = row.get("attempted_candidates")
        rate = f"{100 * n / attempted:.1f}%" if attempted else "—"
        economics.append(
            f"| {name} | {attempted if attempted else '—'} | {n} | {rate} | "
            f"{unit(row, 'model_usd')} | {unit(row, 'compute_usd')} | {unit(row, 'generation_usd')} |"
        )
        labels = "; ".join(f"{v} {k.replace('_', ' ')}" for k, v in row["quality_counts"].items())
        url = "https://huggingface.co/datasets/" + row["repo_id"]
        releases.append(
            f"| {name} | {row['published_tasks']} | {labels} | [Dataset]({url}) · "
            f"[Manifest]({url}/resolve/{row['artifact_commit']}/manifest.json) |"
        )
    shared = data["shared_compute"]
    mean = Decimal(shared["total_usd"]) / shared["sample_exports"]
    economics += [
        "",
        "## How to read the measurements",
        "",
        "**Yield = new exports / distinct recorded task candidates.** Count a candidate once across retries. The available terminal-generator counts start before design screening; filtered and interrupted candidates remain in the denominator. Failures before a candidate identity exists are not counted as candidates, although their costs are included. Older repository runs do not retain a consistently deduplicated denominator, so their yield is unavailable. Reaching a dataset target is not a yield measurement.",
        "",
        f"TerminalWorld's yield includes recordings rejected as unsuitable; it is not the success rate among already approved designs. SETA Evol includes {by_name['seta-evol']['unfinished_candidates']} unfinished candidate and TMax {by_name['tmax']['unfinished_candidates']} when generation stopped. These input domains are not a controlled ranking of algorithms.",
        "",
        f"SWE-smith, R2E, SWE-gen, SWE-Next, R2E-Gym and SCALER shared workers. Together their {shared['sample_exports']} new exports cost **${Decimal(shared['total_usd']):.2f}, or ${mean:.3f} per task**, including estimated compute. Per-recipe compute was not allocated. SCALER uses no generation-model calls, but still incurs compute cost.",
        "",
        f"The CLI-Gym cost sample has {by_name['cli-gym']['sample_exports']} new tasks; its published dataset contains {by_name['cli-gym']['published_tasks']}. Earlier retained-task costs and interactive assistant usage are excluded from these older generation samples. Most recipe samples used CPU workers on Daytona; Tasksmith also used native Modal GPU execution. These measurements include development retries and do not establish unattended production cost.",
        "",
        "The primary recipe author was `claude-sonnet-4-6`; TMax also includes a small unsuccessful `gpt-5.4-mini` comparison. SCALER used no generation model. Configured model identities are retained in the summary; these are not cross-model quality comparisons.",
        "",
        f"Uncertain model charges remain excluded from recorded totals: ${Decimal(shared['unresolved_usd']):.2f} for recipes sharing workers, ${Decimal(by_name['seta-evol']['unresolved_usd']):.2f} for SETA Evol and ${Decimal(by_name['tmax']['unresolved_usd']):.2f} for TMax. They may increase the final cost. Compute figures are resource estimates, not provider invoices.",
        "",
        "## Tasksmith and optional evaluation",
        "",
        f"Tasksmith's measured expansion added **{tasksmith['sample_exports']} accepted PR tasks** and also repaired/revalidated retained tasks. Its **{unit({**sample, 'sample_exports': tasksmith['sample_exports']}, 'total_usd')} per added accepted task** includes authoring, quality review, rollouts and compute. It is not a generation-only price and is excluded from the generation-cost columns above.",
        "",
        "| Component | Recorded cost per added accepted task |",
        "|---|---:|",
    ]
    for label, key in [
        ("Authoring model", "author_model_usd"),
        ("Review and repair models", "review_repair_model_usd"),
        ("Blind solver model", "solver_model_usd"),
        ("Compute across generation and evaluation", "combined_compute_usd"),
        ("Total", "total_usd"),
    ]:
        economics.append(
            f"| {label} | {unit({**sample, 'sample_exports': tasksmith['sample_exports']}, key)} |"
        )
    economics += [
        "",
        f"A further ${Decimal(sample['unresolved_usd']):.2f} remains unresolved for this sample. The final published Tasksmith cohort has {observed['verified']} verified tasks, including {observed['sonnet_solved']} full Sonnet solves. Solver success, generation yield and quality acceptance are separate measures. Comparable independent evaluation costs have not been established for the other full datasets.",
        "",
        *local_economics,
        *native_economics,
        "## Measurement source",
        "",
        "The [sanitized summary](../data/pipelines.json) contains sample sizes, cost scopes, candidate-count definitions and pinned dataset manifests. No private campaign folder is needed to rebuild this page. To update both tables after reviewing new measurements:",
        "",
        "```bash",
        "python docs/_tools/generate_metrics.py",
        "python docs/_tools/generate_metrics.py --check",
        "```",
        "",
    ]
    releases += [
        "",
        *local_releases,
        "## What the labels establish",
        "",
        f"The Tasksmith and research-recipe release contains **{labels_total['verified']:,} verified, {labels_total['needs_repair']:,} needing repair and {labels_total['unverified']:,} unverified** tasks. These totals exclude the historical native inventories above. Tasksmith's verified cohort came from an assisted campaign; this does not claim unattended conversion. Two SWE-flow instruction issues and three TerminalWorld verifier gaps remain explicitly diagnosed. Each dataset manifest supplies task-level labels, diagnostics and evidence scope.",
        "",
        "Publication checks for those 15 datasets compared 214,097 file identities and parsed every selected task with Harbor. This establishes artifact integrity and format, not semantic quality of every task. See [evaluation labels](task_evaluation_labels.md), [yield and cost](economics.md), and [how to publish](dataset_release.md).",
        "",
    ]
    return {"economics.md": "\n".join(economics), "releases.md": "\n".join(releases)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = json.loads((ROOT / "docs/data/pipelines.json").read_text())
    for name, text in render(data).items():
        path = ROOT / "docs/pipelines" / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                raise ValueError(f"{path.name} differs; run python docs/_tools/generate_metrics.py")
        else:
            path.write_text(text)
    print("Public metrics tables checked" if args.check else "Public metrics tables updated")


if __name__ == "__main__":
    main()
