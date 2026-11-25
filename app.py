from __future__ import annotations

import json
import os
import secrets
import sqlite3
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
QUESTION_PATH = BASE_DIR / "data" / "questions.json"
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "instance" / "quiz.db"))
QUESTION_SET_VERSION = "demo-questions-v1"
APP_VERSION = "round-wrong-v12-public-demo"

with QUESTION_PATH.open("r", encoding="utf-8") as f:
    QUESTIONS: list[dict[str, Any]] = json.load(f)
QUESTION_MAP = {q["id"]: q for q in QUESTIONS}
GRADABLE_QUESTION_IDS = tuple(q["id"] for q in QUESTIONS if q.get("gradable", True))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.getenv("SESSION_DAYS", "365"))),
    SESSION_COOKIE_NAME=os.getenv("SESSION_COOKIE_NAME", "maogai_session"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_bool("COOKIE_SECURE", False),
    MAX_CONTENT_LENGTH=256 * 1024,
)
if env_bool("TRUST_PROXY", False):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

REGISTRATION_ENABLED = env_bool("ALLOW_REGISTRATION", True)


@app.after_request
def set_response_cache_headers(response):
    # 动态 HTML 不缓存，避免手机内置浏览器继续使用旧版菜单脚本。
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Maogai-Version"] = f"tju-{APP_VERSION}"
    return response


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    current_round_id INTEGER,
    current_question_id TEXT,
    ui_state TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    FOREIGN KEY (current_round_id) REFERENCES rounds(id)
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, round_number),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS round_progress (
    round_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    selected_answer TEXT NOT NULL DEFAULT '',
    checked INTEGER NOT NULL DEFAULT 0,
    correct INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    review_only INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (round_id, question_id),
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attempt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    round_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    selected_answer TEXT NOT NULL,
    correct INTEGER,
    review_only INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, question_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS wrong_summary (
    user_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    wrong_count INTEGER NOT NULL DEFAULT 0,
    last_wrong_at TEXT NOT NULL,
    PRIMARY KEY (user_id, question_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS round_wrong_questions (
    round_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    wrong_count INTEGER NOT NULL DEFAULT 1,
    first_wrong_at TEXT NOT NULL,
    last_wrong_at TEXT NOT NULL,
    PRIMARY KEY (round_id, question_id),
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS round_completions (
    round_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    total_questions INTEGER NOT NULL,
    correct_count INTEGER NOT NULL,
    wrong_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL,
    question_set_version TEXT NOT NULL DEFAULT 'demo-questions-v1',
    UNIQUE(user_id, round_number),
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rounds_user ON rounds(user_id);
CREATE INDEX IF NOT EXISTS idx_progress_round ON round_progress(round_id);
CREATE INDEX IF NOT EXISTS idx_attempt_user_question ON attempt_history(user_id, question_id);
CREATE INDEX IF NOT EXISTS idx_attempt_user_correct ON attempt_history(user_id, correct, question_id);
CREATE INDEX IF NOT EXISTS idx_wrong_summary_user_time ON wrong_summary(user_id, last_wrong_at DESC);
CREATE INDEX IF NOT EXISTS idx_round_wrong_round_time ON round_wrong_questions(round_id, last_wrong_at DESC);
CREATE INDEX IF NOT EXISTS idx_round_completions_user ON round_completions(user_id, completed_at);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DATABASE_PATH, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA temp_store = MEMORY")
        db.execute("PRAGMA wal_autocheckpoint = 1000")
        db.execute("PRAGMA busy_timeout = 30000")
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def backfill_round_completions(db: sqlite3.Connection) -> None:
    """升级旧数据库时，识别已经完整做完且仍保留答题卡的轮次。"""
    total_questions = len(GRADABLE_QUESTION_IDS)
    if total_questions <= 0:
        return
    placeholders = ",".join("?" for _ in GRADABLE_QUESTION_IDS)
    rounds = db.execute(
        "SELECT id, user_id, round_number FROM rounds ORDER BY id"
    ).fetchall()
    for round_row in rounds:
        stats = db.execute(
            f"""
            SELECT
                COUNT(*) AS answered_count,
                SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct_count,
                MAX(updated_at) AS completed_at
            FROM round_progress
            WHERE round_id = ?
              AND checked = 1
              AND question_id IN ({placeholders})
            """,
            (int(round_row["id"]), *GRADABLE_QUESTION_IDS),
        ).fetchone()
        answered = int(stats["answered_count"] or 0)
        if answered != total_questions:
            continue
        correct = int(stats["correct_count"] or 0)
        db.execute(
            """
            INSERT OR IGNORE INTO round_completions(
                round_id, user_id, round_number, total_questions,
                correct_count, wrong_count, completed_at, question_set_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(round_row["id"]),
                int(round_row["user_id"]),
                int(round_row["round_number"]),
                total_questions,
                correct,
                total_questions - correct,
                stats["completed_at"] or utc_now(),
                QUESTION_SET_VERSION,
            ),
        )


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA)
        completion_columns = {row[1] for row in db.execute("PRAGMA table_info(round_completions)").fetchall()}
        if "question_set_version" not in completion_columns:
            db.execute(
                "ALTER TABLE round_completions ADD COLUMN question_set_version TEXT NOT NULL DEFAULT 'legacy-v10'"
            )
        db.execute(
            "UPDATE round_completions SET question_set_version = 'legacy-v10' "
            "WHERE question_set_version IS NULL OR question_set_version = ''"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_round_completions_version "
            "ON round_completions(question_set_version, user_id, completed_at)"
        )
        # 兼容旧数据库：首次升级时把既有错题历史汇总到增量表。
        db.execute(
            """
            INSERT OR IGNORE INTO wrong_summary(user_id, question_id, wrong_count, last_wrong_at)
            SELECT user_id, question_id, COUNT(*), MAX(created_at)
            FROM attempt_history
            WHERE correct = 0
            GROUP BY user_id, question_id
            """
        )
        # 旧数据库没有区分正式轮次与专项训练的作答类型。为避免把随机20题、
        # 历史错题重练等误归入某一轮，仅从正式答题卡中保守回填旧轮次错题。
        db.execute(
            """
            INSERT OR IGNORE INTO round_wrong_questions(
                round_id, question_id, wrong_count, first_wrong_at, last_wrong_at
            )
            SELECT round_id, question_id, 1, updated_at, updated_at
            FROM round_progress
            WHERE checked = 1 AND correct = 0 AND review_only = 0
            """
        )
        backfill_round_completions(db)
        db.commit()
    finally:
        db.close()


init_db()


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_template_values() -> dict[str, Any]:
    return {"csrf_token": csrf_token()}


def csrf_valid() -> bool:
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    expected = session.get("csrf_token")
    return bool(provided and expected and secrets.compare_digest(provided, expected))


@app.before_request
def protect_csrf() -> Any:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint not in {"health", "login", "register"}:
        if not csrf_valid():
            if request.path.startswith("/api/"):
                return jsonify({"error": "csrf_failed", "message": "安全令牌已失效，请刷新页面后重试。"}), 403
            abort(403)
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "not_authenticated"}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapped


def normalize_username(username: str) -> str:
    return unicodedata.normalize("NFKC", username.strip()).casefold()


def validate_username(username: str) -> str | None:
    if not re.fullmatch(r"\d{10}", username):
        return "学号必须是10位数字。"
    return None


def get_user(user_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_owned_round(user_id: int, round_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM rounds WHERE id = ? AND user_id = ?", (round_id, user_id)
    ).fetchone()


def create_round(db: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    next_number = db.execute(
        "SELECT COALESCE(MAX(round_number), 0) + 1 FROM rounds WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0]
    now = utc_now()
    cur = db.execute(
        "INSERT INTO rounds(user_id, round_number, name, created_at) VALUES (?, ?, ?, ?)",
        (user_id, next_number, f"第{next_number}轮", now),
    )
    round_id = cur.lastrowid
    db.execute("UPDATE users SET current_round_id = ? WHERE id = ?", (round_id, user_id))
    return db.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()


def ensure_current_round(user_id: int) -> sqlite3.Row:
    db = get_db()
    user = get_user(user_id)
    current = None
    if user and user["current_round_id"]:
        current = get_owned_round(user_id, int(user["current_round_id"]))
    if current is None:
        current = create_round(db, user_id)
        db.commit()
    return current


def serialize_progress(round_id: int) -> dict[str, Any]:
    rows = get_db().execute(
        "SELECT * FROM round_progress WHERE round_id = ?", (round_id,)
    ).fetchall()
    result: dict[str, Any] = {}
    for row in rows:
        if row["question_id"] not in QUESTION_MAP:
            continue
        result[row["question_id"]] = {
            "selected": list(row["selected_answer"] or ""),
            "checked": bool(row["checked"]),
            "correct": None if row["correct"] is None else bool(row["correct"]),
            "attempts": int(row["attempts"]),
            "reviewOnly": bool(row["review_only"]),
            "updatedAt": row["updated_at"],
        }
    return result


def serialize_rounds(user_id: int) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT id, round_number, name, created_at FROM rounds WHERE user_id = ? ORDER BY round_number",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def serialize_favorites(user_id: int) -> list[str]:
    rows = get_db().execute(
        "SELECT question_id FROM favorites WHERE user_id = ? ORDER BY created_at", (user_id,)
    ).fetchall()
    return [row["question_id"] for row in rows if row["question_id"] in QUESTION_MAP]


def serialize_wrong_history(user_id: int) -> tuple[list[str], dict[str, int]]:
    rows = get_db().execute(
        """
        SELECT question_id, wrong_count
        FROM wrong_summary
        WHERE user_id = ? AND wrong_count > 0
        ORDER BY last_wrong_at DESC
        """,
        (user_id,),
    ).fetchall()
    rows = [row for row in rows if row["question_id"] in QUESTION_MAP]
    ids = [row["question_id"] for row in rows]
    counts = {row["question_id"]: int(row["wrong_count"]) for row in rows}
    return ids, counts


def serialize_round_wrong_sets(user_id: int) -> list[dict[str, Any]]:
    """返回每个正式轮次中曾经答错过的题目。

    正式轮次错题单独记录在 round_wrong_questions 中，随机20题、历史错题
    重练和高错率专项不会混入；正式轮次后续重做正确也不会抹掉该轮错题。
    """
    rows = get_db().execute(
        """
        SELECT
            r.id AS round_id,
            r.round_number,
            r.name,
            rw.question_id,
            rw.wrong_count,
            rw.last_wrong_at
        FROM rounds AS r
        LEFT JOIN round_wrong_questions AS rw ON rw.round_id = r.id
        WHERE r.user_id = ?
        ORDER BY r.round_number ASC, rw.last_wrong_at DESC, rw.question_id ASC
        """,
        (user_id,),
    ).fetchall()

    result: list[dict[str, Any]] = []
    by_round: dict[int, dict[str, Any]] = {}
    for row in rows:
        round_id = int(row["round_id"])
        item = by_round.get(round_id)
        if item is None:
            item = {
                "roundId": round_id,
                "roundNumber": int(row["round_number"]),
                "name": row["name"],
                "wrongIds": [],
                "wrongCounts": {},
                "wrongCount": 0,
            }
            by_round[round_id] = item
            result.append(item)
        question_id = row["question_id"]
        if question_id and question_id in QUESTION_MAP:
            question_id = str(question_id)
            item["wrongIds"].append(question_id)
            item["wrongCounts"][question_id] = int(row["wrong_count"] or 1)

    for item in result:
        item["wrongCount"] = len(item["wrongIds"])
    return result


def serialize_mistake_stats(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """按累计错题率排序，供用户进行高错率专项训练。"""
    limit = max(1, min(int(limit), 100))
    rows = get_db().execute(
        """
        SELECT
            question_id,
            COUNT(*) AS attempt_count,
            SUM(CASE WHEN correct = 0 THEN 1 ELSE 0 END) AS wrong_count
        FROM attempt_history
        WHERE user_id = ?
          AND review_only = 0
          AND correct IS NOT NULL
        GROUP BY question_id
        HAVING SUM(CASE WHEN correct = 0 THEN 1 ELSE 0 END) > 0
        ORDER BY
            (1.0 * SUM(CASE WHEN correct = 0 THEN 1 ELSE 0 END) / COUNT(*)) DESC,
            wrong_count DESC,
            attempt_count DESC,
            question_id ASC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        question_id = str(row["question_id"])
        if question_id not in QUESTION_MAP:
            continue
        attempts = int(row["attempt_count"] or 0)
        wrong = int(row["wrong_count"] or 0)
        result.append(
            {
                "questionId": question_id,
                "attemptCount": attempts,
                "wrongCount": wrong,
                "wrongRate": round((wrong / attempts) * 100, 1) if attempts else 0.0,
            }
        )
    return result



def round_has_completion(user_id: int, round_id: int) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM round_completions WHERE user_id = ? AND round_id = ? AND question_set_version = ?",
        (user_id, round_id, QUESTION_SET_VERSION),
    ).fetchone()
    return row is not None


def build_completion_summary(
    db: sqlite3.Connection,
    *,
    user_id: int,
    round_id: int,
) -> dict[str, Any]:
    round_row = db.execute(
        "SELECT id, user_id, round_number, name FROM rounds WHERE id = ? AND user_id = ?",
        (round_id, user_id),
    ).fetchone()
    if round_row is None:
        raise ValueError("轮次不存在或不属于当前账号")

    total_questions = len(GRADABLE_QUESTION_IDS)
    if total_questions <= 0:
        raise ValueError("当前题库没有可计分题目")
    placeholders = ",".join("?" for _ in GRADABLE_QUESTION_IDS)
    progress = db.execute(
        f"""
        SELECT
            COUNT(*) AS answered_count,
            SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct_count,
            MAX(updated_at) AS completed_at
        FROM round_progress
        WHERE round_id = ?
          AND checked = 1
          AND question_id IN ({placeholders})
        """,
        (round_id, *GRADABLE_QUESTION_IDS),
    ).fetchone()
    answered = int(progress["answered_count"] or 0)
    if answered < total_questions:
        remaining = total_questions - answered
        raise RuntimeError(f"本轮还有 {remaining} 道题未完成")

    correct = int(progress["correct_count"] or 0)
    wrong = total_questions - correct
    completed_at = progress["completed_at"] or utc_now()
    inserted = db.execute(
        """
        INSERT OR IGNORE INTO round_completions(
            round_id, user_id, round_number, total_questions,
            correct_count, wrong_count, completed_at, question_set_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            round_id,
            user_id,
            int(round_row["round_number"]),
            total_questions,
            correct,
            wrong,
            completed_at,
            QUESTION_SET_VERSION,
        ),
    ).rowcount > 0

    completion = db.execute(
        "SELECT * FROM round_completions WHERE round_id = ? AND user_id = ? AND question_set_version = ?",
        (round_id, user_id, QUESTION_SET_VERSION),
    ).fetchone()
    if completion is None:
        raise ValueError("轮次完成记录生成失败")

    leaderboard = db.execute(
        """
        SELECT
            user_id,
            COUNT(*) AS completed_rounds,
            SUM(total_questions) AS completed_questions,
            SUM(correct_count) AS correct_answers,
            SUM(wrong_count) AS wrong_answers,
            MIN(completed_at) AS first_completed_at
        FROM round_completions
        WHERE question_set_version = ?
        GROUP BY user_id
        ORDER BY
            completed_questions DESC,
            correct_answers DESC,
            completed_rounds DESC,
            first_completed_at ASC,
            user_id ASC
        """,
        (QUESTION_SET_VERSION,),
    ).fetchall()

    current = next((row for row in leaderboard if int(row["user_id"]) == user_id), None)
    if current is None:
        raise ValueError("累计统计生成失败")
    current_metrics = (
        int(current["completed_questions"] or 0),
        int(current["correct_answers"] or 0),
        int(current["completed_rounds"] or 0),
    )
    rank = 1 + sum(
        1
        for row in leaderboard
        if (
            int(row["completed_questions"] or 0),
            int(row["correct_answers"] or 0),
            int(row["completed_rounds"] or 0),
        ) > current_metrics
    )
    users_behind = sum(
        1
        for row in leaderboard
        if (
            int(row["completed_questions"] or 0),
            int(row["correct_answers"] or 0),
            int(row["completed_rounds"] or 0),
        ) < current_metrics
    )
    participant_count = len(leaderboard)
    completed_questions = int(current["completed_questions"] or 0)
    correct_answers = int(current["correct_answers"] or 0)
    wrong_answers = int(current["wrong_answers"] or 0)
    completed_rounds = int(current["completed_rounds"] or 0)
    cumulative_accuracy = (
        round(correct_answers / completed_questions * 100, 1)
        if completed_questions
        else 0.0
    )
    unique_wrong_row = db.execute(
        "SELECT COUNT(*) FROM wrong_summary WHERE user_id = ? AND wrong_count > 0",
        (user_id,),
    ).fetchone()
    unique_wrong_questions = int(unique_wrong_row[0] or 0) if unique_wrong_row else 0

    history_rows = db.execute(
        """
        SELECT round_number, total_questions, correct_count, wrong_count, completed_at
        FROM round_completions
        WHERE user_id = ? AND question_set_version = ?
        ORDER BY completed_at ASC, round_number ASC
        """,
        (user_id, QUESTION_SET_VERSION),
    ).fetchall()
    accuracy_history = []
    for row in history_rows:
        history_total = int(row["total_questions"] or 0)
        history_correct = int(row["correct_count"] or 0)
        accuracy_history.append(
            {
                "roundNumber": int(row["round_number"]),
                "totalQuestions": history_total,
                "correctAnswers": history_correct,
                "wrongAnswers": int(row["wrong_count"] or 0),
                "accuracy": round(history_correct / history_total * 100, 1) if history_total else 0.0,
                "completedAt": str(row["completed_at"]),
            }
        )

    round_total = int(completion["total_questions"])
    round_correct = int(completion["correct_count"])
    return {
        "newlyCompleted": inserted,
        "round": {
            "id": int(completion["round_id"]),
            "number": int(completion["round_number"]),
            "totalQuestions": round_total,
            "correctAnswers": round_correct,
            "wrongAnswers": int(completion["wrong_count"]),
            "accuracy": round(round_correct / round_total * 100, 1) if round_total else 0.0,
        },
        "cumulative": {
            "completedQuestions": completed_questions,
            "completedRounds": completed_rounds,
            "correctAnswers": correct_answers,
            "wrongAnswers": wrong_answers,
            "accuracy": cumulative_accuracy,
            "uniqueWrongQuestions": unique_wrong_questions,
        },
        "ranking": {
            "rank": rank,
            "participantCount": participant_count,
            "usersBehind": users_behind,
            "rule": "按完整轮次累计完成题数优先，其次累计答对题数排名；仅统计至少完成一轮的用户。",
        },
        "accuracyHistory": accuracy_history,
    }

def parse_ui_state(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def bootstrap_payload(user_id: int) -> dict[str, Any]:
    user = get_user(user_id)
    if user is None:
        abort(401)
    current = ensure_current_round(user_id)
    wrong_ids, wrong_counts = serialize_wrong_history(user_id)
    return {
        "user": {"id": user["id"], "username": user["username"]},
        "currentRound": dict(current),
        "rounds": serialize_rounds(user_id),
        "progress": serialize_progress(int(current["id"])),
        "favorites": serialize_favorites(user_id),
        "wrongIds": wrong_ids,
        "wrongCounts": wrong_counts,
        "roundWrongSets": serialize_round_wrong_sets(user_id),
        "mistakeStats": serialize_mistake_stats(user_id),
        "currentRoundCompleted": round_has_completion(user_id, int(current["id"])),
        "uiState": parse_ui_state(user["ui_state"]),
        "currentQuestionId": user["current_question_id"] if user["current_question_id"] in QUESTION_MAP else None,
        "csrfToken": csrf_token(),
        "questionSetVersion": QUESTION_SET_VERSION,
        "questionCount": len(QUESTIONS),
    }


@app.get("/health")
def health():
    return jsonify({"ok": True, "questions": len(QUESTIONS), "version": APP_VERSION, "questionSetVersion": QUESTION_SET_VERSION})


@app.get("/assets/questions.json")
def questions_asset():
    # URL 带版本号；题库未换版本时让浏览器长期复用本地缓存，避免重复经过 ngrok。
    response = send_file(
        QUESTION_PATH,
        mimetype="application/json; charset=utf-8",
        conditional=True,
        etag=True,
        max_age=365 * 24 * 60 * 60,
    )
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/api/username-availability")
def api_username_availability():
    """注册页实时检查用户名；数据库唯一索引仍是最终保障。"""
    username = request.args.get("username", "").strip()
    error = validate_username(username)
    if error is not None:
        return jsonify({"available": False, "valid": False, "message": error})
    exists = get_db().execute(
        "SELECT 1 FROM users WHERE username_key = ?",
        (normalize_username(username),),
    ).fetchone()
    if exists:
        return jsonify({"available": False, "valid": True, "message": "该学号已被注册，请直接登录或核对学号。"})
    return jsonify({"available": True, "valid": True, "message": "该学号可以注册。"})


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not REGISTRATION_ENABLED:
            error = "注册功能暂时关闭，请稍后再试。"
        else:
            username = request.form.get("username", "").strip()
            error = validate_username(username)
            if error is None:
                db = get_db()
                try:
                    cur = db.execute(
                        "INSERT INTO users(username, username_key, created_at) VALUES (?, ?, ?)",
                        (username, normalize_username(username), utc_now()),
                    )
                    user_id = int(cur.lastrowid)
                    create_round(db, user_id)
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    error = "该学号已经被注册，请直接登录或核对学号。"
                else:
                    session.clear()
                    session["user_id"] = user_id
                    session["csrf_token"] = secrets.token_urlsafe(32)
                    session.permanent = True
                    return redirect(url_for("index"))
    return render_template(
        "register.html",
        error=error,
        registration_enabled=REGISTRATION_ENABLED,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        format_error = validate_username(username)
        user = None if format_error else get_db().execute(
            "SELECT * FROM users WHERE username_key = ?", (normalize_username(username),)
        ).fetchone()
        if format_error:
            error = format_error
        elif user is None:
            error = "该学号尚未注册，请先注册。"
        else:
            session.clear()
            session["user_id"] = int(user["id"])
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            get_db().execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?", (utc_now(), user["id"])
            )
            get_db().commit()
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("index"))
    return render_template("login.html", error=error, registration_enabled=REGISTRATION_ENABLED)


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    user_id = int(session["user_id"])
    user = get_user(user_id)
    return render_template(
        "index.html",
        username=user["username"],
        initial_bootstrap=bootstrap_payload(user_id),
        question_count=len(QUESTIONS),
        app_version=APP_VERSION,
    )


@app.get("/api/bootstrap")
@login_required
def api_bootstrap():
    return jsonify(bootstrap_payload(int(session["user_id"])))


@app.get("/api/state")
@login_required
def api_state():
    user_id = int(session["user_id"])
    round_id = request.args.get("round_id", type=int)
    if round_id is None:
        round_id = int(ensure_current_round(user_id)["id"])
    if get_owned_round(user_id, round_id) is None:
        return jsonify({"error": "round_not_found"}), 404
    wrong_ids, wrong_counts = serialize_wrong_history(user_id)
    return jsonify(
        {
            "roundId": round_id,
            "progress": serialize_progress(round_id),
            "favorites": serialize_favorites(user_id),
            "wrongIds": wrong_ids,
            "wrongCounts": wrong_counts,
            "roundWrongSets": serialize_round_wrong_sets(user_id),
            "mistakeStats": serialize_mistake_stats(user_id),
            "roundCompleted": round_has_completion(user_id, round_id),
        }
    )


@app.post("/api/rounds/<int:round_id>/complete")
@login_required
def api_complete_round(round_id: int):
    user_id = int(session["user_id"])
    if get_owned_round(user_id, round_id) is None:
        return jsonify({"error": "round_not_found", "message": "轮次不存在。"}), 404
    db = get_db()
    try:
        summary = build_completion_summary(db, user_id=user_id, round_id=round_id)
        db.commit()
    except RuntimeError as exc:
        db.rollback()
        message = str(exc)
        match = re.search(r"(\d+)", message)
        return jsonify({
            "error": "round_incomplete",
            "message": message,
            "remaining": int(match.group(1)) if match else None,
        }), 409
    except ValueError as exc:
        db.rollback()
        return jsonify({"error": "invalid_round", "message": str(exc)}), 400
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("round completion statistics failed")
        return jsonify({"error": "database_busy", "message": "统计繁忙，请稍后重试。"}), 503
    return jsonify(summary)


@app.get("/api/mistake-stats")
@login_required
def api_mistake_stats():
    user_id = int(session["user_id"])
    limit = request.args.get("limit", default=20, type=int) or 20
    return jsonify({"mistakeStats": serialize_mistake_stats(user_id, limit)})


def validate_selected(question: dict[str, Any], raw: Any) -> str:
    if isinstance(raw, str):
        labels = list(raw)
    elif isinstance(raw, list):
        labels = [str(item) for item in raw]
    else:
        raise ValueError("selected 必须是字符串或数组")
    allowed = {str(option["label"]) for option in question["options"]}
    labels = sorted(set(labels))
    if any(label not in allowed for label in labels):
        raise ValueError("答案包含无效选项")
    return "".join(labels)



def progress_payload(
    selected: str,
    checked: int,
    correct: int | None,
    attempts: int,
    review_only: int,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "selected": list(selected),
        "checked": bool(checked),
        "correct": None if correct is None else bool(correct),
        "attempts": int(attempts),
        "reviewOnly": bool(review_only),
        "updatedAt": updated_at,
    }


def resolve_round_id(user_id: int, raw_round_id: Any = None) -> int:
    if raw_round_id in (None, ""):
        return int(ensure_current_round(user_id)["id"])
    try:
        round_id = int(raw_round_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("无效轮次") from exc
    if get_owned_round(user_id, round_id) is None:
        raise ValueError("轮次不存在或不属于当前账号")
    return round_id


def current_wrong_count(db: sqlite3.Connection, user_id: int, question_id: str) -> int:
    row = db.execute(
        "SELECT wrong_count FROM wrong_summary WHERE user_id = ? AND question_id = ?",
        (user_id, question_id),
    ).fetchone()
    return int(row[0]) if row else 0


def save_submission(
    db: sqlite3.Connection,
    *,
    user_id: int,
    round_id: int,
    question_id: str,
    selected: str,
    submission_id: str,
    validate_round: bool = True,
    include_details: bool = True,
) -> dict[str, Any]:
    question = QUESTION_MAP.get(question_id)
    if question is None:
        raise ValueError("题目不存在")
    if get_owned_round(user_id, round_id) is None:
        raise ValueError("轮次不存在或不属于当前账号")

    duplicate = db.execute(
        "SELECT 1 FROM attempt_history WHERE submission_id = ?", (submission_id,)
    ).fetchone()
    existing = db.execute(
        "SELECT * FROM round_progress WHERE round_id = ? AND question_id = ?",
        (round_id, question_id),
    ).fetchone()

    if duplicate is not None:
        if not include_details:
            return {"duplicate": True}
        if existing is None:
            return {
                "progress": progress_payload(selected, 1, None, 0, 0, utc_now()),
                "wrongCount": current_wrong_count(db, user_id, question_id),
                "duplicate": True,
            }
        return {
            "progress": progress_payload(
                existing["selected_answer"],
                existing["checked"],
                existing["correct"],
                existing["attempts"],
                existing["review_only"],
                existing["updated_at"],
            ),
            "wrongCount": current_wrong_count(db, user_id, question_id),
            "duplicate": True,
        }

    attempts = int(existing["attempts"]) if existing else 0
    review_only = 0 if question.get("gradable", True) else 1
    if review_only:
        correct: int | None = None
    else:
        expected = "".join(sorted(set(str(question.get("answer", "")))))
        correct = 1 if selected == expected else 0
    attempts += 1
    now = utc_now()

    db.execute(
        """
        INSERT INTO attempt_history(
            submission_id, user_id, round_id, question_id,
            selected_answer, correct, review_only, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            submission_id,
            user_id,
            round_id,
            question_id,
            selected,
            correct,
            review_only,
            now,
        ),
    )

    if correct == 0:
        db.execute(
            """
            INSERT INTO wrong_summary(user_id, question_id, wrong_count, last_wrong_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, question_id) DO UPDATE SET
                wrong_count = wrong_summary.wrong_count + 1,
                last_wrong_at = excluded.last_wrong_at
            """,
            (user_id, question_id, now),
        )
        db.execute(
            """
            INSERT INTO round_wrong_questions(
                round_id, question_id, wrong_count, first_wrong_at, last_wrong_at
            ) VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(round_id, question_id) DO UPDATE SET
                wrong_count = round_wrong_questions.wrong_count + 1,
                last_wrong_at = excluded.last_wrong_at
            """,
            (round_id, question_id, now, now),
        )

    db.execute(
        """
        INSERT INTO round_progress(
            round_id, question_id, selected_answer, checked,
            correct, attempts, review_only, updated_at
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(round_id, question_id) DO UPDATE SET
            selected_answer = excluded.selected_answer,
            checked = 1,
            correct = excluded.correct,
            attempts = excluded.attempts,
            review_only = excluded.review_only,
            updated_at = excluded.updated_at
        """,
        (round_id, question_id, selected, correct, attempts, review_only, now),
    )

    if not include_details:
        return {"duplicate": False}
    return {
        "progress": progress_payload(selected, 1, correct, attempts, review_only, now),
        "wrongCount": current_wrong_count(db, user_id, question_id),
        "duplicate": False,
    }


def save_practice_submission(
    db: sqlite3.Connection,
    *,
    user_id: int,
    round_id: int,
    question_id: str,
    selected: str,
    submission_id: str,
) -> dict[str, Any]:
    """保存随机/高错率专项训练记录，但不覆盖正式轮次答题卡。"""
    question = QUESTION_MAP.get(question_id)
    if question is None:
        raise ValueError("题目不存在")
    if get_owned_round(user_id, round_id) is None:
        raise ValueError("轮次不存在或不属于当前账号")

    duplicate = db.execute(
        "SELECT correct, review_only FROM attempt_history WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if duplicate is not None:
        return {
            "duplicate": True,
            "correct": None if duplicate["correct"] is None else bool(duplicate["correct"]),
            "reviewOnly": bool(duplicate["review_only"]),
            "wrongCount": current_wrong_count(db, user_id, question_id),
        }

    review_only = 0 if question.get("gradable", True) else 1
    if review_only:
        correct: int | None = None
    else:
        expected = "".join(sorted(set(str(question.get("answer", "")))))
        correct = 1 if selected == expected else 0
    now = utc_now()
    db.execute(
        """
        INSERT INTO attempt_history(
            submission_id, user_id, round_id, question_id,
            selected_answer, correct, review_only, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (submission_id, user_id, round_id, question_id, selected, correct, review_only, now),
    )
    if correct == 0:
        db.execute(
            """
            INSERT INTO wrong_summary(user_id, question_id, wrong_count, last_wrong_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, question_id) DO UPDATE SET
                wrong_count = wrong_summary.wrong_count + 1,
                last_wrong_at = excluded.last_wrong_at
            """,
            (user_id, question_id, now),
        )
    return {
        "duplicate": False,
        "correct": None if correct is None else bool(correct),
        "reviewOnly": bool(review_only),
        "wrongCount": current_wrong_count(db, user_id, question_id),
    }


def save_ui_state(db: sqlite3.Connection, user_id: int, payload: dict[str, Any]) -> None:
    allowed_keys = {"mode", "chapter", "type", "status", "search", "randomIds", "priorityIds", "wrongScope"}
    clean: dict[str, Any] = {k: payload[k] for k in allowed_keys if k in payload}
    for key in ("randomIds", "priorityIds"):
        if key in clean:
            if not isinstance(clean[key], list):
                clean[key] = []
            clean[key] = [qid for qid in clean[key] if qid in QUESTION_MAP][:20]
    if "wrongScope" in clean:
        value = clean["wrongScope"]
        clean["wrongScope"] = "all" if value == "all" else str(value)[:32]
    current_question_id = payload.get("currentId")
    if current_question_id not in QUESTION_MAP:
        current_question_id = None
    db.execute(
        "UPDATE users SET ui_state = ?, current_question_id = ? WHERE id = ?",
        (json.dumps(clean, ensure_ascii=False), current_question_id, user_id),
    )


@app.post("/api/sync")
@login_required
def api_batch_sync():
    payload = request.get_json(silent=True) or {}
    operations = payload.get("operations")
    if not isinstance(operations, list):
        return jsonify({"error": "invalid_operations", "message": "operations 必须是数组"}), 400
    if len(operations) > 500:
        return jsonify({"error": "too_many_operations", "message": "单次最多同步 500 项"}), 400

    compact = bool(payload.get("compact"))

    user_id = int(session["user_id"])
    db = get_db()
    current_round_id = int(ensure_current_round(user_id)["id"])
    owned_round_ids = {
        int(row["id"])
        for row in db.execute("SELECT id FROM rounds WHERE user_id = ?", (user_id,)).fetchall()
    }
    results: list[dict[str, Any]] = []
    try:
        # 提前取得写锁，让并发批量请求按顺序完成，减少 SQLite 锁竞争。
        db.execute("BEGIN IMMEDIATE")
        for raw in operations:
            if not isinstance(raw, dict):
                continue
            client_op_id = str(raw.get("clientOpId") or "")[:128]
            op_type = str(raw.get("type") or "")
            result: dict[str, Any] = {
                "clientOpId": client_op_id,
                "type": op_type,
                "ok": False,
            }
            try:
                if op_type == "submit":
                    question_id = str(raw.get("questionId") or "")
                    question = QUESTION_MAP.get(question_id)
                    if question is None:
                        raise ValueError("题目不存在")
                    selected = validate_selected(question, raw.get("selected", []))
                    raw_round_id = raw.get("roundId")
                    try:
                        round_id = current_round_id if raw_round_id in (None, "") else int(raw_round_id)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("无效轮次") from exc
                    if round_id not in owned_round_ids:
                        raise ValueError("轮次不存在或不属于当前账号")
                    submission_id = str(raw.get("submissionId") or client_op_id or secrets.token_urlsafe(18))[:128]
                    saved = save_submission(
                        db,
                        user_id=user_id,
                        round_id=round_id,
                        question_id=question_id,
                        selected=selected,
                        submission_id=submission_id,
                        validate_round=False,
                        include_details=not compact,
                    )
                    result.update(
                        ok=True,
                        questionId=question_id,
                        roundId=round_id,
                        duplicate=saved["duplicate"],
                    )
                    if not compact:
                        result.update(
                            progress=saved["progress"],
                            wrongCount=saved["wrongCount"],
                        )
                elif op_type == "practiceSubmit":
                    question_id = str(raw.get("questionId") or "")
                    question = QUESTION_MAP.get(question_id)
                    if question is None:
                        raise ValueError("题目不存在")
                    selected = validate_selected(question, raw.get("selected", []))
                    raw_round_id = raw.get("roundId")
                    try:
                        round_id = current_round_id if raw_round_id in (None, "") else int(raw_round_id)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("无效轮次") from exc
                    if round_id not in owned_round_ids:
                        raise ValueError("轮次不存在或不属于当前账号")
                    submission_id = str(raw.get("submissionId") or client_op_id or secrets.token_urlsafe(18))[:128]
                    saved = save_practice_submission(
                        db,
                        user_id=user_id,
                        round_id=round_id,
                        question_id=question_id,
                        selected=selected,
                        submission_id=submission_id,
                    )
                    result.update(
                        ok=True,
                        questionId=question_id,
                        roundId=round_id,
                        duplicate=saved["duplicate"],
                        correct=saved["correct"],
                        reviewOnly=saved["reviewOnly"],
                    )
                    if not compact:
                        result.update(wrongCount=saved["wrongCount"])
                elif op_type == "deleteProgress":
                    question_id = str(raw.get("questionId") or "")
                    if question_id not in QUESTION_MAP:
                        raise ValueError("题目不存在")
                    raw_round_id = raw.get("roundId")
                    try:
                        round_id = current_round_id if raw_round_id in (None, "") else int(raw_round_id)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("无效轮次") from exc
                    if round_id not in owned_round_ids:
                        raise ValueError("轮次不存在或不属于当前账号")
                    db.execute(
                        "DELETE FROM round_progress WHERE round_id = ? AND question_id = ?",
                        (round_id, question_id),
                    )
                    result.update(ok=True, questionId=question_id, roundId=round_id)
                elif op_type == "favorite":
                    question_id = str(raw.get("questionId") or "")
                    if question_id not in QUESTION_MAP:
                        raise ValueError("题目不存在")
                    favorite = bool(raw.get("favorite"))
                    if favorite:
                        db.execute(
                            "INSERT OR IGNORE INTO favorites(user_id, question_id, created_at) VALUES (?, ?, ?)",
                            (user_id, question_id, utc_now()),
                        )
                    else:
                        db.execute(
                            "DELETE FROM favorites WHERE user_id = ? AND question_id = ?",
                            (user_id, question_id),
                        )
                    result.update(ok=True, questionId=question_id, favorite=favorite)
                elif op_type == "uiState":
                    value = raw.get("value")
                    if not isinstance(value, dict):
                        raise ValueError("界面状态格式错误")
                    save_ui_state(db, user_id, value)
                    result.update(ok=True)
                else:
                    raise ValueError("不支持的同步操作")
            except ValueError as exc:
                result.update(error="invalid_operation", message=str(exc))
            results.append(result)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("batch sync failed")
        return jsonify({"error": "database_busy", "message": "同步繁忙，请稍后自动重试。"}), 503

    if compact:
        processed = [item["clientOpId"] for item in results if item.get("ok") and item.get("clientOpId")]
        failed = [
            {
                "clientOpId": item.get("clientOpId", ""),
                "message": item.get("message", "同步失败"),
            }
            for item in results
            if not item.get("ok")
        ]
        return jsonify(
            {
                "ok": True,
                "processed": processed,
                "failed": failed,
                "serverTime": utc_now(),
            }
        )
    return jsonify({"ok": True, "results": results, "serverTime": utc_now()})


@app.put("/api/progress/<question_id>")
@login_required
def api_save_progress(question_id: str):
    question = QUESTION_MAP.get(question_id)
    if question is None:
        return jsonify({"error": "question_not_found"}), 404
    payload = request.get_json(silent=True) or {}
    action = payload.get("action", "select")
    try:
        selected = validate_selected(question, payload.get("selected", []))
    except ValueError as exc:
        return jsonify({"error": "invalid_answer", "message": str(exc)}), 400

    user_id = int(session["user_id"])
    round_id = resolve_round_id(user_id, payload.get("roundId"))
    db = get_db()
    now = utc_now()

    if action == "select":
        existing = db.execute(
            "SELECT attempts FROM round_progress WHERE round_id = ? AND question_id = ?",
            (round_id, question_id),
        ).fetchone()
        attempts = int(existing["attempts"]) if existing else 0
        db.execute(
            """
            INSERT INTO round_progress(
                round_id, question_id, selected_answer, checked,
                correct, attempts, review_only, updated_at
            ) VALUES (?, ?, ?, 0, NULL, ?, 0, ?)
            ON CONFLICT(round_id, question_id) DO UPDATE SET
                selected_answer = excluded.selected_answer,
                checked = 0,
                correct = NULL,
                attempts = excluded.attempts,
                review_only = 0,
                updated_at = excluded.updated_at
            """,
            (round_id, question_id, selected, attempts, now),
        )
        db.commit()
        return jsonify(
            {
                "questionId": question_id,
                "progress": progress_payload(selected, 0, None, attempts, 0, now),
            }
        )

    if action != "submit":
        return jsonify({"error": "invalid_action"}), 400

    submission_id = str(payload.get("submissionId") or secrets.token_urlsafe(18))[:128]
    try:
        saved = save_submission(
            db,
            user_id=user_id,
            round_id=round_id,
            question_id=question_id,
            selected=selected,
            submission_id=submission_id,
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        return jsonify({"error": "database_busy", "message": "保存繁忙，请稍后重试。"}), 503
    return jsonify(
        {
            "questionId": question_id,
            "roundId": round_id,
            "progress": saved["progress"],
            "wrongCount": saved["wrongCount"],
        }
    )


@app.delete("/api/progress/<question_id>")
@login_required
def api_delete_progress(question_id: str):
    if question_id not in QUESTION_MAP:
        return jsonify({"error": "question_not_found"}), 404
    user_id = int(session["user_id"])
    round_id = int(ensure_current_round(user_id)["id"])
    db = get_db()
    db.execute(
        "DELETE FROM round_progress WHERE round_id = ? AND question_id = ?",
        (round_id, question_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.put("/api/favorites/<question_id>")
@login_required
def api_favorite(question_id: str):
    if question_id not in QUESTION_MAP:
        return jsonify({"error": "question_not_found"}), 404
    payload = request.get_json(silent=True) or {}
    favorite = bool(payload.get("favorite"))
    user_id = int(session["user_id"])
    db = get_db()
    if favorite:
        db.execute(
            "INSERT OR IGNORE INTO favorites(user_id, question_id, created_at) VALUES (?, ?, ?)",
            (user_id, question_id, utc_now()),
        )
    else:
        db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND question_id = ?",
            (user_id, question_id),
        )
    db.commit()
    return jsonify({"favorite": favorite, "favorites": serialize_favorites(user_id)})


@app.post("/api/rounds")
@login_required
def api_create_round():
    user_id = int(session["user_id"])
    db = get_db()
    new_round = create_round(db, user_id)
    db.execute(
        "UPDATE users SET current_question_id = NULL, ui_state = '{}' WHERE id = ?", (user_id,)
    )
    db.commit()
    return jsonify(bootstrap_payload(user_id)), 201


@app.post("/api/rounds/<int:round_id>/activate")
@login_required
def api_activate_round(round_id: int):
    user_id = int(session["user_id"])
    if get_owned_round(user_id, round_id) is None:
        return jsonify({"error": "round_not_found"}), 404
    db = get_db()
    db.execute(
        "UPDATE users SET current_round_id = ?, current_question_id = NULL WHERE id = ?",
        (round_id, user_id),
    )
    db.commit()
    return jsonify(bootstrap_payload(user_id))


@app.delete("/api/rounds/<int:round_id>/progress")
@login_required
def api_reset_round(round_id: int):
    user_id = int(session["user_id"])
    if get_owned_round(user_id, round_id) is None:
        return jsonify({"error": "round_not_found"}), 404
    db = get_db()
    db.execute("DELETE FROM round_progress WHERE round_id = ?", (round_id,))
    db.execute(
        "UPDATE users SET current_question_id = NULL WHERE id = ? AND current_round_id = ?",
        (user_id, round_id),
    )
    db.commit()
    # attempt_history intentionally remains: historical wrong questions are never erased here.
    wrong_ids, wrong_counts = serialize_wrong_history(user_id)
    return jsonify(
        {
            "ok": True,
            "wrongIds": wrong_ids,
            "wrongCounts": wrong_counts,
            "roundWrongSets": serialize_round_wrong_sets(user_id),
            "roundCompleted": round_has_completion(user_id, round_id),
        }
    )


@app.put("/api/ui-state")
@login_required
def api_ui_state():
    payload = request.get_json(silent=True) or {}
    db = get_db()
    save_ui_state(db, int(session["user_id"]), payload)
    db.commit()
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "request_too_large"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=env_bool("FLASK_DEBUG", False))
