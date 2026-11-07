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
import time
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
# 令牌只在本机有效：启动时生成（或取环境变量 NEBULA_PANEL_TOKEN），
# 启动时打印一次（环境变量传入的不打印），之后只存在内存里；
# /api/session 不再返回它，前端每次打开要用户输入一次（见 static/app.js）。
TOKEN = os.environ.get("NEBULA_PANEL_TOKEN") or secrets.token_urlsafe(32)
TOKEN_FROM_ENV = bool(os.environ.get("NEBULA_PANEL_TOKEN"))
STATE_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
PANEL_VERSION = "0.3.0"
# 与 nebula-deploy 的 ALLOWED_REMOTE_PARENTS 保持一致：必须是以下前缀之下的
# 真实子目录（长度必须大于前缀本身，即 /opt/、/home/ 这种裸父目录不算）。
ALLOWED_REMOTE_PARENTS = ("/opt/", "/srv/", "/var/www/", "/home/")
MAX_JOBS = 100
MAX_LOG_BYTES = 1 * 1024 * 1024
AUDIT_FILE = DATA / "audit.log"
AUDIT_MAX_BYTES = 200 * 1024
JOB_OUTPUT_SNIPPET = 500


def initial_state() -> dict:
    return {"servers": [], "projects": [], "last_inspect": {}}


def load_state() -> dict:
    DATA.mkdir(mode=0o700, exist_ok=True)
    if not STATE_FILE.exists():
        save_state(initial_state())
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    # 兼容旧版 state.json：缺失的键补默认值，不丢已有数据。
    state.setdefault("servers", [])
    state.setdefault("projects", [])
    state.setdefault("last_inspect", {})
    return state


def save_state(state: dict) -> None:
    DATA.mkdir(mode=0o700, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(STATE_FILE)


def _audit_line(action: str, target: str) -> str:
    return json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "action": action, "target": target},
        ensure_ascii=False,
    ) + "\n"


def audit_log(action: str, target: str) -> None:
    """操作审计：追加写 data/audit.log（0600），超 ~200KB 时只保留末尾行。"""
    DATA.mkdir(mode=0o700, exist_ok=True)
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as log:
            log.write(_audit_line(action, target))
        os.chmod(AUDIT_FILE, 0o600)
        if AUDIT_FILE.stat().st_size > AUDIT_MAX_BYTES:
            lines = AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            keep = lines[-200:]
            AUDIT_FILE.write_text("".join(keep), encoding="utf-8")
            os.chmod(AUDIT_FILE, 0o600)
    except OSError:
        pass


def read_audit(limit: int = 100) -> list[dict]:
    """返回最近的审计条目，新的在前；文件不存在或某行损坏时跳过该行。"""
    if not AUDIT_FILE.is_file():
        return []
    entries = []
    try:
        for line in AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return list(reversed(entries))[:limit]


def strict_int(value, name: str, minimum: int, maximum: int) -> int:
    """必须是真正的 JSON 整数：bool（isinstance(True, int) 为 True）和
    float、字符串一律拒绝，不做 int() 的静默截断/转换。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def remote_dir_allowed(value: str) -> bool:
    """与 nebula-deploy 的 allowlist 同一条规则：以 /opt/、/srv/、
    /var/www/、/home/ 之一开头，且长度大于前缀本身。"""
    return any(value.startswith(prefix) and len(value) > len(prefix) for prefix in ALLOWED_REMOTE_PARENTS)


def validate_ai_base_url(value) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("AI Base URL 必须是 http(s):// 开头的有效地址")
    return value


def clean_text(value, name: str, pattern: str, maximum=255) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum or not re.fullmatch(pattern, value):
        raise ValueError(f"{name} 格式不正确")
    return value


def validate_server(body: dict) -> dict:
    port = strict_int(body.get("port", 22), "SSH 端口", 1, 65535)
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
    if not remote.is_absolute() or not remote_dir_allowed(str(remote)):
        raise ValueError("远端目录必须是 /opt/、/srv/、/var/www/ 或 /home/<用户>/ 之下的具体目录")
    return {
        "id": body.get("id") or uuid.uuid4().hex[:12],
        "name": clean_text(body.get("name"), "项目名称", r"[^/\x00-\x1f]+"),
        "source_dir": str(source),
        "remote_dir": str(remote),
        "max_source_mb": strict_int(body.get("max_source_mb", 512), "源码上限", 1, 4096),
        "ai_base_url": validate_ai_base_url(body.get("ai_base_url", "")),
        "ai_model": str(body.get("ai_model", "")).strip(),
    }


def check_server_duplicate(state: dict, item: dict, exclude_id: str | None = None) -> None:
    for other in state["servers"]:
        if exclude_id and other["id"] == exclude_id:
            continue
        if (other["host"], other["port"], other["user"]) == (item["host"], item["port"], item["user"]):
            raise ValueError("已存在相同的服务器（地址、端口、用户相同）")


def check_project_duplicate(state: dict, item: dict, exclude_id: str | None = None) -> None:
    for other in state["projects"]:
        if exclude_id and other["id"] == exclude_id:
            continue
        if (other["source_dir"], other["remote_dir"]) == (item["source_dir"], item["remote_dir"]):
            raise ValueError("已存在相同的项目（源码目录、远端目录相同）")


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


def kill_process_group(process, timeout: float = 5.0) -> None:
    """先 SIGTERM，等最多 timeout 秒；还活着就 SIGKILL，再等它死。
    忽略 SIGTERM 的子进程也不会再留下来。进程已退出时什么都不做。"""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def truncate_log_file(log_file: Path, limit: int = MAX_LOG_BYTES) -> None:
    """把日志文件截到最后 limit 字节（按行边界），不存在时什么都不做。"""
    try:
        if not log_file.is_file() or log_file.stat().st_size <= limit:
            return
        raw = log_file.read_bytes()[-limit:]
        cut = raw.find(b"\n")
        log_file.write_bytes(raw[cut + 1:] if cut != -1 else raw)
        os.chmod(log_file, 0o600)
    except OSError:
        pass


def persist_job(job: dict) -> None:
    folder = JOBS_DIR / job["id"]
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = {key: value for key, value in job.items() if key not in {"process", "output", "cancel_requested"}}
    temp = folder / "meta.tmp"
    temp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(folder / "meta.json")


def prune_jobs() -> None:
    """最多保留 MAX_JOBS 个任务：按创建时间删最旧的任务目录（meta + 日志）。"""
    if len(JOBS) <= MAX_JOBS:
        return
    ordered = sorted(JOBS.values(), key=lambda item: item.get("created_at", ""))
    for job in ordered[:len(JOBS) - MAX_JOBS]:
        if job.get("status") in {"queued", "running", "cancelling"}:
            continue
        folder = JOBS_DIR / job["id"]
        JOBS.pop(job["id"], None)
        try:
            for child in folder.iterdir():
                child.unlink()
            folder.rmdir()
        except OSError:
            pass


def restore_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    restored = []
    for meta_file in JOBS_DIR.glob("*/meta.json"):
        try:
            job = json.loads(meta_file.read_text(encoding="utf-8"))
            log_file = meta_file.parent / "output.log"
            truncate_log_file(log_file)
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
    prune_jobs()


def public_job(job: dict, include_output: bool = False) -> dict:
    """任务摘要：列表接口只给摘要（状态、时间、输出长度和末尾片段），
    不返回完整输出；详情接口（include_output=True）才给全文和计划。"""
    result = {key: value for key, value in job.items() if key not in {"process", "cancel_requested", "output"}}
    output = job.get("output", "")
    result["output_len"] = len(output)
    result["output_tail"] = output[-JOB_OUTPUT_SNIPPET:]
    if include_output:
        result["output"] = output
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
        if process.stdout:
            process.stdout.close()
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
        truncate_log_file(log_file)
        if job.get("action") == "inspect" and job.get("server_id"):
            with STATE_LOCK:
                state = load_state()
                state["last_inspect"][job["server_id"]] = {
                    "job_id": job["id"],
                    "finished_at": job["finished_at"],
                    "status": job["status"],
                    "snippet": (job.get("output") or "")[:JOB_OUTPUT_SNIPPET],
                }
                save_state(state)
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
    # 后台升级：先 SIGTERM，等 5 秒还不死就 SIGKILL；run_job 的 wait
    # 返回后状态会变成 cancelled，不会一直卡在 cancelling。
    threading.Thread(target=kill_process_group, args=(process,), daemon=True).start()
    audit_log("job_cancel", f"{job.get('action', '')} {job_id}")
    return public_job(job)


def shutdown_jobs(timeout: float = 5.0) -> None:
    """关面板时终止所有在途任务：先全部 SIGTERM，等最多 timeout 秒，
    还活着的再 SIGKILL，并等它们真正退出。"""
    live = []
    for job in list(JOBS.values()):
        process = job.get("process")
        if process and process.poll() is None:
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            persist_job(job)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            live.append(process)
    deadline = time.monotonic() + timeout
    for process in live:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
    for process in live:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
    for process in live:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


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
    plan_job = ""
    if action == "apply":
        plan_job = clean_text(body.get("plan_job_id"), "计划任务", r"[a-f0-9]{12}")
        plan_file = JOBS_DIR / plan_job / "plan.json"
        if not plan_file.is_file():
            raise ValueError("找不到对应的 plan.json")
        command += ["--plan", str(plan_file), "--yes"]
        if body.get("allow_swap"):
            command.append("--allow-swap")
        retries = strict_int(body.get("auto_repair", 0), "自动修复", 0, 2)
        command += ["--auto-repair", str(retries)]
    now = datetime.now(timezone.utc).isoformat()
    job = {"id": job_id, "action": action, "server_id": server["id"], "project_id": project["id"] if project else "", "server_name": server["name"], "project_name": project["name"] if project else "", "status": "queued", "output": "", "returncode": None, "created_at": now, "plan_job_id": plan_job}
    JOBS[job_id] = job
    prune_jobs()
    persist_job(job)
    audit_log("job_retry" if body.get("retry_of") else "job_create",
              f"{action} {job_id} {server['name']}" + (f" / {project['name']}" if project else ""))
    threading.Thread(target=run_job, args=(job_id, command, job_dir), daemon=True).start()
    return public_job(job)


def delete_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job.get("status") in {"queued", "running", "cancelling"}:
        raise ValueError("任务还在运行，先取消再删除")
    folder = JOBS_DIR / job_id
    JOBS.pop(job_id, None)
    try:
        for child in folder.iterdir():
            child.unlink()
        folder.rmdir()
    except OSError:
        pass
    audit_log("job_delete", f"{job.get('action', '')} {job_id}")


class Handler(BaseHTTPRequestHandler):
    server_version = "NebulaPanel/0.3"

    def log_message(self, fmt, *args):
        print(f"[panel] {self.address_string()} {fmt % args}")

    def check_token(self):
        # 所有 /api 路由都要令牌：读接口也不例外。令牌只在启动时打印一次，
        # /api/session 不再返回它；错了或没有一律 403。
        if self.headers.get("X-Nebula-Token") != TOKEN:
            raise PermissionError("请求令牌无效")

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
            if path.startswith("/api/"):
                self.check_token()
            if path == "/api/session":
                # 只返回状态，不再返回令牌。
                self.json_response({"deploy_ready": DEPLOY.is_file(), "ai_key_ready": bool(os.environ.get("DEPLOY_AI_KEY")), "version": PANEL_VERSION})
            elif path == "/api/audit":
                self.json_response(read_audit())
            elif path == "/api/state":
                self.json_response(load_state())
            elif path == "/api/jobs":
                self.json_response([public_job(job) for job in reversed(list(JOBS.values()))])
            elif path.startswith("/api/jobs/"):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                self.json_response(public_job(job, include_output=True) if job else {"error": "任务不存在"}, 200 if job else 404)
            elif path == "/":
                self.serve_file(STATIC / "index.html")
            elif path.startswith("/static/"):
                target = (STATIC / path.removeprefix("/static/")).resolve()
                if STATIC.resolve() not in target.parents:
                    raise FileNotFoundError()
                self.serve_file(target)
            else:
                self.send_error(404)
        except PermissionError as error:
            self.json_response({"error": str(error)}, 403)
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
                    state = load_state()
                    check_server_duplicate(state, item)
                    state["servers"].append(item); save_state(state)
                audit_log("server_add", f"{item['name']} {item['user']}@{item['host']}:{item['port']}")
                self.json_response(item, 201)
            elif path == "/api/projects":
                item = validate_project(body)
                with STATE_LOCK:
                    state = load_state()
                    check_project_duplicate(state, item)
                    state["projects"].append(item); save_state(state)
                audit_log("project_add", f"{item['name']} {item['source_dir']} -> {item['remote_dir']}")
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

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            match = re.fullmatch(r"/api/(servers|projects)/([a-f0-9]{12})", path)
            if not match:
                return self.json_response({"error": "接口不存在"}, 404)
            collection, item_id = match.groups()
            validate = validate_server if collection == "servers" else validate_project
            check_duplicate = check_server_duplicate if collection == "servers" else check_project_duplicate
            body["id"] = item_id
            item = validate(body)
            with STATE_LOCK:
                state = load_state()
                current = by_id(state[collection], item_id, "服务器" if collection == "servers" else "项目")
                check_duplicate(state, item, exclude_id=item_id)
                state[collection] = [item if entry["id"] == item_id else entry for entry in state[collection]]
                save_state(state)
            audit_log("server_edit" if collection == "servers" else "project_edit",
                      f"{item['name']} {item_id}")
            self.json_response(item)
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
            job_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})", path)
            if job_match:
                delete_job(job_match.group(1))
                return self.json_response({"ok": True})
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
            audit_log("server_delete" if collection == "servers" else "project_delete", item_id)
            self.json_response({"ok": True})
        except PermissionError as error:
            self.json_response({"error": str(error)}, 403)
        except (ValueError, json.JSONDecodeError) as error:
            self.json_response({"error": str(error)}, 400)
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
    if TOKEN_FROM_ENV:
        print("访问令牌来自环境变量 NEBULA_PANEL_TOKEN（浏览器打开面板后输入该令牌）")
    else:
        print("访问令牌（只显示这一次，浏览器打开面板后输入）：")
        print(TOKEN)

    def _request_stop(signum, frame):
        # 收到 SIGTERM/SIGINT 也要清理在途任务。不能在信号处理函数里直接
        # 调 server.shutdown()（主线程正在 serve_forever，会死锁），
        # 所以另起线程触发；finally 里的 shutdown_jobs 再等子进程死透。
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_jobs()
        server.server_close()


if __name__ == "__main__":
    main()
