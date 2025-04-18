# Copyright (c) Microsoft. All rights reserved.

# Project Assistant implementation
#
# This assistant provides project coordination capabilities with Coordinator and Team member roles,
# supporting whiteboard sharing, file synchronization, and team collaboration.


from assistant_extensions.attachments import AttachmentsExtension
from content_safety.evaluators import CombinedContentSafetyEvaluator
from semantic_workbench_api_model import workbench_model
from semantic_workbench_api_model.workbench_model import (
    AssistantStateEvent,
    ConversationEvent,
    ConversationMessage,
    MessageType,
    NewConversationMessage,
    ParticipantRole,
    UpdateParticipant,
)
from semantic_workbench_assistant.assistant_app import (
    AssistantApp,
    AssistantCapability,
    AssistantTemplate,
    ContentSafety,
    ContentSafetyEvaluator,
    ConversationContext,
)

from assistant.command_processor import CommandRegistry
from assistant.respond import respond_to_conversation

from .config import assistant_config
from .logging import logger
from .project_common import detect_assistant_role
from .project_data import LogEntryType
from .project_files import ProjectFileManager
from .project_manager import ProjectManager
from .project_storage import (
    ConversationProjectManager,
    ConversationRole,
    ProjectNotifier,
    ProjectStorage,
)
from .state_inspector import ProjectInspectorStateProvider

service_id = "project-assistant.made-exploration"
service_name = "Project Assistant"
service_description = "A mediator assistant that facilitates file sharing between conversations."


# Content safety.
async def content_evaluator_factory(
    context: ConversationContext,
) -> ContentSafetyEvaluator:
    config = await assistant_config.get(context.assistant)
    return CombinedContentSafetyEvaluator(config.content_safety_config)


content_safety = ContentSafety(content_evaluator_factory)

# Set up the app.
assistant = AssistantApp(
    assistant_service_id=service_id,
    assistant_service_name=service_name,
    assistant_service_description=service_description,
    config_provider=assistant_config.provider,
    content_interceptor=content_safety,
    capabilities={AssistantCapability.supports_conversation_files},
    inspector_state_providers={
        "project_status": ProjectInspectorStateProvider(assistant_config),
    },
    additional_templates=[
        AssistantTemplate(
            id="context_transfer",
            name="Context Transfer Assistant (experimental)",
            description="An assistant for capturing and sharing complex information for others to explore.",
        ),
    ],
)

attachments_extension = AttachmentsExtension(assistant)

app = assistant.fastapi_app()


# Handle the event triggered when the assistant is added to a conversation.
@assistant.events.conversation.on_created_including_mine
async def on_conversation_created(context: ConversationContext) -> None:
    """
    Handle the event triggered when the assistant is added to a conversation.

    The assistant manages three types of conversations:
    1. Coordinator Conversation: The main conversation used by the project coordinator
    2. Shareable Team Conversation: A template conversation that has a share URL and is never directly used
    3. Team Conversation(s): Individual conversations for team members created when they redeem the share URL

    This handler automatically:
    1. Identifies which type of conversation this is based on metadata
    2. For new conversations, creates a project, sets up as coordinator, and creates a shareable team conversation
    3. For team conversations created from the share URL, sets up as team member
    4. For the shareable team conversation itself, initializes it properly
    """
    # Get conversation to access metadata
    conversation = await context.get_conversation()
    metadata = conversation.metadata or {}

    # Define variables for each conversation type
    is_shareable_template = False
    is_team_from_redemption = False
    is_coordinator = False

    # Check if this conversation was imported from another (indicates it's from share redemption)
    if conversation.imported_from_conversation_id:
        # If it was imported AND has team metadata, it's a redeemed team conversation
        if metadata.get("is_team_conversation", False) and metadata.get("project_id"):
            is_team_from_redemption = True

    # First check for an explicit share redemption
    elif metadata.get("share_redemption", {}) and metadata.get("share_redemption", {}).get("conversation_share_id"):
        share_redemption = metadata.get("share_redemption", {})
        is_team_from_redemption = True
        share_metadata = share_redemption.get("metadata", {})

    # Check if this is a template conversation (original team conversation created by coordinator)
    elif (
        metadata.get("is_team_conversation", False)
        and metadata.get("project_id")
        and not conversation.imported_from_conversation_id
    ):
        # If it's a team conversation with project_id but NOT imported, it's the template
        is_shareable_template = True

    # Additional check for team conversations (from older versions without imported_from)
    elif metadata.get("is_team_conversation", False) and metadata.get("project_id"):
        is_team_from_redemption = True

    # If none of the above match, it's a coordinator conversation
    else:
        is_coordinator = True

    # Handle shareable template conversation - No welcome message
    if is_shareable_template:
        # This is a shareable template conversation, not an actual team conversation
        metadata["setup_complete"] = True
        metadata["assistant_mode"] = "team"
        metadata["project_role"] = "team"

        # Associate with the project ID if provided in metadata
        project_id = metadata.get("project_id")
        if project_id:
            # Set this conversation as a team member for the project
            await ConversationProjectManager.associate_conversation_with_project(context, project_id)

        # Update conversation metadata
        await context.send_conversation_state_event(
            AssistantStateEvent(state_id="setup_complete", event="updated", state=None)
        )
        await context.send_conversation_state_event(
            AssistantStateEvent(state_id="project_role", event="updated", state=None)
        )
        await context.send_conversation_state_event(
            AssistantStateEvent(state_id="assistant_mode", event="updated", state=None)
        )

        # No welcome message for the shareable template
        return

    # Handle team conversation from share redemption - Show team welcome message
    if is_team_from_redemption:
        # Get project ID from metadata or share metadata
        project_id = metadata.get("project_id")

        # If no project_id directly in metadata, try to get it from share_redemption
        if not project_id and metadata.get("share_redemption"):
            share_metadata = metadata.get("share_redemption", {}).get("metadata", {})
            project_id = share_metadata.get("project_id")

        if project_id:
            # Set this conversation as a team member for the project
            await ConversationProjectManager.associate_conversation_with_project(context, project_id)

            # Update conversation metadata
            metadata["setup_complete"] = True
            metadata["assistant_mode"] = "team"
            metadata["project_role"] = "team"

            await context.send_conversation_state_event(
                AssistantStateEvent(state_id="setup_complete", event="updated", state=None)
            )
            await context.send_conversation_state_event(
                AssistantStateEvent(state_id="project_role", event="updated", state=None)
            )
            await context.send_conversation_state_event(
                AssistantStateEvent(state_id="assistant_mode", event="updated", state=None)
            )

            # Use team welcome message from config
            try:
                config = await assistant_config.get(context.assistant)
                welcome_message = config.team_config.welcome_message

                # Send welcome message
                await context.send_messages(
                    NewConversationMessage(
                        content=welcome_message,
                        message_type=MessageType.chat,
                        metadata={"generated_content": False},
                    )
                )
            except Exception as e:
                logger.error(f"Error sending team welcome message: {e}", exc_info=True)
            return
        else:
            logger.debug("Team conversation missing project_id in share metadata")

    # Handle coordinator conversation - Show coordinator welcome message
    if is_coordinator:
        # Create a new project
        success, project_id = await ProjectManager.create_project(context)

        if success and project_id:
            # Update conversation metadata
            metadata["setup_complete"] = True
            metadata["assistant_mode"] = "coordinator"
            metadata["project_role"] = "coordinator"

            await context.send_conversation_state_event(
                AssistantStateEvent(state_id="setup_complete", event="updated", state=None)
            )
            await context.send_conversation_state_event(
                AssistantStateEvent(state_id="project_role", event="updated", state=None)
            )
            await context.send_conversation_state_event(
                AssistantStateEvent(state_id="assistant_mode", event="updated", state=None)
            )

            # Create a default project brief with placeholder information
            await ProjectManager.create_project_brief(
                context=context,
                project_name="New Project",
                project_description="This project was automatically created. The project brief is a place for you to craft some information to be shared with others. Ask your assistant to update the project brief with whatever project details you'd like.",
            )

            # Create a team conversation and share URL
            (
                success,
                team_conversation_id,
                share_url,
            ) = await ProjectManager.create_team_conversation(
                context=context, project_id=project_id, project_name="Shared assistant"
            )

            if success and share_url:
                # Store the team conversation information in the coordinator's metadata
                # Using None for state as required by the type system
                metadata["team_conversation_id"] = team_conversation_id
                metadata["team_conversation_share_url"] = share_url

                await context.send_conversation_state_event(
                    AssistantStateEvent(state_id="team_conversation_id", event="updated", state=None)
                )

                await context.send_conversation_state_event(
                    AssistantStateEvent(
                        state_id="team_conversation_share_url",
                        event="updated",
                        state=None,
                    )
                )

                # Use coordinator welcome message from config with the share URL
                config = await assistant_config.get(context.assistant)
                welcome_message = config.coordinator_config.welcome_message.format(share_url=share_url)
            else:
                # Even if share URL creation failed, still use the welcome message
                # but it won't have a working share URL
                config = await assistant_config.get(context.assistant)
                welcome_message = config.coordinator_config.welcome_message.format(
                    share_url="<Share URL generation failed>"
                )
        else:
            # Failed to create project - use fallback mode
            metadata["setup_complete"] = False
            metadata["assistant_mode"] = "setup"

            # Use a simple fallback welcome message
            welcome_message = """# Welcome to the Project Assistant

I'm having trouble setting up your project. Please try again or contact support if the issue persists."""

        # Send the welcome message
        await context.send_messages(
            NewConversationMessage(
                content=welcome_message,
                message_type=MessageType.chat,
                metadata={"generated_content": False},
            )
        )


@assistant.events.conversation.message.chat.on_created
async def on_message_created(
    context: ConversationContext, event: ConversationEvent, message: ConversationMessage
) -> None:
    """
    Handle user chat messages and provide appropriate project coordination responses.

    This manages project setup/detection, role enforcement, and updating the whiteboard
    for coordinator messages.
    """

    # update the participant status to indicate the assistant is thinking
    await context.update_participant_me(UpdateParticipant(status="thinking..."))

    try:
        project_id = await ProjectManager.get_project_id(context)
        debug_metadata = {
            "content_safety": event.data.get(content_safety.metadata_key, {}),
            "project_id": project_id,
        }

        # If this is a Coordinator conversation, store the message for Team access
        role = await detect_assistant_role(context)
        if role == ConversationRole.COORDINATOR and message.message_type == MessageType.chat:
            try:
                if project_id:
                    # Get the sender's name
                    sender_name = "Coordinator"
                    if message.sender:
                        participants = await context.get_participants()
                        for participant in participants.participants:
                            if participant.id == message.sender.participant_id:
                                sender_name = participant.name
                                break

                    # Store the message for Team access
                    ProjectStorage.append_coordinator_message(
                        project_id=project_id,
                        message_id=str(message.id),
                        content=message.content,
                        sender_name=sender_name,
                        is_assistant=message.sender.participant_role == ParticipantRole.assistant,
                        timestamp=message.timestamp,
                    )
                    logger.info(f"Stored Coordinator message for Team access: {message.id}")
            except Exception as e:
                # Don't fail message handling if storage fails
                logger.exception(f"Error storing Coordinator message for Team access: {e}")

        await respond_to_conversation(
            context,
            message=message,
            attachments_extension=attachments_extension,
            debug_metadata=debug_metadata,
        )
    finally:
        await context.update_participant_me(UpdateParticipant(status=None))


@assistant.events.conversation.message.command.on_created
async def on_command_created(
    context: ConversationContext, event: ConversationEvent, message: ConversationMessage
) -> None:
    """
    Handle command messages using the centralized command processor.
    """
    if message.message_type != MessageType.command:
        return

    await context.update_participant_me(UpdateParticipant(status="processing command..."))
    try:
        debug_metadata = {"content_safety": event.data.get(content_safety.metadata_key, {})}

        # Process the command using the command processor
        role = await detect_assistant_role(context)
        command_registry = CommandRegistry()
        command_processed = await command_registry.process_command(context, message, role.value)

        # If the command wasn't recognized or processed, respond normally
        if not command_processed:
            await respond_to_conversation(
                context,
                message=message,
                attachments_extension=attachments_extension,
                debug_metadata=debug_metadata,
            )
    finally:
        # update the participant status to indicate the assistant is done thinking
        await context.update_participant_me(UpdateParticipant(status=None))


@assistant.events.conversation.file.on_created
async def on_file_created(
    context: ConversationContext,
    event: workbench_model.ConversationEvent,
    file: workbench_model.File,
) -> None:
    """
    Handle when a file is created in the conversation.

    For Coordinator files:
    1. Store a copy in project storage
    2. Synchronize to all Team conversations

    For Team files:
    1. Use as-is without copying to project storage
    """
    try:
        # Log file creation event details
        logger.info(f"File created event: filename={file.filename}, size={file.file_size}, type={file.content_type}")
        logger.info(f"Full file object: {file}")

        # Get project ID
        project_id = await ProjectManager.get_project_id(context)
        if not project_id or not file.filename:
            logger.warning(
                f"No project ID found or missing filename: project_id={project_id}, filename={file.filename}"
            )
            return

        # Get the conversation's role
        role = await detect_assistant_role(context)

        # Log file processing
        logger.info(f"Processing file {file.filename} with role: {role}, project: {project_id}")

        # Use ProjectFileManager for file operations

        # Process based on role
        if role == ConversationRole.COORDINATOR:
            # For Coordinator files:
            # 1. Store in project storage (marked as coordinator file)
            logger.info(f"Copying Coordinator file to project storage: {file.filename}")

            # Check project files directory
            files_dir = ProjectFileManager.get_project_files_dir(project_id)
            logger.info(f"Project files directory: {files_dir} (exists: {files_dir.exists()})")

            # Copy file to project storage
            success = await ProjectFileManager.copy_file_to_project_storage(
                context=context,
                project_id=project_id,
                file=file,
                is_coordinator_file=True,
            )

            if not success:
                logger.error(f"Failed to copy file to project storage: {file.filename}")
                return

            # Verify file was stored correctly
            file_path = ProjectFileManager.get_file_path(project_id, file.filename)
            if file_path.exists():
                logger.info(f"File successfully stored at: {file_path} (size: {file_path.stat().st_size} bytes)")
            else:
                logger.error(f"File not found at expected location: {file_path}")

            # Check file metadata was updated
            metadata = ProjectFileManager.read_file_metadata(project_id)
            if metadata and any(f.filename == file.filename for f in metadata.files):
                logger.info(f"File metadata updated successfully for {file.filename}")
            else:
                logger.error(f"File metadata not updated for {file.filename}")

            # 2. Synchronize to all Team conversations
            # Get all Team conversations
            team_conversations = await ProjectFileManager.get_team_conversations(context, project_id)

            if team_conversations:
                logger.info(f"Found {len(team_conversations)} team conversations to update")

                # Copy to each Team conversation
                for team_conv_id in team_conversations:
                    logger.info(f"Copying file to Team conversation {team_conv_id}: {file.filename}")
                    copy_success = await ProjectFileManager.copy_file_to_conversation(
                        context=context,
                        project_id=project_id,
                        filename=file.filename,
                        target_conversation_id=team_conv_id,
                    )
                    logger.info(f"Copy to Team conversation {team_conv_id}: {'Success' if copy_success else 'Failed'}")
            else:
                logger.info("No team conversations found to update files")

            # 3. Update all UIs but don't send notifications to reduce noise
            await ProjectNotifier.notify_project_update(
                context=context,
                project_id=project_id,
                update_type="file_created",
                message=f"Coordinator shared a file: {file.filename}",
                data={"filename": file.filename},
                send_notification=False,  # Don't send notification to reduce noise
            )
        else:
            # For Team files, no special handling needed
            # They're already available in the conversation
            logger.info(f"Team file created (not shared to project storage): {file.filename}")

        # Log file creation to project log for all files
        await ProjectStorage.log_project_event(
            context=context,
            project_id=project_id,
            entry_type="file_shared",
            message=f"File shared: {file.filename}",
            metadata={
                "file_id": getattr(file, "id", ""),
                "filename": file.filename,
                "is_coordinator_file": role.value == "coordinator",
            },
        )

    except Exception as e:
        logger.exception(f"Error handling file creation: {e}")


@assistant.events.conversation.file.on_updated
async def on_file_updated(
    context: ConversationContext,
    event: workbench_model.ConversationEvent,
    file: workbench_model.File,
) -> None:
    """
    Handle when a file is updated in the conversation.

    For Coordinator files:
    1. Update the copy in project storage
    2. Update copies in all Team conversations

    For Team files:
    1. Use as-is without updating in project storage
    """
    try:
        # Get project ID
        project_id = await ProjectManager.get_project_id(context)
        if not project_id or not file.filename:
            return

        # Get the conversation's role
        role = await detect_assistant_role(context)

        # Use ProjectFileManager for file operations

        # Process based on role
        if role == ConversationRole.COORDINATOR:
            # For Coordinator files:
            # 1. Update in project storage
            logger.info(f"Updating Coordinator file in project storage: {file.filename}")
            success = await ProjectFileManager.copy_file_to_project_storage(
                context=context,
                project_id=project_id,
                file=file,
                is_coordinator_file=True,
            )

            if not success:
                logger.error(f"Failed to update file in project storage: {file.filename}")
                return

            # 2. Update in all Team conversations
            # Get all Team conversations
            team_conversations = await ProjectFileManager.get_team_conversations(context, project_id)

            # Update in each Team conversation
            for team_conv_id in team_conversations:
                logger.info(f"Updating file in Team conversation {team_conv_id}: {file.filename}")
                await ProjectFileManager.copy_file_to_conversation(
                    context=context,
                    project_id=project_id,
                    filename=file.filename,
                    target_conversation_id=team_conv_id,
                )

            # 3. Update all UIs but don't send notifications to reduce noise
            await ProjectNotifier.notify_project_update(
                context=context,
                project_id=project_id,
                update_type="file_updated",
                message=f"Coordinator updated a file: {file.filename}",
                data={"filename": file.filename},
                send_notification=False,  # Don't send notification to reduce noise
            )
        else:
            # For Team files, no special handling needed
            # They're already available in the conversation
            logger.info(f"Team file updated (not shared to project storage): {file.filename}")

        # Log file update to project log for all files
        await ProjectStorage.log_project_event(
            context=context,
            project_id=project_id,
            entry_type="file_shared",
            message=f"File updated: {file.filename}",
            metadata={
                "file_id": getattr(file, "id", ""),
                "filename": file.filename,
                "is_coordinator_file": role.value == "coordinator",
            },
        )

    except Exception as e:
        logger.exception(f"Error handling file update: {e}")


@assistant.events.conversation.file.on_deleted
async def on_file_deleted(
    context: ConversationContext,
    event: workbench_model.ConversationEvent,
    file: workbench_model.File,
) -> None:
    """
    Handle when a file is deleted from the conversation.

    For Coordinator files:
    1. Delete from project storage
    2. Notify Team conversations to delete their copies

    For Team files:
    1. Just delete locally, no need to notify others
    """
    try:
        # Get project ID
        project_id = await ProjectManager.get_project_id(context)
        if not project_id or not file.filename:
            return

        # Get the conversation's role
        role = await detect_assistant_role(context)

        # Use ProjectFileManager for file operations

        # Process based on role
        if role == ConversationRole.COORDINATOR:
            # For Coordinator files:
            # 1. Delete from project storage
            logger.info(f"Deleting Coordinator file from project storage: {file.filename}")
            success = await ProjectFileManager.delete_file_from_project_storage(
                context=context, project_id=project_id, filename=file.filename
            )

            if not success:
                logger.error(f"Failed to delete file from project storage: {file.filename}")

            # 2. Update all UIs about the deletion but don't send notifications to reduce noise
            await ProjectNotifier.notify_project_update(
                context=context,
                project_id=project_id,
                update_type="file_deleted",
                message=f"Coordinator deleted a file: {file.filename}",
                data={"filename": file.filename},
                send_notification=False,  # Don't send notification to reduce noise
            )
        else:
            # For Team files, no special handling needed
            # Just delete locally
            logger.info(f"Team file deleted (not shared with project): {file.filename}")

        # Log file deletion to project log for all files
        await ProjectStorage.log_project_event(
            context=context,
            project_id=project_id,
            entry_type="file_deleted",
            message=f"File deleted: {file.filename}",
            metadata={
                "file_id": getattr(file, "id", ""),
                "filename": file.filename,
                "is_coordinator_file": role.value == "coordinator",
            },
        )

    except Exception as e:
        logger.exception(f"Error handling file deletion: {e}")


# Handle the event triggered when a participant joins a conversation
@assistant.events.conversation.participant.on_created
async def on_participant_joined(
    context: ConversationContext,
    event: ConversationEvent,
    participant: workbench_model.ConversationParticipant,
) -> None:
    """
    Handle the event triggered when a participant joins or returns to a conversation.

    This handler is used to detect when a team member returns to a conversation
    and automatically synchronize project files.
    """
    try:
        # Skip the assistant's own join event
        if participant.id == context.assistant.id:
            logger.debug("Skipping assistant's own join event")
            return

        # Open the Brief tab (state inspector).
        await context.send_conversation_state_event(
            AssistantStateEvent(
                state_id="project_status",
                event="focus",
                state=None,
            )
        )

        # Check if this is a Team conversation
        role = await detect_assistant_role(context)
        if role != ConversationRole.TEAM:
            logger.debug(f"Not a Team conversation (role={role}), skipping file sync for participant")
            return

        # Get project ID
        project_id = await ConversationProjectManager.get_associated_project_id(context)
        if not project_id:
            logger.debug("No project ID found, skipping file sync for participant")
            return

        logger.info(f"Team member {participant.name} joined project {project_id}, synchronizing files")

        # Automatically synchronize files from project storage to this conversation
        success = await ProjectFileManager.synchronize_files_to_team_conversation(
            context=context, project_id=project_id
        )

        if success:
            logger.info(f"Successfully synchronized files for returning team member: {participant.name}")
        else:
            logger.warning(f"File synchronization failed for returning team member: {participant.name}")

        await ProjectStorage.log_project_event(
            context=context,
            project_id=project_id,
            entry_type=LogEntryType.PARTICIPANT_JOINED,
            message=f"Participant joined: {participant.name}",
            metadata={
                "participant_id": participant.id,
                "participant_name": participant.name,
                "conversation_id": str(context.id),
            },
        )

    except Exception as e:
        logger.exception(f"Error handling participant join event: {e}")
