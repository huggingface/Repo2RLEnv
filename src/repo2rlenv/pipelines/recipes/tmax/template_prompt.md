You are an expert at creating {{domain_label}} tasks for AI agent training.

{{module}}{{v2_block}}

Universal Task Requirements:
- Challenging to solve: Requires domain knowledge, analytical thinking, and efficient implementation.
- Easy to verify: Success must be determinable by programmatically checking outputs, exit codes, or system state.
- Self-contained: All necessary information must be in the prompt.
- Realistic: The problem should resemble tasks professionals face in this domain.

Respond in XML format using these tags:

<task>
        A detailed task description written as a user would ask an AI assistant.
        Give the names of the precise contents of files, ports, directories, etc.
        This should be a very detailed description of the final state of the system.
        For example, if you are asking the agent to create a log file, you should
        precisely specify the format it should be in so that an automated test can
        verify it.
        Ask the agent to create a log file whenever some verification is required.
        You only have about 1000-1500 words to work with. So balance between
        conciseness and detail.
        DO NOT directly give the commands to the agent.
</task>

<truth>
        Insert *privileged* ground-truth data that automated test suites will
        rely on to verify correct task execution.
        These values **must NOT** appear in the public task description.

        Be very detailed here. Give the names / placeholders of the precise
        contents of files, ports, directories, repositories, websites etc.
        Any processes, files, directories that should be created before the task
        starts should be mentioned here.
        Any files that should be created by the agent and their contents should
        be mentioned here.

        Ground-truth principles (for accurate automated verification):
        * **Consistency:** Anything in *truth* that could be computed from the setup
          code, random seeds, or the task rules must actually follow from them. Do not
          assert derived numbers, digests, or file bodies unless they are implied by
          what you specified—avoid plausible-looking literals produced without that chain.
        * **Reproducibility:** Prefer stating *how* to obtain a golden value (procedure,
          formula, or a short **runnable** snippet that prints the canonical result) over
          pasting opaque constants that nothing in the pipeline verifies.
        * **Causal ordering:** When setup involves multiple steps (random draws, mutations,
          I/O), make the sequence explicit. Headline summaries (e.g. simple counts) must
          reflect the real order of operations, not an informal intuition.
        * **Single source of truth:** Setup scripts, narrative expectations, and any
          “expected output” blocks must agree; resolve contradictions before finishing.
</truth>

Critical Rules:
* No Leakage: Never include code that solves the task in the <task> description.
* Verification: Prioritize tasks with clear, programmatic verification.
* Originality: Tasks should require thought, not just copying standard tutorials.
* Complete Specification: Include all information needed to complete the task (file paths, formats, constraints).
* Place any secret, ground-truth verification data exclusively under <truth>.
* The agent will not have root access. Make sure the right permissions are set for files and directories.
* When you mention a file or directory, write the full path (not relative).
* We will be using apptainer to run the agent. Make sure the task is valid when the container is built.
* Don't create tasks that require having the latest information.
* The home path is /home/user.
* Don't create tasks the setup of which will require su access.
* The task is multi-turn, so the agent will interact in a terminal to finish the task.
* Don't discourage the agent from using console output to finish the task.
* Do not constrain the number of commands the agent may use.