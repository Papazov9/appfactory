from __future__ import annotations

import aiosqlite
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from bot.config import config


class ProjectStatus(str, Enum):
    PENDING = "pending"
    ESTIMATING = "estimating"
    AWAITING_APPROVAL = "awaiting_approval"
    ANALYZING = "analyzing"
    BUILDING = "building"
    INSTALLING = "installing"
    DOCKERIZING = "dockerizing"
    DEPLOYING = "deploying"
    LIVE = "live"
    FAILED = "failed"
    STOPPED = "stopped"


# Maps status to progress percentage and display label
STATUS_PROGRESS: dict[ProjectStatus, tuple[int, str]] = {
    ProjectStatus.PENDING: (0, "⏳ Queued"),
    ProjectStatus.ESTIMATING: (5, "📊 Estimating cost"),
    ProjectStatus.AWAITING_APPROVAL: (7, "⏸️ Awaiting your approval"),
    ProjectStatus.ANALYZING: (10, "🧠 Analyzing brief"),
    ProjectStatus.BUILDING: (30, "🔨 Building application"),
    ProjectStatus.INSTALLING: (60, "📦 Installing dependencies"),
    ProjectStatus.DOCKERIZING: (75, "🐳 Creating container"),
    ProjectStatus.DEPLOYING: (90, "🌐 Setting up subdomain"),
    ProjectStatus.LIVE: (100, "✅ Live!"),
    ProjectStatus.FAILED: (-1, "❌ Failed"),
    ProjectStatus.STOPPED: (-1, "⏹️ Stopped"),
}


@dataclass
class Project:
    id: Optional[int] = None
    name: str = ""
    slug: str = ""  # Used as subdomain
    brief: str = ""
    app_type: str = "fullstack"  # fullstack, static, landing, dashboard
    status: ProjectStatus = ProjectStatus.PENDING
    port: int = 0
    container_id: str = ""
    url: str = ""
    error_log: str = ""
    build_log: str = ""
    # Cost tracking
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    complexity: str = ""
    build_report_json: str = ""  # JSON string of full build report
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    telegram_chat_id: int = 0
    telegram_progress_msg_id: int = 0

    @property
    def project_dir(self) -> Path:
        return config.PROJECTS_DIR / self.slug

    @property
    def progress(self) -> tuple[int, str]:
        return STATUS_PROGRESS.get(self.status, (0, "Unknown"))

    def progress_bar(self) -> str:
        pct, label = self.progress
        if pct < 0:
            return f"{label}\n{'▓' * 20}"
        filled = int(pct / 5)
        empty = 20 - filled
        bar = "▓" * filled + "░" * empty
        return f"{label}\n{bar} {pct}%"


class ProjectDB:
    """Async SQLite wrapper for project persistence."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = str(db_path or config.DB_PATH)

    async def init(self):
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    slug TEXT UNIQUE NOT NULL,
                    brief TEXT NOT NULL,
                    app_type TEXT DEFAULT 'fullstack',
                    status TEXT DEFAULT 'pending',
                    port INTEGER DEFAULT 0,
                    container_id TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    error_log TEXT DEFAULT '',
                    build_log TEXT DEFAULT '',
                    estimated_cost_usd REAL DEFAULT 0,
                    actual_cost_usd REAL DEFAULT 0,
                    total_input_tokens INTEGER DEFAULT 0,
                    total_output_tokens INTEGER DEFAULT 0,
                    complexity TEXT DEFAULT '',
                    build_report_json TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    telegram_chat_id INTEGER DEFAULT 0,
                    telegram_progress_msg_id INTEGER DEFAULT 0
                )
            """)
            # Migrate existing tables — add new columns if they don't exist
            for col, coltype, default in [
                ("estimated_cost_usd", "REAL", "0"),
                ("actual_cost_usd", "REAL", "0"),
                ("total_input_tokens", "INTEGER", "0"),
                ("total_output_tokens", "INTEGER", "0"),
                ("complexity", "TEXT", "''"),
                ("build_report_json", "TEXT", "''"),
            ]:
                try:
                    await db.execute(
                        f"ALTER TABLE projects ADD COLUMN {col} {coltype} DEFAULT {default}"
                    )
                except Exception:
                    pass  # Column already exists
            await db.commit()

    async def save(self, project: Project) -> Project:
        async with aiosqlite.connect(self.db_path) as db:
            if project.id is None:
                cursor = await db.execute(
                    """INSERT INTO projects
                    (name, slug, brief, app_type, status, port, container_id,
                     url, error_log, build_log, estimated_cost_usd, actual_cost_usd,
                     total_input_tokens, total_output_tokens, complexity,
                     build_report_json, created_at, updated_at,
                     telegram_chat_id, telegram_progress_msg_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        project.name, project.slug, project.brief,
                        project.app_type, project.status.value, project.port,
                        project.container_id, project.url, project.error_log,
                        project.build_log, project.estimated_cost_usd,
                        project.actual_cost_usd, project.total_input_tokens,
                        project.total_output_tokens, project.complexity,
                        project.build_report_json, project.created_at,
                        project.updated_at, project.telegram_chat_id,
                        project.telegram_progress_msg_id,
                    ),
                )
                project.id = cursor.lastrowid
            else:
                project.updated_at = time.time()
                await db.execute(
                    """UPDATE projects SET
                    name=?, slug=?, brief=?, app_type=?, status=?, port=?,
                    container_id=?, url=?, error_log=?, build_log=?,
                    estimated_cost_usd=?, actual_cost_usd=?, total_input_tokens=?,
                    total_output_tokens=?, complexity=?, build_report_json=?,
                    updated_at=?, telegram_chat_id=?, telegram_progress_msg_id=?
                    WHERE id=?""",
                    (
                        project.name, project.slug, project.brief,
                        project.app_type, project.status.value, project.port,
                        project.container_id, project.url, project.error_log,
                        project.build_log, project.estimated_cost_usd,
                        project.actual_cost_usd, project.total_input_tokens,
                        project.total_output_tokens, project.complexity,
                        project.build_report_json, project.updated_at,
                        project.telegram_chat_id, project.telegram_progress_msg_id,
                        project.id,
                    ),
                )
            await db.commit()
        return project

    async def get(self, project_id: int) -> Optional[Project]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            )
            row = await cursor.fetchone()
            if row:
                return self._row_to_project(row)
        return None

    async def get_by_slug(self, slug: str) -> Optional[Project]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE slug=?", (slug,)
            )
            row = await cursor.fetchone()
            if row:
                return self._row_to_project(row)
        return None

    async def list_all(self) -> list[Project]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()
            return [self._row_to_project(r) for r in rows]

    async def list_live(self) -> list[Project]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE status='live' ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()
            return [self._row_to_project(r) for r in rows]

    async def next_available_port(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT port FROM projects WHERE status IN ('live', 'building', 'deploying')"
            )
            rows = await cursor.fetchall()
            used = {r[0] for r in rows}
        for port in range(config.PORT_RANGE_START, config.PORT_RANGE_END):
            if port not in used:
                return port
        raise RuntimeError("No available ports in configured range")

    async def delete(self, project_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM projects WHERE id=?", (project_id,))
            await db.commit()

    @staticmethod
    def _row_to_project(row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            brief=row["brief"],
            app_type=row["app_type"],
            status=ProjectStatus(row["status"]),
            port=row["port"],
            container_id=row["container_id"],
            url=row["url"],
            error_log=row["error_log"],
            build_log=row["build_log"],
            estimated_cost_usd=row["estimated_cost_usd"] if "estimated_cost_usd" in row.keys() else 0,
            actual_cost_usd=row["actual_cost_usd"] if "actual_cost_usd" in row.keys() else 0,
            total_input_tokens=row["total_input_tokens"] if "total_input_tokens" in row.keys() else 0,
            total_output_tokens=row["total_output_tokens"] if "total_output_tokens" in row.keys() else 0,
            complexity=row["complexity"] if "complexity" in row.keys() else "",
            build_report_json=row["build_report_json"] if "build_report_json" in row.keys() else "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_progress_msg_id=row["telegram_progress_msg_id"],
        )


# Global instance
db = ProjectDB()
