You are creating realistic Linux-terminal tasks for training an AI agent.

Respond in xml format.

<task>
        Be detailed here. Give the names of the precise contents of files, ports, directories, etc.
        This should be a very detailed description of the final state of the system.
        For example, if you are asking the agent to create a log file, you should precisely specify the format it should be in so that an automated test can verify it.
        Ask the agent to create a log file whenever some verification is required.
        You only have about 1000-1500 words to work with. So balance between conciseness and detail.
        DO NOT directly give the commands to the agent.
</task>

<truth>
        Insert *privileged* ground-truth data that automated
        test suites will rely on to verify correct task execution.
        These values **must NOT** appear in the public task
        description.

        Be very detailed here. Give the names / placeholders of the precise contents of files, ports, directories, repositories, websites etc.
        This should be a very detailed description of the final state of the system.
        For example, if you are asking the agent to create a log file, you should give the name of the log file and the contents of the log file.
        Any processes, files, directories that should be created before the task starts should be mentioned here.
        Any files that should be created by the agent and their contents should be mentioned here.
</truth>

Guidelines:
* Place any secret, ground-truth verification data exclusively under the <truth> element.
* The agent should be able to write to the file and directory that are mentioned in the task description.
* The agent will not have root access. So make sure that the right permissions are set for the files and directories.
* When you mention a file or directory, write the full path to the file or directory, not just relative path.
* The task must be a realistic end-to-end scenario that an AI agent could perform in a Linux terminal. 
* Write the task description in a way that a user might ask an AI assistant.
* Be very specific about the names, paths and contents of the files and directories.
* We will be using apptainer to run the agent. So make sure that the task is valid when the container is built.
* Don't create tasks that require having the latest information.
* The home path is /home/user.
* Don't create tasks the setup of which will require su access.
* The task is multi-turn, so the agent will interact in a terminal to finish the task.
* Don't discourage the agent from using console output to finish the task.
* Do not put a constraint on the number of commands that the agent can use (the complixity that the user provides is for the complexity of the task description).
