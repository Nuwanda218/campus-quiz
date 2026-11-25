#!/usr/bin/env python3
"""将旧题库数据库安全迁移到新的考试范围题库。

迁移原则：
1. 先由升级脚本备份数据库；本脚本自身在一个事务内执行。
2. 保留用户、轮次以及仍在新题库中的答题记录、收藏和错题历史。
3. 退役题目的记录先写入 retired_* 归档表，再从在线统计表移除。
4. 旧题库完整轮次成绩与新题库不可直接比较，归档后重新开始新范围排名。
5. 可重复执行：已经迁移完成时不会重复归档，也不会破坏新题库数据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

QUESTION_SET_VERSION = "demo-questions-v1"
MIGRATION_NAME = "demo-questions-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def table_columns(db: sqlite3.Connection, name: str) -> set[str]:
    if not table_exists(db, name):
        return set()
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({name})").fetchall()}


def normalize_answer(value: Any) -> str:
    return "".join(sorted(set(str(value or "").strip().upper())))


def placeholders(values: Iterable[Any]) -> str:
    values = list(values)
    return ",".join("?" for _ in values)


def load_questions(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list) or not raw:
        raise ValueError("题库 JSON 必须是非空数组")

    result: dict[str, dict[str, Any]] = {}
    gradable_count = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("题库中存在非对象条目")
        qid = str(item.get("id") or "").strip()
        if not qid:
            raise ValueError("题库中存在缺少 id 的题目")
        if qid in result:
            raise ValueError(f"题目 id 重复：{qid}")
        answer = normalize_answer(item.get("answer"))
        labels = {str(o.get("label") or "").upper() for o in item.get("options", [])}
        if not answer or not set(answer).issubset(labels):
            raise ValueError(f"题目 {qid} 的答案与选项不匹配")
        item = dict(item)
        item["answer"] = answer
        result[qid] = item
        if item.get("gradable", True):
            gradable_count += 1
    return result, gradable_count


ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS retired_round_progress (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    selected_answer TEXT NOT NULL DEFAULT '',
    checked INTEGER NOT NULL DEFAULT 0,
    correct INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    review_only INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    UNIQUE(round_id, question_id, question_set_version)
);

CREATE TABLE IF NOT EXISTS retired_attempt_history (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_id INTEGER NOT NULL,
    submission_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    round_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    selected_answer TEXT NOT NULL,
    correct INTEGER,
    review_only INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    UNIQUE(submission_id, question_set_version)
);

CREATE TABLE IF NOT EXISTS retired_favorites (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    UNIQUE(user_id, question_id, question_set_version)
);

CREATE TABLE IF NOT EXISTS retired_wrong_summary (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    wrong_count INTEGER NOT NULL DEFAULT 0,
    last_wrong_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    UNIQUE(user_id, question_id, question_set_version)
);

CREATE TABLE IF NOT EXISTS retired_round_completions (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    total_questions INTEGER NOT NULL,
    correct_count INTEGER NOT NULL,
    wrong_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL,
    source_question_set_version TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    UNIQUE(round_id, source_question_set_version, question_set_version)
);

CREATE TABLE IF NOT EXISTS question_scope_migrations (
    migration_name TEXT PRIMARY KEY,
    question_set_version TEXT NOT NULL,
    question_count INTEGER NOT NULL,
    gradable_question_count INTEGER NOT NULL,
    migrated_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
"""


def count_rows(db: sqlite3.Connection, table: str, where: str = "", args: tuple[Any, ...] = ()) -> int:
    if not table_exists(db, table):
        return 0
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += " WHERE " + where
    return int(db.execute(sql, args).fetchone()[0])


def archive_retired_questions(
    db: sqlite3.Connection,
    active_ids: set[str],
    archived_at: str,
    report: dict[str, Any],
) -> None:
    reason = "题目不在本次划定考试范围内，已从在线题库退役"
    active_list = sorted(active_ids)
    marks = placeholders(active_list)
    not_active = f"question_id NOT IN ({marks})"
    args = tuple(active_list)

    if table_exists(db, "round_progress"):
        report["archived_round_progress"] = count_rows(db, "round_progress", not_active, args)
        db.execute(
            f"""
            INSERT OR IGNORE INTO retired_round_progress(
                round_id, question_id, selected_answer, checked, correct,
                attempts, review_only, updated_at, archived_at, reason, question_set_version
            )
            SELECT round_id, question_id, selected_answer, checked, correct,
                   attempts, review_only, updated_at, ?, ?, ?
            FROM round_progress WHERE {not_active}
            """,
            (archived_at, reason, QUESTION_SET_VERSION, *args),
        )
        db.execute(f"DELETE FROM round_progress WHERE {not_active}", args)

    if table_exists(db, "attempt_history"):
        report["archived_attempt_history"] = count_rows(db, "attempt_history", not_active, args)
        db.execute(
            f"""
            INSERT OR IGNORE INTO retired_attempt_history(
                original_id, submission_id, user_id, round_id, question_id,
                selected_answer, correct, review_only, created_at,
                archived_at, reason, question_set_version
            )
            SELECT id, submission_id, user_id, round_id, question_id,
                   selected_answer, correct, review_only, created_at, ?, ?, ?
            FROM attempt_history WHERE {not_active}
            """,
            (archived_at, reason, QUESTION_SET_VERSION, *args),
        )
        db.execute(f"DELETE FROM attempt_history WHERE {not_active}", args)

    if table_exists(db, "favorites"):
        report["archived_favorites"] = count_rows(db, "favorites", not_active, args)
        db.execute(
            f"""
            INSERT OR IGNORE INTO retired_favorites(
                user_id, question_id, created_at, archived_at, reason, question_set_version
            )
            SELECT user_id, question_id, created_at, ?, ?, ?
            FROM favorites WHERE {not_active}
            """,
            (archived_at, reason, QUESTION_SET_VERSION, *args),
        )
        db.execute(f"DELETE FROM favorites WHERE {not_active}", args)

    if table_exists(db, "wrong_summary"):
        report["archived_wrong_summary"] = count_rows(db, "wrong_summary", not_active, args)
        db.execute(
            f"""
            INSERT OR IGNORE INTO retired_wrong_summary(
                user_id, question_id, wrong_count, last_wrong_at,
                archived_at, reason, question_set_version
            )
            SELECT user_id, question_id, wrong_count, last_wrong_at, ?, ?, ?
            FROM wrong_summary WHERE {not_active}
            """,
            (archived_at, reason, QUESTION_SET_VERSION, *args),
        )
        db.execute(f"DELETE FROM wrong_summary WHERE {not_active}", args)



def archive_incompatible_completions(
    db: sqlite3.Connection,
    gradable_count: int,
    archived_at: str,
    report: dict[str, Any],
) -> None:
    if not table_exists(db, "round_completions"):
        report["archived_round_completions"] = 0
        return

    columns = table_columns(db, "round_completions")
    if "question_set_version" not in columns:
        db.execute(
            "ALTER TABLE round_completions ADD COLUMN question_set_version "
            "TEXT NOT NULL DEFAULT 'legacy-v10'"
        )
    db.execute(
        "UPDATE round_completions SET question_set_version='legacy-v10' "
        "WHERE question_set_version IS NULL OR TRIM(question_set_version)=''"
    )
    where = "question_set_version <> ? OR total_questions <> ?"
    args = (QUESTION_SET_VERSION, gradable_count)
    report["archived_round_completions"] = count_rows(db, "round_completions", where, args)
    reason = "旧题库与当前考试范围题量不同，完整轮次成绩不可直接比较"
    db.execute(
        f"""
        INSERT OR IGNORE INTO retired_round_completions(
            round_id, user_id, round_number, total_questions, correct_count,
            wrong_count, completed_at, source_question_set_version,
            archived_at, reason, question_set_version
        )
        SELECT round_id, user_id, round_number, total_questions, correct_count,
               wrong_count, completed_at, COALESCE(NULLIF(question_set_version,''),'legacy-v10'),
               ?, ?, ?
        FROM round_completions
        WHERE {where}
        """,
        (archived_at, reason, QUESTION_SET_VERSION, *args),
    )
    db.execute(f"DELETE FROM round_completions WHERE {where}", args)



def recalculate_live_records(
    db: sqlite3.Connection,
    questions: dict[str, dict[str, Any]],
    report: dict[str, Any],
) -> None:
    progress_updates = 0
    history_updates = 0

    if table_exists(db, "round_progress"):
        rows = db.execute(
            "SELECT round_id, question_id, selected_answer, checked, correct, review_only "
            "FROM round_progress"
        ).fetchall()
        for row in rows:
            q = questions.get(str(row["question_id"]))
            if q is None:
                continue
            selected = normalize_answer(row["selected_answer"])
            review_only = 0 if q.get("gradable", True) else 1
            if not int(row["checked"] or 0):
                correct: int | None = None
            elif review_only:
                correct = None
            else:
                correct = 1 if selected == q["answer"] else 0
            if (
                selected != str(row["selected_answer"] or "")
                or correct != row["correct"]
                or review_only != int(row["review_only"] or 0)
            ):
                db.execute(
                    "UPDATE round_progress SET selected_answer=?, correct=?, review_only=? "
                    "WHERE round_id=? AND question_id=?",
                    (selected, correct, review_only, row["round_id"], row["question_id"]),
                )
                progress_updates += 1

    if table_exists(db, "attempt_history"):
        rows = db.execute(
            "SELECT id, question_id, selected_answer, correct, review_only FROM attempt_history"
        ).fetchall()
        for row in rows:
            q = questions.get(str(row["question_id"]))
            if q is None:
                continue
            selected = normalize_answer(row["selected_answer"])
            review_only = 0 if q.get("gradable", True) else 1
            correct = None if review_only else (1 if selected == q["answer"] else 0)
            if (
                selected != str(row["selected_answer"] or "")
                or correct != row["correct"]
                or review_only != int(row["review_only"] or 0)
            ):
                db.execute(
                    "UPDATE attempt_history SET selected_answer=?, correct=?, review_only=? WHERE id=?",
                    (selected, correct, review_only, row["id"]),
                )
                history_updates += 1

    report["recalculated_round_progress"] = progress_updates
    report["recalculated_attempt_history"] = history_updates


def rebuild_wrong_summary(db: sqlite3.Connection, report: dict[str, Any]) -> None:
    if not table_exists(db, "wrong_summary") or not table_exists(db, "attempt_history"):
        report["rebuilt_wrong_summary_rows"] = 0
        return
    db.execute("DELETE FROM wrong_summary")
    db.execute(
        """
        INSERT INTO wrong_summary(user_id, question_id, wrong_count, last_wrong_at)
        SELECT user_id, question_id, COUNT(*), MAX(created_at)
        FROM attempt_history
        WHERE correct = 0 AND review_only = 0
        GROUP BY user_id, question_id
        """
    )
    report["rebuilt_wrong_summary_rows"] = count_rows(db, "wrong_summary")


def sanitize_users(db: sqlite3.Connection, active_ids: set[str], report: dict[str, Any]) -> None:
    if not table_exists(db, "users"):
        report["sanitized_users"] = 0
        report["cleared_current_question_ids"] = 0
        return
    allowed_keys = {"mode", "chapter", "type", "status", "search", "randomIds", "priorityIds"}
    sanitized = 0
    cleared = 0
    rows = db.execute("SELECT id, current_question_id, ui_state FROM users").fetchall()
    for row in rows:
        current_id = row["current_question_id"]
        if current_id and current_id not in active_ids:
            current_id = None
            cleared += 1
        try:
            raw = json.loads(row["ui_state"] or "{}")
            if not isinstance(raw, dict):
                raw = {}
        except (json.JSONDecodeError, TypeError):
            raw = {}
        clean = {k: raw[k] for k in allowed_keys if k in raw}
        for key in ("randomIds", "priorityIds"):
            vals = clean.get(key, [])
            if not isinstance(vals, list):
                vals = []
            clean[key] = [str(v) for v in vals if str(v) in active_ids][:20]
        if clean.get("mode") not in {"all", "wrong", "favorite", "random", "priority"}:
            clean["mode"] = "all"
        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        if encoded != str(row["ui_state"] or "{}") or current_id != row["current_question_id"]:
            db.execute(
                "UPDATE users SET ui_state=?, current_question_id=? WHERE id=?",
                (encoded, current_id, row["id"]),
            )
            sanitized += 1
    report["sanitized_users"] = sanitized
    report["cleared_current_question_ids"] = cleared


def run_migration(db_path: Path, questions_path: Path, report_path: Path | None) -> dict[str, Any]:
    questions, gradable_count = load_questions(questions_path)
    active_ids = set(questions)
    migrated_at = utc_now()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=60)
    db.row_factory = sqlite3.Row
    report: dict[str, Any] = {
        "migration": MIGRATION_NAME,
        "question_set_version": QUESTION_SET_VERSION,
        "question_count": len(questions),
        "gradable_question_count": gradable_count,
        "migrated_at": migrated_at,
        "database": str(db_path),
    }
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=60000")
        # 归档表结构可重复创建；实际数据迁移统一置于下面的写事务中。
        db.executescript(ARCHIVE_SCHEMA)
        db.execute("BEGIN IMMEDIATE")

        before = {
            name: count_rows(db, name)
            for name in (
                "users",
                "rounds",
                "round_progress",
                "attempt_history",
                "favorites",
                "wrong_summary",
                "round_completions",
            )
        }
        report["before"] = before

        archive_retired_questions(db, active_ids, migrated_at, report)
        archive_incompatible_completions(db, gradable_count, migrated_at, report)
        recalculate_live_records(db, questions, report)
        rebuild_wrong_summary(db, report)
        sanitize_users(db, active_ids, report)

        after = {
            name: count_rows(db, name)
            for name in (
                "users",
                "rounds",
                "round_progress",
                "attempt_history",
                "favorites",
                "wrong_summary",
                "round_completions",
            )
        }
        archive_totals = {
            name: count_rows(db, name)
            for name in (
                "retired_round_progress",
                "retired_attempt_history",
                "retired_favorites",
                "retired_wrong_summary",
                "retired_round_completions",
            )
        }
        report["after"] = after
        report["archive_totals"] = archive_totals

        report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)
        db.execute(
            """
            INSERT INTO question_scope_migrations(
                migration_name, question_set_version, question_count,
                gradable_question_count, migrated_at, report_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(migration_name) DO UPDATE SET
                question_set_version=excluded.question_set_version,
                question_count=excluded.question_count,
                gradable_question_count=excluded.gradable_question_count,
                migrated_at=excluded.migrated_at,
                report_json=excluded.report_json
            """,
            (
                MIGRATION_NAME,
                QUESTION_SET_VERSION,
                len(questions),
                gradable_count,
                migrated_at,
                report_json,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def print_summary(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("毛概刷题网 · 考试范围题库数据库安全迁移")
    print("=" * 72)
    print(
        f"当前题库：{report['question_count']} 题；"
        f"题库版本：{report['question_set_version']}"
    )
    print(f"保留账号：{report['after'].get('users', 0)} 个；保留轮次：{report['after'].get('rounds', 0)} 个")
    print(
        "已归档并从在线统计移除："
        f"进度 {report.get('archived_round_progress', 0)} 条，"
        f"作答历史 {report.get('archived_attempt_history', 0)} 条，"
        f"收藏 {report.get('archived_favorites', 0)} 条，"
        f"错题汇总 {report.get('archived_wrong_summary', 0)} 条。"
    )
    print(
        f"旧范围完整轮次归档：{report.get('archived_round_completions', 0)} 条；"
        "新范围排名与正确率从当前题库重新统计。"
    )
    print(
        f"当前在线进度 {report['after'].get('round_progress', 0)} 条，"
        f"作答历史 {report['after'].get('attempt_history', 0)} 条，"
        f"错题汇总 {report['after'].get('wrong_summary', 0)} 条。"
    )
    print("迁移已在单个事务中完成；归档表可供后续审计或人工恢复。")


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移毛概刷题网考试范围题库数据库")
    parser.add_argument("database", type=Path, help="SQLite 数据库路径")
    parser.add_argument("questions", type=Path, help="当前 questions.json 路径")
    parser.add_argument("report", type=Path, nargs="?", help="可选：JSON 迁移报告输出路径")
    args = parser.parse_args()
    if not args.questions.is_file():
        parser.error(f"题库文件不存在：{args.questions}")
    report = run_migration(args.database, args.questions, args.report)
    print_summary(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        raise
