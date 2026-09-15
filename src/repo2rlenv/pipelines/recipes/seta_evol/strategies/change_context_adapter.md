## CHANGE_CONTEXT Strategy

Your goal is to **port the task to a different domain or technology** while preserving similar structural complexity.

### What "change context" means:
- Swap the core technology while keeping the same type of reasoning (e.g., nginx → apache2, Python → Bash, PostgreSQL → MySQL)
- Port to a related but different domain (e.g., web server config → reverse proxy config, file parsing → log analysis)
- Preserve the number of steps and difficulty level — the new task should be roughly as hard as the original
- The new domain must be realistic and have deterministic, testable outcomes

### Examples of context changes:
- nginx reverse-proxy config → apache2 reverse-proxy config (same structure, different syntax)
- Python CSV parsing script → equivalent Bash awk/sed solution
- Docker single-container setup → Podman equivalent
- systemd service configuration → OpenRC service configuration
- PostgreSQL schema migration → MySQL schema migration

### Key rules:
- **Preserve structural complexity**: Same number of distinct steps, similar depth of domain knowledge required
- **Realistic domain**: The new technology/domain must be real, well-documented, and commonly used
- **Testable outcomes**: The evolved task must have deterministic, verifiable results
- **Different enough**: Don't just change minor syntax — the technology should genuinely differ (e.g., different config format, different commands, different paradigm)

### What NOT to do:
- Don't make it harder or easier — that's INCREASE_DIFFICULTY or DECREASE_DIFFICULTY
- Don't just rename files or change strings — the domain change should require genuinely different commands and knowledge
- Don't pick obscure or unmaintained technologies that would be hard to test

### Long-horizon filter:
The evolved task MUST require ≥5 distinct non-trivial steps. If the ported task would be too simple in the new domain, mark it FILTERED.

### Research:
Use WebSearch/WebFetch to look up the target technology's documentation. Include URLs and relevant config examples in the draft spec so the datapoint agent can build a correct environment and tests.
