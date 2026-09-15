## DECREASE_DIFFICULTY Strategy

Your goal is to create an **easier** version of the input task within the same domain.

### What "easier" means:
- **Fewer steps**: Remove or combine subtasks so the agent has less to do
- **Pre-seeded environment**: Provide starter files, partial configs, or scaffolding so the agent doesn't start from scratch
- **Reduced scope**: Focus on one core aspect of the original task instead of all of them
- **Relaxed constraints**: Remove edge cases, error handling requirements, or multi-service coordination
- **Simpler tooling**: Use a simpler subset of the same technology (e.g., basic config instead of advanced features)

### Examples of difficulty decreases:
- A multi-service orchestration task → single-service config with health check
- An nginx task with TLS + rate limiting + upstream health checks → basic nginx reverse-proxy setup
- A Python script with CLI parsing, logging, and error handling → the same script with just the core logic
- A disk-wipe task with multiple sanitization standards → single-pass wipe with basic verification
- A multi-user sudo policy task → single-user sudo rule

### How to simplify well:
- **Keep the domain**: The task stays in the same technology — don't change to a different tool
- **Keep it non-trivial**: The simplified task should still require understanding and multiple commands — not a single copy-paste
- **Pre-seed wisely**: Provide starter files that show the structure but leave the key parts for the agent to fill in
- **Preserve testability**: The simplified task must still have clear, deterministic, verifiable outcomes

### What NOT to do:
- Don't make it trivial (a single command or config edit is too simple)
- Don't change the domain/technology — that's CHANGE_CONTEXT
- Don't just remove tests — reduce the task scope instead
- Don't add hints to the instruction — that's INCLUDE_HINT

### Long-horizon filter:
DECREASE_DIFFICULTY variants are **exempt** from the ≥5 step requirement since they intentionally lower complexity. However, the task must still require ≥2 distinct non-trivial steps. If the simplified version is a single-command task, mark it FILTERED.

### Diversity across variants:
If you have multiple variant slots, simplify in different dimensions (e.g., one removes multi-service coordination, another pre-seeds config files, another reduces scale).
