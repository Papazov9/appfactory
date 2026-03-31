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
from bot.services.orchestrator import create_project
from bot.services.transcriber import Transcriber

logger = logging.getLogger(__name__)

# Conversation states
NAME, APP_TYPE, BRIEF = range(3)

# Quick voice flow states
VOICE_WAIT, VOICE_CONFIRM, VOICE_EDIT = 10, 11, 12

APP_TYPE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🌐 Full-Stack Web App", "📄 Landing Page"],
        ["📊 Dashboard", "📁 Static Site"],
    ],
    one_time_keyboard=True,
    resize_keyboard=True,
)

APP_TYPE_MAP = {
    "🌐 Full-Stack Web App": "fullstack",
    "📄 Landing Page": "landing",
    "📊 Dashboard": "dashboard",
    "📁 Static Site": "static",
}

CONFIRM_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ Build it!", "✏️ Edit brief", "🔄 Change type", "❌ Cancel"]],
    one_time_keyboard=True,
    resize_keyboard=True,
)


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
        "What type of application?",
        reply_markup=APP_TYPE_KEYBOARD,
    )
    return APP_TYPE


async def receive_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    app_type = APP_TYPE_MAP.get(choice, "fullstack")
    context.user_data["app_type"] = app_type

    await update.message.reply_text(
        "Now send me the project brief.\n\n"
        "You can send:\n"
        "• A text description\n"
        "• A 🎤 <b>voice message</b> (I'll transcribe it!)\n"
        "• An audio file of a recorded meeting\n\n"
        "The more detail, the better. Send /cancel to abort.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    return BRIEF


async def receive_brief_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    brief = update.message.text.strip()
    name = context.user_data.get("project_name", "unnamed")
    app_type = context.user_data.get("app_type", "fullstack")

    await update.message.reply_text(
        f"🚀 Starting build for <b>{name}</b>...\n"
        f"Type: {app_type}\n\n"
        "I'll send you progress updates below.",
        parse_mode="HTML",
    )

    await create_project(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        name=name,
        brief=brief,
        app_type=app_type,
    )

    context.user_data.clear()
    return ConversationHandler.END


async def receive_brief_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = await update.message.reply_text("🎙️ Transcribing your voice message...")

    transcript = await _handle_voice(update)
    if not transcript:
        await msg.edit_text("❌ Couldn't transcribe. Try again or send text instead.")
        return BRIEF

    await msg.edit_text(
        f"🎙️ <b>Transcription:</b>\n\n<i>{transcript[:800]}"
        f"{'...' if len(transcript) > 800 else ''}</i>",
        parse_mode="HTML",
    )

    name = context.user_data.get("project_name", "unnamed")
    app_type = context.user_data.get("app_type", "fullstack")

    await update.message.reply_text(
        f"🚀 Starting build for <b>{name}</b>...\nType: {app_type}",
        parse_mode="HTML",
    )

    await create_project(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        name=name,
        brief=transcript,
        app_type=app_type,
    )

    context.user_data.clear()
    return ConversationHandler.END


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
    context.user_data["app_type"] = extracted.get("app_type", "fullstack")
    context.user_data["brief"] = extracted.get("brief", transcript)
    context.user_data["raw_transcript"] = transcript

    summary = extracted.get("summary", transcript[:200])
    name = context.user_data["project_name"]
    app_type = context.user_data["app_type"]
    brief_preview = context.user_data["brief"][:400]

    await msg.edit_text(
        f"🎯 <b>Here's what I extracted:</b>\n\n"
        f"📛 <b>Project:</b> {name}\n"
        f"📦 <b>Type:</b> {app_type}\n"
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
        app_type = context.user_data.get("app_type", "fullstack")
        brief = context.user_data.get("brief", "")

        await update.message.reply_text(
            f"🚀 Building <b>{name}</b>...\nType: {app_type}\n\nProgress updates below.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )

        await create_project(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            name=name,
            brief=brief,
            app_type=app_type,
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
            "Pick the app type:",
            reply_markup=APP_TYPE_KEYBOARD,
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
    app_type = APP_TYPE_MAP.get(choice, context.user_data.get("app_type", "fullstack"))
    context.user_data["app_type"] = app_type
    await update.message.reply_text(
        f"Type → <b>{app_type}</b>. What next?",
        reply_markup=CONFIRM_KEYBOARD,
        parse_mode="HTML",
    )
    return VOICE_CONFIRM


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
                MessageHandler(filters.VOICE | filters.AUDIO, receive_brief_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_brief_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="new_project",
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
