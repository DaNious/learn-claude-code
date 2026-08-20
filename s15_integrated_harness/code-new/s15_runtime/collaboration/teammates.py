from ..foundation.bootstrap import MODEL, Path, client, threading
from ..tools.basic import call_tool_handler, run_bash, run_edit, run_glob, run_read, run_write
from ..workspace.tasks import (
    Task,
    _owner_in_progress,
    assignment_versions,
    can_start,
    claim_task,
    complete_task,
    list_tasks,
    load_task,
    task_lock,
    teammate_assignments,
)
from ..workspace.worktrees import (
    assignment_cwd,
    release_completed_assignment,
    release_teammate_assignment,
    task_worktree_cwd,
)
from .messaging import (
    BUS,
    RESERVED_TEAMMATE_NAMES,
    ProtocolState,
    active_teammates,
    is_valid_agent_name,
    new_request_id,
    pending_requests,
    plan_gates,
    plan_request_ids,
    team_lock,
)

# -- Team Task Assignment --

IDLE_SCAN_INTERVAL = 2.0


def scan_unclaimed_tasks() -> list[Task]:
    """Return ready tasks whose optional worktree binding is usable."""
    with task_lock:
        ready = []
        for task in list_tasks():
            if (task.status != "pending" or task.owner is not None
                    or not can_start(task.id)):
                continue
            _, error = task_worktree_cwd(task)
            if not error:
                ready.append(task)
        return ready


def claim_next_task(name: str) -> Task | None:
    """Claim the first still-available task, never a second assignment."""
    with task_lock:
        if teammate_assignments.get(name) or _owner_in_progress(name):
            return None
    for task in scan_unclaimed_tasks():
        result = claim_task(task.id, owner=name)
        if result.startswith("Claimed "):
            return load_task(task.id)
    return None


def _last_assistant_text(content) -> str:
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


def current_work_identity(owner: str) -> tuple[int, str | None]:
    with task_lock:
        assignment = teammate_assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return assignment_versions.get(owner, 0), task_id


def _run_teammate_tool(name: str, block, handlers: dict) -> str:
    from ..runtime.hooks import trigger_hooks

    gate = plan_gates.get(name, "not_required")
    if (block.name in {"bash", "write_file", "edit_file"}
            and gate not in {"not_required", "approved"}):
        return f"Blocked: plan status is {gate}."
    blocked = trigger_hooks("PreToolUse", block)
    if blocked is not None:
        return str(blocked)
    handler = handlers.get(block.name)
    output = call_tool_handler(handler, block.input, block.name)
    trigger_hooks("PostToolUse", block, output)
    return str(output)


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """Apply only the Lead response for this teammate's current plan."""
    metadata = msg.get("metadata", {})
    request_id = metadata.get("request_id", "")
    work_version, task_id = current_work_identity(name)
    with team_lock:
        state = pending_requests.get(request_id)
        expected_id = plan_request_ids.get(name)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and request_id == expected_id
            and state is not None
            and state.type == "plan_approval"
            and state.sender == name
            and state.target == "lead"
            and state.work_version == work_version
            and state.task_id == task_id
            and state.status in {"approved", "rejected"}
            and metadata.get("approve", False)
            == (state.status == "approved")
        )
        if not valid:
            return False, "[Ignored plan response: request mismatch]"
        assert state is not None
        plan_gates[name] = state.status
        active_teammates[name] = "working"
        plan_request_ids.pop(name, None)
        outcome = state.status
    return True, f"[Plan {outcome}] {msg['content']}"


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """Accept only a pending shutdown request sent by Lead to this teammate."""
    request_id = msg.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    with team_lock:
        if to != "lead" and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"


# -- Teammate Thread --

def spawn_teammate_thread(name: str, role: str, prompt: str,
                          task_id: str | None = None,
                          require_plan: bool = False) -> str:
    if not is_valid_agent_name(name):
        return ("Invalid teammate name: use 1-64 letters, digits, "
                "underscores, or dashes")
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if any(existing.casefold() == name.casefold()
               for existing in active_teammates):
            return f"Teammate '{name}' already exists"
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
        assignment_versions[name] = 0

    if task_id:
        try:
            claimed = claim_task(task_id, owner=name)
        except (FileNotFoundError, ValueError) as exc:
            claimed = f"Error: {exc}"
        if not claimed.startswith("Claimed "):
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"

    system = (f"You are '{name}', a {role}. "
              "Use tools to complete tasks. "
              "You can list and claim tasks from the board. If the initial "
              "message contains [Assigned task], it is already claimed; do not "
              "call claim_task for it again. "
              "The runtime runs every filesystem tool in the claimed task's "
              "working directory. When asked for a plan, submit it before "
              "bash, write_file, or edit_file and wait for approval. The runtime "
              "delivers your final text to Lead. Use send_message only for "
              "intermediate coordination, and address the coordinator as 'lead'.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        if msg_type == "shutdown_request":
            accepted, notice = apply_shutdown_request(name, msg)
            if not accepted:
                messages.append({"role": "user", "content": notice})
                return False
            req_id = notice
            BUS.send(name, "lead", "Shutting down gracefully.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "
                  f"({req_id})\033[0m")
            return True

        if msg_type == "plan_approval_response":
            _, notice = apply_plan_response(name, msg)
            messages.append({"role": "user",
                "content": notice})
        elif msg_type == "plan_request":
            messages.append({"role": "user",
                "content": f"[Plan required] {msg['content']}"})
        elif msg_type == "message":
            messages.append({"role": "user",
                "content": f"[Message from {msg['from']}] {msg['content']}"})
        return False

    def run_loop():
        def current_cwd() -> tuple[Path | None, str | None]:
            if name not in teammate_assignments:
                return None, "Error: Claim a Task before using workspace tools."
            try:
                return assignment_cwd(name), None
            except (FileNotFoundError, ValueError) as exc:
                return None, f"Error: Invalid task assignment: {exc}"

        def _run_bash(command: str) -> str:
            cwd, error = current_cwd()
            return error or run_bash(command, cwd=cwd)

        def _run_read(path: str, limit: int | None = None,
                      offset: int = 0) -> str:
            cwd, error = current_cwd()
            return error or run_read(path, limit=limit, offset=offset, cwd=cwd)

        def _run_write(path: str, content: str) -> str:
            cwd, error = current_cwd()
            return error or run_write(path, content, cwd=cwd)

        def _run_edit(path: str, old_text: str, new_text: str) -> str:
            cwd, error = current_cwd()
            return error or run_edit(path, old_text, new_text, cwd=cwd)

        def _run_glob(pattern: str) -> str:
            cwd, error = current_cwd()
            return error or run_glob(pattern, cwd=cwd)

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            try:
                return claim_task(task_id, owner=name)
            except ValueError as exc:
                return f"Error: {exc}"
            except FileNotFoundError:
                return f"Error: Task {task_id} not found"

        def _run_complete_task(task_id: str):
            try:
                return complete_task(task_id, owner=name)
            except ValueError as exc:
                return f"Error: {exc}"
            except FileNotFoundError:
                return f"Error: Task {task_id} not found"

        initial_prompt = prompt
        if task_id:
            task = load_task(task_id)
            initial_prompt += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {assignment_cwd(name)}"
            )
        if require_plan:
            initial_prompt += ("\n\n[Plan required] Submit a plan and wait for "
                               "Lead approval before bash, write_file, or edit_file.")
        messages: list[dict] = [{"role": "user", "content": initial_prompt}]
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "path": {"type": "string"},
                                  "limit": {"type": "integer"},
                                  "offset": {"type": "integer"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace text in a file.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "path": {"type": "string"},
                                  "old_text": {"type": "string"},
                                  "new_text": {"type": "string"}},
                              "required": ["path", "old_text", "new_text"]}},
            {"name": "glob", "description": "Find files by glob pattern.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "pattern": {"type": "string"}},
                              "required": ["pattern"]}},
            {"name": "send_message",
             "description": "Send an intermediate message to 'lead' or an active teammate.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks on the board.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write, "edit_file": _run_edit,
            "glob": _run_glob,
            "send_message": lambda to, content: _teammate_send_message(
                name, to, content),
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        should_stop = False
        while not should_stop:
            for msg in BUS.read_inbox(name):
                if handle_inbox_message(name, msg, messages):
                    should_stop = True
                    break
            if should_stop:
                break
            with team_lock:
                active_teammates[name] = "working"
            try:
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages,
                    tools=sub_tools, max_tokens=8000)
            except Exception as exc:
                BUS.send(name, "lead",
                         f"{type(exc).__name__}: {exc}", "error")
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_calls = [
                block for block in response.content if block.type == "tool_use"
            ]
            if tool_calls:
                results = []
                for block in tool_calls:
                    output = _run_teammate_tool(name, block, sub_handlers)
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(output)})
                messages.append({"role": "user", "content": results})
                continue

            summary = _last_assistant_text(response.content)
            gate = plan_gates.get(name, "not_required")
            if gate != "pending" and summary:
                BUS.send(name, "lead", summary, "result")
            if gate == "pending":
                with team_lock:
                    active_teammates[name] = "waiting_approval"
            else:
                release_completed_assignment(name)
                with team_lock:
                    active_teammates[name] = "idle"
                BUS.send(name, "lead", "Waiting for more work.",
                         "idle_notification")

            while True:
                inbox = BUS.wait_for_messages(name, IDLE_SCAN_INTERVAL)
                if inbox:
                    for msg in inbox:
                        if handle_inbox_message(name, msg, messages):
                            should_stop = True
                            break
                    if should_stop or messages[-1]["role"] == "user":
                        break
                    continue

                task = claim_next_task(name)
                if not task:
                    continue
                try:
                    workdir = str(assignment_cwd(name))
                except (FileNotFoundError, ValueError) as exc:
                    workdir = f"unavailable ({exc})"
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Auto-claimed task {task.id}] "
                        f"{task.subject}\n{task.description}\n"
                        f"Work directory: {workdir}"
                    ),
                })
                print(f"  \033[32m[idle] {name} claimed "
                      f"{task.id}: {task.subject}\033[0m")
                break

    def run():
        try:
            run_loop()
        except Exception as exc:
            try:
                BUS.send(name, "lead", f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            try:
                release_teammate_assignment(name)
            except Exception as exc:
                try:
                    BUS.send(
                        name, "lead",
                        f"Assignment cleanup failed: {type(exc).__name__}: {exc}",
                        "error",
                    )
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                plan_request_ids.pop(name, None)
            print(f"  \033[32m[teammate] {name} finished\033[0m")

    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
    )


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    with task_lock:
        assignment = teammate_assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = assignment_versions.get(from_name, 0)
        with team_lock:
            if plan_gates.get(from_name) == "pending":
                return "A plan is already waiting for review."
            req_id = new_request_id()
            pending_requests[req_id] = ProtocolState(
                request_id=req_id, type="plan_approval",
                sender=from_name, target="lead",
                status="pending", payload=plan,
                work_version=work_version, task_id=task_id)
            plan_gates[from_name] = "pending"
            plan_request_ids[from_name] = req_id
            active_teammates[from_name] = "waiting_approval"
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Wait for Lead's decision."


# -- Lead Team Tools --

def run_request_shutdown(teammate: str) -> str:
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        req_id = new_request_id()
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=teammate,
            status="pending", payload="")
    BUS.send("lead", teammate, "Finish the current step and shut down.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request -> {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown requested from {teammate} ({req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        plan_gates[teammate] = "required"
    BUS.send("lead", teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    work_version, task_id = current_work_identity(state.sender)
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        if state.work_version != work_version or state.task_id != task_id:
            return f"Request {request_id} belongs to an earlier assignment"
        if plan_request_ids.get(state.sender) != request_id:
            return f"Request {request_id} is not the current plan"
        state.status = "approved" if approve else "rejected"
    content = feedback or ("Plan approved." if approve
                           else "Revise the plan and submit it again.")
    BUS.send("lead", state.sender, content,
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "approved" if approve else "rejected"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {state.status} ({request_id})"


__all__ = (
    "scan_unclaimed_tasks",
    "claim_next_task",
    "_last_assistant_text",
    "current_work_identity",
    "_run_teammate_tool",
    "apply_plan_response",
    "apply_shutdown_request",
    "_teammate_send_message",
    "spawn_teammate_thread",
    "_teammate_submit_plan",
    "run_request_shutdown",
    "run_request_plan",
    "run_review_plan",
    "IDLE_SCAN_INTERVAL",
)
