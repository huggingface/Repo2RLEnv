// Every pipeline, grouped by the kind of task it produces. This drives the
// pipeline catalogue, the landing page and the summary strip on each pipeline
// page, so the three always agree. Facts come from docs/pipelines/*.md and
// src/repo2rlenv/pipelines/recipes/catalog.json; keep them in sync.

export type GroupId = 'repair' | 'implementation' | 'similarity' | 'terminal' | 'reasoning';

export type Pipeline = {
  /** What you pass on the command line. */
  id: string;
  /** Page slug under /pipelines/. */
  page: string;
  group: GroupId;
  kind: 'native' | 'agentic' | 'recipe';
  /** Pipeline family, for recipes (`--pipeline <family> --recipe <id>`). */
  family?: string;
  /** The published method a recipe adapts. */
  method?: string;
  shape: string;
  reward: string;
  runs: 'local' | 'remote';
  status: 'stable' | 'experimental';
  dataset?: { label: string; href: string };
  film?: string;
};

export const groups: { id: GroupId; title: string; summary: string }[] = [
  {
    id: 'repair',
    title: 'Repository repair',
    summary: 'Fix real code in a real repository. The repository’s own tests decide the reward.',
  },
  {
    id: 'implementation',
    title: 'Implementation and reconstruction',
    summary: 'Write or restore functionality, graded by hidden or differential tests.',
  },
  {
    id: 'similarity',
    title: 'Patch similarity',
    summary: 'Reproduce a real change. The patch is scored against the merged one, no test suite needed.',
  },
  {
    id: 'terminal',
    title: 'Terminal tasks',
    summary: 'Work in a shell to reach a state a verifier can check.',
  },
  {
    id: 'reasoning',
    title: 'Reasoning and optimization',
    summary: 'Problems without a repository: exact answers, or open objectives with graded scores.',
  },
];

const hub = (repo: string) => `https://huggingface.co/datasets/${repo}`;

export const pipelines: Pipeline[] = [
  // ---- repository repair ----
  {
    id: 'tasksmith',
    page: 'tasksmith',
    group: 'repair',
    kind: 'agentic',
    shape: 'An agent turns one merged PR into a verified environment, then reviews and repairs it',
    reward: 'Private tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '50 verified tasks', href: hub('HuggingEnvs/HF_ML_Tasksmith') },
  },
  {
    id: 'pr_runtime',
    page: 'pr_runtime',
    group: 'repair',
    kind: 'native',
    shape: 'Fix the issue a merged PR fixed; the PR’s own tests stay hidden',
    reward: 'f2p_rate × p2p_rate',
    runs: 'local',
    status: 'stable',
    dataset: { label: '100 tasks', href: hub('AdithyaSK/repo2rlenv-pr-runtime') },
    film: 'pr-runtime',
  },
  {
    id: 'commit_runtime',
    page: 'commit_runtime',
    group: 'repair',
    kind: 'native',
    shape: 'Fix the bug a commit fixed, from an LLM-written symptom report',
    reward: 'f2p_rate × p2p_rate',
    runs: 'local',
    status: 'stable',
    dataset: { label: '100 tasks', href: hub('AdithyaSK/repo2rlenv-commit-runtime') },
    film: 'commit-runtime',
  },
  {
    id: 'cve_patches',
    page: 'cve_patches',
    group: 'repair',
    kind: 'native',
    shape: 'Patch a published vulnerability from its stripped advisory',
    reward: 'f2p_rate × p2p_rate',
    runs: 'local',
    status: 'experimental',
    dataset: { label: '19 tasks', href: hub('AdithyaSK/repo2rlenv-cve-patches') },
    film: 'cve-patches',
  },
  {
    id: 'swe_smith',
    page: 'repo_mutate',
    group: 'repair',
    kind: 'recipe',
    family: 'repo_mutate',
    method: 'SWE-smith',
    shape: 'Fix a seeded single-site defect in a healthy Python repository',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-swe-smith') },
  },
  {
    id: 'swe_next',
    page: 'swe_next',
    group: 'repair',
    kind: 'recipe',
    family: 'pr_runtime',
    method: 'SWE-Next',
    shape: 'Repair a historical change mined from merged PRs',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-swe-next') },
  },
  {
    id: 'r2e_gym',
    page: 'r2e_gym',
    group: 'repair',
    kind: 'recipe',
    family: 'commit_runtime',
    method: 'R2E-Gym',
    shape: 'Repair a historical change mined from first-parent commits',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-r2e-gym') },
  },
  // ---- implementation and reconstruction ----
  {
    id: 'code_instruct',
    page: 'code_instruct',
    group: 'implementation',
    kind: 'native',
    shape: 'Write a module for an LLM-authored problem anchored in the repository’s API',
    reward: 'Hidden test passes (0/1)',
    runs: 'local',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('AdithyaSK/repo2rlenv-code-instruct') },
    film: 'code-instruct',
  },
  {
    id: 'equivalence_tests',
    page: 'equivalence_tests',
    group: 'implementation',
    kind: 'native',
    shape: 'Reimplement a stubbed real function to match its reference',
    reward: 'Hidden test passes (0/1)',
    runs: 'local',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('AdithyaSK/repo2rlenv-equivalence-tests') },
    film: 'equivalence-tests',
  },
  {
    id: 'r2e',
    page: 'r2e',
    group: 'implementation',
    kind: 'recipe',
    family: 'equivalence_tests',
    method: 'R2E',
    shape: 'Implement a documented function against differential tests',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-r2e') },
  },
  {
    id: 'swe_flow',
    page: 'repo_reconstruct',
    group: 'implementation',
    kind: 'recipe',
    family: 'repo_reconstruct',
    method: 'SWE-Flow',
    shape: 'Reimplement functions removed from a working Python repository',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-swe-flow') },
  },
  {
    id: 'swe_gen',
    page: 'pr_to_env',
    group: 'implementation',
    kind: 'recipe',
    family: 'pr_to_env',
    method: 'SWE-gen',
    shape: 'Restore a merged PR’s source change, from explicit PR URLs',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-swe-gen') },
  },
  {
    id: 'codemidas',
    page: 'codemidas',
    group: 'implementation',
    kind: 'recipe',
    family: 'repo_reconstruct',
    method: 'CodeMidas',
    shape: 'Rebuild a removed feature from its written behavioral contract',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
  },
  // ---- patch similarity ----
  {
    id: 'pr_diff',
    page: 'pr_diff',
    group: 'similarity',
    kind: 'native',
    shape: 'Reproduce a merged PR’s fix from its description',
    reward: 'Diff similarity in [0, 1]',
    runs: 'local',
    status: 'stable',
    dataset: { label: '181 tasks', href: hub('AdithyaSK/repo2rlenv-pr-diff') },
    film: 'pr-diff',
  },
  // ---- terminal tasks ----
  {
    id: 'seta_seed2synth',
    page: 'terminal_synth',
    group: 'terminal',
    kind: 'recipe',
    family: 'terminal_synth',
    method: 'SETA Seed2Synth',
    shape: 'Terminal task synthesized from a question-and-answer seed',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-seta-seed2synth') },
  },
  {
    id: 'seta_evol',
    page: 'task_evolve',
    group: 'terminal',
    kind: 'recipe',
    family: 'task_evolve',
    method: 'SETA Evol',
    shape: 'Evolved variant of an existing Harbor task',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-seta-evol') },
  },
  {
    id: 'dataarc',
    page: 'dataarc',
    group: 'terminal',
    kind: 'recipe',
    family: 'terminal_synth',
    method: 'DataArc',
    shape: 'Augmented variant of a Harbor seed task',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-dataarc') },
  },
  {
    id: 'tmax',
    page: 'tmax',
    group: 'terminal',
    kind: 'recipe',
    family: 'terminal_synth',
    method: 'TMax',
    shape: 'Terminal task combining three to five sampled skills',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '55 tasks', href: hub('HuggingEnvs/repo2rlenv-tmax') },
  },
  {
    id: 'endless_terminals',
    page: 'endless_terminals',
    group: 'terminal',
    kind: 'recipe',
    family: 'terminal_synth',
    method: 'Endless Terminals',
    shape: 'Terminal task from a sampled category, complexity and scenario',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-endless-terminals') },
  },
  {
    id: 'terminalworld',
    page: 'terminalworld',
    group: 'terminal',
    kind: 'recipe',
    family: 'terminal_reconstruct',
    method: 'TerminalWorld',
    shape: 'Task reconstructed from a real terminal recording',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-terminalworld') },
  },
  {
    id: 'cli_gym',
    page: 'env_repair',
    group: 'terminal',
    kind: 'recipe',
    family: 'env_repair',
    method: 'CLI-Gym',
    shape: 'Repair a deliberately broken development environment',
    reward: 'Tests pass (0/1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '25 tasks', href: hub('HuggingEnvs/repo2rlenv-cli-gym') },
  },
  // ---- reasoning and optimization ----
  {
    id: 'scaler',
    page: 'scaler',
    group: 'reasoning',
    kind: 'recipe',
    family: 'reasoning_synth',
    method: 'SCALER',
    shape: 'Reasoning problem generated from a parameterized problem family',
    reward: 'Answer match (−1/+1)',
    runs: 'remote',
    status: 'experimental',
    dataset: { label: '100 tasks', href: hub('HuggingEnvs/repo2rlenv-scaler') },
  },
  {
    id: 'frontiersmith',
    page: 'frontiersmith',
    group: 'reasoning',
    kind: 'recipe',
    family: 'optimization_synth',
    method: 'FrontierSmith',
    shape: 'Improve an algorithm against deterministic graded objectives',
    reward: 'Continuous score in [0, 1]',
    runs: 'remote',
    status: 'experimental',
  },
];

export const byPage = new Map(pipelines.map((p) => [p.page, p]));

export const inGroup = (group: GroupId) => pipelines.filter((p) => p.group === group);

export const kindLabel: Record<Pipeline['kind'], string> = {
  native: 'Native',
  agentic: 'Agentic',
  recipe: 'Research recipe',
};

/** How you invoke it, for display. */
export function invocation(p: Pipeline): string {
  if (p.kind === 'agentic') return 'repo2rlenv tasksmith run';
  if (p.kind === 'recipe') return `--pipeline ${p.family} --recipe ${p.id}`;
  return `--pipeline ${p.id}`;
}
