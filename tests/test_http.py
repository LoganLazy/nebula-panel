"""HTTP tests with isolated local state; no SSH or model calls.

A stub nebula-deploy script stands in for the real CLI: the fast stub
finishes immediately, the stubborn stub ignores SIGTERM and sleeps, so
cancel/shutdown escalation can be verified live.
"""
import json
import os
import time
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server as panel

FAST_STUB = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
action = sys.argv[1]
print(f"stub: {action}", flush=True)
if action == "plan":
    Path("plan.json").write_text(json.dumps({"steps": [{"cmd": "echo hi"}]}), encoding="utf-8")
print("stub done", flush=True)
"""

STUBBORN_STUB = """#!/usr/bin/env python3
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("stubborn: ignoring SIGTERM, sleeping", flush=True)
time.sleep(60)
print("stubborn done", flush=True)
"""


class PanelHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "src").mkdir()
        self.fast = self.base / "deploy-fast.py"
        self.fast.write_text(FAST_STUB, encoding="utf-8")
        self.stubborn = self.base / "deploy-stubborn.py"
        self.stubborn.write_text(STUBBORN_STUB, encoding="utf-8")
        self.patcher = patch.multiple(
            panel, DATA=self.base, STATE_FILE=self.base / "state.json",
            JOBS_DIR=self.base / "jobs", AUDIT_FILE=self.base / "audit.log",
            JOBS={}, DEPLOY=self.fast)
        self.patcher.start()
        self.httpd = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.httpd.server_port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.patcher.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, token=panel.TOKEN, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Nebula-Token"] = token
        req = urllib.request.Request(
            self.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def add_server(self, **overrides):
        body = {"name": "Example", "host": "example.test", "user": "deploy"}
        body.update(overrides)
        status, item = self.request("/api/servers", body)
        self.assertEqual(status, 201, item)
        return item

    def add_project(self, **overrides):
        body = {"name": "App", "source_dir": str(self.base / "src"),
                "remote_dir": "/opt/app", "ai_base_url": "https://api.example.com/v1",
                "ai_model": "model"}
        body.update(overrides)
        status, item = self.request("/api/projects", body)
        self.assertEqual(status, 201, item)
        return item

    def wait_job(self, job_id, statuses, timeout=10):
        deadline = time.monotonic() + timeout
        job = {}
        while time.monotonic() < deadline:
            status, job = self.request(f"/api/jobs/{job_id}")
            self.assertEqual(status, 200, job)
            if job["status"] in statuses:
                return job
            time.sleep(0.2)
        self.fail(f"job {job_id} did not reach {statuses} in {timeout}s (last: {job['status']})")

    def test_session_has_no_token_and_reads_require_token(self):
        status, body = self.request("/api/session", token=None)
        self.assertEqual(status, 403)
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertNotIn("token", session)
        self.assertEqual(session["version"], panel.PANEL_VERSION)
        for path in ("/api/state", "/api/jobs", "/api/audit"):
            status, _ = self.request(path, token=None)
            self.assertEqual(status, 403, path)
        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["servers"], [])

    def test_strict_int_port(self):
        for bad in (True, 22.5, "abc", 0, 70000):
            status, body = self.request(
                "/api/servers", {"name": "X", "host": "h", "user": "u", "port": bad})
            self.assertEqual(status, 400, bad)
        status, item = self.request(
            "/api/servers", {"name": "X", "host": "h", "user": "u", "port": 22})
        self.assertEqual(status, 201)

    def test_remote_dir_allowlist_and_ai_base_url(self):
        for bad in ("/etc/ssh", "/root", "/dev", "/tmp/x", "/home", "/opt"):
            status, body = self.request("/api/projects", {
                "name": "P", "source_dir": str(self.base / "src"), "remote_dir": bad,
                "ai_base_url": "https://api.example.com/v1", "ai_model": "m"})
            self.assertEqual(status, 400, bad)
        for good in ("/opt/app", "/srv/app", "/var/www/app", "/home/alice/app"):
            status, item = self.request("/api/projects", {
                "name": "P-" + good.replace("/", "_"), "source_dir": str(self.base / "src"),
                "remote_dir": good, "ai_base_url": "https://api.example.com/v1",
                "ai_model": "m"})
            self.assertEqual(status, 201, good)
            self.request("/api/projects/" + item["id"], {}, token=panel.TOKEN, method="DELETE")
        for bad_url in ("notaurl", "ftp://x", "https://", "://no-scheme"):
            status, body = self.request("/api/projects", {
                "name": "P", "source_dir": str(self.base / "src"), "remote_dir": "/opt/app",
                "ai_base_url": bad_url, "ai_model": "m"})
            self.assertEqual(status, 400, bad_url)

    def test_duplicate_server_and_project_rejected(self):
        self.add_server()
        status, body = self.request(
            "/api/servers", {"name": "Other", "host": "example.test", "user": "deploy"})
        self.assertEqual(status, 400)
        self.add_project()
        status, body = self.request("/api/projects", {
            "name": "Other", "source_dir": str(self.base / "src"),
            "remote_dir": "/opt/app", "ai_base_url": "", "ai_model": ""})
        self.assertEqual(status, 400)

    def test_edit_preserves_id_and_rejects_duplicate(self):
        first = self.add_server()
        second = self.add_server(name="Second", host="other.test")
        status, edited = self.request(
            "/api/servers/" + first["id"],
            {"name": "Renamed", "host": "example.test", "user": "deploy", "port": 2222},
            method="PUT")
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["id"], first["id"])
        self.assertEqual(edited["name"], "Renamed")
        status, body = self.request(
            "/api/servers/" + second["id"],
            {"name": "Second", "host": "example.test", "user": "deploy", "port": 2222},
            method="PUT")
        self.assertEqual(status, 400)
        project = self.add_project()
        status, edited = self.request(
            "/api/projects/" + project["id"],
            {"name": "App2", "source_dir": str(self.base / "src"),
             "remote_dir": "/opt/app2", "ai_base_url": "", "ai_model": "m"},
            method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual(edited["id"], project["id"])

    def test_inspect_job_records_last_inspect(self):
        server = self.add_server()
        status, job = self.request(
            "/api/jobs", {"action": "inspect", "server_id": server["id"]})
        self.assertEqual(status, 202, job)
        job = self.wait_job(job["id"], {"success"})
        status, state = self.request("/api/state")
        last = state["last_inspect"][server["id"]]
        self.assertEqual(last["status"], "success")
        self.assertEqual(last["job_id"], job["id"])
        self.assertIn("stub: inspect", last["snippet"])

    def test_job_list_is_summary_and_detail_has_full_output(self):
        server = self.add_server()
        status, job = self.request(
            "/api/jobs", {"action": "inspect", "server_id": server["id"]})
        self.wait_job(job["id"], {"success"})
        status, jobs = self.request("/api/jobs")
        self.assertEqual(status, 200)
        summary = [j for j in jobs if j["id"] == job["id"]][0]
        self.assertNotIn("output", summary)
        self.assertEqual(summary["output_len"], len("stub: inspect\nstub done\n"))
        self.assertIn("stub done", summary["output_tail"])
        status, detail = self.request(f"/api/jobs/{job['id']}")
        self.assertEqual(detail["output"], "stub: inspect\nstub done\n")

    def test_delete_job(self):
        server = self.add_server()
        status, job = self.request(
            "/api/jobs", {"action": "inspect", "server_id": server["id"]})
        self.wait_job(job["id"], {"success"})
        status, body = self.request(f"/api/jobs/{job['id']}", {}, method="DELETE")
        self.assertEqual(status, 200, body)
        status, body = self.request(f"/api/jobs/{job['id']}")
        self.assertEqual(status, 404)
        status, body = self.request(f"/api/jobs/{job['id']}", {}, method="DELETE")
        self.assertEqual(status, 400)

    def test_cancel_kills_sigterm_ignoring_child(self):
        panel.DEPLOY = self.stubborn
        server = self.add_server()
        status, job = self.request(
            "/api/jobs", {"action": "inspect", "server_id": server["id"]})
        self.assertEqual(status, 202, job)
        job_id = job["id"]
        started = time.monotonic()
        process = None
        while time.monotonic() - started < 5:
            process = panel.JOBS[job_id].get("process")
            if process is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(process, "child never started")
        pid = process.pid
        status, body = self.request(f"/api/jobs/{job_id}/cancel", {}, method="POST")
        self.assertEqual(status, 200, body)
        cancelled = self.wait_job(job_id, {"cancelled"}, timeout=10)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_audit_log_records_server_add(self):
        self.add_server()
        status, entries = self.request("/api/audit")
        self.assertEqual(status, 200)
        self.assertTrue(any(
            e["action"] == "server_add" and "Example" in e["target"] for e in entries))


class HelperTest(unittest.TestCase):
    def test_strict_int(self):
        self.assertEqual(panel.strict_int(22, "端口", 1, 65535), 22)
        for bad in (True, False, 22.5, "22", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                panel.strict_int(bad, "端口", 1, 65535)
        with self.assertRaises(ValueError):
            panel.strict_int(0, "端口", 1, 65535)

    def test_remote_dir_allowed(self):
        for good in ("/opt/app", "/srv/x", "/var/www/site", "/home/alice", "/home/alice/app"):
            self.assertTrue(panel.remote_dir_allowed(good), good)
        for bad in ("/opt", "/srv", "/var/www", "/home", "/etc/ssh", "/root", "/dev", "/tmp/x", "opt/app"):
            self.assertFalse(panel.remote_dir_allowed(bad), bad)

    def test_truncate_log_file(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "output.log"
            log.write_bytes(b"x\n" * 700_000)  # ~1.4MB
            panel.truncate_log_file(log)
            self.assertLessEqual(log.stat().st_size, panel.MAX_LOG_BYTES)
            self.assertTrue(log.read_bytes().endswith(b"x\n"))

    def test_prune_jobs_keeps_newest_100(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.multiple(panel, JOBS_DIR=base, JOBS={}):
                for i in range(105):
                    job_id = f"{i:012x}"
                    folder = base / job_id
                    folder.mkdir()
                    (folder / "meta.json").write_text("{}", encoding="utf-8")
                    panel.JOBS[job_id] = {
                        "id": job_id, "status": "success",
                        "created_at": f"2026-11-07T00:00:00+00:00-{i:05d}"}
                panel.prune_jobs()
                self.assertEqual(len(panel.JOBS), 100)
                self.assertNotIn("000000000000", panel.JOBS)
                self.assertIn("000000000068", panel.JOBS)
                self.assertFalse((base / "000000000000").exists())


if __name__ == "__main__":
    unittest.main()
