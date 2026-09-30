"""Render scoped experiment accounting; never turn missing costs into zeros."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from frontmatter import with_title

D = Decimal


def money(value: str | Decimal | None, places: int = 2) -> str:
    return "n/a" if value is None else f"${D(str(value)):,.{places}f}"


def total(rows: list[dict], key: str) -> Decimal:
    return sum((D(str(r[key])) for r in rows), D(0))


def validate(data: dict, audit: dict, frontier: dict) -> None:
    """Fail the docs build on overlap-prone or internally inconsistent accounting."""
    native = {r["pipeline"]: r for r in data["native_history"]["pipelines"]}
    for row in audit["native_model_metadata"]:
        if (
            row["tasks"] != native[row["pipeline"]]["tasks"]
            or sum(row["synthesis_models"].values()) != row["tasks"]
        ):
            raise ValueError("Native model metadata disagrees with cohort")
    for name, controls in audit["repository_expansion_controls"]["summary"].items():
        sample = next(r for r in data["pipelines"] if r["recipe"] == name)
        if (
            controls["new_exports"] != sample["sample_exports"]
            or not 0 <= controls["new_matched_baseline_reference"] <= controls["new_exports"]
        ):
            raise ValueError("Control coverage disagrees with expansion cohort")
    groups = audit["expansion_groups"]
    if len({r["id"] for r in groups}) != len(groups):
        raise ValueError("Duplicate expansion ledger group")
    for group in groups:
        if total(list(group["stages"].values()), "accounted_usd") != D(group["accounted_usd"]):
            raise ValueError("Stage costs do not reconcile to ledger group")
        if total(list(group["stages"].values()), "held_usd") != D(group["held_usd"]):
            raise ValueError("Stage holds do not reconcile to ledger group")
        for ref in group["sources"]:
            if ref not in audit["sources"]:
                raise ValueError("Missing source fingerprint")
    for row in data["pipelines"]:
        if row["recipe"] == "tasksmith":
            continue
        detail = audit["recipe_expansion"][row["recipe"]]
        if D(detail["accounted_usd"]) != D(row["model_usd"]):
            raise ValueError("Recipe API costs disagree with published measurement")
        if D(detail["held_usd"]) != D(row["unresolved_usd"]):
            raise ValueError("Recipe API holds disagree with published measurement")
        if total(list(detail["stages"].values()), "accounted_usd") != D(detail["accounted_usd"]):
            raise ValueError("Recipe stages do not reconcile")
        if total(list(detail["stages"].values()), "held_usd") != D(detail["held_usd"]):
            raise ValueError("Recipe stage holds do not reconcile")
        if total(detail["models"], "accounted_usd") != D(detail["accounted_usd"]):
            raise ValueError("Model costs do not reconcile")
        for model in detail["models"]:
            if (
                not 0
                <= model["receipts_with_tokens"]
                <= model["settled_calls"]
                <= model["operations"]
            ):
                raise ValueError("Invalid token receipt coverage")
    initial = audit["initial_program"]
    whole = initial["whole_program"]
    booked = D(whole["booked_usd"])
    if total(list(initial["phases"].values()), "held_usd") != D(whole["held_usd"]):
        raise ValueError("Initial holds disagree")
    if total(list(initial["phases"].values()), "booked_usd") != booked:
        raise ValueError("Initial phase totals disagree")
    if total(list(initial["direct_cost_by_population"].values()), "booked_usd") != booked:
        raise ValueError("Initial attribution totals disagree")
    if D(whole["booked_including_external_usd"]) != booked + D(whole["external_prior_usd"]):
        raise ValueError("External prior costs disagree")
    if sum(v["accounted_micros"] for v in audit["codemidas"]["stages"].values()) != int(
        D(data["codemidas_campaign"]["accounted_usd"]) * 1_000_000
    ):
        raise ValueError("CodeMidas stages disagree")
    if total(audit["frontiersmith"]["ledger_groups"], "held_usd") != D(frontier["reserved_usd"]):
        raise ValueError("FrontierSmith holds disagree")
    if total(audit["frontiersmith"]["ledger_groups"], "accounted_usd") != D(frontier["total_usd"]):
        raise ValueError("FrontierSmith pilot/expansion totals disagree")
    # Parent subcampaign transfers must not be added to their leaf operations.
    if any("subcampaign" in g["stages"] for g in groups):
        raise ValueError("Double-counted parent transfer")
    known_expansion = D(data["shared_compute"]["total_usd"]) + sum(
        (D(r["generation_usd"]) for r in data["pipelines"] if r.get("generation_usd")), D(0)
    )
    if sum(
        (D(r.get("unresolved_usd", "0")) for r in data["pipelines"] if r["recipe"] != "tasksmith"),
        D(0),
    ) != total(groups, "held_usd"):
        raise ValueError("Expansion holds disagree")
    if known_expansion != total(groups, "accounted_usd"):
        raise ValueError("Expansion ledger totals disagree with recipe scopes")


def program_table(data: dict, audit: dict, frontier: dict) -> list[str]:
    initial = audit["initial_program"]["whole_program"]
    expansion = audit["expansion_groups"]
    cm = data["codemidas_campaign"]
    recipe_rows = [r for r in data["pipelines"] if r["recipe"] != "tasksmith"]
    expansion_exports = sum(r["sample_exports"] for r in recipe_rows)
    rows = [
        (
            "Initial research pilots + Tasksmith development/evaluation",
            D(initial["booked_including_external_usd"]),
            D(initial["held_usd"]),
            "291 recipe exports + 50 final Tasksmith tasks; includes failures, shared work and $22.43 prior spend",
        ),
        (
            "Research-recipe expansion",
            total(expansion, "accounted_usd"),
            total(expansion, "held_usd"),
            f"{expansion_exports} new exports across {len(recipe_rows)} recipes; earlier retained tasks excluded",
        ),
        (
            "CodeMidas campaign",
            D(cm["accounted_usd"]),
            D(cm["unresolved_usd"]),
            f"{cm['curated_tasks']} curated tasks; includes historical pilots, generation and evaluation",
        ),
        (
            "FrontierSmith campaign",
            D(frontier["total_usd"]),
            D(frontier["reserved_usd"]),
            f"{frontier['selected_tasks']} selected tasks; includes pilot, expansion and sample evaluation",
        ),
    ]
    booked = sum((r[1] for r in rows), D(0))
    held = sum((r[2] for r in rows), D(0))
    lines = [
        "## Recorded experiment spend",
        "",
        f"**{money(booked)} accounted, plus {money(held)} unresolved/reserved**, across the disjoint scopes below. The combined accounting exposure is {money(booked + held)}. This is a recovered experiment subtotal, not an invoice or a complete lifetime project bill. Older native-pipeline costs and interactive assistant usage are outside it.",
        "",
        "| Measurement scope | Accounted | Held separately | What it paid for |",
        "|---|---:|---:|---|",
    ]
    lines += [
        f"| {label} | {money(cost)} | {money(reserve)} | {scope} |"
        for label, cost, reserve, scope in rows
    ]
    lines += [
        "",
        "Do not add the Tasksmith expansion, original recipe pilots, or per-stage tables again: they are subsets of these rows. Parent-to-child budget transfers were excluded. The [accounting detail](experiment_accounting.md#how-the-total-is-reconciled) explains scope, attribution and evidence.",
        "",
    ]
    return lines


def overview(data: dict, audit: dict, frontier: dict, local: list[str], native: list[str]) -> str:
    validate(data, audit, frontier)
    recipes = [r for r in data["pipelines"] if r["recipe"] != "tasksmith"]
    tasksmith = next(r for r in data["pipelines"] if r["recipe"] == "tasksmith")
    lines = [
        "# Yield and cost per task",
        "",
        f"Evidence audited **{audit['audited_on']}**. Recipe/Tasksmith samples were measured September 14, CodeMidas September 25, and FrontierSmith September 29, 2026. These are historical experiments on different inputs and checks, not a controlled price or quality ranking.",
        "",
        "Read the [stage costs, models, tokens and compute](experiment_accounting.md) for each recipe, or the [published inventory](releases.md) for dataset versions and quality labels.",
        "",
        "```mermaid",
        "flowchart TD",
        "    A[Candidate discovery and screening] --> B[Author task and verifier]",
        "    B --> C[Bootstrap and construction checks]",
        "    C --> D[Export Harbor task]",
        "    C --> R[Bounded repair]",
        "    R --> B",
        "    C --> X[Retain rejected candidates and diagnostics]",
        "    D --> E[Independent review and blind rollout]",
        "    E --> F[Select and label task]",
        "    G[Model usage + worker lifetime + unresolved calls] -. account at each stage .-> C",
        "```",
        "",
        "## Measurement definitions",
        "",
        "| Term | Definition |",
        "|---|---|",
        "| Candidate yield | Exports divided by distinct recorded candidates in the same sample; retries do not become new candidates. The recipe can start counting before design screening. |",
        "| Attempt yield | Used explicitly for CodeMidas/FrontierSmith: exports or selections divided by recorded attempts; historical revisions and repeated seeds are identified. |",
        "| Export / selected / accepted | A written Harbor bundle / a curated subset / a task that passed its named quality profile. These are different denominators. |",
        "| Accounted cost | Recorded API-usage estimates plus attributable compute estimates, including unsuccessful attempts and repairs within the stated scope. |",
        "| Held cost | Unresolved or reserved charges, reported separately from accounted spend; not evidence of a paid invoice. |",
        "| Per-task cost | The stated sample cost divided by its new outputs. Retained tasks and later validation must not silently enter the denominator. |",
        "| n/a | Evidence unavailable or not safely attributable. It never means free. |",
        "",
        *program_table(data, audit, frontier),
        "## Research recipes and Tasksmith generation",
        "",
        "These 14 generation samples added **989 tasks** to 291 retained recipe tasks. The final published recipe inventory is 1,280 tasks. Costs include failed generation and bounded repairs, but independent quality-pilot costs belong to the earlier program and are reported separately.",
        "",
        "| Recipe | Candidate denominator | New / final tasks | Export yield |",
        "|---|---:|---:|---:|",
    ]
    for r in recipes:
        n = r.get("attempted_candidates")
        rate = f"{100 * r['sample_exports'] / n:.1f}%" if n else "n/a"
        lines.append(
            f"| [{r['recipe']}]({r['guide']}) | {n or 'n/a'} | {r['sample_exports']} / {r['published_tasks']} | {rate} |"
        )
    lines += [
        "",
        "The first six repository recipes and CLI-Gym/SWE-flow lack a reliable deduplicated attempt denominator for these cost samples. Hitting a 100-task target is not 100% yield. TerminalWorld counted 1,293 recordings before suitability screening; 1,145 were screened out, leaving 148 candidates and 80 exports (54.1% after screening). SETA Evol includes one unfinished candidate; TMax includes eight.",
        "",
        "| Recipe | API total | Compute total | Held | API / new task | Combined / new task |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in recipes:
        n = r["sample_exports"]
        combined = r.get("generation_usd")
        lines.append(
            f"| [{r['recipe']}](experiment_accounting.md#{r['recipe']}) | {money(r['model_usd'])} | {money(r.get('compute_usd'))} | {money(r['unresolved_usd'])} | {money(D(r['model_usd']) / n, 3)} | {money(D(combined) / n if combined else None, 3)} |"
        )
    shared = data["shared_compute"]
    lines += [
        "",
        f"The six repository recipes share **{money(shared['compute_usd'])} compute**, without a defensible per-recipe allocation. Their combined 476 new exports cost **{money(shared['total_usd'])}**, or **{money(D(shared['total_usd']) / 476, 3)}/task**. SCALER has zero generation API spend, not zero runtime cost.",
        "",
        "Most expansion calls used `claude-sonnet-4-6`; TMax also tried `gpt-5.4-mini`. The earlier pilots used Sonnet and Opus in different proportions. Repository expansion started with six Modal workers, then used 26 Daytona workers; terminal/reconstruction expansion used Daytona. All were CPU workers. See the [per-recipe detail](experiment_accounting.md#per-recipe-generation-detail) for stages, exact model identifiers, calls and tokens.",
        "",
        "## Tasksmith and optional evaluation",
        "",
        "The final cohort contains **50 verified tasks and 19 full Sonnet solves**. It came from assisted development; neither the archived PR inventory nor the 50-task target establishes unattended conversion yield.",
        "",
        "| Cost scope | Accounted | Denominator | Cost per task |",
        "|---|---:|---|---:|",
        "| Direct operations attributed to the final 50 | $649.56 | 50 accepted tasks | $12.991 |",
        f"| Expansion, including retained-task repair/revalidation | {money(tasksmith['evaluation_sample']['total_usd'])} | 26 additional accepted tasks | {money(D(tasksmith['evaluation_sample']['total_usd']) / 26, 3)} |",
        "",
        "These are overlapping views, not additive bills. Direct attribution excludes shared/unattributed costs and rejected PRs; expansion cost includes unsuccessful attempts and work on previously retained tasks. Do not call either a complete marginal production price.",
        "",
        "| Expansion component | Total | Per added accepted task |",
        "|---|---:|---:|",
    ]
    for label, key in [
        ("Authoring model", "author_model_usd"),
        ("Review and repair models", "review_repair_model_usd"),
        ("Blind solver model", "solver_model_usd"),
        ("Compute, including validation", "combined_compute_usd"),
    ]:
        v = D(tasksmith["evaluation_sample"][key])
        lines.append(f"| {label} | {money(v)} | {money(v / 26, 3)} |")
    lines += [
        "",
        "A further $4.98 is held in that expansion scope. Authoring and blind solving used Sonnet 4.6; the larger program also used Opus 4.6 and GPT-6 Astra for review/repair. The final task requirements comprise 39 CPU tasks and 11 L4 GPU tasks. Cloud controllers, learner resources and model API inference are separate costs. See [Tasksmith accounting and interventions](experiment_accounting.md#tasksmith-development-and-quality-work).",
        "",
        *local,
        *native,
        "## Measurement source",
        "",
        "Tables are generated from [pipeline measurements](../data/pipelines.json), [reconciled experiment evidence](../data/experiment-economics.json), and the [FrontierSmith collection report](../data/frontiersmith-campaign.json). The [accounting guide](experiment_accounting.md#evidence-and-refresh) describes the source fingerprints and refresh procedure. Raw generated tasks, receipts and campaign scripts remain outside Git.",
        "",
        "```bash",
        "python3 docs/_tools/generate_metrics.py",
        "python3 docs/_tools/generate_metrics.py --check",
        "```",
        "",
    ]
    return with_title("\n".join(lines))


STAGES = {
    "build": "Artifact/test building and repair",
    "design": "Design and suitability screening",
    "review": "Infrastructure/contract review",
    "tests": "Additional tests",
    "issue": "Issue/instruction authoring",
    "instruction": "Instruction authoring",
    "r2e-test": "Equivalence tests and repairs",
    "r2e-spec": "Public specification",
    "swe_next": "PR task instructions",
    "r2e_gym": "Commit task instructions",
    "cli-gym": "Environment break/repair proposals",
    "docstring": "Docstring reconstruction",
    "specification": "Specification reconstruction",
}

PITFALLS = {
    "swe-smith": "Mutation and instruction generation are cheap on cached small repositories. Not every expansion export retained matching nop/reference receipts: 68 of 76 did in the completion audit. Five earlier quality-pilot tasks are not a gate for the final 100.",
    "r2e": "Equivalence-test authoring and retries dominate API spend. Passing a frozen reference only establishes behavior on the generated tests; sampled solver success does not prove exhaustive equivalence.",
    "swe-gen": "Reuses existing PR fixes and tests, so the measured API work is mostly instructions. Repository discovery, dependency setup and shared worker costs prevent treating the API figure as an all-in price.",
    "swe-next": "Uses existing PR tests and fixes. Input context is large relative to instruction output; test discovery and offline setup can fail before export.",
    "r2e-gym": "Commit mining supplies the implementation and tests. The missing-response reservation is preserved; do not count an unknown response as free.",
    "scaler": "Deterministic generators require no authoring LLM. Shared compute and later solver evaluation still cost money; construction format checks are not an independent semantic audit.",
    "endless-terminals": "Terminal design and environment building include rejected attempts and repair. The initial pilot used Opus, the expansion Sonnet, so pooling them hides a model change.",
    "cli-gym": "Only five expansion exports were measured, added to 20 retained pilot tasks. The first five sampled Sonnet rollouts scored zero before repairs; later repairs and reruns are separate evidence, not initial successes.",
    "swe-flow": "Reuses source for reconstruction and generates docstrings/specifications. Compute dominates the expansion bill; two published instruction issues remain labelled needs_repair.",
    "seta-seed2synth": "Design, build and review may repeat before export. Three of five earlier quality-pilot tasks were selected after revision; the published 100 are not all independently validated.",
    "seta-evol": "Evolution can produce ambiguous instructions or weak state checks. One candidate was unfinished and one model charge remained unresolved at stop.",
    "tmax": "Multi-pass design/build/review makes failed attempts expensive. The final target was reduced to 55; eight expansion candidates were unfinished. A small unsuccessful GPT-5.4-mini comparison is included in cost.",
    "terminalworld": "Recorded sessions often fail suitability screening: 1,145 of 1,293 candidates were screened out. That explains much of the 6.2% raw yield. Three published verifier gaps remain labelled needs_repair.",
    "dataarc": "High construction yield does not establish task quality. The published cohort used recipe v1; current v2 differs. None of five earlier quality-pilot tasks was selected under that pilot review despite five initial solver rewards of one.",
}


def hardware_rows(workers: list[dict]) -> list[str]:
    groups = defaultdict(list)
    for w in workers:
        groups[(w["provider"], w["cpus"], w["memory_mb"], w["disk_gb"])].append(w)
    lines = []
    for (provider, cpu, memory, disk), rows in sorted(groups.items(), key=lambda x: str(x[0])):
        timed = [w for w in rows if w["lifetime_seconds"] is not None]
        hours = sum(w["lifetime_seconds"] for w in timed) / 3600
        lines.append(
            f"| {provider} | {cpu} | {memory} | {disk if disk is not None else 'n/a'} | {len(rows)} | {f'{hours:.2f}' if timed else 'n/a'} ({len(timed)}/{len(rows)} timed) |"
        )
    return lines


def detail(data: dict, audit: dict, frontier: dict) -> str:
    initial = audit["initial_program"]
    pilots = {r["recipe"]: r for r in initial["recipe_pilots"]}
    lines = [
        "# Experiment accounting: models, compute and stage costs",
        "",
        f"Audited **{audit['audited_on']}** from saved receipts and frozen reports. The [yield and cost overview](economics.md) gives the comparable scope definitions; this page explains what the experiments actually consumed. No paid reruns were needed for this audit.",
        "",
        "## How the total is reconciled",
        "",
        *program_table(data, audit, frontier)[2:],
        "The initial program ledger contains **5,004 operations and $950.02 accounted**, plus $22.43 recorded as prior external spend. It includes the initial recipe pilots, Tasksmith, independent quality work and shared costs. The extra $22.43 has no recovered stage or provider split and is retained as an external prior amount.",
        "",
        "Research expansion adds $445.94 in separate leaf ledgers. The repository controller also carries a $21.29 transfer for terminal finishing; the audit excluded that parent row and counted the child costs once. Operation IDs in those expansion ledgers were checked for overlap with the frozen initial ledger. CodeMidas and FrontierSmith have separate campaign ledgers; FrontierSmith's pilot is included exactly once.",
        "",
        "This subtotal excludes unledgered interactive assistance, historical native pipeline expenses without a complete ledger, and any provider charge not represented by the selected records. Invoice-level reconciliation has not been performed. Do not divide it by the total published inventory: its scope includes development, discarded work, retained-task repair and different evaluation coverage.",
        "",
        "### Initial program cost allocation",
        "",
        "| Attributed population | Accounted | Held | Interpretation |",
        "|---|---:|---:|---|",
    ]
    labels = {
        "final50": ("Final 50 Tasksmith tasks", "Directly attributable operations only"),
        "other_tasksmith_prs": ("Other Tasksmith PRs", "Attempts outside the final 50"),
        "reproduction_candidates": (
            "Recipe candidates",
            "Initial generation/evaluation attributable to tasks",
        ),
        "shared_or_unattributed": (
            "Shared or unattributed",
            "Cannot safely allocate to a task or recipe",
        ),
    }
    for key, (label, note) in labels.items():
        r = initial["direct_cost_by_population"][key]
        lines.append(f"| {label} | {money(r['booked_usd'])} | {money(r['held_usd'])} | {note} |")
    lines += [
        "",
        "The direct-allocation and stage views below partition the same $950.02 ledger. Neither includes the separate $22.43 prior amount.",
        "",
        "| Initial program stage | Accounted | Held | Operations |",
        "|---|---:|---:|---:|",
    ]
    for name, r in initial["phases"].items():
        lines.append(
            f"| {name.replace('_', ' ')} | {money(r['booked_usd'])} | {money(r['held_usd'])} | {r['operations']:,} |"
        )
    lines += [
        "",
        "“Other/unattributed” includes recipe authoring and unclassified operations; it is not all compute. The initial ledger spans multiple experiments, so its compute/model ratio is not Tasksmith's isolated generation ratio.",
        "",
        "## Tasksmith development and quality work",
        "",
        "The archived inventory contains 56 distinct PRs and 50 final accepted tasks. Six other PRs and earlier prototypes remain outside that cohort. The archive does not establish a complete pre-screening denominator, so this is not a claim of 50/56 unattended yield. All 50 final blind solver configurations used `anthropic/claude-sonnet-4-6`; 19 reached full reward.",
        "",
        "| Model role in initial program | Recorded model | Accounted | Held | Operations |",
        "|---|---|---:|---:|---:|",
    ]
    for stage, models in initial["phase_models"].items():
        for name, r in models.items():
            lines.append(
                f"| {stage.replace('_', ' ')} | `{name}` | {money(r['accounted_usd'])} | {money(r['held_usd'])} | {r['operations']} |"
            )
    lines += [
        "",
        "This model table covers explicitly model-labelled ledger rows. Blind Harbor trial rows are a separate stage in the program table and are not re-added here.",
        "",
        "| Final task requirement | Tasks | CPU | Memory MiB | GPU |",
        "|---|---:|---:|---:|---|",
    ]
    resources = Counter(
        (t["cpus"], t["memory_mb"], t["gpus"], tuple(t["gpu_types"]))
        for t in initial["tasksmith_tasks"]
    )
    for (cpu, mem, gpu, types), n in sorted(resources.items()):
        lines.append(
            f"| {'GPU' if gpu else 'CPU'} task | {n} | {cpu} | {mem} | {str(gpu) + ' × ' + ', '.join(types) if gpu else 'None'} |"
        )
    lines += [
        "",
        "These are learner task requirements, not the controller sizes. Tasksmith used Modal CPU controllers and native Modal L4 execution for GPU tasks. Model inference was an API charge. The recovered cost scope does not provide a defensible per-task split of CPU, GPU, image build and shared idle costs.",
        "",
        "| Recorded assistance in final 50 | Tasks |",
        "|---|---:|",
    ]
    for name, n in initial["manual_categories"].items():
        lines.append(f"| {name.replace('_', ' ')} | {n} |")
    lines += [
        "",
        "Automated repair evidence exists for **35 tasks**. This overlaps the assistance categories; it is not 35 additional tasks. “No confirmed intervention” means history is incomplete, not proven hands-off generation. Per-task direct costs, resource requirements, solver rewards and assistance categories are retained in the [sanitized accounting data](../data/experiment-economics.json).",
        "",
        "## Cloud resource measurements",
        "",
        "Each row below counts saved worker records with their configured resources. Worker-hours include setup, builds, execution, waiting and idle time. Concurrent worker-hours add together; they are neither wall-clock completion time nor CPU utilization. Receipts with no stop timestamp are excluded from hours and exposed in the coverage count.",
        "",
    ]
    pools = [("Initial recipe/development workers", initial["pilot_workers"])]
    pools += [(g["id"], g["workers"]) for g in audit["expansion_groups"]]
    pools += [
        ("CodeMidas", audit["codemidas"]["workers"]),
        (
            "FrontierSmith, pilot + expansion",
            [w for g in audit["frontiersmith"]["ledger_groups"] for w in g["workers"]],
        ),
    ]
    for name, workers in pools:
        lines += [
            f"### {name}",
            "",
            "| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |",
            "|---|---:|---:|---:|---:|---:|",
            *hardware_rows(workers),
            "",
        ]
    lines += [
        "### How compute was priced",
        "",
        "Historical recipe receipts generally used a 4-CPU / 8-GiB / 10-GB Daytona rate of **$0.00009215 per second ($0.33174/hour)** plus an explicit **$1 image-build allowance per worker**. The allowance is a conservative accounting choice, not a measured provider build charge. Short or failed workers can therefore have high cost per task. Modal estimates used their recorded resource/lifetime basis; they are not silently repriced with Daytona rates.",
        "",
        "CodeMidas used the same nominal Daytona hourly rate with a **2× safety factor and $1 build allowance** on the shown cost receipt. FrontierSmith used 2-CPU / 4-GiB workers and lifecycle estimates without a free-tier deduction. These assumptions differ, so compute totals are not a hardware benchmark. Stored example cost receipts and their hashes are included in the accounting data; no claim is made about today's provider prices.",
        "",
        "## Repository expansion control coverage",
        "",
        "The completion audit matched executable bundle hashes to baseline/reference receipts for the new exports. Retained pilot tasks were outside this check. A missing match means evidence was not established by this audit, not that the task necessarily failed.",
        "",
        "| Recipe | New exports | Matching baseline/reference evidence |",
        "|---|---:|---:|",
        *[
            f"| {name} | {r['new_exports']} | {r['new_matched_baseline_reference']} |"
            for name, r in audit["repository_expansion_controls"]["summary"].items()
        ],
        "",
        "## Per-recipe generation detail",
        "",
        "The initial pilot API costs below are already inside the initial program total. Expansion costs are disjoint from those pilot costs. Full-lifecycle compute cannot be allocated per recipe from the shared initial workers; no equal-share estimate is presented as measured spend. Stage buckets include retries. Exact historical prompt text and source behavior may differ from the current release.",
        "",
    ]
    for r in data["pipelines"]:
        name = r["recipe"]
        if name == "tasksmith":
            continue
        q = audit["recipe_expansion"][name]
        p = pilots[name]
        lines += [
            f"### {name}",
            "",
            f"[{name} pipeline]({r['guide']}). Earlier pilot: **{p['exports']} exports, {money(str(p['generation_api_usd']))} generation API**, {money(str(p['unresolved_generation_reserve_usd']))} held. Expansion: **{r['sample_exports']} new exports**, {money(r['model_usd'])} API and {money(r.get('compute_usd'))} attributable compute. Final inventory: {r['published_tasks']}. Compute pool: `{q['ledger_group']}`.",
            "",
            f"Combined recorded pilot + expansion authoring API: **{money(D(str(p['generation_api_usd'])) + D(r['model_usd']))}**, or **{money((D(str(p['generation_api_usd'])) + D(r['model_usd'])) / r['published_tasks'], 3)} per final task**. This excludes independent quality work and all compute.",
            "",
            "Pilot author models: "
            + (
                ", ".join(
                    f"`{m.removeprefix('LLM ')}` ({count} calls)"
                    for m, count in p["models"].items()
                )
                or "none; deterministic generation"
            )
            + ".",
            "",
            "| Expansion stage | Accounted API | Held | Operations |",
            "|---|---:|---:|---:|",
        ]
        for name_stage, s in q["stages"].items():
            lines.append(
                f"| {STAGES.get(name_stage, name_stage)} | {money(s['accounted_usd'])} | {money(s['held_usd'])} | {s['operations']} |"
            )
        if not q["stages"]:
            lines.append("| No authoring calls | $0.00 | $0.00 | 0 |")
        lines += [
            "",
            "| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |",
            "|---|---:|---:|---:|---:|",
        ]
        for model in q["models"]:
            tok = model["reported_tokens"]
            lines.append(
                f"| `{model['model']}` | {money(model['accounted_usd'])} | {model['receipts_with_tokens']} / {model['settled_calls']} | {tok.get('input', 0):,} | {tok.get('output', 0):,} |"
            )
        if not q["models"]:
            lines.append("| None | $0.00 | 0 / 0 | 0 | 0 |")
        lines += [
            "",
            f"Earlier quality pilot: {p['control_pairs_passed']}/{p['sample_size']} baseline/reference control pairs passed; {p['original_sonnet_reward_1']}/{p['sample_size']} original Sonnet attempts scored one; **{p['selected_with_revisions']}/{p['sample_size']} selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.",
            "",
            PITFALLS[name],
            "",
        ]
    lines += [
        "## CodeMidas stage accounting",
        "",
        "GPT-6 Luna handled source/task construction and ordinary solving; GPT-6 Sol handled construction review, independent rollout review and difficulty screening. Daytona workers ran CPU tasks. Costs include 233 construction attempts with historical revisions, while the current-collection yield uses 213 attempts and 128 unique exports. Reporting the narrower yield does not remove historical costs.",
        "",
        "| Stage | Accounted | Held | Ledger operations |",
        "|---|---:|---:|---:|",
    ]
    for name, r in audit["codemidas"]["stages"].items():
        lines.append(
            f"| {name.replace('_', ' ')} | {money(D(r['accounted_micros']) / 1_000_000)} | {money(D(r['held_micros']) / 1_000_000)} | {r['operations']} |"
        )
    lines += [
        "",
        "The adversarial-attempt spend does not mean those checks succeeded: all 100 curated tasks remain blocked for full-method acceptance. Review found concrete defects in 26/128 exports. [CodeMidas results](codemidas.md#measured-local-campaign) give the solver and source breakdown. Token totals are not reconciled across every author and agent trace here, so no whole-campaign token figure is claimed.",
        "",
        "## FrontierSmith stage accounting",
        "",
        "All authoring, seed creation, reviews and sampled rollouts used `gpt-6-sol` in separate contexts. The 100-task selection cost includes the original ten-task pilot, original seed authoring, 153 candidate attempts, two post-construction generator repairs and collection review. It is not the cost of a single successful generation run.",
        "",
        "| Model stage | API cost |",
        "|---|---:|",
    ]
    for name, value in frontier["model_costs_by_stage"].items():
        lines.append(f"| {name.replace('_', ' ')} | {money(value)} |")
    lines += [
        "",
        "Add $3.76 estimated compute to $64.66 API usage for $68.42 accounted; keep $0.67 held separately. Baseline/sample generation and test infrastructure dominate the cost. All 100 are construction-checked; 24 final tasks had blind rollout attempts and 21 completed feasibly. None has full optimization-aware quality acceptance. References are sampled feasible programs, not proven optima. [Collection details](frontiersmith.md#measured-100-task-collection) retain those limits. A unified token total across author and Harbor-agent receipts is not claimed.",
        "",
        "## Historical native pipeline accounting",
        "",
        "The native datasets predate the campaign ledgers above. A fresh read of archived task metadata recovered the generation model for three 100-task cohorts. This names the recorded model; it does not recover missing token, retry or compute charges.",
        "",
        "| Native pipeline | Cohort metadata | Recorded synthesis API total |",
        "|---|---|---:|",
        *[
            f"| {r['pipeline']} | "
            + ", ".join(f"`{model}` on {n} tasks" for model, n in r["synthesis_models"].items())
            + f" | {money(next(p['recorded_synthesis_usd'] for p in data['native_history']['pipelines'] if p['pipeline'] == r['pipeline']), 6)} |"
            for r in audit["native_model_metadata"]
        ],
        "",
        "Code-instruct's total sums the last run-cumulative counter for each of five repositories. Equivalence tests include only productive-run counters, so its total is a lower bound. Commit-runtime has no complete synthesis ledger despite its model stamp. These partial amounts are excluded from the program subtotal to preserve its stated scope.",
        "",
        "PR-diff, PR-runtime and CVE-patch costs and generation-model coverage remain unavailable for their exact selected inventories. Do not infer an old run's model from today's example configuration. Historical solver samples include Sonnet 4.6, GPT-5.3-Codex and Qwen3.6-35B-A3B, on different tasks; their small model-cost samples and missing setup/compute charges are documented in [native results](native_results.md).",
        "",
        "## Evidence and refresh",
        "",
        "The [portable audit data](../data/experiment-economics.json) records SHA-256 fingerprints of the saved reports, ledgers and worker receipts, grouped costs and selected sanitized task metadata. Paths are relative evidence identifiers, not required checkout files. The [pipeline summary](../data/pipelines.json) supplies the published sample definitions and native history; the [FrontierSmith report](../data/frontiersmith-campaign.json) supplies the final collection identity.",
        "",
        "To refresh: identify the exact output cohort; freeze the source reports; exclude transfer envelopes and duplicate/revision rows; retain unknown-call holds; reconcile each API stage and resource group; then update the portable summaries. Do not sum run-cumulative per-task counters or multiply an old unit cost by a newer dataset size.",
        "",
        "The generator validates totals, per-model/stage sums, token coverage, source references and transfer exclusions before writing pages. A clean clone can rebuild the documentation using only the committed summaries:",
        "",
        "```bash",
        "python3 docs/_tools/generate_metrics.py",
        "python3 docs/_tools/generate_metrics.py --check",
        "```",
        "",
        "Token counts are reported input/output counts on recovered settled receipts, not a new price calculation. Cache fields are retained separately and must not be added twice. Uncertain requests have no usable token totals. The recipe expansion receipts used recorded LiteLLM estimates; other campaigns used their configured rate tables or agent-reported costs. None is presented as an invoice.",
        "",
    ]
    return with_title("\n".join(lines), nav_title="Experiment accounting")
