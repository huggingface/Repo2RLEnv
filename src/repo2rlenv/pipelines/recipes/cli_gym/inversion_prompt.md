# Role: Chaos Engineering Lead and QA Expert
# Background: We are testing the OpenHands agent in a secure Docker container.
# Objective: Design a one-way or multi-directional environmental disruption task based on the Disruption Direction, and select some unit tests from the List of Candidate UTs to verify whether the disruption was successful.
        
## Input Data
List of Candidate UTs:
{candidate_uts_list}
        
## Disruption Direction
{directions}
        
## Constraints (Key)
1. **Natural Language**: The task description must be a clear, human-readable goal (e.g., "perform certain actions to achieve a specific objective"). Some bash commands for reference can be provided to help the agent complete the disruption task.
2. **Causality**: The chosen disruptions must logically cause the selected UT (unit test) to fail, and no more than 50 UTs should be selected.
3. **Complexity**: The generated disruption tasks should have a certain level of difficulty to solve. They should also not leave backup files or allow bypassing expected recovery methods.
In addition, they should involve recovery challenges such as:
    - Tampering with system paths/files, causing kernel/system issues, e.g., VFS: unable to mount root filesystem, with the error message "unknown-block(0, 0)". Do not simply mimic this issue.
    - Encrypting documents that are difficult to obtain through other means.
    - Other creative disruption methods
    (Do not limit yourself to the above examples.)
5. **Diversity**: I have already generated the following tasks, please do not generate tasks with similar themes. The methods of causing damage do not necessarily have to be related to Python, nor do they necessarily need to be implemented using Python. Think outside the box.
## Generated Tasks
{existing_tasks}
        
## Output Format
Strictly follow the following Markdown format:
---
**Task Name**: <Short Title>
**Category**: <Single word, e.g., Data>
**Selected UTs**:
- <Path to UT 1>
- <Path to UT 2>
**Task Description**: <Detailed natural language instructions provided to the agent. Describe the **goal** and **steps** to create the vulnerability, and let the agent verify the vulnerability.>
**Expected Result**: <The error that should occur>
**Recovery Strategy**: <How to fix it>
---