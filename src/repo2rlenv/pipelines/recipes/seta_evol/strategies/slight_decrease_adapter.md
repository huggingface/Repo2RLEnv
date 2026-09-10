## SLIGHT_DECREASE Strategy

Your goal is to create a **slightly easier** version of the input task — a **bounded** difficulty decrease within the same domain.

### Context: this task is currently TOO HARD

An 8B LLM agent currently scores 0% on this task (no tests pass across all runs). You need to remove **just enough** complexity that the agent can make partial progress (some tests should pass, but not necessarily all). Do NOT make it trivially easy.

### What "slightly easier" means — pick ONE dimension:

1. **Make instructions more explicit**: spell out exact filenames, column names, output formats, or paths that the agent currently has to guess
2. **Pre-seed one piece of scaffolding**: provide a starter config, directory structure, or template file so the agent doesn't start from scratch
3. **Simplify one test**: relax one overly strict assertion (e.g., exact float precision → within tolerance, exact string match → substring match)
4. **Remove one secondary requirement**: if the task has 6+ requirements, drop the least essential one (e.g., remove the "also generate a visualization" requirement)
5. **Reduce scale**: if the task operates on many items, reduce to fewer while keeping the logic the same

### Examples of slight decreases:
- A task requiring 4 output files where none are produced → make instructions explicitly list each filename and expected columns
- A task with strict numeric assertions that all fail → relax tolerance from exact match to ±1% or round to 2 decimals
- A task where the agent can't figure out the data format → pre-seed an example or schema file in the environment
- A multi-model comparison with 6 requirements → keep 5, drop the "also plot learning curves" requirement

### What NOT to do:
- Don't gut the entire task — keep the core challenge intact
- Don't reduce to fewer than 3 distinct steps
- Don't provide the solution or near-solution in the environment
- Don't change the domain — that's CHANGE_CONTEXT
- Don't make it so easy that an agent solves it 100% of the time (target partial success)
- Don't just remove tests without simplifying the underlying requirement

### Calibration:
Think of it as going from "impossible exam question" to "hard but fair exam question." The core challenge stays the same; one barrier to entry is lowered.

### Long-horizon filter:
SLIGHT_DECREASE variants are **exempt** from the ≥5 step requirement. However, the task must still require ≥3 distinct non-trivial steps. If the simplified version would be trivial, mark it FILTERED.
