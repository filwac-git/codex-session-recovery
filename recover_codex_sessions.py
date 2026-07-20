#!/usr/bin/env python3
"""Find, back up, and recover local Codex conversations on Windows.

The tool uses only Python's standard library. Scan is read-only. Repair requires
an explicit command, creates a complete backup first, and is designed to be run
while the Codex desktop app is fully closed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from typing import Any, Iterable


SESSION_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
PROVIDER_PAIR_RE = re.compile(
    rb'("model_provider"\s*:\s*")custom(")'
)
CUSTOM_TABLE_RE = re.compile(
    r"(?ms)^\[model_providers\.custom\][ \t]*\r?\n.*?(?=^\[|\Z)"
)
OFFICIAL_COMPAT_BLOCK = """
# Legacy conversation compatibility. Despite the historical table name
# `custom`, this endpoint is the official ChatGPT Codex service.
[model_providers.custom]
name = "ChatGPT Official (legacy compatibility)"
base_url = "https://chatgpt.com/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
""".strip()


@dataclass
class SessionRecord:
    session_id: str
    path: Path
    location: str
    created_at: int
    updated_at: int
    cwd: str = ""
    source: Any = "unknown"
    thread_source: str = "user"
    cli_version: str = ""
    model_providers: set[str] = field(default_factory=set)
    session_meta_count: int = 0
    custom_meta_count: int = 0
    first_user_message: str = ""
    invalid_json_lines: int = 0
    title: str = ""
    in_database: bool = False
    database_archived: bool = False

    @property
    def preview(self) -> str:
        return compact_text(self.first_user_message, 240)


@dataclass
class ScanResult:
    sessions: dict[str, SessionRecord]
    shadow_files: list[Path]
    index_titles: dict[str, str]
    database_rows: dict[str, dict[str, Any]]
    database_error: str | None


def compact_text(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def parse_timestamp(value: Any, fallback: int) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except ValueError:
        return fallback


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif item.get("type") in {"input_text", "output_text"}:
                    parts.append(str(item.get("content", "")))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return extract_text(content.get("content") or content.get("text"))
    return ""


def find_first_user_message(obj: dict[str, Any]) -> str:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return ""
    if payload.get("role") == "user":
        return extract_text(payload.get("content"))
    if payload.get("type") in {"user_message", "userMessage"}:
        return extract_text(payload.get("message") or payload.get("content"))
    return ""


def session_id_from_path(path: Path) -> str | None:
    match = SESSION_ID_RE.search(path.name)
    return match.group(1).lower() if match else None


def inspect_rollout(path: Path, location: str) -> SessionRecord | None:
    fallback_time = int(path.stat().st_mtime)
    path_id = session_id_from_path(path)
    record: SessionRecord | None = None
    latest_time = 0

    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if record:
                    record.invalid_json_lines += 1
                continue

            timestamp = parse_timestamp(obj.get("timestamp"), 0)
            if timestamp:
                latest_time = max(latest_time, timestamp)
            if obj.get("type") == "session_meta":
                payload = obj.get("payload") or {}
                # The rollout filename is the canonical identity. Spawned
                # sessions can begin with inherited parent metadata before
                # their own session_meta record appears later in the file.
                session_id = str(
                    path_id or payload.get("session_id") or payload.get("id") or ""
                ).lower()
                if not session_id:
                    continue
                if record is None:
                    record = SessionRecord(
                        session_id=session_id,
                        path=path,
                        location=location,
                        created_at=parse_timestamp(
                            payload.get("timestamp") or obj.get("timestamp"), fallback_time
                        ),
                        updated_at=latest_time or fallback_time,
                        cwd=str(payload.get("cwd") or ""),
                        source=payload.get("source") or "unknown",
                        thread_source=str(payload.get("thread_source") or "user"),
                        cli_version=str(payload.get("cli_version") or ""),
                    )
                record.session_meta_count += 1
                provider = payload.get("model_provider")
                if isinstance(provider, str):
                    record.model_providers.add(provider)
                    if provider == "custom":
                        record.custom_meta_count += 1

            if record and not record.first_user_message:
                record.first_user_message = find_first_user_message(obj)

    if record is None and path_id:
        record = SessionRecord(
            session_id=path_id,
            path=path,
            location=location,
            created_at=fallback_time,
            updated_at=fallback_time,
        )
    if record:
        record.updated_at = latest_time or fallback_time
    return record


def load_index_titles(codex_home: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return result
    with index_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(item.get("id") or "").lower()
            title = str(item.get("thread_name") or "").strip()
            if session_id and title:
                result[session_id] = title
    return result


def read_database_rows(codex_home: Path) -> tuple[dict[str, dict[str, Any]], str | None]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return {}, "state_5.sqlite 不存在"
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT id, title, rollout_path, archived, model_provider, model, cwd, "
                "created_at, updated_at FROM threads"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["id"]).lower(): dict(row) for row in rows}, None
    except sqlite3.Error as error:
        return {}, str(error)


def discover_sessions(codex_home: Path) -> ScanResult:
    index_titles = load_index_titles(codex_home)
    database_rows, database_error = read_database_rows(codex_home)
    candidates: list[tuple[Path, str]] = []
    for directory_name, location in (("sessions", "active"), ("archived_sessions", "archived")):
        root = codex_home / directory_name
        if root.exists():
            candidates.extend((path, location) for path in root.rglob("*.jsonl"))

    sessions: dict[str, SessionRecord] = {}
    shadows: list[Path] = []
    for path, location in sorted(candidates, key=lambda item: str(item[0])):
        record = inspect_rollout(path, location)
        if record is None:
            shadows.append(path)
            continue
        existing = sessions.get(record.session_id)
        if existing:
            choose_new = (
                existing.location == "archived" and record.location == "active"
            ) or record.updated_at > existing.updated_at
            if choose_new:
                shadows.append(existing.path)
                sessions[record.session_id] = record
            else:
                shadows.append(record.path)
        else:
            sessions[record.session_id] = record

    for session_id, record in sessions.items():
        db_row = database_rows.get(session_id)
        record.in_database = db_row is not None
        record.database_archived = bool(db_row and db_row.get("archived"))
        record.title = (
            index_titles.get(session_id)
            or str((db_row or {}).get("title") or "").strip()
            or compact_text(record.first_user_message, 80)
            or f"恢复的会话 {session_id[:8]}"
        )
    return ScanResult(sessions, shadows, index_titles, database_rows, database_error)


def format_time(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def print_scan(result: ScanResult) -> None:
    print(f"发现 {len(result.sessions)} 个具有本地 rollout 文件的会话。")
    print(
        f"其中数据库缺失 {sum(not s.in_database for s in result.sessions.values())} 个，"
        f"归档 {sum(s.location == 'archived' or s.database_archived for s in result.sessions.values())} 个，"
        f"仍使用 custom 元数据 {sum(bool(s.custom_meta_count) for s in result.sessions.values())} 个。"
    )
    print()
    for record in sorted(result.sessions.values(), key=lambda item: item.updated_at, reverse=True):
        flags: list[str] = []
        if record.location == "archived" or record.database_archived:
            flags.append("归档")
        if not record.in_database:
            flags.append("数据库缺失")
        if record.custom_meta_count:
            flags.append(f"custom×{record.custom_meta_count}")
        if record.invalid_json_lines:
            flags.append(f"损坏行×{record.invalid_json_lines}")
        flag_text = ", ".join(flags) if flags else "正常"
        print(f"- [{flag_text}] {record.title}")
        print(f"  ID: {record.session_id}")
        print(f"  更新时间: {format_time(record.updated_at)}")
        print(f"  文件: {record.path}")
    if result.shadow_files:
        print(f"\n另有 {len(result.shadow_files)} 个重复或无法识别的 JSONL 文件，恢复时不会覆盖主副本。")
    stale = sorted(set(result.index_titles) - set(result.sessions))
    if stale:
        print(f"\n索引中有 {len(stale)} 个条目没有本地会话文件，无法恢复正文：")
        for session_id in stale:
            print(f"- {result.index_titles[session_id]} ({session_id})")
    database_only = sorted(set(result.database_rows) - set(result.sessions))
    if database_only:
        print(f"\n数据库中有 {len(database_only)} 个记录没有本地会话文件，无法恢复正文：")
        for session_id in database_only:
            row = result.database_rows[session_id]
            title = str(row.get("title") or "无标题")
            print(f"- {title} ({session_id}) -> {row.get('rollout_path') or '无路径'}")
    if result.database_error:
        print(f"\n数据库读取提示: {result.database_error}")


def codex_is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except OSError:
        return False
    return "Codex.exe" in result.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def create_backup(codex_home: Path, result: ScanResult, backup_root: Path | None) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = backup_root or (codex_home / "session-recovery-backups")
    target = root / timestamp
    target.mkdir(parents=True, exist_ok=False)

    files: set[Path] = {record.path for record in result.sessions.values()}
    files.update(result.shadow_files)
    for name in (
        "config.toml",
        "session_index.jsonl",
        "state_5.sqlite",
        "state_5.sqlite-wal",
        "state_5.sqlite-shm",
    ):
        path = codex_home / name
        if path.exists():
            files.add(path)

    manifest: list[dict[str, Any]] = []
    for source in sorted(files, key=str):
        try:
            relative = source.relative_to(codex_home)
        except ValueError:
            relative = Path("external") / source.name
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest.append(
            {
                "relative_path": str(relative),
                "size": destination.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )

    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "codex_home": str(codex_home),
                "files": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def active_destination(codex_home: Path, record: SessionRecord) -> Path:
    created = dt.datetime.fromtimestamp(record.created_at)
    return codex_home / "sessions" / f"{created:%Y}" / f"{created:%m}" / f"{created:%d}" / record.path.name


def restore_archived_files(codex_home: Path, result: ScanResult) -> int:
    restored = 0
    for record in result.sessions.values():
        if record.location != "archived":
            continue
        destination = active_destination(codex_home, record)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) != sha256_file(record.path):
                raise RuntimeError(f"活动目录存在同名但内容不同的会话: {destination}")
        else:
            shutil.copy2(record.path, destination)
            restored += 1
        record.path = destination
        record.location = "active"
    return restored


def migrate_rollout_metadata(result: ScanResult) -> int:
    updated_records = 0
    for record in result.sessions.values():
        if not record.custom_meta_count:
            continue
        temp_path = record.path.with_suffix(record.path.suffix + ".session-recovery.tmp")
        changed = 0
        with record.path.open("rb") as source, temp_path.open("wb") as destination:
            for raw_line in source:
                new_line = raw_line
                try:
                    obj = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    obj = None
                if (
                    isinstance(obj, dict)
                    and obj.get("type") == "session_meta"
                    and (obj.get("payload") or {}).get("model_provider") == "custom"
                ):
                    new_line, replacements = PROVIDER_PAIR_RE.subn(rb"\1openai\2", raw_line, count=1)
                    if replacements != 1:
                        raise RuntimeError(f"无法精确迁移 provider 字段: {record.path}")
                    changed += 1
                destination.write(new_line)
            destination.flush()
            os.fsync(destination.fileno())
        if changed != record.custom_meta_count:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"会话元数据数量不一致: {record.path}; expected={record.custom_meta_count}, changed={changed}"
            )
        os.replace(temp_path, record.path)
        record.model_providers.discard("custom")
        record.model_providers.add("openai")
        record.custom_meta_count = 0
        updated_records += 1
    return updated_records


def load_default_model(codex_home: Path) -> str:
    config_path = codex_home / "config.toml"
    if config_path.exists():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            model = config.get("model")
            if isinstance(model, str) and model:
                return model
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            pass
    return "gpt-5.6-sol"


def serialize_source(source: Any) -> str:
    return source if isinstance(source, str) else json.dumps(source, ensure_ascii=False)


def ensure_database_rows(codex_home: Path, result: ScanResult) -> tuple[int, int]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        raise RuntimeError("state_5.sqlite 不存在，无法重建数据库索引")
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    inserted = 0
    updated = 0
    default_model = load_default_model(codex_home)
    try:
        connection.execute("BEGIN IMMEDIATE")
        column_info = connection.execute("PRAGMA table_info(threads)").fetchall()
        columns = {row["name"] for row in column_info}
        existing_ids = {
            str(row[0]).lower()
            for row in connection.execute("SELECT id FROM threads").fetchall()
        }
        for record in result.sessions.values():
            values: dict[str, Any] = {
                "id": record.session_id,
                "rollout_path": str(record.path.resolve()),
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "source": serialize_source(record.source),
                "model_provider": "openai",
                "cwd": record.cwd,
                "title": record.title,
                "sandbox_policy": json.dumps({"type": "read-only"}),
                "approval_mode": "on-request",
                "tokens_used": 0,
                "has_user_event": 1 if record.first_user_message else 0,
                "archived": 0,
                "archived_at": None,
                "cli_version": record.cli_version,
                "first_user_message": record.first_user_message,
                "memory_mode": "enabled",
                "model": default_model,
                "created_at_ms": record.created_at * 1000,
                "updated_at_ms": record.updated_at * 1000,
                "thread_source": record.thread_source,
                "preview": record.preview,
                "recency_at": record.updated_at,
                "recency_at_ms": record.updated_at * 1000,
                "history_mode": "legacy",
            }
            values = {key: value for key, value in values.items() if key in columns}
            if record.session_id in existing_ids:
                update_keys = [
                    key
                    for key in (
                        "rollout_path",
                        "model_provider",
                        "archived",
                        "archived_at",
                        "title",
                    )
                    if key in values
                ]
                connection.execute(
                    "UPDATE threads SET "
                    + ", ".join(f"{key}=?" for key in update_keys)
                    + " WHERE id=?",
                    [values[key] for key in update_keys] + [record.session_id],
                )
                updated += 1
            else:
                missing_required = [
                    row["name"]
                    for row in column_info
                    if row["notnull"]
                    and row["dflt_value"] is None
                    and not row["pk"]
                    and row["name"] not in values
                ]
                if missing_required:
                    raise RuntimeError(
                        "当前 Codex 数据库新增了工具尚未支持的必填列: "
                        + ", ".join(missing_required)
                    )
                keys = list(values)
                connection.execute(
                    f"INSERT INTO threads ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    [values[key] for key in keys],
                )
                inserted += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return inserted, updated


def update_session_index(codex_home: Path, result: ScanResult) -> int:
    path = codex_home / "session_index.jsonl"
    existing = load_index_titles(codex_home)
    appended = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in sorted(result.sessions.values(), key=lambda item: item.updated_at):
            if existing.get(record.session_id) == record.title:
                continue
            updated_at = dt.datetime.fromtimestamp(
                record.updated_at, tz=dt.timezone.utc
            ).isoformat().replace("+00:00", "Z")
            handle.write(
                json.dumps(
                    {
                        "id": record.session_id,
                        "thread_name": record.title,
                        "updated_at": updated_at,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            appended += 1
    return appended


def update_config(codex_home: Path, add_compat_alias: bool) -> None:
    path = codex_home / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    text = CUSTOM_TABLE_RE.sub("", text).rstrip()
    if add_compat_alias:
        block = OFFICIAL_COMPAT_BLOCK.replace("\n", newline)
        text = f"{text}{newline}{newline}{block}{newline}"
    elif text:
        text += newline
    tomllib.loads(text)
    temp = path.with_suffix(path.suffix + ".session-recovery.tmp")
    temp.write_text(text, encoding="utf-8", newline="")
    os.replace(temp, path)


def verify(codex_home: Path) -> list[str]:
    issues: list[str] = []
    result = discover_sessions(codex_home)
    for record in result.sessions.values():
        if record.custom_meta_count:
            issues.append(f"会话仍含 custom 元数据: {record.session_id}")
        if record.location == "archived" or record.database_archived:
            issues.append(f"会话仍处于归档状态: {record.session_id}")
        if not record.in_database:
            issues.append(f"会话缺少数据库记录: {record.session_id}")
    for session_id, row in result.database_rows.items():
        if row.get("model_provider") == "custom":
            issues.append(f"数据库仍含 custom provider: {session_id}")
        rollout_path = Path(str(row.get("rollout_path") or "").removeprefix("\\\\?\\"))
        if rollout_path and str(rollout_path) != "." and not rollout_path.exists():
            issues.append(f"数据库指向不存在的文件: {session_id} -> {rollout_path}")

    config_path = codex_home / "config.toml"
    if not config_path.exists():
        issues.append("config.toml 不存在")
    else:
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            issues.append(f"config.toml 无法解析: {error}")
        else:
            custom = (config.get("model_providers") or {}).get("custom")
            if custom and custom.get("base_url") != "https://chatgpt.com/backend-api/codex":
                issues.append("custom 兼容入口不是 ChatGPT 官方地址")
    return issues


def repair(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.resolve()
    if codex_is_running() and not args.allow_running:
        print("错误：检测到 Codex.exe 正在运行。请完全退出 Codex 后再执行恢复。", file=sys.stderr)
        print("原因：运行中的应用会缓存旧 provider，并可能把 custom 状态写回数据库。", file=sys.stderr)
        return 2

    before = discover_sessions(codex_home)
    if not before.sessions:
        print("没有找到可恢复的本地会话。")
        return 1
    backup = create_backup(codex_home, before, args.backup_root)
    print(f"备份完成: {backup}")

    restored = restore_archived_files(codex_home, before)
    migrated = migrate_rollout_metadata(before)
    inserted, updated = ensure_database_rows(codex_home, before)
    index_appended = update_session_index(codex_home, before)
    update_config(codex_home, not args.no_compat_alias)

    issues = verify(codex_home)
    print(f"恢复归档文件: {restored}")
    print(f"迁移会话 provider: {migrated}")
    print(f"新增数据库记录: {inserted}")
    print(f"更新数据库记录: {updated}")
    print(f"补充标题索引: {index_appended}")
    if issues:
        print("\n恢复完成，但验证发现以下问题：", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 3
    print("\n恢复与验证全部通过。现在可以重新启动 Codex。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    default_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description="扫描和恢复本地 Codex 会话")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_home,
        help=f"Codex 数据目录（默认：{default_home}）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("scan", help="只读扫描本地会话")
    repair_parser = subparsers.add_parser("repair", help="备份并恢复到 ChatGPT 官方提供商")
    repair_parser.add_argument("--backup-root", type=Path, help="自定义备份根目录")
    repair_parser.add_argument(
        "--allow-running",
        action="store_true",
        help="允许 Codex 运行时修复（不推荐，可能被缓存状态覆盖）",
    )
    repair_parser.add_argument(
        "--no-compat-alias",
        action="store_true",
        help="不添加指向 ChatGPT 官方服务的旧 custom 名称兼容入口",
    )
    subparsers.add_parser("verify", help="只读验证恢复结果")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    codex_home = args.codex_home.resolve()
    if not codex_home.exists():
        print(f"Codex 数据目录不存在: {codex_home}", file=sys.stderr)
        return 2
    if args.command == "scan":
        print_scan(discover_sessions(codex_home))
        return 0
    if args.command == "verify":
        issues = verify(codex_home)
        if not issues:
            print("验证通过：没有发现会话恢复问题。")
            return 0
        print("验证未通过：")
        for issue in issues:
            print(f"- {issue}")
        return 3
    return repair(args)


if __name__ == "__main__":
    raise SystemExit(main())
