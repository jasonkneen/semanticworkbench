# utils/tool_utils.py
import asyncio
import logging
from textwrap import dedent
from typing import AsyncGenerator

import deepmerge
from mcp import Tool
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent
from mcp_extensions import execute_tool_with_retries

from ._model import (
    ExtendedCallToolRequestParams,
    ExtendedCallToolResult,
    MCPSession,
)

logger = logging.getLogger(__name__)


def retrieve_mcp_tools_from_sessions(mcp_sessions: list[MCPSession], exclude_tools: list[str] = []) -> list[Tool]:
    """
    Retrieve tools from all MCP sessions, excluding any tools that are disabled in the tools config
    and any duplicate keys (names) - first tool wins.
    """
    tools = []
    tool_names = set()
    for mcp_session in mcp_sessions:
        for tool in mcp_session.tools:
            if tool.name in tool_names:
                logger.warning(
                    "Duplicate tool name '%s' found in session %s; skipping",
                    tool.name,
                    mcp_session.config.server_config.key,
                )
                # Skip duplicate tools
                continue

            if tool.name in exclude_tools:
                # Skip excluded tools
                continue

            tools.append(tool)
            tool_names.add(tool.name)
    return tools


def retrieve_mcp_tools_and_sessions_from_sessions(
    mcp_sessions: list[MCPSession], exclude_tools: list[str] = []
) -> list[tuple[Tool, MCPSession]]:
    """
    Retrieve tools from all MCP sessions, excluding any tools that are disabled in the tools config
    and any duplicate keys (names) - first tool wins.
    """
    tools = []
    tool_names = set()
    for mcp_session in mcp_sessions:
        for tool in mcp_session.tools:
            if tool.name in tool_names:
                logger.warning(
                    "Duplicate tool name '%s' found in session %s; skipping",
                    tool.name,
                    mcp_session.config.server_config.key,
                )
                # Skip duplicate tools
                continue

            if tool.name in exclude_tools:
                # Skip excluded tools
                continue

            tools.append((tool, mcp_session))
            tool_names.add(tool.name)
    return tools


def get_mcp_session_and_tool_by_tool_name(
    mcp_sessions: list[MCPSession],
    tool_name: str,
) -> tuple[MCPSession | None, Tool | None]:
    """
    Retrieve the MCP session and tool by tool name.
    """
    return next(
        ((mcp_session, tool) for mcp_session in mcp_sessions for tool in mcp_session.tools if tool.name == tool_name),
        (None, None),
    )


async def handle_mcp_tool_call(
    mcp_sessions: list[MCPSession],
    tool_call: ExtendedCallToolRequestParams,
    method_metadata_key: str,
) -> ExtendedCallToolResult:
    # Find the tool and session by tool name.
    mcp_session, tool = get_mcp_session_and_tool_by_tool_name(mcp_sessions, tool_call.name)

    if not mcp_session or not tool:
        return ExtendedCallToolResult(
            id=tool_call.id,
            content=[
                TextContent(
                    type="text",
                    text=f"Tool '{tool_call.name}' not found in any session.",
                )
            ],
            isError=True,
            metadata={},
        )

    # Execute the tool call using our robust error-handling function.
    return await execute_tool(mcp_session, tool_call, method_metadata_key)


async def handle_long_running_tool_call(
    mcp_sessions: list[MCPSession],
    tool_call: ExtendedCallToolRequestParams,
    method_metadata_key: str,
) -> AsyncGenerator[ExtendedCallToolResult, None]:
    """
    Handle the streaming tool call by invoking the appropriate tool and returning a ToolCallResult.
    """

    # Find the tool and session from the full collection of sessions
    mcp_session, tool = get_mcp_session_and_tool_by_tool_name(mcp_sessions, tool_call.name)

    if not mcp_session or not tool:
        yield ExtendedCallToolResult(
            id=tool_call.id,
            content=[
                TextContent(
                    type="text",
                    text=f"Tool '{tool_call.name}' not found in any of the sessions.",
                )
            ],
            isError=True,
            metadata={},
        )
        return

    # For now, let's just hack to return an immediate response to indicate that the tool call was received
    # and is being processed and that the results will be sent in a separate message.
    yield ExtendedCallToolResult(
        id=tool_call.id,
        content=[
            TextContent(
                type="text",
                text=dedent(f"""
                Processing tool call '{tool_call.name}'.
                Estimated time to completion: {mcp_session.config.server_config.task_completion_estimate}
            """).strip(),
            ),
        ],
        metadata={},
    )

    # Perform the tool call
    tool_call_result = await execute_tool(mcp_session, tool_call, method_metadata_key)
    yield tool_call_result


async def execute_tool(
    mcp_session: MCPSession,
    tool_call: ExtendedCallToolRequestParams,
    method_metadata_key: str,
) -> ExtendedCallToolResult:
    # Initialize metadata
    metadata = {}

    # Prepare to capture tool output
    tool_result = None
    tool_output: list[TextContent | ImageContent | EmbeddedResource] = []
    content_items: list[str] = []

    async def tool_call_function() -> CallToolResult:
        return await mcp_session.client_session.call_tool(tool_call.name, tool_call.arguments)

    logger.debug(
        f"Invoking '{mcp_session.config.server_config.key}.{tool_call.name}' with arguments: {tool_call.arguments}"
    )

    try:
        tool_result = await execute_tool_with_retries(tool_call_function, tool_call.name)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if isinstance(e, ExceptionGroup) and len(e.exceptions) == 1:
            e = e.exceptions[0]
        error_message = str(e).strip() or "Peer disconnected; no error message received."
        # Check if the error indicates a disconnection.
        if "peer closed connection" in error_message.lower():
            mcp_session.is_connected = False
        logger.exception(f"Error executing tool '{tool_call.name}': {error_message}")
        error_text = f"Tool '{tool_call.name}' failed with error: {error_message}"
        return ExtendedCallToolResult(
            id=tool_call.id,
            content=[TextContent(type="text", text=error_text)],
            isError=True,
            metadata={"debug": {method_metadata_key: {"error": error_message}}},
        )

    tool_output = tool_result.content

    # Merge debug metadata for the successful result
    deepmerge.always_merger.merge(
        metadata,
        {
            "debug": {
                method_metadata_key: {
                    "tool_result": tool_output,
                },
            },
        },
    )

    # FIXME: for now, we'll just dump the tool output as text but we should support other content types
    # Process tool output and convert to text content.
    for tool_output_item in tool_output:
        if isinstance(tool_output_item, TextContent):
            if tool_output_item.text.strip() != "":
                content_items.append(tool_output_item.text)
        elif isinstance(tool_output_item, ImageContent):
            content_items.append(tool_output_item.model_dump_json())
        elif isinstance(tool_output_item, EmbeddedResource):
            content_items.append(tool_output_item.model_dump_json())

    # Return the successful tool call result
    return ExtendedCallToolResult(
        id=tool_call.id,
        content=[
            TextContent(
                type="text",
                text="\n\n".join(content_items) if content_items else "[tool call successful, but no output]",
            ),
        ],
        metadata=metadata,
    )
