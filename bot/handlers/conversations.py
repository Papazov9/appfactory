from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import config
from bot.services.orchestrator import create_project, update_project, import_project
from bot.services.transcriber import Transcriber
from bot.services import stacks
from bot.models.project import db

logger = logging.getLogger(__name__)

# Conversation states
NAME, APP_TYPE, BRIEF = range(3)

# Quick voice flow states
VOICE_WAIT, VOICE_CONFIRM, VOICE_EDIT = 10, 11, 12

# Update flow states
UPDATE_SELECT, UPDATE_INSTRUCTIONS = 20, 21

# Import flow states (adopt an existing repo for deployment)
IMPORT_URL, IMPORT_NAME = 30, 31

STACK_KEYBOARD = ReplyKeyboardMarkup(
    stacks.keyboard_rows(),
    one_time_keyboard=True,
    resize_keyboard=True,
)

CONFIRM_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ Build it!", "✏️ Edit brief", "🔄 Change type", "❌ Cancel"]],
    one_time_keyboard=True,
    resize_keyboard=True,
)

# Shown while collecting a (possibly multi-message) brief or update instructions.
COLLECT_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ Done", "❌ Cancel"]],
    resize_keyboard=True,
)
DONE_WORDS = {"✅ Done", "Done", "done", "🚀 Build it!"}
CANCEL_WORDS = {"❌ Cancel", "Cancel", "cancel"}


# ──────────────────────────────────────────────
#  Standard /new flow (text-based)
# ──────────────────────────────────────────────

async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🏭 <b>New Project</b>\n\n"
        "What's the project name?\n"
        "<i>(This becomes the subdomain, e.g., 'acme-site' → acme-site.yourdomain.com)</i>",
        parse_mode="HTML",
    )
    return NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["project_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Which stack should I build it in?",
        reply_markup=STACK_KEYBOARD,
    )
    return APP_TYPE


async def receive_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    stack = stacks.stack_from_keyboard(choice)
    context.user_data["stack"] = stack

    context.user_data["parts"] = []
    context.user_data["images"] = []
    await update.message.reply_text(
        "Now send me the project brief.\n\n"
        "You can send <b>several messages</b> — text, 🎤 voice notes, and 🖼️ images "
        "(screenshots, mockups, branding). I'll combine them all.\n\n"
        "Tap <b>✅ Done</b> when you've sent everything.",
        reply_markup=COLLECT_KEYBOARD,
        parse_mode="HTML",
    )
    return BRIEF


def _collected_summary(context: ContextTypes.DEFAULT_TYPE) -> str:
    parts = context.user_data.get("parts", [])
    images = context.user_data.get("images", [])
    bits = []
    if parts:
        bits.append(f"{len(parts)} text part(s)")
    if images:
        bits.append(f"{len(images)} image(s)")
    return ", ".join(bits) if bits else "nothing yet"


async def _finalize_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = context.user_data.get("parts", [])
    images = context.user_data.get("images", [])
    brief = "\n\n".join(parts).strip()
    if not brief and not images:
        await update.message.reply_text("Send a description or an image first, then tap ✅ Done.")
        return BRIEF
    if not brief:
        brief = "Build according to the attached design reference image(s)."

    name = context.user_data.get("project_name", "unnamed")
    stack = context.user_data.get("stack", stacks.DEFAULT_STACK)

    await update.message.reply_text(
        f"🚀 Starting build for <b>{name}</b>...\n"
        f"Stack: {stacks.stack_display(stack)}"
        + (f"\n🖼️ {len(images)} reference image(s) attached" if images else "")
        + "\n\nProgress updates below.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await create_project(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        name=name,
        brief=brief,
        stack=stack,
        images=images,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_brief_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in DONE_WORDS:
        return await _finalize_new(update, context)
    if text in CANCEL_WORDS:
        return await cancel(update, context)

    context.user_data.setdefault("parts", []).append(text)
    await update.message.reply_text(
        f"📝 Added. Collected so far: {_collected_summary(context)}.\n"
        "Send more, or tap ✅ Done.",
        reply_markup=COLLECT_KEYBOARD,
    )
    return BRIEF


async def receive_brief_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = await update.message.reply_text("🎙️ Transcribing your voice message...")
    transcript = await _handle_voice(update)
    if not transcript:
        await msg.edit_text("❌ Couldn't transcribe. Try again or send text instead.")
        return BRIEF

    context.user_data.setdefault("parts", []).append(transcript)
    await msg.edit_text(
        f"🎙️ <b>Transcribed:</b> <i>{transcript[:300]}"
        f"{'...' if len(transcript) > 300 else ''}</i>",
        parse_mode="HTML",
    )
    await update.message.reply_text(
        f"Collected so far: {_collected_summary(context)}. Send more, or tap ✅ Done.",
        reply_markup=COLLECT_KEYBOARD,
    )
    return BRIEF


async def receive_brief_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = await _handle_photo(update)
    if not path:
        await update.message.reply_text("❌ Couldn't read that image. Try again.")
        return BRIEF

    context.user_data.setdefault("images", []).append(path)
    caption = (update.message.caption or "").strip()
    if caption:
        context.user_data.setdefault("parts", []).append(caption)

    await update.message.reply_text(
        f"🖼️ Image added. Collected so far: {_collected_summary(context)}.\n"
        "Send more, or tap ✅ Done.",
        reply_markup=COLLECT_KEYBOARD,
    )
    return BRIEF


# ──────────────────────────────────────────────
#  Quick /voice flow: record → AI analyzes → confirm → build
# ──────────────────────────────────────────────

async def voice_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # If they sent a voice message directly (no /voice command first)
    if update.message.voice or update.message.audio:
        return await voice_receive(update, context)

    await update.message.reply_text(
        "🎙️ <b>Voice-to-App</b>\n\n"
        "Send me a voice message describing what you want built.\n"
        "I'll transcribe it, extract requirements, and start building.\n\n"
        "Perfect for right after a client call!",
        parse_mode="HTML",
    )
    return VOICE_WAIT


async def voice_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = await update.message.reply_text("🎙️ Transcribing...")

    transcript = await _handle_voice(update)
    if not transcript:
        await msg.edit_text("❌ Couldn't transcribe. Try again or use /new.")
        return ConversationHandler.END

    await msg.edit_text("🧠 Analyzing requirements from your recording...")

    # Use Claude to extract structured requirements
    try:
        extracted = await Transcriber.extract_requirements(transcript)
    except Exception as e:
        logger.exception("Requirement extraction failed")
        extracted = {
            "project_name": "voice-project",
            "app_type": "fullstack",
            "summary": transcript[:200],
            "brief": transcript,
        }

    # Store extracted data
    context.user_data["project_name"] = extracted.get("project_name", "voice-project")
    context.user_data["stack"] = stacks.normalize_stack(
        extracted.get("stack") or extracted.get("app_type") or ""
    )
    context.user_data["brief"] = extracted.get("brief", transcript)
    context.user_data["raw_transcript"] = transcript

    summary = extracted.get("summary", transcript[:200])
    name = context.user_data["project_name"]
    stack = context.user_data["stack"]
    brief_preview = context.user_data["brief"][:400]

    await msg.edit_text(
        f"🎯 <b>Here's what I extracted:</b>\n\n"
        f"📛 <b>Project:</b> {name}\n"
        f"🧱 <b>Stack:</b> {stacks.stack_display(stack)}\n"
        f"📝 <b>Summary:</b> {summary}\n\n"
        f"<b>Brief:</b>\n<i>{brief_preview}{'...' if len(context.user_data['brief']) > 400 else ''}</i>",
        parse_mode="HTML",
    )

    await update.message.reply_text(
        "What would you like to do?",
        reply_markup=CONFIRM_KEYBOARD,
    )
    return VOICE_CONFIRM


async def voice_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()

    if choice == "✅ Build it!":
        name = context.user_data.get("project_name", "voice-project")
        stack = context.user_data.get("stack", stacks.DEFAULT_STACK)
        brief = context.user_data.get("brief", "")

        await update.message.reply_text(
            f"🚀 Building <b>{name}</b>...\nStack: {stacks.stack_display(stack)}\n\nProgress updates below.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )

        await create_project(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            name=name,
            brief=brief,
            stack=stack,
        )
        context.user_data.clear()
        return ConversationHandler.END

    elif choice == "✏️ Edit brief":
        await update.message.reply_text(
            "Send the updated brief or additional instructions:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return VOICE_EDIT

    elif choice == "🔄 Change type":
        await update.message.reply_text(
            "Pick the stack:",
            reply_markup=STACK_KEYBOARD,
        )
        # After they pick, we go back to VOICE_CONFIRM via voice_retype
        return APP_TYPE

    else:  # Cancel
        context.user_data.clear()
        await update.message.reply_text(
            "Cancelled.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END


async def voice_edit_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["brief"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Brief updated! What next?",
        reply_markup=CONFIRM_KEYBOARD,
    )
    return VOICE_CONFIRM


async def voice_retype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    stack = stacks.stack_from_keyboard(choice)
    context.user_data["stack"] = stack
    await update.message.reply_text(
        f"Stack → <b>{stacks.stack_display(stack)}</b>. What next?",
        reply_markup=CONFIRM_KEYBOARD,
        parse_mode="HTML",
    )
    return VOICE_CONFIRM


# ──────────────────────────────────────────────
#  /update flow: modify an existing project
# ──────────────────────────────────────────────

async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /update — pick a project then give instructions."""
    # If they passed an ID directly: /update 3
    if context.args:
        try:
            project_id = int(context.args[0])
            project = await db.get(project_id)
            if not project:
                await update.message.reply_text(f"Project #{project_id} not found.")
                return ConversationHandler.END
            context.user_data["update_project_id"] = project.id
            context.user_data["update_project_name"] = project.name
            context.user_data["parts"] = []
            context.user_data["images"] = []
            await update.message.reply_text(
                f"📝 <b>Updating #{project.id} {project.name}</b>\n\n"
                f"Describe the changes — you can send <b>several messages</b>, "
                f"🎤 voice notes, and 🖼️ images.\n\n"
                f"Tap <b>✅ Done</b> when you're finished.",
                reply_markup=COLLECT_KEYBOARD,
                parse_mode="HTML",
            )
            return UPDATE_INSTRUCTIONS
        except ValueError:
            pass

    # No ID given — show project list to pick from
    projects = await db.list_all()
    if not projects:
        await update.message.reply_text("No projects yet. Use /new to create one.")
        return ConversationHandler.END

    lines = ["🔧 <b>Update a project</b>\n\nPick a project by sending its <b>ID number</b>:\n"]
    for p in projects:
        icon = {"live": "🟢", "stopped": "⚪", "failed": "🔴"}.get(p.status.value, "🟡")
        lines.append(f"{icon} <b>#{p.id}</b> — {p.name} ({p.status.value})")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )
    return UPDATE_SELECT


async def update_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picks which project to update."""
    try:
        project_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Send a project ID number. Example: 3")
        return UPDATE_SELECT

    project = await db.get(project_id)
    if not project:
        await update.message.reply_text(f"Project #{project_id} not found. Try again.")
        return UPDATE_SELECT

    context.user_data["update_project_id"] = project.id
    context.user_data["update_project_name"] = project.name
    context.user_data["parts"] = []
    context.user_data["images"] = []

    await update.message.reply_text(
        f"📝 <b>Updating #{project.id} {project.name}</b>\n\n"
        f"Describe the changes — you can send <b>several messages</b>, "
        f"🎤 voice notes, and 🖼️ images.\n\n"
        f"Tap <b>✅ Done</b> when you're finished.",
        reply_markup=COLLECT_KEYBOARD,
        parse_mode="HTML",
    )
    return UPDATE_INSTRUCTIONS


async def _finalize_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = context.user_data.get("parts", [])
    images = context.user_data.get("images", [])
    instructions = "\n\n".join(parts).strip()
    if not instructions and not images:
        await update.message.reply_text("Describe the change or attach an image first, then tap ✅ Done.")
        return UPDATE_INSTRUCTIONS
    if not instructions:
        instructions = "Apply the changes shown in the attached image(s)."

    project_id = context.user_data.get("update_project_id")
    project_name = context.user_data.get("update_project_name", "project")

    await update.message.reply_text(
        f"🔧 Updating <b>{project_name}</b>..."
        + (f"\n🖼️ {len(images)} image(s) attached" if images else "")
        + "\nProgress updates below.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update_project(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        project_id=project_id,
        instructions=instructions,
        images=images,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def update_instructions_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect text instructions for an update (multi-message)."""
    text = update.message.text.strip()
    if text in DONE_WORDS:
        return await _finalize_update(update, context)
    if text in CANCEL_WORDS:
        return await cancel(update, context)

    context.user_data.setdefault("parts", []).append(text)
    await update.message.reply_text(
        f"📝 Added. Collected so far: {_collected_summary(context)}.\n"
        "Send more, or tap ✅ Done.",
        reply_markup=COLLECT_KEYBOARD,
    )
    return UPDATE_INSTRUCTIONS


async def update_instructions_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect voice instructions for an update."""
    msg = await update.message.reply_text("🎙️ Transcribing...")
    transcript = await _handle_voice(update)
    if not transcript:
        await msg.edit_text("❌ Couldn't transcribe. Try again or send text.")
        return UPDATE_INSTRUCTIONS

    context.user_data.setdefault("parts", []).append(transcript)
    await msg.edit_text(
        f"🎙️ <b>Transcribed:</b> <i>{transcript[:300]}"
        f"{'...' if len(transcript) > 300 else ''}</i>",
        parse_mode="HTML",
    )
    await update.message.reply_text(
        f"Collected so far: {_collected_summary(context)}. Send more, or tap ✅ Done.",
        reply_markup=COLLECT_KEYBOARD,
    )
    return UPDATE_INSTRUCTIONS


async def update_instructions_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect a reference image for an update."""
    path = await _handle_photo(update)
    if not path:
        await update.message.reply_text("❌ Couldn't read that image. Try again.")
        return UPDATE_INSTRUCTIONS

    context.user_data.setdefault("images", []).append(path)
    caption = (update.message.caption or "").strip()
    if caption:
        context.user_data.setdefault("parts", []).append(caption)

    await update.message.reply_text(
        f"🖼️ Image added. Collected so far: {_collected_summary(context)}.\n"
        "Send more, or tap ✅ Done.",
        reply_markup=COLLECT_KEYBOARD,
    )
    return UPDATE_INSTRUCTIONS


# ──────────────────────────────────────────────
#  /import flow: adopt an existing repo and deploy it
# ──────────────────────────────────────────────

def _looks_like_git_url(url: str) -> bool:
    return url.startswith(("http://", "https://", "git@", "ssh://"))


async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /import — adopt an already-built repo and deploy it by subdomain."""
    # Allow `/import <url>` to skip straight to the name step.
    if context.args and _looks_like_git_url(context.args[0].strip()):
        context.user_data["import_url"] = context.args[0].strip()
        await update.message.reply_text(
            "📥 <b>Import repo</b>\n\n"
            "What subdomain name should it get?\n"
            "<i>(e.g. 'acme-api' → acme-api.yourdomain.com)</i>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return IMPORT_NAME

    await update.message.reply_text(
        "📥 <b>Import an existing repo</b>\n\n"
        "Send the <b>HTTPS Git URL</b> of a repo you've already built and committed.\n"
        "<i>(e.g. https://github.com/you/my-app)</i>\n\n"
        "I'll clone it, detect the stack, add a Dockerfile if one's missing, "
        "and deploy it — you only need to pick a subdomain next.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return IMPORT_URL


async def import_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    if url in CANCEL_WORDS:
        return await cancel(update, context)
    if not _looks_like_git_url(url):
        await update.message.reply_text(
            "That doesn't look like a Git URL. Send something like "
            "<code>https://github.com/you/my-app</code> (or /cancel).",
            parse_mode="HTML",
        )
        return IMPORT_URL

    context.user_data["import_url"] = url
    await update.message.reply_text(
        "What subdomain name should it get?\n"
        "<i>(e.g. 'acme-api' → acme-api.yourdomain.com)</i>",
        parse_mode="HTML",
    )
    return IMPORT_NAME


async def import_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if name in CANCEL_WORDS:
        return await cancel(update, context)

    from bot.services.orchestrator import slugify
    if not slugify(name):
        await update.message.reply_text(
            "That name has no usable characters for a subdomain. Try another (or /cancel)."
        )
        return IMPORT_NAME

    url = context.user_data.get("import_url", "")
    await update.message.reply_text(
        f"📥 Importing <b>{name}</b> from\n<code>{url}</code>\n\nProgress updates below.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await import_project(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        name=name,
        repo_url=url,
    )
    context.user_data.clear()
    return ConversationHandler.END


# ──────────────────────────────────────────────
#  Shared helpers
# ──────────────────────────────────────────────

async def _handle_voice(update: Update) -> str | None:
    voice = update.message.voice or update.message.audio
    if not voice:
        return None

    try:
        config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        file = await voice.get_file()
        suffix = ".ogg" if update.message.voice else ".mp3"
        temp_path = config.TEMP_DIR / f"voice_{update.message.message_id}{suffix}"
        await file.download_to_drive(str(temp_path))

        transcript = await Transcriber.transcribe(str(temp_path))
        temp_path.unlink(missing_ok=True)
        return transcript

    except Exception as e:
        logger.exception("Voice handling failed")
        return None


async def _handle_photo(update: Update) -> str | None:
    """Download the largest size of a photo message to TEMP_DIR; return its path."""
    photos = update.message.photo
    if not photos:
        return None
    try:
        config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        photo = photos[-1]  # highest resolution
        file = await photo.get_file()
        path = config.TEMP_DIR / f"img_{update.message.message_id}_{photo.file_unique_id}.jpg"
        await file.download_to_drive(str(path))
        return str(path)
    except Exception:
        logger.exception("Photo handling failed")
        return None


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Cancelled. Use /new or /voice to start over.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
#  Handler builders
# ──────────────────────────────────────────────

def get_conversation_handler() -> ConversationHandler:
    """Standard /new flow: name → type → brief (text or voice)."""
    return ConversationHandler(
        entry_points=[CommandHandler("new", new_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            APP_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_type)],
            BRIEF: [
                MessageHandler(filters.PHOTO, receive_brief_photo),
                MessageHandler(filters.VOICE | filters.AUDIO, receive_brief_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_brief_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="new_project",
    )


def get_update_conversation_handler() -> ConversationHandler:
    """/update flow: pick project → give instructions → rebuild with changes."""
    return ConversationHandler(
        entry_points=[CommandHandler("update", update_start)],
        states={
            UPDATE_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_select),
            ],
            UPDATE_INSTRUCTIONS: [
                MessageHandler(filters.PHOTO, update_instructions_photo),
                MessageHandler(filters.VOICE | filters.AUDIO, update_instructions_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_instructions_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="update_flow",
    )


def get_import_conversation_handler() -> ConversationHandler:
    """/import flow: repo URL → subdomain name → clone & deploy (no AI)."""
    return ConversationHandler(
        entry_points=[CommandHandler("import", import_start)],
        states={
            IMPORT_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, import_receive_url),
            ],
            IMPORT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, import_receive_name),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="import_flow",
    )


def get_voice_conversation_handler() -> ConversationHandler:
    """
    Quick /voice flow: send voice → AI extracts everything → confirm → build.
    Also catches standalone voice messages sent outside any conversation.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("voice", voice_start),
            # Catch standalone voice messages when no other conversation is active
            MessageHandler(filters.VOICE & ~filters.COMMAND, voice_receive),
        ],
        states={
            VOICE_WAIT: [
                MessageHandler(filters.VOICE | filters.AUDIO, voice_receive),
            ],
            VOICE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, voice_confirm_handler),
            ],
            VOICE_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, voice_edit_brief),
            ],
            APP_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, voice_retype),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="voice_flow",
    )
