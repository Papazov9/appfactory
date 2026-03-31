from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from bot.config import config

logger = logging.getLogger(__name__)

# Approximate costs per 1M tokens (Sonnet 4)
INPUT_COST_PER_1M = 3.00   # $3 per 1M input tokens
OUTPUT_COST_PER_1M = 15.00  # $15 per 1M output tokens

# Complexity tiers with estimated token ranges
COMPLEXITY_TIERS = {
    "trivial": {
        "label": "🟢 Trivial",
        "description": "Simple static page or minor change",
        "input_tokens": 5_000,
        "output_tokens": 10_000,
        "agents_needed": ["architect", "frontend"],
        "max_turns": 10,
        "estimated_minutes": 2,
    },
    "simple": {
        "label": "🟡 Simple",
        "description": "Landing page, basic site, single-purpose app",
        "input_tokens": 15_000,
        "output_tokens": 40_000,
        "agents_needed": ["architect", "frontend", "qa"],
        "max_turns": 20,
        "estimated_minutes": 5,
    },
    "moderate": {
        "label": "🟠 Moderate",
        "description": "Multi-page app, dashboard with charts, CRUD app",
        "input_tokens": 40_000,
        "output_tokens": 100_000,
        "agents_needed": ["architect", "backend", "frontend", "integrator", "qa"],
        "max_turns": 40,
        "estimated_minutes": 10,
    },
    "complex": {
        "label": "🔴 Complex",
        "description": "Full-stack app with auth, DB, multiple features",
        "input_tokens": 80_000,
        "output_tokens": 200_000,
        "agents_needed": ["architect", "backend", "database", "frontend", "integrator", "qa"],
        "max_turns": 60,
        "estimated_minutes": 20,
    },
    "massive": {
        "label": "⚫ Massive",
        "description": "Enterprise-grade, multi-module, complex business logic",
        "input_tokens": 150_000,
        "output_tokens": 400_000,
        "agents_needed": ["architect", "backend", "database", "frontend", "integrator", "qa"],
        "max_turns": 100,
        "estimated_minutes": 40,
    },
}


@dataclass
class CostEstimate:
    complexity: str
    tier_label: str
    tier_description: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    agents_needed: list[str]
    max_turns: int
    estimated_minutes: int
    features_detected: list[str]
    tech_stack_suggestion: str
    risk_notes: list[str]

    def format_telegram(self) -> str:
        cost_str = f"${self.estimated_cost_usd:.2f}"
        total_tokens = self.estimated_input_tokens + self.estimated_output_tokens
        agents_str = " → ".join(f"🤖 {a.title()}" for a in self.agents_needed)

        lines = [
            f"📊 <b>Cost Estimate</b>\n",
            f"{self.tier_label} — {self.tier_description}\n",
            f"💰 <b>Estimated cost:</b> {cost_str}",
            f"🪙 <b>Tokens:</b> ~{total_tokens:,} ({self.estimated_input_tokens:,} in / {self.estimated_output_tokens:,} out)",
            f"⏱️ <b>Build time:</b> ~{self.estimated_minutes} minutes",
            f"\n🏗️ <b>Agent Pipeline:</b>",
            f"{agents_str}",
            f"\n🔧 <b>Tech Stack:</b> {self.tech_stack_suggestion}",
        ]

        if self.features_detected:
            lines.append(f"\n📋 <b>Features detected:</b>")
            for feat in self.features_detected[:8]:
                lines.append(f"  • {feat}")

        if self.risk_notes:
            lines.append(f"\n⚠️ <b>Notes:</b>")
            for note in self.risk_notes:
                lines.append(f"  • {note}")

        return "\n".join(lines)


async def estimate_project(brief: str, app_type: str) -> CostEstimate:
    """
    Use Claude API to analyze the brief and estimate complexity/cost.
    This is a lightweight call (~500 tokens) that saves money by right-sizing the build.
    """
    prompt = f"""Analyze this project brief and classify its complexity.

APP TYPE: {app_type}

BRIEF:
{brief}

Respond with ONLY a JSON object (no markdown, no backticks):
{{
    "complexity": "one of: trivial, simple, moderate, complex, massive",
    "features": ["list", "of", "features", "detected", "in", "the", "brief"],
    "tech_stack": "Short tech stack recommendation (e.g., 'Express + React + SQLite')",
    "risk_notes": ["any concerns", "or things that might be tricky"],
    "reasoning": "One sentence explaining why you chose this complexity level"
}}

Classification guide:
- trivial: Static page, single HTML file, no logic
- simple: Landing page, portfolio, basic site with a few pages
- moderate: Dashboard, CRUD app, multi-page with some interactivity
- complex: Full-stack with auth, database, multiple API endpoints, real business logic
- massive: Enterprise app, many modules, complex workflows, admin panels"""

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        # Fallback: guess based on brief length and keywords
        return _heuristic_estimate(brief, app_type)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            data = response.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        parsed = json.loads(text)
        complexity = parsed.get("complexity", "moderate")
        if complexity not in COMPLEXITY_TIERS:
            complexity = "moderate"

        tier = COMPLEXITY_TIERS[complexity]

        input_tokens = tier["input_tokens"]
        output_tokens = tier["output_tokens"]
        cost = (input_tokens / 1_000_000 * INPUT_COST_PER_1M) + \
               (output_tokens / 1_000_000 * OUTPUT_COST_PER_1M)

        return CostEstimate(
            complexity=complexity,
            tier_label=tier["label"],
            tier_description=tier["description"],
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_usd=round(cost, 2),
            agents_needed=tier["agents_needed"],
            max_turns=tier["max_turns"],
            estimated_minutes=tier["estimated_minutes"],
            features_detected=parsed.get("features", []),
            tech_stack_suggestion=parsed.get("tech_stack", "Node.js + vanilla HTML"),
            risk_notes=parsed.get("risk_notes", []),
        )

    except Exception as e:
        logger.warning(f"Estimation API call failed: {e}, using heuristic")
        return _heuristic_estimate(brief, app_type)


def _heuristic_estimate(brief: str, app_type: str) -> CostEstimate:
    """Fallback estimation based on brief length and keywords."""
    words = len(brief.split())
    keywords_complex = ["auth", "login", "database", "payment", "api", "admin",
                        "dashboard", "user management", "roles", "permissions"]
    complexity_score = sum(1 for k in keywords_complex if k in brief.lower())

    if app_type == "static" or words < 30:
        complexity = "trivial"
    elif app_type == "landing" or words < 80:
        complexity = "simple"
    elif complexity_score >= 4 or words > 300:
        complexity = "complex"
    elif complexity_score >= 2 or words > 150:
        complexity = "moderate"
    else:
        complexity = "simple"

    tier = COMPLEXITY_TIERS[complexity]
    input_tokens = tier["input_tokens"]
    output_tokens = tier["output_tokens"]
    cost = (input_tokens / 1_000_000 * INPUT_COST_PER_1M) + \
           (output_tokens / 1_000_000 * OUTPUT_COST_PER_1M)

    return CostEstimate(
        complexity=complexity,
        tier_label=tier["label"],
        tier_description=tier["description"],
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=round(cost, 2),
        agents_needed=tier["agents_needed"],
        max_turns=tier["max_turns"],
        estimated_minutes=tier["estimated_minutes"],
        features_detected=[],
        tech_stack_suggestion="Auto-detected",
        risk_notes=["Estimate is heuristic-based (API unavailable)"],
    )
