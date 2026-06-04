"""Supported tech stacks.

Projects are constrained to these stacks so the generated code stays
conventional and idiomatic — the point is that the user can clone the repo
and be proficient maintaining it by hand. The AI no longer free-picks the
language; it builds within the chosen stack.

This module is pure data + helpers (no project imports) so it can be used
from handlers, the estimator, the agent builder, and the docker manager
without import cycles.
"""

from __future__ import annotations

STACK_PYTHON = "python"
STACK_SPRING = "spring"
STACK_ANGULAR = "angular"
STACK_SPRING_ANGULAR = "spring-angular"

# Ordered so the keyboard renders predictably.
STACKS: dict[str, dict] = {
    STACK_PYTHON: {
        "display": "Python API",
        "keyboard_label": "🐍 Python API",
        "default_db": "sqlite",
        # Specialist agents that apply to this stack, in run order (Phase 2).
        "agents": ["architect", "backend", "database", "qa"],
        "summary": "Python backend (FastAPI or Flask) with SQLite",
    },
    STACK_SPRING: {
        "display": "Spring Boot",
        "keyboard_label": "☕ Spring Boot",
        "default_db": "postgres",
        "agents": ["architect", "backend", "database", "qa"],
        "summary": "Spring Boot (Java) REST API with a PostgreSQL database",
    },
    STACK_ANGULAR: {
        "display": "Angular SPA",
        "keyboard_label": "🅰️ Angular SPA",
        "default_db": "none",
        "agents": ["architect", "frontend", "qa"],
        "summary": "Angular single-page app (frontend only)",
    },
    STACK_SPRING_ANGULAR: {
        "display": "Spring + Angular",
        "keyboard_label": "🧩 Spring + Angular",
        "default_db": "postgres",
        "agents": ["architect", "backend", "database", "frontend", "integrator", "qa"],
        "summary": "Angular frontend served by a Spring Boot backend + PostgreSQL",
    },
}

DEFAULT_STACK = STACK_PYTHON

# keyboard label -> stack key
_KEYBOARD_LABEL_TO_STACK = {
    meta["keyboard_label"]: key for key, meta in STACKS.items()
}

# Free-text aliases (used by voice extraction / heuristics).
_ALIASES = {
    "py": STACK_PYTHON,
    "fastapi": STACK_PYTHON,
    "flask": STACK_PYTHON,
    "django": STACK_PYTHON,
    "java": STACK_SPRING,
    "spring": STACK_SPRING,
    "spring-boot": STACK_SPRING,
    "springboot": STACK_SPRING,
    "ng": STACK_ANGULAR,
    "angular": STACK_ANGULAR,
    "spa": STACK_ANGULAR,
    "frontend": STACK_ANGULAR,
    "fullstack": STACK_SPRING_ANGULAR,
    "full-stack": STACK_SPRING_ANGULAR,
    "spring+angular": STACK_SPRING_ANGULAR,
}


def normalize_stack(value: str) -> str:
    """Coerce arbitrary input to a known stack key, defaulting safely."""
    if not value:
        return DEFAULT_STACK
    v = value.strip().lower()
    if v in STACKS:
        return v
    return _ALIASES.get(v, DEFAULT_STACK)


def stack_from_keyboard(label: str) -> str:
    """Map a keyboard button label back to a stack key."""
    return _KEYBOARD_LABEL_TO_STACK.get(label.strip(), DEFAULT_STACK)


def default_db_for(stack: str) -> str:
    """Default database kind for a stack: 'none' | 'sqlite' | 'postgres'."""
    return STACKS[normalize_stack(stack)]["default_db"]


def agents_for_stack(stack: str) -> list[str]:
    """Specialist agent pipeline for a stack (used by the builder/estimator)."""
    return list(STACKS[normalize_stack(stack)]["agents"])


def stack_display(stack: str) -> str:
    """Human-friendly name for a stack."""
    return STACKS[normalize_stack(stack)]["display"]


def stack_summary(stack: str) -> str:
    return STACKS[normalize_stack(stack)]["summary"]


def keyboard_rows() -> list[list[str]]:
    """Two-per-row keyboard layout of stack labels for python-telegram-bot."""
    labels = [meta["keyboard_label"] for meta in STACKS.values()]
    return [labels[i:i + 2] for i in range(0, len(labels), 2)]


# ──────────────────────────────────────────────
#  Per-stack build conventions
# ──────────────────────────────────────────────
#  These are injected into every agent prompt so the generated code is
#  conventional, idiomatic, and matches what the Dockerfile (Phase 3) expects:
#  the app must read PORT from the environment, bind 0.0.0.0, and never start
#  a long-running server during the build.

_PYTHON_RULES = """- Use FastAPI (preferred) with uvicorn, or Flask. Entry point: `app.py` at the project root.
- `app.py` must expose the app object AND start the server when run directly:
      import os, uvicorn
      if __name__ == "__main__":
          uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", {port})))
- `requirements.txt` must pin EVERY dependency (include `uvicorn[standard]` for FastAPI).
{db_block}"""

_PYTHON_DB = """- Database: SQLite at `./data/app.db` (the `./data` directory is a persistent Docker volume — create it at startup if missing). Use SQLAlchemy or the stdlib `sqlite3`. No external services or API keys."""

_SPRING_RULES = """- Maven project: `pom.xml` at root, sources under `src/main/java`, config in `src/main/resources/application.properties`.
- Spring Boot 3.x on Java 17+. The build `mvn -q -DskipTests package` must produce a single runnable jar in `target/`.
- Read the port from the environment in application.properties: `server.port=${{PORT:{port}}}` and bind `0.0.0.0`.
{db_block}
- Seed realistic demo data on startup (e.g. a `CommandLineRunner`) so the app looks populated."""

_SPRING_DB = """- Database: PostgreSQL via `spring-boot-starter-data-jpa` + the `org.postgresql:postgresql` driver. Configure the datasource from env vars with these exact defaults (the bot links a Postgres container reachable as host `db`):
      spring.datasource.url=${{SPRING_DATASOURCE_URL:jdbc:postgresql://db:5432/app}}
      spring.datasource.username=${{SPRING_DATASOURCE_USERNAME:app}}
      spring.datasource.password=${{SPRING_DATASOURCE_PASSWORD:app}}
      spring.jpa.hibernate.ddl-auto=update"""

_ANGULAR_RULES = """- Angular CLI project (Angular 17+): `angular.json`, `package.json`, sources under `src/`. Use standalone components and the modern control-flow (`@if`/`@for`) syntax.
- `npm run build` must output a production build to `dist/<project>/browser`. The container serves these static files via nginx; Angular itself does not read PORT.
- No backend in this stack: use in-memory/mock data or a configurable `environment.apiUrl`. Never call external APIs that need secrets.
- Make it look like a real product — thoughtful, responsive, polished loading/empty/error states."""

_SPRING_ANGULAR_RULES = """- One repo, two parts: a Spring Boot backend at the project root (Maven, as for the Spring stack) and an Angular frontend in `./frontend`.
- The Angular build output is copied into the backend's `src/main/resources/static`, so the single Spring Boot jar serves BOTH the REST API and the SPA on PORT. (The Dockerfile runs the multi-stage build — you only need the correct structure.)
- Backend: REST endpoints under `/api/**`, PostgreSQL via JPA (env-configured as in the Spring stack), demo data seeded on startup, `server.port=${{PORT:{port}}}`.
{db_block}
- Frontend: the Angular app calls the backend at same-origin `/api`. Use Angular's dev proxy only for local development."""

_STACK_RULE_BODIES = {
    STACK_PYTHON: (_PYTHON_RULES, _PYTHON_DB),
    STACK_SPRING: (_SPRING_RULES, _SPRING_DB),
    STACK_ANGULAR: (_ANGULAR_RULES, ""),
    STACK_SPRING_ANGULAR: (_SPRING_ANGULAR_RULES, _SPRING_DB),
}


def stack_rules(stack: str, port: int, db_kind: str) -> str:
    """Return the STACK RULES block injected into every agent prompt."""
    stack = normalize_stack(stack)
    body_tmpl, db_tmpl = _STACK_RULE_BODIES[stack]
    db_block = db_tmpl.format(port=port) if (db_tmpl and db_kind != "none") else ""
    body = body_tmpl.format(port=port, db_block=db_block).replace("\n\n", "\n")
    header = (
        f"=== STACK RULES — {stack_display(stack)} ===\n"
        f"The app MUST listen on the port from the PORT environment variable, "
        f"falling back to {port}; bind 0.0.0.0 (never localhost).\n"
        f"Do NOT start any long-running server during the build (the app is "
        f"started later in Docker) — only write files and run short "
        f"build/install/syntax-check commands.\n"
        f"Keep the code conventional and idiomatic so a human can clone the "
        f"repo and maintain it by hand.\n"
    )
    return header + body
