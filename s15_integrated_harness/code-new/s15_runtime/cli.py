"""Interactive command-line entry point for the integrated harness."""

from .agent import loop
from .foundation import bootstrap
from .runtime import cron, hooks


def run_cli() -> None:
    """Run the original S15 interactive loop."""
    bootstrap.CLI_ACTIVE = True
    cron.start_runtime_services()
    print("s15: integrated harness")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = loop.update_context({}, [])
    session_state = {"active_user_request": "(no active user request)"}
    bootstrap.threading.Thread(
        target=loop.async_event_loop,
        args=(history, context, session_state),
        daemon=True,
    ).start()
    while True:
        try:
            query = bootstrap.CONSOLE.ask(bootstrap.PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        with loop.agent_lock:
            hooks.trigger_hooks("UserPromptSubmit", query)
            turn_start = len(history)
            session_state["active_user_request"] = query
            history.append({"role": "user", "content": query})
            loop.agent_loop(history, context, query)
            context = loop.update_context(context, history)
            loop.print_turn_assistants(history, turn_start)
        print()


__all__ = ("run_cli",)
