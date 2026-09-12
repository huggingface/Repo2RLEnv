
As you are trying to generate synthetic issues, you will follow these guidelines

1. Keep the issue concise and informative.
2. Describe the failing test, including the input that causes the failure, the nature of the failure, and the expected behavior. Do NOT mention test functions or files directly. Do NOT mention pytest, hypothesis, or other testing frameworks.
3. Do not reveal the solution to the problem in the issue. Only describe the bug and the expected behavior.
4. If there are multiple failing tests, focus on the most informative one or a subset that best describes the general nature of the failure.
5. Describe the expected output of the failing test:
   - For errors, describe the error message.
   - For failing tests, mention what is supposed to happen. If the expected output is large and complex, describe the difference between the current and expected output instead of directly copying it (as human might do). Do NOT use assert statment is issue text, you are not writing test cases. 
6. Write the issue as a human would, using simple language without excessive formatting.
7. Use concrete terms to describe the nature of the failure. Avoid vague terms like "specific output" or "certain data".
8. INCLUDE test code to describe the bug but keep it brief and relevant. Truncate or simplify tests longer than 5-6 lines.
9. Do not mention external files unless absolutely necessary.
10. Format code snippets using triple backticks (```).

Before drafting the issue, analyze the following 
- Identify and quote key parts of the commit details and test results.
- What is the main problem highlighted by the test results?
- What is the expected behavior?
- What is the actual behavior or error message?
- How can you describe the issue concisely while providing enough information for developers to understand and investigate?
- Envision yourself as a human stumbling upon this bug. Provide the bug report from that perspective. Focus on clarity and naturalness in your writing.

After your analysis, draft the GitHub issue enclosed in [ISSUE] [/ISSUE] tags. The issue should include:
1. A clear and concise title (choose the best one from your brainstormed list)
2. A description of the problem 
    2.1 ensure adding a detailed example buggy code with sufficient explaintation
    2.2 ensure the example buggy code is natural, it should resemble a unittest, it should not have assertions 
    2.3 add details about the test scaffolding if necessary
3. Expected behavior
4. Actual behavior or error message

IMPORTANT: Strictly follow the above guidelines and use the provided test execution results to write the issue. Draw inspiration from the examples provided and make sure to provide good concise and natural issues. Remember to write the issue as a human would, focusing on clarity and relevance. For naturalness, envi

