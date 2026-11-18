# -*- coding: utf-8 -*-
"""知识材料的本地存储、同步和安全写入。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .catalog import labels, tag_options

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv"}
DOCUMENTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub", ".mobi"}
TEXT = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".java",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sql",
    ".log",
    ".yaml",
    ".yml",
}
STATES = {
    "queued": "待上传",
    "remote": "云端处理中",
    "received": "待写入",
    "done": "已入库",
    "source_only": "仅保存来源",
    "retry": "等待重试",
    "quota": "等待处理额度",
    "failed": "处理失败",
    "conflict": "写入冲突",
}


class CaptureCancelled(Exception):
    pass


def file_kind(name):
    ext = Path(name).suffix.lower()
    for kind, extensions in [
        ("image", IMAGES),
        ("audio", AUDIO),
        ("video", VIDEO),
        ("document", DOCUMENTS),
        ("text", TEXT),
    ]:
        if ext in extensions:
            return kind
    raise ValueError("暂不支持这个文件类型；支持图片、文档、文字、音频和视频")


def safe_name(value, limit=72):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]#^]', "_", str(value)).strip(" .")[:limit].rstrip(" .")
    if not name or name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "收集资料_" + name
    return name


def inside(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("保存位置必须位于知识库内")
    return target


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def readable_path(root, folder, title, suffix, *, source=None, capture_id=""):
    """Use a readable title; resolve genuine collisions without exposing UUIDs."""
    base = safe_name(title, 32)
    for number in range(1, 10001):
        relative = f"{folder}/{base}" + (f"-{number}" if number > 1 else "") + suffix
        path = inside(root, relative)
        if not path.exists():
            return relative
        if capture_id and path.stat().st_size < 2 * 1024 * 1024:
            if f'capture_id: "{capture_id}"' in path.read_text(encoding="utf-8-sig"):
                return relative
        if source and path.stat().st_size == Path(source).stat().st_size:
            with path.open("rb") as left, Path(source).open("rb") as right:
                while chunk := left.read(1024 * 1024):
                    if chunk != right.read(len(chunk)):
                        break
                else:
                    return relative
    raise ValueError("同名材料过多，请调整标题")


def validate_server(url):
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("服务器地址不能包含账号、密码、查询参数或片段")
    if not parts.hostname or (
        parts.scheme != "https"
        and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("请填写 HTTPS 地址；仅本机调试可使用 HTTP")


class CaptureStore:
    def __init__(self, directory: str | Path):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "queue.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, digest TEXT UNIQUE, kind TEXT, name TEXT, path TEXT,
            created REAL, state TEXT DEFAULT 'queued', error TEXT DEFAULT '',
            remote TEXT DEFAULT '', result TEXT DEFAULT '', note_path TEXT DEFAULT '',
            note_hash TEXT DEFAULT '', attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
            ack INTEGER DEFAULT 0, unlimited INTEGER DEFAULT 0, body TEXT DEFAULT '')""")
        if "body" not in {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN body TEXT DEFAULT ''")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        for name, definition in {
            "disposition": "TEXT DEFAULT 'active'",
            "removed_at": "REAL DEFAULT 0",
            "cancel_pending": "INTEGER DEFAULT 0",
            "sent": "INTEGER DEFAULT 0",
            "stage": "TEXT DEFAULT ''",
            "progress": "INTEGER DEFAULT -1",
            "user_tags": "TEXT DEFAULT '[]'",
            "instructions": "TEXT DEFAULT ''",
            "metadata_version": "INTEGER DEFAULT 0",
            "sent_version": "INTEGER DEFAULT -1",
            "editing": "INTEGER DEFAULT 0",
            "reprocess_pending": "INTEGER DEFAULT 0",
            "edited_body": "TEXT DEFAULT NULL",
            "edited_path": "TEXT DEFAULT ''",
            "manual_note": "INTEGER DEFAULT 0",
            "draft": "TEXT DEFAULT ''",
            "processing_mode": "TEXT DEFAULT 'index'",
            "search_text": "TEXT DEFAULT NULL",
            "search_tags": "TEXT DEFAULT '[]'",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        self.db.execute("UPDATE tasks SET sent=1 WHERE remote!=''")
        self.db.execute(
            "UPDATE tasks SET sent_version=0 WHERE sent=1 AND metadata_version=0 AND sent_version<0"
        )
        self._columns = ",".join(
            r[1]
            for r in self.db.execute("PRAGMA table_info(tasks)")
            if r[1] not in {"search_text", "search_tags"}
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created DESC, id DESC)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS tasks_disposition_created ON tasks(disposition, created DESC, id DESC)"
        )
        for row in self.db.execute("SELECT id FROM tasks WHERE search_text IS NULL").fetchall():
            self._index_task(row["id"])
        self.db.commit()

    def get(self, task_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(f"SELECT {self._columns} FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def _index_task(self, task_id: str) -> None:
        """只在正文或元信息变化时生成检索内容，和记录修改在同一事务中提交。"""
        row = self.get(task_id)
        if not row:
            return
        result = json.loads(row["result"]) if row["result"] else {}
        meta = labels(row)
        text = "\n".join(
            [
                row["name"],
                row["body"],
                row["edited_body"] or "",
                *(str(result.get(k, "")) for k in ("title", "summary", "original", "purpose")),
                *result.get("points", []),
                *result.get("tags", []),
                *meta["mine"],
                row["instructions"],
            ]
        ).casefold()
        tags = list(dict.fromkeys(tag.casefold() for tag in meta["mine"] + meta["ai"]))
        self.db.execute(
            "UPDATE tasks SET search_text=?, search_tags=? WHERE id=?",
            (text, json.dumps(tags, ensure_ascii=False), task_id),
        )

    def get_many(self, task_ids: list[str]) -> list[dict]:
        """批量获取卡片状态，避免每张卡片单独查询。"""
        rows = []
        with self.lock:
            for start in range(0, len(task_ids), 500):
                batch = task_ids[start : start + 500]
                marks = ",".join("?" for _ in batch)
                rows.extend(
                    dict(row)
                    for row in self.db.execute(
                        f"SELECT {self._columns} FROM tasks WHERE id IN ({marks})", batch
                    )
                )
        return rows

    def update(self, task_id: str, **fields) -> None:
        allowed = {
            "state",
            "error",
            "remote",
            "result",
            "note_path",
            "note_hash",
            "attempts",
            "retry_at",
            "ack",
            "unlimited",
            "disposition",
            "removed_at",
            "cancel_pending",
            "sent",
            "stage",
            "progress",
            "user_tags",
            "instructions",
            "metadata_version",
            "sent_version",
            "editing",
            "reprocess_pending",
            "edited_body",
            "edited_path",
            "manual_note",
            "draft",
            "processing_mode",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid task fields")
        with self.lock, self.db:
            self.db.execute(
                "UPDATE tasks SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                (*fields.values(), task_id),
            )
            if fields.keys() & {"result", "edited_body", "user_tags", "instructions"}:
                self._index_task(task_id)

    def reset_editing(self) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET editing=0")

    def queue_upload(self, task_id: str) -> str:
        """已取消的服务端 UUID 不复用；显式重传时换新 ID。"""
        with self.lock, self.db:
            row = self.get(task_id)
            new_id = str(uuid.uuid4()) if row["sent"] else task_id
            self.db.execute("UPDATE tasks SET id=? WHERE id=?", (new_id, task_id))
            self.update(
                new_id,
                disposition="active",
                remote="",
                sent=0,
                sent_version=-1,
                editing=0,
                reprocess_pending=0,
                ack=0,
                state="queued",
                result="",
                error="",
                retry_at=time.time() + 10,
                progress=-1,
                stage="",
            )
            return new_id

    def sync_rows(self, *, network: bool = True) -> list[dict]:
        """每类工作只取最早的一条，排队量不会放大单次同步的内存分配。"""
        active = "disposition='active' AND editing=0 AND draft=''"
        queries = [(active + " AND state='received'", ())]
        if network:
            now = time.time()
            queries.insert(0, ("cancel_pending=1 AND retry_at<=?", (now,)))
            queries.extend(
                [
                    (
                        active + " AND cancel_pending=0 AND retry_at<=? "
                        "AND state IN ('queued','uploading','retry','remote') "
                        "AND NOT (sent=1 AND remote='' AND metadata_version>sent_version)",
                        (now,),
                    ),
                    (
                        "disposition='active' AND state IN ('done','source_only') "
                        "AND ack=0 AND remote!='' AND retry_at<=?",
                        (now,),
                    ),
                ]
            )
        rows = []
        with self.lock:
            for where, params in queries:
                row = self.db.execute(
                    f"SELECT {self._columns} FROM tasks WHERE {where} ORDER BY created ASC, id ASC LIMIT 1",
                    params,
                ).fetchone()
                if row:
                    rows.append(dict(row))
        return rows

    def pending_reprocess_ids(self) -> list[str]:
        with self.lock:
            return [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM tasks WHERE reprocess_pending=1 AND cancel_pending=0 "
                    "AND editing=0 AND draft='' AND disposition!='trash'"
                )
            ]

    def update_active(self, task_id, **fields):
        with self.lock:
            row = self.get(task_id)
            if not row or row["disposition"] != "active":
                return False
            self.update(task_id, **fields)
            return True

    def disposition(self, task_id, value):
        if value not in {"active", "local", "trash"}:
            raise ValueError("Invalid disposition")
        with self.lock:
            row = self.get(task_id)
            if not row:
                return
            self.update(
                task_id,
                disposition=value,
                removed_at=time.time() if value == "trash" else 0,
                cancel_pending=int(
                    bool(
                        row["cancel_pending"]
                        or (row["disposition"] == "active" and (row["sent"] or row["remote"]))
                    )
                ),
                retry_at=0,
                stage="",
                progress=-1,
                reprocess_pending=0,
            )

    def save_metadata(self, task_id, tags, instructions):
        if (
            not isinstance(tags, list)
            or len(tags) > 10
            or any(not isinstance(t, str) or not t.strip() or len(t.strip()) > 32 for t in tags)
        ):
            raise ValueError("最多 10 个标签，每个标签最多 32 字")
        tags = list(dict.fromkeys(t.strip() for t in tags))
        if not isinstance(instructions, str) or len(instructions) > 1000:
            raise ValueError("补充说明最多 1000 字")
        payload = json.dumps(
            {"tags": tags, "instructions": instructions.strip(), "version": 0}, ensure_ascii=False
        ).encode("utf-8")
        if len(payload) > 4096:
            raise ValueError("标签与说明总长度偏大，请适当缩短")
        with self.lock:
            task = self.get(task_id)
            if not task:
                raise ValueError("材料已不存在")
            changed = (
                task["user_tags"] != json.dumps(tags, ensure_ascii=False)
                or task["instructions"] != instructions.strip()
            )
            fields = dict(
                user_tags=json.dumps(tags, ensure_ascii=False),
                instructions=instructions.strip(),
                metadata_version=task["metadata_version"] + int(changed),
                editing=0,
            )
            if not task["sent"]:
                fields["retry_at"] = time.time() + 10
            elif (
                changed
                and task["result"]
                and task["disposition"] == "active"
                and task["state"] in {"done", "source_only"}
            ):
                fields["state"] = "received"  # Write manual tags/notes, without calling AI again.
            self.update(task_id, **fields)

    def cleanup_trash(self):
        removed = 0
        with self.lock:
            expired = [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM tasks WHERE disposition='trash' AND cancel_pending=0 AND removed_at<?",
                    (time.time() - 7 * 86400,),
                )
            ]
            for row in expired:
                path = Path(row["path"]).resolve()
                if path.is_relative_to(self.root.resolve()):
                    try:
                        for edited in self.root.glob(path.stem + "-body-*.txt"):
                            edited.unlink(missing_ok=True)
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue  # A locked file stays recoverable; retry cleanup next time.
                with self.db:
                    self.db.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
                removed += 1
        return removed

    def rows(
        self,
        query: str = "",
        *,
        trash: bool = False,
        all_rows: bool = False,
        kind: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """在数据库中过滤后分页，保留 Unicode 大小写折叠和字面子串搜索。"""
        clauses, params = [], []
        if not all_rows:
            clauses.append("disposition='trash'" if trash else "disposition!='trash'")
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if tag is not None:
            if tag == "":
                clauses.append("search_tags='[]'")
            else:
                clauses.append("EXISTS (SELECT 1 FROM json_each(search_tags) WHERE value=?)")
                params.append(tag.casefold())
        for word in query.casefold().split():
            clauses.append("instr(search_text, ?) > 0")
            params.append(word)
        sql = f"SELECT {self._columns} FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created DESC, id DESC"
        sql += " LIMIT ? OFFSET ?"
        params.extend([max(0, limit) if limit is not None else -1, max(0, offset)])
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params)]

    def tag_options(self, *, trash: bool = False) -> list[tuple[str, str]]:
        """筛选菜单只读取标签，避免传回整篇材料。"""
        where = "disposition='trash'" if trash else "disposition!='trash'"
        with self.lock:
            rows = [
                dict(row)
                for row in self.db.execute(
                    "SELECT kind, note_path, user_tags, json_object('tags', "
                    "json(COALESCE(json_extract(NULLIF(result, ''), '$.tags'), '[]'))) AS result "
                    f"FROM tasks WHERE {where} ORDER BY created DESC, id DESC"
                )
            ]
        return tag_options(rows)

    def capture(self, *, source=None, text=None, data=None, name="", max_mb=256, delay_s=0, local_only=False):
        """Copy and fsync before adding a durable row; never move the original."""
        if source is not None:
            source = Path(source)
            if not source.is_file() or source.is_symlink():
                raise ValueError("请选择普通文件，暂不支持目录或符号链接")
            name = source.name
        kind = file_kind(name) if name else "text"
        if text is not None:
            if not text.strip():
                raise ValueError("没有可以收集的内容")
            name = safe_name(text.strip().splitlines()[0], 60) + ".txt"
            try:
                parts = urlsplit(text.strip())
                kind = (
                    "url"
                    if parts.scheme in {"http", "https"} and parts.hostname and "\n" not in text.strip()
                    else "text"
                )
            except ValueError:
                kind = "text"
            data = text.encode("utf-8")
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("单次文字最多 8 MiB")
        size = source.stat().st_size if source else len(data or b"")
        if size > max_mb * 1024 * 1024:
            raise ValueError(f"文件超过 {max_mb} MiB 上限，原文件未改动")
        task_id = str(uuid.uuid4())
        path = self.root / (task_id + Path(name).suffix.lower())
        temp = path.with_suffix(".tmp")
        digest = hashlib.sha256(kind.encode() + b"\0")
        total = 0
        try:
            with temp.open("xb") as out:
                if source:
                    with source.open("rb") as inp:
                        while chunk := inp.read(1024 * 1024):
                            total += len(chunk)
                            if total > max_mb * 1024 * 1024:
                                raise ValueError("复制过程中发现文件超出大小上限")
                            out.write(chunk)
                            digest.update(chunk)
                else:
                    out.write(data or b"")
                    digest.update(data or b"")
                out.flush()
                os.fsync(out.fileno())
            with self.lock:
                previous = self.db.execute(
                    "SELECT id FROM tasks WHERE digest=?", (digest.hexdigest(),)
                ).fetchone()
                if previous:
                    return self.get(previous["id"]), True
                os.replace(temp, path)
                try:
                    with self.db:
                        self.db.execute(
                            "INSERT INTO tasks(id,digest,kind,name,path,created,body,retry_at,disposition) VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                task_id,
                                digest.hexdigest(),
                                kind,
                                name,
                                str(path),
                                time.time(),
                                text or "",
                                time.time() + delay_s,
                                "local" if local_only else "active",
                            ),
                        )
                        self._index_task(task_id)
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
            return self.get(task_id), False
        finally:
            temp.unlink(missing_ok=True)


def render_note(task, result, attachment):
    title = result.get("title") or task["name"]
    summary = result.get("summary") or "尚未提取正文，已保留原始材料。"
    created = datetime.fromtimestamp(task["created"]).astimezone().isoformat(timespec="seconds")
    mine = json.loads(task.get("user_tags", "[]"))
    ai_tags = result.get("tags", [])[:5]
    tags = list(dict.fromkeys(re.sub(r"[^\w\u4e00-\u9fff/-]", "", str(t)) for t in mine + ai_tags))
    meta = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in [
            ("type", "resource"),
            ("source", "wisland"),
            ("capture_id", task["id"]),
            ("created", created),
            ("status", result.get("status", "processed")),
            ("tags", tags),
            ("user_tags", mine),
            ("ai_tags", ai_tags),
            ("category", result.get("category", "")),
            ("topic", result.get("topic", "")),
            ("agent_managed", bool(result.get("organized"))),
        ]
    )
    points = "\n".join("- " + str(p) for p in result.get("points", [])) or "- 未提取到可确认的要点。"
    source = result.get("source_url", "")
    original = result.get("original", "")
    if task["kind"] == "text" or (task["kind"] == "url" and not original):
        original = Path(task["path"]).read_text(encoding="utf-8-sig", errors="replace")
    if task.get("edited_body") is not None:
        original = task["edited_body"]
    aliases = result.get("attachment_aliases", {})
    if isinstance(aliases, dict):
        original = re.sub(
            r"\[\[([^\]|]+)([^\]]*)\]\]",
            lambda m: "[[" + str(aliases.get(m[1], m[1])) + m[2] + "]]",
            original,
        )
    link = ("!" if task["kind"] == "image" else "") + "[[" + attachment + "]]"
    instructions = task.get("instructions", "")
    extra = ("## 我的补充说明\n\n" + instructions + "\n\n") if instructions else ""
    if result.get("index_only"):
        coverage = result.get("coverage", {})
        label = (
            coverage.get("label", "基于有限预览生成索引，未阅读全文")
            if isinstance(coverage, dict)
            else "基于有限预览生成索引，未阅读全文"
        )
        extra += "## 阅读范围\n\n" + str(label) + "\n\n"
        if result.get("purpose"):
            extra += "## 资料用途\n\n" + str(result["purpose"]) + "\n\n"
    if task.get("metadata_version", 0) > result.get("metadata_version", max(0, task.get("sent_version", 0))):
        extra += "> 标签或说明在上传后更新，本次 AI 结果未使用这些新修改；可在知识页重新整理。\n\n"
    sources = result.get("source_notes", [])
    references = ""
    if isinstance(sources, list):
        for source_note in sources:
            if isinstance(source_note, str) and not any(char in source_note for char in "[]|\n"):
                references += "- [[" + source_note.removesuffix(".md") + "]]\n"
    if references:
        extra += "## 原始收集记录\n\n" + references + "\n"
    topic_note = result.get("topic_note")
    if isinstance(topic_note, str) and topic_note and not any(c in topic_note for c in "[]|\n"):
        extra += "## 所属专题\n\n[[" + topic_note.removesuffix(".md") + "]]\n\n"
    review_notes = result.get("review_notes", [])
    if isinstance(review_notes, list) and review_notes:
        extra += "## 校验补充\n\n" + "\n".join("- " + n for n in review_notes if isinstance(n, str)) + "\n\n"
    related = result.get("related_notes", [])
    if isinstance(related, list):
        links = []
        for item in related:
            path = item.get("path", "") if isinstance(item, dict) else ""
            if isinstance(path, str) and path and not any(c in path for c in "[]|\n"):
                label = (
                    str(item.get("title", ""))
                    .replace("[", "（")
                    .replace("]", "）")
                    .replace("|", "／")
                    .replace("\n", " ")
                )
                links.append(
                    "- [["
                    + path.removesuffix(".md")
                    + ("|" + label if label else "")
                    + "]]："
                    + str(item.get("reason", ""))
                )
        if links:
            extra += "## 相关资料\n\n" + "\n".join(links) + "\n\n"
    original_section = (
        ""
        if result.get("index_only")
        else f"## 提取正文 / 原始文字\n\n{original or '该材料尚无可用文字提取结果。'}\n\n"
    )
    if result.get("index_only") and task.get("edited_body") is not None:
        original_section = "## 我补充的正文\n\n" + task["edited_body"] + "\n\n"
    heading = (
        "AI 资料索引，原件保留；阅读范围见下。"
        if result.get("index_only")
        else "AI 整理草稿，请结合原始材料核对。"
    )
    return (
        f"---\n{meta}\n---\n\n# {title}\n\n> {heading}\n\n"
        + extra
        + f"## 摘要\n\n{summary}\n\n## 关键点\n\n{points}\n\n"
        + original_section
        + f"## 原始材料\n\n{link}\n\n"
        + (f"来源：<{source}>\n\n" if source else "")
        + (f"## 处理说明\n\n{result['warning']}\n" if result.get("warning") else "")
    )


class VaultConflict(Exception):
    pass


def editor_document(task, vault):
    """Read the real note when available; otherwise edit a separate text layer."""
    result = json.loads(task["result"]) if task["result"] else {}
    target = ""
    checksum = ""
    prefix = ""
    if task["note_path"]:
        note = inside(vault, task["note_path"])
        raw = note.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("笔记超过 2 MiB，请在 Obsidian 中编辑")
        content = raw.decode("utf-8-sig")
        frontmatter = re.match(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", content, re.DOTALL)
        if frontmatter:
            prefix = frontmatter.group(0)
            content = content[len(prefix) :]
        target, checksum = str(note), hashlib.sha256(raw).hexdigest()
    elif task.get("edited_body") is not None:
        content = task["edited_body"]
    else:
        content = task["body"] or result.get("original", "")
        if not content and task["kind"] == "text":
            raw = Path(task["path"]).read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("文字超过 2 MiB，请用外部编辑器处理")
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("gb18030")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("正文超过 2 MiB，请用外部编辑器处理")
    return {
        "content": content,
        "target": target,
        "checksum": checksum,
        "prefix": prefix,
        "version": task["metadata_version"],
    }


def save_document(store, task_id, vault, document, content, tags, instructions):
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("正文最多 2 MiB")
    with store.lock:
        row = store.get(task_id)
        if not row or row["metadata_version"] != document["version"]:
            raise VaultConflict("记录已发生变化，请取消后重新打开编辑")
        if document["target"]:
            note = inside(vault, row["note_path"])
            if (
                str(note) != document["target"]
                or hashlib.sha256(note.read_bytes()).hexdigest() != document["checksum"]
            ):
                raise VaultConflict("笔记在编辑期间被外部修改，草稿已保留，请先核对")
        # Validate metadata before touching files; save_metadata also validates on commit.
        if (
            not isinstance(tags, list)
            or len(tags) > 10
            or any(not isinstance(t, str) or not t.strip() or len(t.strip()) > 32 for t in tags)
            or not isinstance(instructions, str)
            or len(instructions) > 1000
        ):
            raise ValueError("最多 10 个标签（每个 32 字），说明最多 1000 字")
        if (
            len(
                json.dumps(
                    {"tags": tags, "instructions": instructions, "version": 0}, ensure_ascii=False
                ).encode("utf-8")
            )
            > 4096
        ):
            raise ValueError("标签和说明过长，请适当缩短")
        changed = content != document["content"]
        payload = row["edited_path"]
        if changed:
            payload = str(store.root / (Path(row["path"]).stem + "-body-" + uuid.uuid4().hex + ".txt"))
            atomic_write(payload, content.encode("utf-8"))
            if document["target"]:
                backup = store.root / "edit-backups" / (uuid.uuid4().hex + ".md")
                atomic_write(backup, note.read_bytes())
                atomic_write(note, (document.get("prefix", "") + content).encode("utf-8"))
        store.save_metadata(task_id, tags, instructions)
        if changed:
            current = store.get(task_id)
            fields = dict(
                edited_body=content, edited_path=payload, metadata_version=current["metadata_version"] + 1
            )
            if document["target"]:
                fields.update(note_hash="", manual_note=1, state=row["state"])
            store.update(task_id, **fields)
        elif document["target"]:
            store.update(task_id, state=row["state"])


def write_note(store, task, vault):
    # The same lock serializes the final write against cancel/trash actions.
    with store.lock:
        current = store.get(task["id"])
        if current and current["disposition"] != "active":
            raise CaptureCancelled()
        return _write_note(store, current or task, vault)


def _write_note(store, task, vault):
    root = Path(vault).expanduser().resolve()
    if not (root / ".obsidian").is_dir():
        raise ValueError("请选择包含 .obsidian 的实际知识库目录")
    result = json.loads(task["result"])
    processed = result.get("status") == "processed"
    from .agent_worker import CATEGORIES

    category = result.get("category")
    if result.get("index_only"):
        folder = (
            "05 Resources/" + category if processed and category in CATEGORIES else "00 Inbox收件箱/待补充"
        )
    elif processed and result.get("organized") and category in CATEGORIES:
        folder = "05 Resources/" + category
    else:
        folder = "05 Resources/灵动岛收集" if processed else "00 Inbox收件箱/灵动岛待处理"
    title = result.get("title") or task["name"]
    relative = task["note_path"] or readable_path(root, folder, title, ".md", capture_id=task["id"])
    note = inside(root, relative)
    attachment = ""
    if isinstance(result.get("existing_attachment"), str):
        candidate = inside(root, result["existing_attachment"])
        if candidate.is_file():
            attachment = result["existing_attachment"]
    if not attachment:
        attachment = readable_path(
            root,
            "05 Resources/Attachments",
            title + "-原始材料",
            Path(task["name"]).suffix.lower(),
            source=task["path"],
        )
    target = inside(root, attachment)
    content = render_note(task, result, attachment).encode("utf-8")
    checksum = hashlib.sha256(content).hexdigest()
    if note.exists():
        existing = hashlib.sha256(note.read_bytes()).hexdigest()
        if task.get("manual_note") and existing != checksum:
            raise VaultConflict("笔记正文已经手动编辑，AI 结果请另存，避免覆盖")
        if existing != checksum and (not task["note_hash"] or existing != task["note_hash"]):
            raise VaultConflict("笔记已存在或已被修改，保留现状，请打开查看后另存")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            with Path(task["path"]).open("rb") as inp, tmp.open("wb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    atomic_write(note, content)
    result["existing_attachment"] = attachment
    store.update(
        task["id"],
        note_path=relative,
        note_hash=checksum,
        state="done" if processed else "source_only",
        error="",
        manual_note=0,
        result=json.dumps(result, ensure_ascii=False),
    )
    return note


def sync_one(store, settings, transport=None):
    """One bounded step. Caller serializes calls; received results need no network."""
    url, token = settings.get("server_url", "").rstrip("/"), settings.get("token", "")
    rows = store.sync_rows(network=bool(url and token))
    controls = [r for r in rows if r["cancel_pending"] and r["retry_at"] <= time.time()]
    if controls and url and token:
        validate_server(url)
        task = controls[0]
        try:
            with httpx.Client(
                timeout=15, transport=transport, trust_env=False, headers={"Authorization": "Bearer " + token}
            ) as client:
                response = client.post(url + f"/v1/tasks/{task['remote'] or task['id']}/cancel")
                response.raise_for_status()
                state = response.json().get("state")
                if state == "cancelled":
                    store.update(task["id"], cancel_pending=0, error="", retry_at=0)
                else:
                    store.update(
                        task["id"], error="云端正在取消；已发送给模型的内容无法撤回", retry_at=time.time() + 5
                    )
        except (httpx.HTTPError, ValueError):
            store.update(task["id"], error="云端清理待完成，连接恢复后自动重试", retry_at=time.time() + 15)
        return True
    for task in rows:
        if (
            task["disposition"] == "active"
            and task["state"] == "received"
            and not task["editing"]
            and not task["draft"]
        ):
            try:
                write_note(store, task, settings.get("vault", ""))
            except VaultConflict as exc:
                store.update_active(task["id"], state="conflict", error=str(exc))
            except CaptureCancelled:
                pass
            except (OSError, ValueError) as exc:
                store.update_active(task["id"], state="failed", error=str(exc))
            return True
    if not url or not token:
        return False
    validate_server(url)
    eligible = [
        r
        for r in rows
        if r["disposition"] == "active"
        and not r["cancel_pending"]
        and not r["editing"]
        and not r["draft"]
        and not (r["sent"] and not r["remote"] and r["metadata_version"] > r["sent_version"])
        and r["state"] in {"queued", "uploading", "retry", "remote"}
        and r["retry_at"] <= time.time()
    ]
    acknowledgements = [
        r
        for r in rows
        if r["disposition"] == "active"
        and r["state"] in {"done", "source_only"}
        and not r["ack"]
        and r["remote"]
        and r["retry_at"] <= time.time()
    ]
    if not eligible and not acknowledgements:
        return False
    task = (eligible or acknowledgements)[0]
    try:
        with httpx.Client(
            base_url=url + "/",
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(180, connect=10),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            if not eligible:
                response = client.post(f"v1/tasks/{task['remote']}/ack")
                response.raise_for_status()
                store.update(task["id"], ack=1)
            elif not task["remote"]:
                upload_path = Path(task["edited_path"] or task["path"])
                upload_kind = "text" if task["edited_path"] else task["kind"]
                upload_digest = task["digest"]
                if task["edited_path"]:
                    upload_digest = hashlib.sha256(
                        upload_kind.encode() + b"\0" + upload_path.read_bytes()
                    ).hexdigest()
                with store.lock:
                    current = store.get(task["id"])
                    if (
                        current["editing"]
                        or current["metadata_version"] != task["metadata_version"]
                        or current["processing_mode"] != task["processing_mode"]
                        or not store.update_active(
                            task["id"],
                            state="uploading",
                            sent=1,
                            progress=0,
                            stage="正在上传",
                            sent_version=task["metadata_version"],
                        )
                    ):
                        return True
                metadata = base64.b64encode(
                    json.dumps(
                        {
                            "tags": json.loads(task["user_tags"]),
                            "instructions": task["instructions"],
                            "version": task["metadata_version"],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).decode("ascii")

                def chunks():
                    size = upload_path.stat().st_size
                    done = 0
                    with upload_path.open("rb") as stream:
                        while chunk := stream.read(256 * 1024):
                            current = store.get(task["id"])
                            if current["disposition"] != "active":
                                raise CaptureCancelled()
                            yield chunk
                            done += len(chunk)
                            store.update_active(task["id"], progress=int(done * 100 / max(1, size)))

                response = client.post(
                    f"v1/tasks/{task['id']}",
                    content=chunks(),
                    headers={
                        "Content-Length": str(upload_path.stat().st_size),
                        "X-Capture-Metadata": metadata,
                    },
                    params={
                        "name": task["name"],
                        "kind": upload_kind,
                        "digest": upload_digest,
                        "unlimited": str(task["unlimited"]),
                        "mode": task["processing_mode"],
                    },
                )
                response.raise_for_status()
                result = response.json()
                uuid.UUID(result["id"])
                store.update(task["id"], remote=result["id"])
                store.update_active(
                    task["id"], state="remote", error="", retry_at=0, stage="云端排队", progress=-1
                )
            else:
                response = client.get(f"v1/tasks/{task['remote']}")
                response.raise_for_status()
                remote = response.json()
                if remote["state"] == "ready":
                    result = remote["result"]
                    if not isinstance(result, dict) or result.get("status") not in {
                        "processed",
                        "source_only",
                    }:
                        raise ValueError("服务器返回了无效结果")
                    if "metadata_version" in result and (
                        type(result["metadata_version"]) is not int or result["metadata_version"] < 0
                    ):
                        raise ValueError("服务器返回了无效说明版本")
                    if not isinstance(result.get("title", ""), str) or not isinstance(
                        result.get("summary", ""), str
                    ):
                        raise ValueError("服务器返回了无效笔记字段")
                    if not all(
                        isinstance(result.get(k, ""), str) for k in ("original", "source_url", "warning")
                    ):
                        raise ValueError("服务器返回了无效原文或来源")
                    if not all(
                        isinstance(result.get(k, []), list)
                        and all(isinstance(v, str) for v in result.get(k, []))
                        for k in ("points", "tags")
                    ):
                        raise ValueError("服务器返回了无效要点或标签")
                    store.update_active(
                        task["id"],
                        result=json.dumps(result, ensure_ascii=False),
                        state="received",
                        error="",
                        retry_at=0,
                        stage="待写入笔记",
                        progress=-1,
                    )
                elif remote["state"] in {"failed", "quota"}:
                    store.update_active(
                        task["id"], state=remote["state"], error=remote.get("error", "")[:300]
                    )
                elif remote["state"] == "expired":
                    store.update_active(task["id"], remote="", state="queued", error="", retry_at=0)
                elif remote["state"] in {"cancelled", "cancelling"}:
                    store.update_active(
                        task["id"], state="failed", error="云端任务已取消，可重新收集或仅保留本机"
                    )
                else:
                    store.update_active(
                        task["id"],
                        retry_at=time.time() + 5,
                        stage=str(
                            remote.get("stage")
                            or ("云端排队" if remote["state"] == "queued" else "AI 整理中")
                        )[:120],
                        progress=remote.get("progress")
                        if isinstance(remote.get("progress"), int) and -1 <= remote["progress"] <= 100
                        else -1,
                    )
        return True
    except CaptureCancelled:
        return True
    except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
        attempt = task["attempts"] + 1
        # Persist the original remote ID so uncertain uploads/polls remain idempotent.
        error = (
            ("服务连接失败：HTTP " + str(exc.response.status_code))
            if isinstance(exc, httpx.HTTPStatusError)
            else "服务连接或结果处理失败，请检查设置后重试"
        )
        store.update_active(
            task["id"],
            state="retry" if eligible else task["state"],
            error=error,
            attempts=attempt,
            retry_at=time.time() + min(300, 5 * 2 ** min(attempt, 6)),
        )
        return True
