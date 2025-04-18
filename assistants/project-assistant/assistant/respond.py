import re
from typing import Any, Dict, List

import deepmerge
import openai_client
from assistant_extensions.attachments import AttachmentsExtension
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionUserMessageParam,
)
from openai_client.completion import message_content_from_completion
from openai_client.tools import complete_with_tool_calls
from semantic_workbench_api_model.workbench_model import (
    ConversationMessage,
    ConversationParticipantList,
    MessageType,
    NewConversationMessage,
)
from semantic_workbench_assistant.assistant_app import (
    ConversationContext,
)

from assistant.project_tools import ProjectTools
from assistant.utils import load_text_include

from .config import assistant_config
from .logging import logger
from .project_common import ConfigurationTemplate, detect_assistant_role, get_template
from .project_data import RequestStatus
from .project_manager import ProjectManager
from .project_storage import ConversationRole, ProjectStorage

SILENCE_TOKEN = "{{SILENCE}}"
CONTEXT_TRANSFER_ASSISTANT = ConfigurationTemplate.CONTEXT_TRANSFER_ASSISTANT
PROJECT_ASSISTANT = ConfigurationTemplate.PROJECT_ASSISTANT


def is_project_assistant(context: ConversationContext) -> bool:
    """
    Check if the assistant is a project assistant.
    """
    template = get_template(context)
    return template == PROJECT_ASSISTANT


# Format message helper function
def format_message(participants: ConversationParticipantList, message: ConversationMessage) -> str:
    """Consistent formatter that includes the participant name for multi-participant and name references"""
    conversation_participant = next(
        (participant for participant in participants.participants if participant.id == message.sender.participant_id),
        None,
    )
    participant_name = conversation_participant.name if conversation_participant else "unknown"
    message_datetime = message.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    return f"[{participant_name} - {message_datetime}]: {message.content}"


async def respond_to_conversation(
    context: ConversationContext,
    message: ConversationMessage,
    attachments_extension: AttachmentsExtension,
    debug_metadata: Dict[str, Any],
) -> None:
    """
    Respond to a conversation message.
    """
    config = await assistant_config.get(context.assistant)

    # Get the conversation Role.
    role = await detect_assistant_role(context)
    debug_metadata["role"] = role

    # Get the assistant's configuration template (Is this is context transfer or project assistant?)
    template = get_template(context)
    debug_metadata["template"] = template

    max_tokens = config.request_config.max_tokens
    available_tokens = max_tokens

    ###
    ### SYSTEM MESSAGE
    ###

    # Instruction and assistant name
    system_message_content = f'\n\n{config.instruction_prompt}\n\nYour name is "{context.assistant.name}".'

    # Add role-specific instructions
    role_specific_prompt = ""
    if role == ConversationRole.COORDINATOR:
        role_specific_prompt = load_text_include("coordinator_prompt.txt")
    else:
        role_specific_prompt = load_text_include("team_prompt.txt")

    if role_specific_prompt:
        system_message_content += f"\n\n{role_specific_prompt}"

    # If this is a multi-participant conversation, add a note about the participants
    participants = await context.get_participants(include_inactive=True)
    if len(participants.participants) > 2:
        system_message_content += (
            "\n\n"
            f"There are {len(participants.participants)} participants in the conversation,"
            " including you as the assistant and the following users:"
            + ",".join([
                f' "{participant.name}"'
                for participant in participants.participants
                if participant.id != context.assistant.id
            ])
            + "\n\nYou do not need to respond to every message. Do not respond if the last thing said was a closing"
            " statement such as 'bye' or 'goodbye', or just a general acknowledgement like 'ok' or 'thanks'. Do not"
            f' respond as another user in the conversation, only as "{context.assistant.name}".'
            " Sometimes the other users need to talk amongst themselves and that is ok. If the conversation seems to"
            f' be directed at you or the general audience, go ahead and respond.\n\nSay "{SILENCE_TOKEN}" to skip'
            " your turn."
        )

    ###
    ### SYSTEM MESSAGE: Project information
    ###

    project_id = await ProjectManager.get_project_id(context)
    if not project_id:
        raise ValueError("Project ID not found in context")

    project_data = {}
    all_requests = []

    try:
        # Get comprehensive project data for prompt
        briefing = ProjectStorage.read_project_brief(project_id)
        project_info = ProjectStorage.read_project_info(project_id)
        whiteboard = ProjectStorage.read_project_whiteboard(project_id)
        all_requests = ProjectStorage.get_all_information_requests(project_id)

        # Include project info
        project_info_text = ""
        if project_info:
            project_info_text = f"""
### PROJECT INFO
**Current State:** {project_info.state.value}
"""
            if project_info.status_message:
                project_info_text += f"**Status Message:** {project_info.status_message}\n"
            project_data["status"] = project_info_text

        # Include project brief
        project_brief_text = ""
        if briefing:
            # Basic project brief without goals
            project_brief_text = f"""
### PROJECT BRIEF
**Name:** {briefing.project_name}
**Description:** {briefing.project_description}
"""
            # Only include briefing goals and progress tracking if the assistant is a project assistant
            if is_project_assistant(context) and briefing.goals:
                project_brief_text += """
#### PROJECT GOALS:
"""
                for i, goal in enumerate(briefing.goals):
                    # Count completed criteria
                    completed = sum(1 for c in goal.success_criteria if c.completed)
                    total = len(goal.success_criteria)

                    project_brief_text += f"{i + 1}. **{goal.name}** - {goal.description}\n"
                    if goal.success_criteria:
                        project_brief_text += f"   Progress: {completed}/{total} criteria complete\n"
                        for j, criterion in enumerate(goal.success_criteria):
                            check = "✅" if criterion.completed else "⬜"
                            project_brief_text += f"   {check} {criterion.description}\n"
                    project_brief_text += "\n"
                project_data["briefing"] = project_brief_text

        # Include whiteboard content
        whiteboard_text = ""
        if whiteboard and whiteboard.content:
            whiteboard_text = "\n### ASSISTANT WHITEBOARD\n"
            whiteboard_text += f"{whiteboard.content}\n\n"
            project_data["whiteboard"] = whiteboard_text

    except Exception as e:
        logger.warning(f"Failed to fetch project data for prompt: {e}")

    # Construct role-specific messages with comprehensive project data
    if role == ConversationRole.COORDINATOR:
        # Include information requests
        coordinator_requests = ""
        if all_requests:
            active_requests = [r for r in all_requests if r.status != RequestStatus.RESOLVED]

            if active_requests:
                coordinator_requests = "\n\n### ACTIVE INFORMATION REQUESTS\n"
                coordinator_requests += (
                    "> 📋 **Use the request ID (not the title) with resolve_information_request()**\n\n"
                )

                for req in active_requests[:10]:  # Limit to 10 for brevity
                    priority_marker = {
                        "low": "🔹",
                        "medium": "🔶",
                        "high": "🔴",
                        "critical": "⚠️",
                    }.get(req.priority.value, "🔹")

                    coordinator_requests += f"{priority_marker} **{req.title}** ({req.status.value})\n"
                    coordinator_requests += f"   **Request ID:** `{req.request_id}`\n"
                    coordinator_requests += f"   **Description:** {req.description}\n\n"

                if len(active_requests) > 10:
                    coordinator_requests += f'*...and {len(active_requests) - 10} more requests. Use get_project_info(info_type="requests") to see all.*\n'
                project_data["information_requests"] = coordinator_requests

    else:  # team role
        # Fetch current information requests for this conversation
        information_requests_info = ""
        my_requests = []

        if all_requests:
            # Filter for requests from this conversation that aren't resolved
            my_requests = [
                r for r in all_requests if r.conversation_id == str(context.id) and r.status != RequestStatus.RESOLVED
            ]

            if my_requests:
                information_requests_info = "\n\n### YOUR CURRENT INFORMATION REQUESTS:\n"
                for req in my_requests:
                    information_requests_info += (
                        f"- **{req.title}** (ID: `{req.request_id}`, Priority: {req.priority})\n"
                    )
                information_requests_info += (
                    '\nYou can delete any of these requests using `delete_information_request(request_id="the_id")`\n'
                )
                project_data["information_requests"] = information_requests_info

    # Add project data to system message
    project_info = "\n\n## CURRENT PROJECT INFORMATION\n\n" + "\n".join(project_data.values())
    system_message_content += f"\n\n{project_info}"
    system_message: ChatCompletionMessageParam = {
        "role": "system",
        "content": system_message_content,
    }

    # Calculate token count for the system message
    system_message_tokens = openai_client.num_tokens_from_messages(
        model=config.request_config.openai_model,
        messages=[system_message],
    )
    available_tokens -= system_message_tokens

    # Initialize message list with system message
    completion_messages: list[ChatCompletionMessageParam] = [
        system_message,
    ]

    ###
    ### ATTACHMENTS
    ###

    # Generate the attachment messages
    attachment_messages: List[ChatCompletionMessageParam] = openai_client.convert_from_completion_messages(
        await attachments_extension.get_completion_messages_for_attachments(
            context,
            config=config.attachments_config,
        )
    )

    # Update token count to include attachment messages
    attachment_tokens = openai_client.num_tokens_from_messages(
        model=config.request_config.openai_model,
        messages=attachment_messages,
    )
    available_tokens -= attachment_tokens

    # Add attachment messages to completion messages
    completion_messages.extend(attachment_messages)

    ###
    ### USER MESSAGE
    ###

    # Format the current message
    # Create the message parameter based on sender with proper typing
    if message.sender.participant_id == context.assistant.id:
        user_message: ChatCompletionMessageParam = ChatCompletionAssistantMessageParam(
            role="assistant",
            content=format_message(participants, message),
        )
    else:
        user_message: ChatCompletionMessageParam = ChatCompletionUserMessageParam(
            role="user",
            content=format_message(participants, message),
        )

    # Calculate tokens for this message
    user_message_tokens = openai_client.num_tokens_from_messages(
        model=config.request_config.openai_model,
        messages=[user_message],
    )
    available_tokens -= user_message_tokens

    ###
    ### HISTORY MESSAGES
    ###

    # Get the conversation history
    # For pagination, we'll retrieve messages in batches as needed
    history_messages: list[ChatCompletionMessageParam] = []
    before_message_id = message.id

    # Track token usage and overflow
    history_messages_tokens = 0
    token_overage = 0

    # We'll fetch messages in batches until we hit the token limit or run out of messages
    while True:
        # Get a batch of messages
        messages_response = await context.get_messages(
            before=before_message_id,
            limit=100,  # Get messages in batches of 100
            message_types=[MessageType.chat],  # Include only chat messages
        )

        messages_list = messages_response.messages

        # If no messages found, break the loop
        if not messages_list or len(messages_list) == 0:
            break

        # Set before_message_id for the next batch
        before_message_id = messages_list[0].id

        # Process messages in reverse order (oldest first for history)
        for msg in reversed(messages_list):
            # Format this message for inclusion
            formatted_message = format_message(participants, msg)

            # Create the message parameter based on sender with proper typing
            if msg.sender.participant_id == context.assistant.id:
                current_message: ChatCompletionMessageParam = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=formatted_message,
                )
            else:
                current_message: ChatCompletionMessageParam = ChatCompletionUserMessageParam(
                    role="user",
                    content=formatted_message,
                )

            # Calculate tokens for this message
            user_message_tokens = openai_client.num_tokens_from_messages(
                model=config.request_config.openai_model,
                messages=[current_message],
            )

            # Check if we can add this message without exceeding the token limit
            if token_overage == 0 and history_messages_tokens + user_message_tokens < available_tokens:
                # Add message to the front of history_messages (to maintain chronological order)
                history_messages.insert(0, current_message)
                history_messages_tokens += user_message_tokens
            else:
                # We've exceeded the token limit, track the overage
                token_overage += user_message_tokens

        # If we've already exceeded the token limit, no need to fetch more messages
        if token_overage > 0:
            break

    # Add all chat messages.
    completion_messages.extend(history_messages)
    completion_messages.append(user_message)

    # Log the token usage
    total_tokens = max_tokens - available_tokens
    debug_metadata["token_usage"] = {"total_tokens": total_tokens}

    if available_tokens <= 0:
        logger.warning(f"Token limit exceeded: {total_tokens} > {max_tokens}. ")

    # These are the tools that are available to the assistant.
    project_tools = ProjectTools(context, role)

    # For team role, analyze message for possible information request needs.
    # Send a notification if we think it might be one.
    if role is ConversationRole.TEAM:
        # Check if the message indicates a potential information request
        detection_result = await project_tools.detect_information_request_needs(message.content)

        # If an information request is detected with reasonable confidence
        if detection_result.get("is_information_request", False) and detection_result.get("confidence", 0) > 0.5:
            # Get detailed information from detection
            suggested_title = detection_result.get("potential_title", "")
            suggested_priority = detection_result.get("suggested_priority", "medium")
            potential_description = detection_result.get("potential_description", "")
            reason = detection_result.get("reason", "")

            # Create a better-formatted suggestion using the detailed analysis
            suggestion = (
                f"**Information Request Detected**\n\n"
                f"It appears that you might need information from the Coordinator. {reason}\n\n"
                f"Would you like me to create an information request?\n"
                f"**Title:** {suggested_title}\n"
                f"**Description:** {potential_description}\n"
                f"**Priority:** {suggested_priority}\n\n"
                f"I can create this request for you, or you can use `/request-info` to create it yourself with custom details."
            )

            await context.send_messages(
                NewConversationMessage(
                    content=suggestion,
                    message_type=MessageType.notice,
                )
            )

    ##
    ## MAKE THE LLM CALL
    ##

    async with openai_client.create_client(config.service_config, api_version="2024-06-01") as client:
        try:
            # Create a completion dictionary for tool call handling
            completion_args = {
                "messages": completion_messages,
                "model": config.request_config.openai_model,
                "max_tokens": config.request_config.response_tokens,
            }

            # If the messaging API version supports tool functions, use them
            try:
                # Call the completion API with tool functions
                logger.info(f"Using tool functions for completions (role: {role})")

                # Record the tool names available for this role for validation
                available_tool_names = set(project_tools.tool_functions.function_map.keys())
                logger.info(f"Available tools for {role}: {available_tool_names}")

                # Make the API call
                completion_response, additional_messages = await complete_with_tool_calls(
                    async_client=client,
                    completion_args=completion_args,
                    tool_functions=project_tools.tool_functions,
                    metadata=debug_metadata,
                )

                # Process tool function calls and add intermediate messages to the conversation.
                # This would be better if complete_with_tool_calls returned an iterator.
                for additional_message in additional_messages:
                    if additional_message.get("role") == "function" and additional_message.get("content"):
                        # Send function call result messages as notice type
                        # Ensure content is a string
                        content_str = str(additional_message.get("content", "") or "")
                        await context.send_messages(
                            NewConversationMessage(
                                content=content_str,
                                message_type=MessageType.notice,
                                metadata={"function_result": True, "name": additional_message.get("name", "tool_call")},
                            )
                        )
                    elif additional_message.get("role") == "assistant" and additional_message.get("function_call"):
                        # Send function call messages as notice type
                        function_call = additional_message.get("function_call", {}) or {}
                        function_name = function_call.get("name", "unknown")
                        await context.send_messages(
                            NewConversationMessage(
                                content=f"Calling function: {function_name}",
                                message_type=MessageType.notice,
                                metadata={"function_call": True, "name": function_name},
                            )
                        )

                debug_metadata["additional_messages"] = additional_messages

                # Get the final content from the completion response
                content = message_content_from_completion(completion_response)
                if not content:
                    content = "I've processed your request, but couldn't generate a proper response."

            except (ImportError, AttributeError):
                # Fallback to standard completions if tool calls aren't supported
                logger.info("Tool functions not supported, falling back to standard completion")

                # Call the OpenAI chat completion endpoint to get a response
                completion = await client.chat.completions.create(**completion_args)

                # Get the content from the completion response
                content = completion.choices[0].message.content

                # Merge the completion response into the passed in metadata
                deepmerge.always_merger.merge(
                    debug_metadata,
                    {
                        "request": completion_args,
                        "response": completion.model_dump() if completion else "[no response from openai]",
                    },
                )

        except Exception as e:
            logger.exception(f"exception occurred calling openai chat completion: {e}")
            # if there is an error, set the content to an error message
            content = "An error occurred while calling the OpenAI API. Is it configured correctly?"

            # merge the error into the passed in metadata
            deepmerge.always_merger.merge(
                debug_metadata,
                {
                    "request": {
                        "model": config.request_config.openai_model,
                        "messages": completion_messages,
                    },
                    "error": str(e),
                },
            )

    # set the message type based on the content
    message_type = MessageType.chat

    # various behaviors based on the content
    if content:
        # strip out the username from the response
        if isinstance(content, str) and content.startswith("["):
            content = re.sub(r"\[.*\]:\s", "", content)

        # check for the silence token, in case the model chooses not to respond
        # model sometimes puts extra spaces in the response, so remove them
        # when checking for the silence token
        if isinstance(content, str) and content.replace(" ", "") == SILENCE_TOKEN:
            # normal behavior is to not respond if the model chooses to remain silent
            # but we can override this behavior for debugging purposes via the assistant config
            if config.enable_debug_output:
                # update the metadata to indicate the assistant chose to remain silent
                deepmerge.always_merger.merge(
                    debug_metadata,
                    {
                        "silence_token": True,
                        "generated_content": False,
                    },
                )
                # send a notice to the user that the assistant chose to remain silent
                await context.send_messages(
                    NewConversationMessage(
                        message_type=MessageType.notice,
                        content="[assistant chose to remain silent]",
                        metadata={"debug": debug_metadata},
                    )
                )
            return

        # override message type if content starts with "/", indicating a command response
        if isinstance(content, str) and content.startswith("/"):
            message_type = MessageType.command_response

    # send the response to the conversation
    await context.send_messages(
        NewConversationMessage(
            content=str(content) if content is not None else "[no response from openai]",
            message_type=message_type,
            metadata={"debug": debug_metadata},
        )
    )
