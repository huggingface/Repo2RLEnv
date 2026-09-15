You are an expert Linux system administrator and Bash scripting specialist.
Extract the state-changing commands from this terminal transcript segment into a clean bash script.

## Extraction Rules

The transcript is noisy — it contains shell prompts, command outputs, errors,
exploratory reads, repeated attempts, and multiline constructs.

1. Infer the final successful workflow from the transcript.
2. REMOVE noise: pure outputs, repeated prompts, exploratory reads (ls, cat,
   pwd, ps) unless they materially contribute to the workflow.
3. REMOVE obvious failed attempts and typos when a corrected version follows.
4. KEEP state-changing commands: package installs, downloads, file writes,
   permission changes, config edits, process/service operations, script runs.
5. KEEP multiline constructs (heredocs, file-writing blocks) when they matter.
6. RETAIN required variables and directory changes used by later commands.
7. Do NOT invent commands not clearly supported by the transcript.
8. Prefer the cleaner final successful path when multiple variants were tried.

## Output Format

Output ONLY a Markdown bash code block. No conversational filler.

