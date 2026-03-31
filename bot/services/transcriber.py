from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from bot.config import config

logger = logging.getLogger(__name__)


class Transcriber:
    """
    Transcribes voice messages and audio files.
    
    Supports two backends:
    1. OpenAI Whisper API (if OPENAI_API_KEY is set) — fastest, most accurate
    2. Local whisper CLI (if `whisper` is installed) — free, runs on VPS
    
    After transcription, uses Claude API to summarize and extract
    project requirements from the raw text.
    """

    @staticmethod
    async def transcribe(audio_path: str) -> str:
        """Transcribe an audio file to text."""
        if config.OPENAI_API_KEY:
            return await Transcriber._transcribe_openai(audio_path)
        else:
            return await Transcriber._transcribe_local(audio_path)

    @staticmethod
    async def _transcribe_openai(audio_path: str) -> str:
        """Use OpenAI Whisper API."""
        import httpx

        async with httpx.AsyncClient(timeout=120) as client:
            with open(audio_path, "rb") as f:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                    files={"file": (Path(audio_path).name, f, "audio/ogg")},
                    data={"model": "whisper-1"},
                )
            response.raise_for_status()
            return response.json()["text"]

    @staticmethod
    async def _transcribe_local(audio_path: str) -> str:
        """Use local whisper CLI (pip install openai-whisper)."""
        # First convert OGG to WAV (whisper works better with WAV)
        wav_path = audio_path.replace(".ogg", ".wav")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", audio_path, "-ar", "16000", "-ac", "1",
            "-y", wav_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # Run whisper
        output_dir = tempfile.mkdtemp()
        proc = await asyncio.create_subprocess_exec(
            "whisper", wav_path,
            "--model", "base",  # Use "small" or "medium" for better accuracy
            "--language", "en",
            "--output_format", "txt",
            "--output_dir", output_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"Whisper failed: {stderr.decode()}")
            raise RuntimeError(f"Transcription failed: {stderr.decode()[:200]}")

        # Read the output text file
        txt_file = Path(output_dir) / (Path(wav_path).stem + ".txt")
        if txt_file.exists():
            return txt_file.read_text().strip()

        # Fallback: parse stdout
        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    async def extract_requirements(transcript: str) -> dict:
        """
        Use Claude API to analyze a meeting transcript and extract:
        - Project name suggestion
        - App type (fullstack, landing, dashboard, static)
        - Structured requirements/brief
        """
        import httpx

        prompt = f"""Analyze this meeting transcript/voice note and extract project requirements.

TRANSCRIPT:
{transcript}

Respond with ONLY a JSON object (no markdown, no backticks):
{{
    "project_name": "short-slug-name for the project (lowercase, hyphens, max 30 chars)",
    "app_type": "one of: fullstack, landing, dashboard, static",
    "summary": "2-3 sentence summary of what was discussed",
    "brief": "Detailed project brief extracted from the conversation. Include: what the app should do, key features, target audience, design preferences, any specific requirements mentioned. Write this as clear instructions for a developer."
}}"""

        api_key = config.ANTHROPIC_API_KEY
        if not api_key:
            # Fallback: return raw transcript as brief
            return {
                "project_name": "voice-project",
                "app_type": "fullstack",
                "summary": transcript[:200],
                "brief": transcript,
            }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": config.CLAUDE_MODEL,
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            data = response.json()

        # Extract the text content
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        # Parse JSON from response
        try:
            # Clean up potential markdown formatting
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)
            return result
        except json.JSONDecodeError:
            logger.warning(f"Could not parse Claude response as JSON: {text[:200]}")
            return {
                "project_name": "voice-project",
                "app_type": "fullstack",
                "summary": transcript[:200],
                "brief": transcript,
            }
