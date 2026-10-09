"""Exercise the PowerShell script against a local mock Transmission RPC server."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer


SCRIPT = Path(__file__).resolve().parents[1] / "Remove-TransmissionPublicTorrents.ps1"
POWERSHELL_RUNNER = r"""
$ErrorActionPreference = "Stop"
function Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    if ($AsSecureString) {
        return ConvertTo-SecureString $env:TRANSMISSION_TEST_PASSWORD -AsPlainText -Force
    }
    return $env:TRANSMISSION_TEST_USERNAME
}
try {
    & $env:TRANSMISSION_TEST_SCRIPT -TransmissionHost "127.0.0.1" `
        -TransmissionPort ([int]$env:TRANSMISSION_TEST_PORT) -TransmissionScheme "http" `
        -RemovePrivateOlderThan30Days:([bool]::Parse($env:TRANSMISSION_TEST_REMOVE_PRIVATE))
}
catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
"""


class RpcHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body, headers=None):
        content = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.server.get_requests += 1
        self.reply(405, {"message": "RPC requires POST"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request = json.loads(body)
        server = self.server
        server.requests.append((self.path, dict(self.headers), request))
        credentials = f"{server.username}:{server.password}".encode("utf-8")
        expected_auth = "Basic " + base64.b64encode(credentials).decode("ascii")
        if self.headers.get("Authorization") != expected_auth:
            self.reply(401, {"message": "Unauthorized"})
            return

        if request["method"] == "torrent_remove" and server.rotate_on_remove:
            server.session_id = "renewed-session"
            server.rotate_on_remove = False

        if server.always_conflict or self.headers.get("X-Transmission-Session-Id") != server.session_id:
            headers = {} if server.missing_session_header else {"X-Transmission-Session-Id": server.session_id}
            self.reply(409, {"message": "Session ID required"}, headers)
            return

        response = {"jsonrpc": "2.0", "id": request.get("id")}
        if request["method"] == server.rpc_error_method:
            response["error"] = {"code": -32602, "message": "Invalid params"}
        elif request["method"] == "torrent_get":
            response["result"] = {"torrents": server.torrents}
        elif request["method"] == "torrent_remove":
            server.removals.append(request["params"])
            response["result"] = {}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        self.reply(200, response)


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
class TransmissionRpcTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), RpcHandler)
        self.server.username = "rpc-user"
        self.server.password = "example-password"
        self.server.session_id = "initial-session"
        self.server.requests = []
        self.server.removals = []
        self.server.get_requests = 0
        self.server.rotate_on_remove = False
        self.server.always_conflict = False
        self.server.missing_session_header = False
        self.server.rpc_error_method = None
        now = int(time.time())
        self.server.torrents = [
            self.torrent(1, False, 1, now - 40 * 86400),
            self.torrent(2, False, 0.5, now - 40 * 86400),
            self.torrent(3, True, 1, now - 40 * 86400),
            self.torrent(4, True, 1, now - 5 * 86400),
            self.torrent(5, True, 0.5, now - 40 * 86400),
            self.torrent(6, True, 1, 0),
        ]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    @staticmethod
    def torrent(torrent_id, private, progress, added_date):
        return {
            "id": torrent_id,
            "name": f"Torrent {torrent_id}",
            "is_private": private,
            "percent_done": progress,
            "added_date": added_date,
        }

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def run_script(self, remove_private=False, password=None):
        env = dict(os.environ)
        env.update({
            "TRANSMISSION_TEST_SCRIPT": str(SCRIPT),
            "TRANSMISSION_TEST_PORT": str(self.server.server_port),
            "TRANSMISSION_TEST_USERNAME": self.server.username,
            "TRANSMISSION_TEST_PASSWORD": self.server.password if password is None else password,
            "TRANSMISSION_TEST_REMOVE_PRIVATE": str(remove_private),
        })
        result = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-Command", POWERSHELL_RUNNER],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(self.server.get_requests, 0)
        for path, _, request in self.server.requests:
            self.assertEqual(path, "/transmission/rpc")
            self.assertEqual(request["jsonrpc"], "2.0")
            self.assertIsNotNone(request["id"])
            self.assertNotIn("arguments", request)
            if request["method"] == "torrent_get":
                self.assertEqual(set(request["params"]["fields"]), {
                    "id", "name", "percent_done", "is_private", "added_date",
                })
        return result

    def assert_success(self, result, deleted, remaining):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Torrents deleted: {deleted}", result.stdout)
        self.assertIn(f"Torrents remaining: {remaining}", result.stdout)
        for removal in self.server.removals:
            self.assertIs(removal["delete_local_data"], True)
            self.assertEqual(len(removal["ids"]), 1)

    def test_full_password_and_default_public_removal(self):
        result = self.run_script()
        self.assert_success(result, deleted=1, remaining=5)
        self.assertEqual([r["ids"] for r in self.server.removals], [[1]])
        self.assertEqual(self.server.requests[0][2], self.server.requests[1][2])
        self.assertEqual(self.server.requests[1][1]["X-Transmission-Session-Id"], "initial-session")

    def test_unicode_credentials(self):
        self.server.username = "rpc-usér"
        self.server.password = "pässword-long-🔑"
        self.assert_success(self.run_script(), deleted=1, remaining=5)

    def test_private_age_option_preserves_incomplete_recent_and_unknown_age(self):
        result = self.run_script(remove_private=True)
        self.assert_success(result, deleted=2, remaining=4)
        self.assertEqual([r["ids"] for r in self.server.removals], [[1], [3]])

    def test_session_expiration_during_removal_retries_same_request(self):
        self.server.rotate_on_remove = True
        self.assert_success(self.run_script(remove_private=True), deleted=2, remaining=4)
        self.assertEqual(self.server.requests[2][2], self.server.requests[3][2])
        self.assertEqual(self.server.requests[3][1]["X-Transmission-Session-Id"], "renewed-session")
        self.assertEqual(self.server.requests[4][1]["X-Transmission-Session-Id"], "renewed-session")

    def test_invalid_credentials_stop_on_first_request(self):
        result = self.run_script(password="incorrect-password")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authentication failed (HTTP 401)", result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.removals, [])

    def test_missing_session_header_stops_without_retry(self):
        self.server.missing_session_header = True
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("409 without an X-Transmission-Session-Id header", result.stderr)
        self.assertEqual(len(self.server.requests), 1)

    def test_repeated_session_conflict_has_bounded_retry(self):
        self.server.always_conflict = True
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.removals, [])

    def test_rpc_errors_stop_without_reporting_success(self):
        for method in ("torrent_get", "torrent_remove"):
            with self.subTest(method=method):
                self.server.rpc_error_method = method
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid params", result.stderr)
                self.assertNotIn("Torrents deleted:", result.stdout)
                self.assertEqual(self.server.removals, [])

    def test_empty_and_single_torrent_counts(self):
        for torrents in ([], self.server.torrents[:1]):
            with self.subTest(count=len(torrents)):
                self.server.torrents = torrents
                self.server.removals = []
                result = self.run_script()
                self.assert_success(result, deleted=len(torrents), remaining=0)
                self.assertIn(f"Total torrents checked: {len(torrents)}", result.stdout)


if __name__ == "__main__":
    unittest.main()
