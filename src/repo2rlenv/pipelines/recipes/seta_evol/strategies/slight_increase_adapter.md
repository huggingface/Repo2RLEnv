## SLIGHT_INCREASE Strategy

Your goal is to create a **slightly harder** version of the input task — a **bounded** difficulty increase within the same domain.

### Context: this task is currently EASY

An 8B LLM agent currently solves this task >50% of the time. You need to add **just enough** complexity that the agent can still partially solve it (~20–40% of runs should fully pass). Do NOT make it so hard that the agent scores 0%.

### What "slightly harder" means — pick ONE dimension:

1. **One additional validation step**: e.g., add a check that the output format is correct, or that edge cases are handled
2. **One tricky edge case**: e.g., handle missing values, empty inputs, Unicode, or malformed data
3. **One extra requirement**: e.g., add a summary statistic, an additional output file, or a constraint on performance
4. **Slightly stricter tests**: e.g., test for exact numeric precision, specific column ordering, or a threshold that requires tuning

### Examples of slight increases:
- A CSV analysis task that produces 3 output files → same task but also produce a correlation matrix and handle missing values explicitly
- A classification pipeline → same pipeline but require cross-validation reporting with specific format
- A script that processes one file → same script but handle a directory of files with error logging

### What NOT to do:
- Don't add 3+ new requirements at once — pick ONE dimension to increase
- Don't restructure the entire task or change its core goal
- Don't make it require entirely new technologies or libraries
- Don't change the domain — that's CHANGE_CONTEXT
- Don't make it so hard that an agent can't make any progress (target partial success, not zero)
- Don't just add more of the same thing (e.g., "10 models instead of 4" is not harder, just more work)

### Calibration:
Think of it as going from "straightforward homework" to "homework with one tricky bonus question." The core task stays the same; one aspect becomes more demanding.

### Long-horizon filter:
The evolved task MUST require ≥5 distinct non-trivial steps. If it would be too simple, mark it FILTERED.
