from __future__ import annotations

import argparse
import sqlite3

from app import DATABASE_PATH, init_db, normalize_username


def connect() -> sqlite3.Connection:
    init_db()
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def list_users() -> None:
    with connect() as db:
        rows = db.execute(
            "SELECT id, username, created_at, last_login_at FROM users ORDER BY id"
        ).fetchall()
    if not rows:
        print("暂无用户。")
        return
    for row in rows:
        print(f"{row['id']:>3}  {row['username']}  创建:{row['created_at']}  最近登录:{row['last_login_at'] or '-'}")


def delete_user(student_id: str) -> None:
    with connect() as db:
        cur = db.execute(
            "DELETE FROM users WHERE username_key = ?",
            (normalize_username(student_id),),
        )
        if cur.rowcount == 0:
            raise SystemExit("未找到该学号。")
        db.commit()
    print("该学号及其轮次、进度、收藏和答题历史已删除。")


def main() -> None:
    parser = argparse.ArgumentParser(description="毛概刷题网学号账号管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-users", help="列出用户")
    delete = sub.add_parser("delete-user", help="删除用户及其全部数据")
    delete.add_argument("student_id")
    args = parser.parse_args()
    if args.command == "list-users":
        list_users()
    elif args.command == "delete-user":
        delete_user(args.student_id)


if __name__ == "__main__":
    main()
