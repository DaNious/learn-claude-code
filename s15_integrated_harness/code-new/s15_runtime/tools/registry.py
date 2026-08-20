

def assemble_tool_pool() -> tuple[list[dict], dict]:
    from ..integrations.mcp import (
        MCP_HOST_POLICY,
        mcp_clients,
        normalize_mcp_name,
    )
    from ..runtime import hooks
    from .schemas import BUILTIN_HANDLERS, BUILTIN_TOOLS

    """Merge builtin tools + all MCP tools into one pool."""
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    policies: dict[str, str] = {}
    origins = {tool["name"]: f"built-in tool {tool['name']!r}"
               for tool in tools}
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if len(prefixed) > 64:
                raise ValueError(
                    f"MCP tool name is longer than 64 characters: {prefixed}"
                )
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")
            origins[prefixed] = origin
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            })
            handlers[prefixed] = (
                lambda *, client=mcp_client, tool=raw_name, **kwargs:
                client.call_tool(tool, kwargs)
            )
            policies[prefixed] = MCP_HOST_POLICY.get(
                (server_name, raw_name), "confirm"
            )
    hooks.mcp_tool_policies = policies
    return tools, handlers


__all__ = (
    "assemble_tool_pool",
)
