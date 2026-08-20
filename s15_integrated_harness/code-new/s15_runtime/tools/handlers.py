from ..collaboration.messaging import BUS, active_teammates, team_lock
from ..collaboration.teammates import spawn_teammate_thread
from ..integrations.mcp import connect_mcp
from ..workspace.tasks import (
    claim_task,
    complete_task,
    create_task,
    get_task_json,
    list_tasks,
    update_task,
)
from ..workspace.worktrees import create_worktree

# -- Lead Worktree Tools --

def run_create_worktree(name: str, task_id: str) -> str:
    return create_worktree(name, task_id)

# -- Basic Tool Handlers --

def run_create_task(subject: str, description: str = "") -> str:
    task = create_task(subject, description)
    print(f"  \033[34m[create] {task.subject}\033[0m")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    try:
        task = update_task(task_id, addBlockedBy)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  \033[34m[update] {task.subject} blockedBy: {dependencies}\033[0m")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    try:
        return get_task_json(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_spawn_teammate(name: str, role: str, prompt: str,
                       task_id: str | None = None,
                       require_plan: bool = False) -> str:
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    with team_lock:
        if not active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}"
            for name, status in sorted(active_teammates.items())
        )


def run_send_message(to: str, content: str) -> str:
    if to not in active_teammates:
        return f"Teammate '{to}' is not active"
    BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


__all__ = (
    "run_create_worktree",
    "run_create_task",
    "run_update_task",
    "run_list_tasks",
    "run_get_task",
    "run_claim_task",
    "run_complete_task",
    "run_spawn_teammate",
    "run_list_teammates",
    "run_send_message",
    "run_connect_mcp",
)
