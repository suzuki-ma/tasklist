# Tasklist project guidance

- When the user asks to list, add, complete, or reopen personal tasks, use `codex_task_client.py` and the local Codex API.
- Never edit `data/tasks.csv` directly for Codex task operations.
- Use `python codex_task_client.py list --status open` to inspect open tasks before acting on an ambiguous task.
- After changing application code, run a Python syntax check and test the affected route with isolated task data.
