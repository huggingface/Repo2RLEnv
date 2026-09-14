"""Build the campaign delivery report from local release and expansion receipts.

This is a read-only artifact report, not a provider-liveness or quality evaluator.
It performs no model calls, task execution or publication.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from repo2rlenv.pipelines.recipes.catalog import get_recipe

GUIDES = {
    "swe-smith": "repo_mutate.md",
    "r2e": "r2e.md",
    "swe-gen": "pr_to_env.md",
    "swe-next": "swe_next.md",
    "r2e-gym": "r2e_gym.md",
    "scaler": "scaler.md",
    "endless-terminals": "endless_terminals.md",
    "cli-gym": "env_repair.md",
    "swe-flow": "repo_reconstruct.md",
    "seta-seed2synth": "terminal_synth.md",
    "seta-evol": "task_evolve.md",
    "tmax": "tmax.md",
    "terminalworld": "terminalworld.md",
    "dataarc": "dataarc.md",
    "tasksmith": "tasksmith.md",
}
WAVE1 = {"swe-smith", "r2e", "swe-gen", "swe-next", "r2e-gym", "scaler"}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def inventory(releases: Path, expansion: Path) -> dict:
    rows = []
    targets = {"cli-gym": 20, "tasksmith": 50}
    for name, guide in GUIDES.items():
        version = "v3" if name in WAVE1 else "v4"
        stage = releases / ("datasets-" + version) / name
        receipt = releases / ("receipts-" + version) / (name + ".json")
        manifest = load(stage / "manifest.json") if stage.exists() else None
        publication = load(receipt) if receipt.exists() else {}
        if manifest is not None:
            count = len(list((stage / "tasks").glob("*/task.toml")))
            if count != manifest["task_count"]:
                raise ValueError(f"Staged task inventory changed: {name}")
            economics = manifest["economics"]
        else:
            campaign = expansion / "campaigns" / name
            retained = load(campaign / "inputs/retained.json")["tasks"]
            count = len(retained) + len(list((campaign / "generated" / name).glob("*/task.toml")))
            economics = {"scope": "Expansion in progress; see the current ledger snapshot."}
        if publication.get("state") == "completed" and publication["task_count"] != count:
            raise ValueError(f"Publication receipt disagrees with selected tasks: {name}")
        recipe = get_recipe(name.replace("-", "_")) if name != "tasksmith" else None
        rows.append(
            {
                "recipe": name,
                "guide": guide,
                "pipeline": recipe.pipeline if recipe else "tasksmith",
                "target": targets.get(name, 100),
                "generated": count,
                "publication_state": publication.get("state", "not_started"),
                "repo_id": manifest["repo_id"] if manifest else None,
                "artifact_commit": publication.get(
                    "artifact_commit", publication.get("commit_sha")
                ),
                "publication_commit": publication.get("commit_sha"),
                "quality_counts": manifest.get("quality_counts") if manifest else None,
                "economics": economics,
            }
        )
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "scope": "Generated counts and saved publication receipts; not live worker status or independent quality acceptance.",
        "generated": sum(row["generated"] for row in rows),
        "minimum_target": sum(row["target"] for row in rows),
        "expected_final_inventory": 1375,
        "generation_targets_reached": sum(row["generated"] >= row["target"] for row in rows),
        "published_datasets": sum(row["publication_state"] == "completed" for row in rows),
        "published_tasks": sum(
            row["generated"] for row in rows if row["publication_state"] == "completed"
        ),
        "collection": load(releases / "collection.json")["slug"],
        "recipes": rows,
    }


def money(value) -> str:
    return f"${Decimal(str(value)):.2f}"


def render(report: dict) -> str:
    lines = [
        "# Harbor dataset release inventory",
        "",
        "Snapshot: " + report["as_of"],
        "",
        f"**{report['generated']} generated tasks; {report['generation_targets_reached']}/15 generation targets reached; "
        f"{report['published_datasets']} datasets published ({report['published_tasks']} tasks).**",
        "",
        "The target is 100 tasks for thirteen owned recipes, 50 verified Tasksmith tasks, "
        "and at least 20 CLI-Gym tasks. CLI-Gym already produced 25; all are retained, "
        "so the final inventory is expected to contain **1,375 tasks**. SEC-bench remains excluded.",
        "",
        f"Browse the [HuggingEnvs collection](https://huggingface.co/collections/{report['collection']}). "
        "It also links the six earlier native-pipeline datasets under their existing owners. "
        "Those historical collections are outside this new-generation denominator.",
        "",
        "## Delivery by method",
        "",
        "| Recipe and guide | CLI pipeline / recipe | Generated / target | Publication |",
        "|---|---|---:|---|",
    ]
    for row in report["recipes"]:
        name = row["recipe"]
        route = (
            "tasksmith run"
            if name == "tasksmith"
            else f"{row['pipeline']} / {name.replace('-', '_')}"
        )
        state = row["publication_state"]
        publication = (
            f"[Published](https://huggingface.co/datasets/{row['repo_id']})"
            if state == "completed"
            else (
                "Uploading / checking"
                if state in {"batch_upload", "upload_dispatched", "uploaded"}
                else "Pending"
            )
        )
        lines.append(
            f"| [{name}]({row['guide']}) | `{route}` | {row['generated']} / {row['target']} | {publication} |"
        )
    lines += [
        "",
        "## What the counts establish",
        "",
        "Each released task is a Harbor directory with its instruction, configuration, "
        "environment, trusted verifier and reference. Archives preserve executable modes. "
        "The [release workflow](dataset_release.md) records exact bundle identities, "
        "per-file publication audits and commit-pinned registries; the generic tabular "
        "Hub viewer is disabled in favor of Harbor Visualiser.",
        "",
        "Generation controls and quality acceptance are separate. New expansion exports "
        "have their recipe's native checks and recorded baseline/reference contrast. "
        "Historical retained tasks have separate evidence scope. Tasksmith's selected "
        "50 carry verified labels from its audited, assisted campaign; this does not "
        "imply unattended acceptance or that every task was solved by Sonnet. "
        "Known instruction/verifier defects remain diagnosed and labeled for repair.",
        "",
        "## Measured economics",
        "",
        "These costs include unsuccessful attempts within the stated campaign. Model-only "
        "prices and costs including estimated cloud usage are deliberately identified. "
        "Earlier retained-task costs, interactive assistant usage and future independent "
        "quality campaigns are excluded unless the row says otherwise.",
        "",
        "| Recipe | New exports in cost scope | Recorded USD | USD per new export | Cost scope |",
        "|---|---:|---:|---:|---|",
    ]
    for row in report["recipes"]:
        name, cost = row["recipe"], row["economics"]
        if "recipe" in cost:
            item = cost["recipe"]
            n, amount = item["new_exports"], item["new_model_booked_usd"]
            scope = "Model only; shared cloud costs below"
        elif name == "tasksmith":
            n, amount = 26, cost["expansion"]["booked_usd"]
            scope = "Historical expansion including quality, rollouts and estimated compute; not the cost of all 50"
        elif "new_exports" in cost:
            n, amount = cost["new_exports"], cost["accounted_usd"]
            scope = "Models + estimated cloud/build usage"
            if Decimal(str(cost.get("reserved_usd", 0))) > 0:
                scope += "; " + money(cost["reserved_usd"]) + " remains reserved separately"
        else:
            lines.append(
                f"| {name} | In progress | See ledger | — | [Current expansion snapshot](economics/waves34/README.md) |"
            )
            continue
        lines.append(
            f"| {name} | {n} | {money(amount)} | {money(Decimal(str(amount)) / n)} | {scope} |"
        )
    lines += [
        "",
        "Wave 1's six recipes share **$43.09** in estimated cloud/build costs, "
        "in addition to **$32.81** of model usage: **$75.90 accounted for 476 new exports**, "
        "with **$2.25** of uncertain model holds. Count this shared campaign once. "
        "Its per-recipe [source and timing reports](economics/wave1/README.md) explain "
        "why a zero-model-cost SCALER export is not free compute.",
        "",
        "Wave 2's [settled economics](evidence/wave2-generation-economics.json) include "
        "all four expansion campaigns. Tasksmith's [retrospective](tasksmith_campaign_retrospective.md) "
        "separates investigation, repair, rollout and cloud estimates. Parent allocations "
        "and child costs are the same funds; outstanding and uncertain reservations "
        "remain separate from actual recorded charges. Cloud estimates are not invoices.",
        "",
        "The [machine-readable snapshot](evidence/release-inventory.json) records artifact "
        "commits, quality-label distributions and each cost scope. Refresh this page with "
        "`python docs/_tools/update_release_inventory.py --releases workspace/owned-releases-100 "
        "--expansion workspace/owned-waves34-100`. This reads saved artifacts; it does not "
        "dispatch work or establish live provider state.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--releases", type=Path, required=True)
    parser.add_argument("--expansion", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs/pipelines/releases.md"))
    args = parser.parse_args()
    report = inventory(args.releases, args.expansion)
    content = render(report)
    for guide in GUIDES.values():
        if not (args.out.parent / guide).is_file():
            raise FileNotFoundError(guide)
    evidence = args.out.parent / "evidence/release-inventory.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.out.write_text(content)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "generated",
                    "generation_targets_reached",
                    "published_datasets",
                    "published_tasks",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
