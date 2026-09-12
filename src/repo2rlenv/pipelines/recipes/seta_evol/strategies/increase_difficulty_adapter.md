## INCREASE_DIFFICULTY Strategy

Your goal is to create a **harder** version of the input task within the same domain.

### What "harder" means:
- **More steps**: Add additional subtasks the agent must complete (target ≥5 distinct non-trivial steps)
- **Tighter constraints**: Add edge cases, error handling, validation, or rollback requirements
- **Larger scale**: Scale up the problem (more files, more services, more data)
- **Failure modes**: Require the agent to handle errors gracefully (e.g., retry logic, idempotent operations)
- **Deeper domain knowledge**: Require understanding of more advanced features of the same technology

### Examples of difficulty increases:
- A file-copy task → file-copy with integrity checks, atomic operations, and rollback on failure
- A single-service config task → multi-service orchestration with health checks and dependency ordering
- An nginx config task → nginx with TLS, rate limiting, upstream health checks, and custom error pages
- A basic Python script → the same script with proper logging, CLI argument parsing, error handling, and config file support

### What NOT to do:
- Don't just add more of the same thing (e.g., "copy 10 files instead of 3" is not harder, just more)
- Don't change the domain/technology — that's CHANGE_CONTEXT
- Don't add artificial constraints that aren't realistic (e.g., "do it in exactly 4 commands")

### Long-horizon filter:
The evolved task MUST require ≥5 distinct non-trivial steps. If your idea would be too simple after evolution, mark it FILTERED.

### Diversity across variants:
If you have multiple variant slots, make each one harder in a different dimension (e.g., one adds error handling, another adds scale, another adds multi-service coordination). Don't repeat the same type of difficulty increase.
