"""Isolated runtime check: no real API calls and no production queue changes."""

import hashlib
import tempfile
import threading
import uuid
from pathlib import Path

import httpx

from .app import Jobs, Worker, make_server


def main():
    token = "isolated-smoke-token-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="knowledge-smoke-") as directory:
        jobs = Jobs(directory)
        server = make_server(jobs, token, ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            task_id = str(uuid.uuid4())
            payload = "部署验证用的临时材料".encode("utf-8")
            digest = hashlib.sha256(b"text\0" + payload).hexdigest()
            with httpx.Client(
                base_url=url, headers={"Authorization": "Bearer " + token}, trust_env=False
            ) as client:
                response = client.get("/health")
                response.raise_for_status()
                assert response.json()["api_version"] == 3
                response = client.post(
                    "/v1/tasks/" + task_id,
                    params={"kind": "text", "name": "smoke.txt", "digest": digest},
                    content=payload,
                )
                response.raise_for_status()
                result = {
                    "status": "processed",
                    "title": "部署验证",
                    "summary": "模拟 AI 结果",
                    "points": [],
                    "tags": [],
                    "original": payload.decode(),
                    "warning": "",
                }
                assert Worker(jobs, lambda _task: dict(result)).step()
                response = client.get("/v1/tasks/" + task_id)
                response.raise_for_status()
                assert response.json()["state"] == "ready"
                assert response.json()["result"]["title"] == "部署验证"
                response = client.post("/v1/tasks/" + task_id + "/cancel")
                response.raise_for_status()
                assert response.json()["state"] == "cancelled"
                assert not Path(jobs.get(task_id)["path"]).exists()
            print(
                "PASS: authenticated HTTP, persistent queue, mock AI result and cancellation. No real API calls."
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            jobs.db.close()


if __name__ == "__main__":
    main()
