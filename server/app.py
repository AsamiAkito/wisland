"""Authenticated single-worker service behind an HTTPS reverse proxy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .processing import DeepSeekProcessor, QuotaExceeded


class JobCancelled(Exception):
    pass


class Jobs:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "jobs.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, digest TEXT UNIQUE, kind TEXT, name TEXT, path TEXT,
            created REAL, state TEXT DEFAULT 'queued', error TEXT DEFAULT '',
            result TEXT DEFAULT '', attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
            unlimited INTEGER DEFAULT 0, ack_at REAL DEFAULT 0)""")
        self.db.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
        self.db.execute("CREATE TABLE IF NOT EXISTS cancellations(id TEXT PRIMARY KEY, created REAL)")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for name, definition in {
            "stage": "TEXT DEFAULT ''",
            "progress": "INTEGER DEFAULT -1",
            "user_tags": "TEXT DEFAULT '[]'",
            "instructions": "TEXT DEFAULT ''",
            "metadata_version": "INTEGER DEFAULT 0",
            "processing_mode": "TEXT DEFAULT 'index'",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.db.commit()
        # A stopped process cannot finish an in-flight cancellation. Complete it on restart.
        for row in self.db.execute("SELECT * FROM jobs WHERE state='cancelling'").fetchall():
            Path(row["path"]).unlink(missing_ok=True)
            self.update(row["id"], state="cancelled", result="", stage="已取消", progress=-1)

    def get(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def update(self, task_id, **fields):
        allowed = {
            "state",
            "error",
            "result",
            "attempts",
            "retry_at",
            "ack_at",
            "unlimited",
            "stage",
            "progress",
            "name",
            "user_tags",
            "instructions",
            "processing_mode",
        }
        if not fields.keys() <= allowed:
            raise ValueError("Invalid fields")
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                (*fields.values(), task_id),
            )

    def next(self):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE state='queued' AND retry_at<=? ORDER BY created LIMIT 1",
                (time.time(),),
            ).fetchone()
            return dict(row) if row else None

    def cancelled(self, task_id):
        with self.lock:
            return (
                self.db.execute("SELECT 1 FROM cancellations WHERE id=?", (task_id,)).fetchone() is not None
            )

    def cancel(self, task_id):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO cancellations(id,created) VALUES(?,?)", (task_id, time.time())
            )
            row = self.get(task_id)
            if not row:
                return "cancelled"
            state = "cancelling" if row["state"] in {"processing", "cancelling"} else "cancelled"
            self.update(
                task_id,
                state=state,
                result="",
                name="已取消材料",
                user_tags="[]",
                instructions="",
                stage="正在取消" if state == "cancelling" else "已取消",
                progress=-1,
            )
            if state == "cancelled":
                Path(row["path"]).unlink(missing_ok=True)
            return state

    def cleanup(self):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM jobs")]
        now = time.time()
        for row in rows:
            expired = now - (row["ack_at"] or row["created"]) > (7 if row["ack_at"] else 30) * 86400
            if expired and row["state"] != "processing":
                Path(row["path"]).unlink(missing_ok=True)
                self.update(
                    row["id"],
                    state="expired",
                    result="",
                    error="服务器保留期已到；电脑上的原始副本仍可重新提交",
                )


class Worker(threading.Thread):
    def __init__(self, jobs, processor):
        super().__init__(daemon=True, name="knowledge-worker")
        self.jobs, self.processor = jobs, processor
        self.stopping = threading.Event()

    def step(self):
        with self.jobs.lock:
            task = self.jobs.next()
            if not task:
                return False
            if self.jobs.cancelled(task["id"]):
                self.jobs.cancel(task["id"])
                return True
            self.jobs.update(task["id"], state="processing", stage="提取材料", progress=-1)

        def checkpoint():
            if self.jobs.cancelled(task["id"]):
                raise JobCancelled()

        def progress(stage, value=-1):
            checkpoint()
            self.jobs.update(task["id"], stage=stage, progress=value)

        if isinstance(self.processor, DeepSeekProcessor):
            self.processor.checkpoint = checkpoint
            self.processor.progress = progress
        try:
            checkpoint()
            result = self.processor(task)
            if not isinstance(result, dict) or result.get("status") not in {"processed", "source_only"}:
                raise ValueError("处理器返回格式错误")
            result["metadata_version"] = task["metadata_version"]
            with self.jobs.lock:
                checkpoint()
                self.jobs.update(
                    task["id"],
                    state="ready",
                    result=json.dumps(result, ensure_ascii=False),
                    error="",
                    stage="整理完成",
                    progress=100,
                )
        except JobCancelled:
            self.jobs.update(task["id"], state="cancelled", result="", stage="已取消", progress=-1)
            Path(task["path"]).unlink(missing_ok=True)
        except QuotaExceeded as exc:
            self.jobs.update(task["id"], state="quota", error=str(exc)[:300])
        except Exception as exc:
            if self.jobs.cancelled(task["id"]):
                self.jobs.update(task["id"], state="cancelled", result="", stage="已取消", progress=-1)
                Path(task["path"]).unlink(missing_ok=True)
                return True
            import httpx

            attempt = task["attempts"] + 1
            transient = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in {408, 429, 500, 502, 503, 504}
            )
            retry = transient and attempt < 4
            error = (
                ("AI 接口返回 HTTP " + str(exc.response.status_code))
                if isinstance(exc, httpx.HTTPStatusError)
                else (
                    str(exc)[:250]
                    if isinstance(exc, (ValueError, FileNotFoundError))
                    else "后台处理失败，请检查服务器配置或稍后重试"
                )
            )
            self.jobs.update(
                task["id"],
                state="queued" if retry else "failed",
                error=error,
                attempts=attempt,
                retry_at=time.time() + min(300, 10 * 2**attempt),
            )
        return True

    def run(self):
        last_cleanup = 0
        while not self.stopping.is_set():
            if time.time() - last_cleanup > 3600:
                self.jobs.cleanup()
                last_cleanup = time.time()
            if not self.step():
                self.stopping.wait(1)


def make_server(jobs, token, address=("127.0.0.1", 8766), max_mb=256, max_storage_gb=10):
    if len(token) < 24:
        raise ValueError("KNOWLEDGE_TOKEN 至少需要 24 个字符")
    upload_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Knowledge/1"

        def log_message(self, *_args):
            pass  # No URLs, note contents, credentials or filenames in access logs.

        def send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + token).encode("utf-8")
            ):
                self.send_json(401, {"error": {"code": "UNAUTHORIZED", "message": "访问令牌无效"}})
                return False
            return True

        def route(self):
            parts = urlsplit(self.path)
            route = parts.path.strip("/").split("/")
            if len(route) < 3 or route[:2] != ["v1", "tasks"]:
                raise ValueError("未知接口")
            uuid.UUID(route[2])
            return route[2], route[3:] or [], parse_qs(parts.query)

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/health":
                self.send_json(
                    200,
                    {
                        "status": "ok",
                        "api_version": 3,
                        "default_mode": "index",
                        "processing_modes": ["index", "deep"],
                    },
                )
                return
            try:
                task_id, suffix, _params = self.route()
                row = jobs.get(task_id)
                if not row and jobs.cancelled(task_id):
                    self.send_json(200, {"id": task_id, "state": "cancelled", "result": None, "error": ""})
                    return
                if not row or suffix:
                    self.send_json(404, {"error": {"code": "NOT_FOUND", "message": "任务不存在"}})
                    return
                self.send_json(
                    200,
                    {
                        "id": row["id"],
                        "state": row["state"],
                        "error": row["error"],
                        "stage": row["stage"],
                        "progress": row["progress"],
                        "result": json.loads(row["result"]) if row["result"] else None,
                    },
                )
            except (ValueError, KeyError):
                self.send_json(400, {"error": {"code": "INVALID_REQUEST", "message": "请求格式错误"}})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                task_id, suffix, params = self.route()
                if suffix == ["cancel"]:
                    state = jobs.cancel(task_id)
                    self.send_json(200, {"id": task_id, "state": state})
                    return
                if suffix:
                    row = jobs.get(task_id)
                    if not row:
                        self.send_json(404, {"error": {"code": "NOT_FOUND", "message": "任务不存在"}})
                        return
                    if jobs.cancelled(task_id):
                        self.send_json(
                            410, {"error": {"code": "CANCELLED", "message": "任务已取消，请使用新的收集任务"}}
                        )
                        return
                    if suffix == ["ack"] and row["state"] == "ready":
                        jobs.update(task_id, ack_at=time.time())
                    elif suffix == ["retry"] and (
                        row["state"] in {"failed", "quota"}
                        or (row["state"] == "ready" and params.get("reprocess") == ["1"])
                    ):
                        mode = params.get("mode", [row.get("processing_mode", "index")])[0]
                        if mode not in {"index", "deep"}:
                            raise ValueError("处理模式无效")
                        jobs.update(
                            task_id,
                            state="queued",
                            attempts=0,
                            retry_at=0,
                            error="",
                            result="",
                            ack_at=0,
                            unlimited=int(params.get("unlimited", ["0"])[0] == "1"),
                            processing_mode=mode,
                        )
                    else:
                        raise ValueError("当前任务不支持该操作")
                    self.send_json(200, {"id": task_id})
                    return
                kind = params.get("kind", [""])[0]
                name = params.get("name", [""])[0]
                digest = params.get("digest", [""])[0]
                content_digest = digest
                mode = params.get("mode", ["index"])[0]
                if mode not in {"index", "deep"}:
                    raise ValueError("处理模式无效")
                metadata_header = self.headers.get("X-Capture-Metadata", "")
                metadata = {"tags": [], "instructions": "", "version": 0}
                if metadata_header:
                    if len(metadata_header) > 5500:
                        raise ValueError("补充信息过长")
                    raw_metadata = base64.b64decode(metadata_header, validate=True)
                    if len(raw_metadata) > 4096:
                        raise ValueError("补充信息过长")
                    metadata = json.loads(raw_metadata)
                    if not isinstance(metadata, dict) or set(metadata) != {"tags", "instructions", "version"}:
                        raise ValueError("补充信息格式错误")
                    if (
                        not isinstance(metadata["tags"], list)
                        or len(metadata["tags"]) > 10
                        or any(
                            not isinstance(t, str) or not t.strip() or len(t) > 32 for t in metadata["tags"]
                        )
                    ):
                        raise ValueError("标签格式错误")
                    if (
                        not isinstance(metadata["instructions"], str)
                        or len(metadata["instructions"]) > 1000
                        or type(metadata["version"]) is not int
                        or metadata["version"] < 0
                    ):
                        raise ValueError("说明格式错误")
                if len(content_digest) != 64:
                    raise ValueError("内容校验格式错误")
                int(content_digest, 16)
                if metadata["tags"] or metadata["instructions"] or metadata["version"]:
                    digest = hashlib.sha256(
                        (content_digest + json.dumps(metadata, sort_keys=True, ensure_ascii=False)).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                if mode == "deep":
                    digest = hashlib.sha256((digest + ":deep").encode("ascii")).hexdigest()
                length = int(self.headers.get("Content-Length", "-1"))
                if jobs.cancelled(task_id):
                    self.send_json(410, {"error": {"code": "CANCELLED", "message": "任务已取消"}})
                    return
                if (
                    self.headers.get("Transfer-Encoding")
                    or kind not in {"text", "url", "image", "document", "audio", "video"}
                    or not name
                    or len(name) > 512
                    or len(digest) != 64
                ):
                    raise ValueError("材料元信息格式错误")
                int(digest, 16)
                if length < 0 or length > max_mb * 1024 * 1024:
                    self.send_json(413, {"error": {"code": "TOO_LARGE", "message": "材料超过上传大小上限"}})
                    return
                if not upload_lock.acquire(blocking=False):
                    self.send_json(429, {"error": {"code": "BUSY", "message": "正在接收其他材料，请重试"}})
                    return
                tmp = jobs.root / (task_id + ".upload")
                try:
                    with jobs.lock:
                        previous = jobs.db.execute(
                            "SELECT * FROM jobs WHERE id=? OR digest=?", (task_id, digest)
                        ).fetchone()
                    if previous:
                        if previous["digest"] != digest:
                            self.send_json(
                                409, {"error": {"code": "CONFLICT", "message": "任务标识冲突或保留期已到"}}
                            )
                            return
                        elif previous["state"] not in {"expired", "cancelled"}:
                            # Consume replayed bodies before closing the response. Otherwise a
                            # streaming client can see a reset instead of the idempotent 200.
                            self.connection.settimeout(60)
                            remaining = length
                            while remaining:
                                chunk = self.rfile.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ValueError("重复上传未完成")
                                remaining -= len(chunk)
                            self.send_json(200, {"id": previous["id"]})
                            return
                        else:
                            with jobs.lock, jobs.db:
                                jobs.db.execute("DELETE FROM jobs WHERE id=?", (previous["id"],))
                    used = sum(p.stat().st_size for p in jobs.root.rglob("*") if p.is_file())
                    if (
                        used + length > max_storage_gb * 1024**3
                        or shutil.disk_usage(jobs.root).free < length + 512 * 1024**2
                    ):
                        self.send_json(
                            507, {"error": {"code": "STORAGE_FULL", "message": "服务器存储空间不足"}}
                        )
                        return
                    checksum = hashlib.sha256(kind.encode() + b"\0")
                    self.connection.settimeout(60)
                    remaining = length
                    with tmp.open("wb") as stream:
                        while remaining:
                            if jobs.cancelled(task_id):
                                raise ValueError("任务已取消")
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("上传未完成")
                            stream.write(chunk)
                            checksum.update(chunk)
                            remaining -= len(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if checksum.hexdigest() != content_digest:
                        raise ValueError("材料校验失败")
                    ext = Path(name.replace("\\", "/")).suffix.lower()
                    if len(ext) > 12 or not all(c.isalnum() or c == "." for c in ext):
                        ext = ".bin"
                    path = jobs.root / (task_id + ext)
                    with jobs.lock, jobs.db:
                        if jobs.cancelled(task_id):
                            raise ValueError("任务已取消")
                        os.replace(tmp, path)
                        jobs.db.execute(
                            "INSERT INTO jobs(id,digest,kind,name,path,created,unlimited,user_tags,instructions,metadata_version,processing_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                task_id,
                                digest,
                                kind,
                                name,
                                str(path),
                                time.time(),
                                int(params.get("unlimited", ["0"])[0] == "1"),
                                json.dumps(metadata["tags"], ensure_ascii=False),
                                metadata["instructions"],
                                metadata["version"],
                                mode,
                            ),
                        )
                    self.send_json(201, {"id": task_id})
                finally:
                    tmp.unlink(missing_ok=True)
                    upload_lock.release()
            except (ValueError, KeyError, OSError):
                self.send_json(
                    400, {"error": {"code": "INVALID_REQUEST", "message": "材料接收失败，请检查格式后重试"}}
                )

    class BoundedServer(ThreadingHTTPServer):
        daemon_threads = True
        slots = threading.BoundedSemaphore(12)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                request.close()
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                request.settimeout(60)
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    return BoundedServer(address, Handler)


def main():
    jobs = Jobs(os.environ.get("KNOWLEDGE_DATA", "/data"))
    processor = DeepSeekProcessor(
        os.environ.get("DEEPSEEK_API_KEY", ""),
        os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        float(os.environ.get("MAX_MEDIA_MINUTES", "60")),
    )
    worker = Worker(jobs, processor)
    server = make_server(
        jobs,
        os.environ.get("KNOWLEDGE_TOKEN", ""),
        ("0.0.0.0", 8766),
        int(os.environ.get("MAX_UPLOAD_MB", "256")),
        int(os.environ.get("MAX_STORAGE_GB", "10")),
    )
    worker.start()
    try:
        server.serve_forever()
    finally:
        worker.stopping.set()
        server.server_close()


if __name__ == "__main__":
    main()
