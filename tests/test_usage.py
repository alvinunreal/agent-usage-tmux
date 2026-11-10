import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_claude_usage as claude
import usage_data as usage


class ParsingTests(unittest.TestCase):
    def test_copilot_monthly_quota_and_credits(self):
        data = usage.parse_copilot({
            "quota_reset_date": "2026-12-01",
            "quota_snapshots": {"premium_interactions": {
                "entitlement": 20000, "remaining": 11472,
                "percent_remaining": 57.3, "credits_used": 8527,
            }},
        })
        self.assertEqual(data["windows"]["monthly"]["remaining"], 57)
        self.assertEqual(data["windows"]["monthly"]["reset_at"], 1796083200)
        self.assertEqual(data["credits_used"], 8527)

    def test_copilot_derives_missing_percentage(self):
        data = usage.parse_copilot({"quota_snapshots": {
            "premium_interactions": {"entitlement": 300, "remaining": 60},
        }})
        self.assertEqual(data["windows"]["monthly"]["remaining"], 20)
        self.assertIsNone(data["windows"]["monthly"]["reset_at"])

    def test_copilot_unlimited_and_zero_placeholders_are_not_full_bars(self):
        for quota in (
            {"unlimited": True},
            {"entitlement": 0, "remaining": 0, "percent_remaining": 100, "credits_used": 42},
        ):
            with self.subTest(quota=quota):
                self.assertEqual(usage.parse_copilot({
                    "quota_snapshots": {"premium_interactions": quota},
                })["windows"], {})

    def test_missing_quotas_and_invalid_numbers_are_errors(self):
        for value in (None, float("nan"), float("inf"), True, "bad"):
            with self.subTest(value=value), self.assertRaises(usage.UsageError):
                usage.number(value)
        for payload in ({}, {"quota_snapshots": []}, {"access_type_sku": "subscription_ended"}):
            with self.subTest(payload=payload), self.assertRaises(usage.UsageError):
                usage.parse_copilot(payload)

    def test_opencode_three_windows_and_api_percentage_units(self):
        data = usage.parse_opencode({"usage": {
            "rolling": {"usagePercent": 0.6, "resetInSec": 600},
            "weekly": {"usedPercent": 25, "resetAt": "2026-12-01T00:00:00Z"},
            "monthly": {"used": 3, "limit": 4},
        }}, now=1000)
        self.assertEqual(data["windows"]["5h"]["remaining"], 99)
        self.assertEqual(data["windows"]["5h"]["reset_at"], 1600)
        self.assertEqual(data["windows"]["weekly"]["remaining"], 75)
        self.assertEqual(data["windows"]["monthly"]["remaining"], 25)
        self.assertIsNone(data["windows"]["monthly"]["reset_at"])

    def test_opencode_missing_windows_stay_absent_and_missing_usage_is_error(self):
        data = usage.parse_opencode({"usage": {"rolling": {"usagePercent": 12}}}, now=1000)
        self.assertEqual(list(data["windows"]), ["5h"])
        for payload in ({}, {"usage": {"rolling": {}}}, {"usage": {"rolling": {"used": 1, "limit": 0}}}):
            with self.subTest(payload=payload), self.assertRaises(usage.UsageError):
                usage.parse_opencode(payload, now=1000)

    def test_claude_polling_is_read_only(self):
        payload = {"five_hour": {"utilization": 42, "resets_at": "2026-12-01T00:00:00Z"}}
        with patch.object(claude, "urlopen") as opener:
            opener.return_value.__enter__.return_value = io.StringIO(json.dumps(payload))
            headers = claude.fetch_rate_limit_headers(claude.DEFAULT_MESSAGES_URL, "test-token", 15)
        request = opener.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertEqual(claude.parse_utilisation(headers, "5h")[:2], (58, 1796083200))

    def test_codex_primary_can_be_weekly(self):
        payload = {"rate_limit": {"primary_window": {
            "limit_window_seconds": 604800, "used_percent": 100,
            "reset_at": 2000000000, "reset_after_seconds": 600,
        }}}
        with patch.object(usage.codex, "load_auth", return_value=("token", "account")), \
             patch.object(usage.codex, "fetch_usage", return_value=payload):
            data = usage.fetch_provider("codex")
        self.assertEqual(list(data["windows"]), ["weekly"])
        self.assertEqual(data["windows"]["weekly"]["label"], "Weekly")
        self.assertEqual(data["windows"]["weekly"]["remaining"], 0)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "opencode"
        self.directory.mkdir()
        self.environment = patch.dict(os.environ, {
            "XDG_DATA_HOME": self.temp.name, "OPENCODE_DB": "opencode.db",
            "OPENCODE_GO_API_KEY": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def database(self, rows):
        path = self.directory / "opencode.db"
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE credential (integration_id TEXT, active INTEGER, value TEXT)")
            connection.executemany("INSERT INTO credential VALUES (?,1,?)", [
                (integration, json.dumps(value)) for integration, value in rows
            ])
            connection.commit()
        return path

    def test_go_precedes_general_login_without_modifying_database(self):
        path = self.database([
            ("opencode", {"type": "key", "key": "general"}),
            ("opencode-go", {"type": "key", "key": "go"}),
        ])
        before = path.read_bytes()
        self.assertEqual(usage.opencode_token(), "go")
        self.assertEqual(path.read_bytes(), before)

    def test_v2_logout_does_not_resurrect_legacy_login(self):
        self.database([])
        (self.directory / "auth.json").write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "stale"},
        }))
        with self.assertRaisesRegex(usage.UsageError, "no active"):
            usage.opencode_token()

    def test_legacy_and_explicit_key(self):
        (self.directory / "auth.json").write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy"},
        }))
        self.assertEqual(usage.opencode_token(), "legacy")
        with patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "explicit"}):
            self.assertEqual(usage.opencode_token(), "explicit")

    def test_ambiguous_and_expired_logins_fail_without_fallback(self):
        self.database([
            ("opencode-go", {"type": "key", "key": "one"}),
            ("opencode-go", {"type": "key", "key": "two"}),
        ])
        with self.assertRaisesRegex(usage.UsageError, "ambiguous"):
            usage.opencode_token()
        with self.assertRaisesRegex(usage.UsageError, "expired"):
            usage.credential_token({"type": "oauth", "expires": 1000, "access": "expired"})

    def test_copilot_explicit_token_and_gh_fallback(self):
        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "explicit"}), \
             patch.object(usage.subprocess, "run") as run:
            self.assertEqual(usage.copilot_token(), "explicit")
            run.assert_not_called()
        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}), \
             patch.object(usage.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 0, stdout="oauth-token\n")) as run:
            self.assertEqual(usage.copilot_token(), "oauth-token")
            self.assertIn("github.com", run.call_args.args[0])


class CacheTests(unittest.TestCase):
    def test_cache_reuses_windows_and_refresh_nonce_bypasses_it(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
             patch.object(usage, "fetch_provider", return_value={
                 "windows": {"5h": usage.meter(80, 2000000000, "5 hours")},
             }) as fetch, patch.object(usage.time, "time", return_value=1000):
            first = usage.cached_usage("opencodego")
            self.assertEqual(usage.cached_usage("opencodego"), first)
            self.assertEqual(fetch.call_count, 1)
            usage.cached_usage("opencodego", "refresh")
            self.assertEqual(fetch.call_count, 2)
            path = Path(directory) / "agent-usage-tmux/opencodego.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_errors_are_explicit_and_retry_earlier_than_successes(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
             patch.object(usage, "fetch_provider", side_effect=usage.UsageError("login expired")) as fetch:
            with patch.object(usage.time, "time", return_value=1000):
                result = usage.cached_usage("copilot")
            self.assertEqual(result["windows"], {})
            self.assertEqual(result["error"], "login expired")
            with patch.object(usage.time, "time", return_value=1029):
                usage.cached_usage("copilot")
                self.assertEqual(fetch.call_count, 1)
            with patch.object(usage.time, "time", return_value=1030):
                usage.cached_usage("copilot")
                self.assertEqual(fetch.call_count, 2)

    def test_legacy_http_error_body_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
             patch.object(usage, "fetch_provider", side_effect=SystemExit("HTTP 401: sensitive body")):
            result = usage.cached_usage("codex")
            self.assertIn("HTTP 401", result["error"])
            self.assertNotIn("sensitive", json.dumps(result))


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.socket = f"agent-usage-test-{uuid.uuid4().hex}"
        cls.environment = dict(os.environ)
        cls.environment.pop("TMUX", None)
        cls.tmux("new-session", "-d", "-s", "test")
        cls.environment["TMUX"] = cls.tmux(
            "display-message", "-p", "#{socket_path},#{pid},0").stdout.strip()
        cls.environment["TERM"] = "xterm-256color"
        cls.master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        cls.client = subprocess.Popen(
            ["tmux", "-L", cls.socket, "attach-session", "-t", "test"],
            env=cls.environment, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        for _ in range(50):
            if cls.tmux("list-clients", "-F", "#{client_pid}").stdout.strip():
                break
            time.sleep(0.02)
        else:
            cls.tmux("kill-server")
            cls.client.wait(timeout=5)
            os.close(cls.master)
            raise RuntimeError("test tmux client did not attach")

    @classmethod
    def tearDownClass(cls):
        cls.tmux("kill-server")
        os.close(cls.master)
        cls.client.wait(timeout=5)

    @classmethod
    def tmux(cls, *args):
        return subprocess.run(
            ["tmux", "-L", cls.socket, "-f", "/dev/null", *args],
            env=cls.environment, capture_output=True, text=True, check=True)

    def setUp(self):
        for provider in usage.PROVIDERS:
            self.tmux("set-option", "-g", f"@agent_usage_cmd_{provider}", "printf 50")
            self.tmux("set-option", "-g", f"@agent_usage_reset_cmd_{provider}", "printf 3600")
            self.tmux("set-option", "-g", f"@agent_usage_window_{provider}", usage.WINDOWS[provider][0])

    def test_bash32_renders_all_providers_and_monthly_tag(self):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "scripts/agent_usage.sh"), "all", "5h", "5h", "monthly", "5h", "0"],
            env=self.environment, capture_output=True, text=True, check=True)
        for provider in usage.PROVIDERS:
            self.assertIn(f"au-{provider}", result.stdout)
        self.assertIn("GH m50%", result.stdout)
        self.assertNotIn("N/A", result.stdout)

    def test_failed_custom_command_is_na_not_zero(self):
        self.tmux("set-option", "-g", "@agent_usage_cmd_copilot", "exit 1")
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "scripts/agent_usage.sh"), "copilot"],
            env=self.environment, capture_output=True, text=True, check=True)
        self.assertIn("GH N/A", result.stdout)
        self.assertNotIn("0%", result.stdout)

    def test_plugin_interpolation_preserves_interval_and_reload_bindings(self):
        self.tmux("set-option", "-g", "status-right",
                  "#{claude_usage} | #{copilot_usage} | #{opencodego_usage} | #{copilot_usage}")
        self.tmux("set-option", "-g", "status-interval", "7")
        for _ in range(2):
            subprocess.run(["/bin/bash", str(ROOT / "agent-usage.tmux")],
                           env=self.environment, capture_output=True, text=True, check=True)
        status = self.tmux("show-option", "-gqv", "status-right").stdout
        self.assertNotIn("#{copilot_usage}", status)
        self.assertEqual(status.count("agent_usage.sh\" copilot"), 2)
        self.assertIn("agent_usage.sh\" opencodego", status)
        self.assertEqual(self.tmux("show-option", "-gqv", "status-interval").stdout.strip(), "7")
        binding = self.tmux("list-keys", "-T", "root", "MouseDown1Status").stdout
        self.assertEqual(binding.count("usage_menu.sh"), 1)

    def test_go_cycles_three_windows_and_copilot_stays_monthly(self):
        menu = str(ROOT / "scripts/usage_menu.sh")
        for expected in ("weekly", "monthly", "5h"):
            subprocess.run(["/bin/bash", menu, "toggle", "au-opencodego"],
                           env=self.environment, capture_output=True, text=True, check=True)
            self.assertEqual(self.tmux(
                "show-option", "-gqv", "@agent_usage_window_opencodego").stdout.strip(), expected)
        subprocess.run(["/bin/bash", menu, "toggle", "au-copilot"],
                       env=self.environment, capture_output=True, text=True, check=True)
        self.assertEqual(self.tmux("show-option", "-gqv", "@agent_usage_window_copilot").stdout.strip(), "monthly")

    def test_prefix_u_opens_scrollable_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "agent-usage-tmux"
            cache.mkdir()
            for provider in usage.PROVIDERS:
                data = {"windows": {
                    window: usage.meter(50, None, window.title()) for window in usage.WINDOWS[provider]
                }, "provider": provider, "fetched_at": time.time(), "nonce": "0"}
                (cache / f"{provider}.json").write_text(json.dumps(data))
            self.tmux("set-environment", "-g", "XDG_CACHE_HOME", directory)
            self.tmux("set-option", "-g", "prefix", "C-a")
            self.tmux("bind-key", "u", "display-popup", "-w", "90%", "-h", "80%", "-E",
                      f'bash "{ROOT}/scripts/usage_popup.sh"')
            while select.select([self.master], [], [], 0)[0]:
                os.read(self.master, 65536)
            os.write(self.master, b"\x01u")
            output = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([self.master], [], [], 0.1)[0]:
                    output += os.read(self.master, 65536)
                    if all(text in output for text in (b"Copilot", b"AGENT USAGE", b"Monthly")):
                        break
            try:
                self.assertIn(b"AGENT USAGE", output)
                self.assertIn(b"Copilot", output)
                self.assertIn(b"Monthly", output)
            finally:
                os.write(self.master, b"q")
                self.tmux("set-environment", "-gu", "XDG_CACHE_HOME")


if __name__ == "__main__":
    unittest.main()
