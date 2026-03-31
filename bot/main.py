#!/usr/bin/env python3
"""
AppFactory Bot — Telegram → AI Agent → Live Web App pipeline.

Usage:
    python -m bot.main
"""

import asyncio
import logging
import sys

from telegram.ext import ApplicationBuilder, CommandHandler

from bot.config import config
from bot.models.project import db
from bot.handlers.commands import (
    cmd_start,
    cmd_list,
    cmd_status,
    cmd_stop,
    cmd_delete,
    cmd_logs,
    cmd_rebuild,
    cmd_approve,
    cmd_cancel_build,
    cmd_cost,
    cmd_scan,
)
from bot.handlers.conversations import get_conversation_handler, get_voice_conversation_handler

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application):
    """Called after the application is initialized but before polling starts."""
    await db.init()
    logger.info("Database initialized")


def main():
    # Validate config
    errors = config.validate()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        sys.exit(1)

    # Ensure directories exist
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Build the Telegram application
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    # The conversation handler for /new must be added before the generic command handlers
    app.add_handler(get_conversation_handler())
    # Voice flow — catches /voice command AND standalone voice messages
    app.add_handler(get_voice_conversation_handler())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("rebuild", cmd_rebuild))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("cancel_build", cmd_cancel_build))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("scan", cmd_scan))

    # Start polling
    logger.info("AppFactory Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
