## INCREASE_DIFFICULTY_AND_CHANGE_CONTEXT Strategy

Your goal is to **simultaneously port the task to a different domain/technology AND make it harder** — combining both evolution dimensions in one step.

### What this means:

You must apply BOTH of the following transformations at once:

#### 1. Change Context (port to a different technology/domain)
- Swap the core technology while keeping the same type of reasoning (e.g., nginx → apache2, Python → Bash, PostgreSQL → MySQL)
- Port to a related but different domain (e.g., web server config → reverse proxy config, file parsing → log analysis)
- The new domain must be realistic and have deterministic, testable outcomes
- Don't pick obscure or unmaintained technologies

#### 2. Increase Difficulty (make it harder)
- **More steps**: Add additional subtasks the agent must complete (target ≥5 distinct non-trivial steps)
- **Tighter constraints**: Add edge cases, error handling, validation, or rollback requirements
- **Larger scale**: Scale up the problem (more files, more services, more data)
- **Failure modes**: Require the agent to handle errors gracefully (e.g., retry logic, idempotent operations)
- **Deeper domain knowledge**: Require understanding of more advanced features of the new technology

### Examples of combined evolution:
- nginx reverse-proxy config → apache2 reverse-proxy with TLS termination, rate limiting, and health checks
- Python CSV parsing script → Bash awk/sed solution with error handling, streaming for large files, and validation
- Docker single-container setup → Podman pod with multiple containers, shared volumes, and health checks
- systemd service config → OpenRC service with dependency ordering, failure recovery, and log rotation
- PostgreSQL schema migration → MySQL schema migration with rollback support, data validation, and foreign key constraints

### What NOT to do:
- Don't only change the domain without making it harder — that's just CHANGE_CONTEXT
- Don't only make it harder in the same domain — that's just INCREASE_DIFFICULTY
- Don't just add more of the same thing (e.g., "copy 10 files instead of 3" is not harder, just more)
- Don't add artificial constraints that aren't realistic
- Don't just rename files or change strings — the domain change should require genuinely different commands and knowledge

### Long-horizon filter:
The evolved task MUST require ≥5 distinct non-trivial steps. If the combined evolution would be too simple, mark it FILTERED.

### Diversity across variants:
If you have multiple variant slots, vary both dimensions: different target technologies AND different difficulty increases. Don't repeat the same combination.

### Research:
Use WebSearch/WebFetch to look up the target technology's documentation. Include URLs and relevant config examples in the draft spec so the datapoint agent can build a correct environment and tests.
