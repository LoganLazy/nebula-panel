#!/usr/bin/env python3
"""Nebula Panel - local web console for Nebula Deploy."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
JOBS_DIR = DATA / "jobs"
DEPLOY = Path(os.environ.get("NEBULA_DEPLOY_PATH", ROOT.parent / "nebula-deploy" / "nebula_deploy.py")).resolve()
TOKEN = secrets.token_urlsafe(32)
STATE_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
PANEL_VERSION = "0.2.0"


def initial_state() -> dict:
    return {"servers": [], "projects": []}


def load_state() -> dict:
    DATA.mkdir(mode=0o700, exist_ok=True)
    if not STATE_FILE.exists():
        save_state(initial_state())
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    DATA.mkdir(mode=0o700, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(STATE_FILE)


def clean_text(value, name: str, pattern: str, maximum=255) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum or not re.fullmatch(pattern, value):
        raise ValueError(f"{name} 格式不正确")
    return value


def validate_server(body: dict) -> dict:
    port = int(body.get("port", 22))
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口不正确")
    identity = str(body.get("identity_file", "")).strip()
    if identity and not Path(identity).expanduser().is_file():
        raise ValueError("SSH 私钥文件不存在")
    return {
        "id": body.get("id") or uuid.uuid4().hex[:12],
        "name": clean_text(body.get("name"), "服务器名称", r"[^/\x00-\x1f]+"),
        "host": clean_text(body.get("host"), "服务器地址", r"[A-Za-z0-9._:-]+"),
        "port": port,
        "user": clean_text(body.get("user"), "SSH 用户", r"[A-Za-z0-9._-]+"),
        "identity_file": identity,
    }


def validate_project(body: dict) -> dict:
    source = Path(str(body.get("source_dir", ""))).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("源码目录不存在")
    remote = PurePosixPath(str(body.get("remote_dir", "")))
    if not remote.is_absolute() or str(remote) in {"/", "/etc", "/usr", "/var", "/home"}:
        raise ValueError("远端目录必须是具体的绝对路径")
    return {
        "id": body.get("id") or uuid.uuid4().hex[:12],
        "name": clean_text(body.get("name"), "项目名称", r"[^/\x00-\x1f]+"),
        "source_dir": str(source),
        "remote_dir": str(remote),
        "max_source_mb": max(1, min(int(body.get("max_source_mb", 512)), 4096)),
        "ai_base_url": str(body.get("ai_base_url", "")).strip(),
        "ai_model": str(body.get("ai_model", "")).strip(),
    }


def by_id(items: list[dict], item_id: str, label: str) -> dict:
    match = next((item for item in items if item["id"] == item_id), None)
    if not match:
        raise ValueError(f"{label}不存在")
    return match


def deploy_config(server: dict, project: dict | None) -> dict:
    project = project or {
        "source_dir": str(ROOT),
        "remote_dir": "/tmp/nebula-panel-inspect",
        "max_source_mb": 1,
        "ai_base_url": "",
        "ai_model": "",
    }
    return {
        "ssh": {key: server[key] for key in ("host", "port", "user", "identity_file")},
        "source_dir": project["source_dir"],
        "remote_dir": project["remote_dir"],
        "max_source_mb": project["max_source_mb"],
        "ai": {
            "base_url": project["ai_base_url"],
            "model": project["ai_model"],
            "api_key_env": "DEPLOY_AI_KEY",
        },
    }


def persist_job(job: dict) -> None:
    folder = JOBS_DIR / job["id"]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = {key: value for key, value in job.items() if key not in {"process", "output", "cancel_requested"}}
    temp = folder / "meta.tmp"
    temp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(folder / "meta.json")


def restore_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    restored = []
    for meta_file in JOBS_DIR.glob("*/meta.json"):
        try:
            job = json.loads(meta_file.read_text(encoding="utf-8"))
            log_file = meta_file.parent / "output.log"
            job["output"] = log_file.read_text(encoding="utf-8", errors="replace")[-100_000:] if log_file.exists() else ""
            if job.get("status") in {"queued", "running", "cancelling"}:
                job["status"], job["returncode"] = "interrupted", None
                message = "\nPanel restarted before this task finished.\n"
                job["output"] += message
                with log_file.open("a", encoding="utf-8") as log: log.write(message)
                persist_job(job)
            restored.append(job)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    for job in sorted(restored, key=lambda item: item.get("created_at", "")):
        JOBS[job["id"]] = job


def public_job(job: dict) -> dict:
    result = {key: value for key, value in job.items() if key not in {"process", "cancel_requested"}}
    plan_file = JOBS_DIR / job["id"] / "plan.json"
    if job.get("action") == "plan" and plan_file.is_file():
        try:
            result["plan"] = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result["plan"] = None
    return result


def run_job(job_id: str, command: list[str], cwd: Path) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    persist_job(job)
    log_file = cwd / "output.log"
    try:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        job["process"] = process
        assert process.stdout
        with log_file.open("a", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line); log.flush()
                job["output"] = (job["output"] + line)[-100_000:]
        code = process.wait()
        job["returncode"] = code
        job["status"] = "cancelled" if job.get("cancel_requested") else ("success" if code == 0 else "failed")
    except Exception as error:
        message = f"\nPanel error: {error}\n"
        job["output"] += message
        with log_file.open("a", encoding="utf-8") as log: log.write(message)
        job["returncode"] = 2
        job["status"] = "failed"
    finally:
        job.pop("process", None)
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist_job(job)


def cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise ValueError("任务不存在")
    process = job.get("process")
    if job.get("status") not in {"queued", "running"} or not process:
        raise ValueError("该任务当前不能取消")
    job["cancel_requested"] = True
    job["status"] = "cancelling"
    persist_job(job)
    os.killpg(process.pid, signal.SIGTERM)
    return public_job(job)


def shutdown_jobs() -> None:
    for job in list(JOBS.values()):
        process = job.get("process")
        if process and process.poll() is None:
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            persist_job(job)
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass


def create_job(body: dict) -> dict:
    if not DEPLOY.is_file():
        raise RuntimeError(f"Nebula Deploy 不存在：{DEPLOY}")
    state = load_state()
    action = body.get("action")
    if action not in {"inspect", "plan", "apply"}:
        raise ValueError("任务类型不正确")
    server = by_id(state["servers"], str(body.get("server_id", "")), "服务器")
    project = None if action == "inspect" else by_id(state["projects"], str(body.get("project_id", "")), "项目")
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, mode=0o700)
    config_file = job_dir / "deploy.json"
    config_file.write_text(json.dumps(deploy_config(server, project), ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(config_file, 0o600)
    command = [sys.executable, str(DEPLOY), action, "--config", str(config_file)]
    if action == "apply":
        plan_job = clean_text(body.get("plan_job_id"), "计划任务", r"[a-f0-9]{12}")
        plan_file = JOBS_DIR / plan_job / "plan.json"
        if not plan_file.is_file():
            raise ValueError("找不到对应的 plan.json")
        command += ["--plan", str(plan_file), "--yes"]
        if body.get("allow_swap"):
            command.append("--allow-swap")
        retries = max(0, min(int(body.get("auto_repair", 0)), 2))
        command += ["--auto-repair", str(retries)]
    now = datetime.now(timezone.utc).isoformat()
    job = {"id": job_id, "action": action, "server_id": server["id"], "project_id": project["id"] if project else "", "server_name": server["name"], "project_name": project["name"] if project else "", "status": "queued", "output": "", "returncode": None, "created_at": now}
    JOBS[job_id] = job
    persist_job(job)
    threading.Thread(target=run_job, args=(job_id, command, job_dir), daemon=True).start()
    return public_job(job)


class Handler(BaseHTTPRequestHandler):
    server_version = "NebulaPanel/0.2"

    def log_message(self, fmt, *args):
        print(f"[panel] {self.address_string()} {fmt % args}")

    def json_response(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        if self.headers.get("X-Nebula-Token") != TOKEN:
            raise PermissionError("请求令牌无效")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 application/json")
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/session":
                self.json_response({"token": TOKEN, "deploy_ready": DEPLOY.is_file(), "ai_key_ready": bool(os.environ.get("DEPLOY_AI_KEY")), "version": PANEL_VERSION})
            elif path == "/api/state":
                self.json_response(load_state())
            elif path == "/api/jobs":
                self.json_response([public_job(job) for job in reversed(list(JOBS.values()))])
            elif path.startswith("/api/jobs/"):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                self.json_response(public_job(job) if job else {"error": "任务不存在"}, 200 if job else 404)
            elif path == "/":
                self.serve_file(STATIC / "index.html")
            elif path.startswith("/static/"):
                target = (STATIC / path.removeprefix("/static/")).resolve()
                if STATIC.resolve() not in target.parents:
                    raise FileNotFoundError()
                self.serve_file(target)
            else:
                self.send_error(404)
        except FileNotFoundError:
            self.json_response({"error": "文件不存在"}, 404)
        except Exception as error:
            self.json_response({"error": str(error)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/servers":
                item = validate_server(body)
                with STATE_LOCK:
                    state = load_state(); state["servers"].append(item); save_state(state)
                self.json_response(item, 201)
            elif path == "/api/projects":
                item = validate_project(body)
                with STATE_LOCK:
                    state = load_state(); state["projects"].append(item); save_state(state)
                self.json_response(item, 201)
            elif path == "/api/jobs":
                self.json_response(create_job(body), 202)
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{12}/cancel", path):
                self.json_response(cancel_job(path.split("/")[3]))
            else:
                self.json_response({"error": "接口不存在"}, 404)
        except PermissionError as error:
            self.json_response({"error": str(error)}, 403)
        except (ValueError, json.JSONDecodeError) as error:
            self.json_response({"error": str(error)}, 400)
        except Exception as error:
            self.json_response({"error": str(error)}, 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            self.read_json()
            match = re.fullmatch(r"/api/(servers|projects)/([a-f0-9]{12})", path)
            if not match:
                return self.json_response({"error": "接口不存在"}, 404)
            collection, item_id = match.groups()
            with STATE_LOCK:
                state = load_state()
                before = len(state[collection])
                state[collection] = [item for item in state[collection] if item["id"] != item_id]
                if len(state[collection]) == before:
                    return self.json_response({"error": "记录不存在"}, 404)
                save_state(state)
            self.json_response({"ok": True})
        except PermissionError as error:
            self.json_response({"error": str(error)}, 403)
        except Exception as error:
            self.json_response({"error": str(error)}, 400)

    def serve_file(self, path: Path):
        if not path.is_file():
            return self.send_error(404)
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", (mimetypes.guess_type(path.name)[0] or "application/octet-stream") + ("; charset=utf-8" if path.suffix in {".html", ".css", ".js"} else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("首版仅允许监听本机地址")
    load_state()
    restore_jobs()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Nebula Panel: http://{args.host}:{args.port}")
    print(f"Nebula Deploy: {DEPLOY}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_jobs()
        server.server_close()


if __name__ == "__main__":
    main()
