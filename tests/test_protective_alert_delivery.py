"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "exitmgr/manager.py"
TREE = ast.parse(SOURCE.read_text())
CLASS = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "ExitManager")
METHOD = next(node for node in CLASS.body if isinstance(node, ast.FunctionDef) and node.name == "_post_unthrottled_alert")
NAMESPACE = {"os": os, "asyncio": asyncio}
exec(compile(ast.fix_missing_locations(ast.Module(body=[METHOD], type_ignores=[])), str(SOURCE), "exec"), NAMESPACE)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.alerting = types.SimpleNamespace(post=Mock(return_value=True),
                                             alerts_channel=Mock(return_value="C_ALERTS"),
                                             error_channel=Mock(return_value="C_ERRORS"))
        self.package = types.ModuleType("exitmgr")
        self.package.alerting = self.alerting
        self.manager = types.SimpleNamespace(config=types.SimpleNamespace(alerts_channel="C_CONFIG"))

    def send(self, body="Protective exit withheld", what="stops-withheld"):
        output = io.StringIO()
        with patch.dict(sys.modules, {"exitmgr": self.package}), redirect_stdout(output):
            NAMESPACE["_post_unthrottled_alert"](self.manager, body, what)
        return output.getvalue()

    def test_missing_environment_token_still_uses_native_verified_transport(self):
        with patch.dict(os.environ, {}, clear=True):
            self.send()
        self.alerting.post.assert_called_once_with(
            "Protective exit withheld", "C_CONFIG", label="stops-withheld",
            fallback_channel="C_ERRORS", timeout=8, dedup=False)

    def test_missing_config_channel_uses_native_channel_resolution(self):
        self.manager.config.alerts_channel = ""
        self.send()
        self.alerting.post.assert_called_once()
        self.assertEqual(self.alerting.post.call_args.args[1], "C_ALERTS")

    def test_two_cycles_remain_unthrottled(self):
        self.send(); self.send()
        self.assertEqual(self.alerting.post.call_count, 2)
        self.assertTrue(all(call.kwargs["dedup"] is False for call in self.alerting.post.call_args_list))

    def test_rejected_slack_response_is_loud_and_never_crashes_cycle(self):
        self.alerting.post.return_value = False
        self.assertIn("UNDELIVERED", self.send())

    def test_transport_exception_does_not_crash_cycle(self):
        self.alerting.post.side_effect = TimeoutError("fixture transport timeout")
        self.assertIn("fixture transport timeout", self.send())

    def test_empty_body_does_not_contact_transport(self):
        self.send(body="")
        self.alerting.post.assert_not_called()

    def test_real_native_transport_reads_file_token_checks_body_and_reroutes(self):
        native = types.ModuleType("fixture_native_alerting")
        exec(compile((ROOT / "exitmgr/alerting.py").read_text(), "fixture_native_alerting", "exec"), native.__dict__)
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "fixture.env"
            env_path.write_text("SLACK_BOT_TOKEN=fixture-native-file-token\n")
            native.HERMES_ENV = str(env_path)
            native._cache = {"alerts_channel": "C_ALERTS", "error_channel": "C_ERRORS"}
            self.package.alerting = native
            output = io.StringIO()
            responses = [io.BytesIO(b'{"ok":false,"error":"channel_not_found"}'),
                         io.BytesIO(b'{"ok":true}')]
            with patch.dict(os.environ, {}, clear=True), patch.object(
                    native.urllib.request, "urlopen", side_effect=responses) as urlopen, redirect_stderr(output):
                self.assertNotIn("UNDELIVERED", self.send())
            self.assertEqual(urlopen.call_count, 2)
            first, fallback = urlopen.call_args_list
            self.assertEqual(first.kwargs["timeout"], 8)
            self.assertEqual(fallback.kwargs["timeout"], 8)
            self.assertEqual(json.loads(first.args[0].data)["channel"], "C_CONFIG")
            self.assertEqual(json.loads(fallback.args[0].data)["channel"], "C_ERRORS")
            self.assertEqual(first.args[0].get_header("Authorization"), "Bearer fixture-native-file-token")
            self.assertIn("re-routed to fallback", output.getvalue())

    def test_real_native_transport_timeout_is_bounded_and_failure_is_loud(self):
        native = types.ModuleType("fixture_native_alerting")
        exec(compile((ROOT / "exitmgr/alerting.py").read_text(), "fixture_native_alerting", "exec"), native.__dict__)
        native._cache = {"error_channel": "C_ERRORS"}
        self.package.alerting = native
        with (patch.dict(os.environ, {"SLACK_BOT_TOKEN": "fixture-only"}), patch.object(
                native.urllib.request, "urlopen", side_effect=TimeoutError("fixture timeout")) as urlopen,
                redirect_stderr(io.StringIO())):
            self.assertIn("UNDELIVERED", self.send())
        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == 8 for call in urlopen.call_args_list))

    def test_only_blind_price_alert_is_regular_session_gated(self):
        block = next(node for node in ast.walk(CLASS) if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Name) and node.test.id == "blind_positions")
        code = compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), str(SOURCE), "exec")
        calendar = types.ModuleType("exitmgr.trader")
        manager = types.SimpleNamespace(_post_stops_withheld_alert=Mock())
        for opened in (False, True):
            calendar._market_open = lambda: opened
            with patch.dict(sys.modules, {"exitmgr.trader": calendar}), redirect_stdout(io.StringIO()):
                exec(code, {"self": manager, "blind_positions": [("TEST", 12, "unpriceable")]})
            self.assertEqual(manager._post_stops_withheld_alert.call_count, int(opened))


class AsyncDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_condition_coalesces_changing_age_then_retries_failed_delivery(self):
        release = threading.Event()
        calls = []
        def post(body, *args, **kwargs):
            calls.append(body)
            release.wait(2)
            return len(calls) > 1
        package = types.ModuleType("exitmgr")
        package.alerting = types.SimpleNamespace(post=post,
            alerts_channel=lambda: "C_ALERTS", error_channel=lambda: "C_ERRORS")
        manager = types.SimpleNamespace(config=types.SimpleNamespace(alerts_channel="C_CONFIG"))
        with patch.dict(sys.modules, {"exitmgr": package}), redirect_stdout(io.StringIO()):
            try:
                started = time.monotonic()
                for age in range(20):
                    NAMESPACE["_post_unthrottled_alert"](manager, f"age={age}", "intent",
                        condition_key=(1, 189, 29, "exact-ref"), repeat_seconds=900)
                await asyncio.sleep(0.04)
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertEqual(len(manager._active_risk_alert_tasks), 1)
                self.assertEqual(len(calls), 1)
                release.set()
                await asyncio.gather(*list(manager._active_risk_alert_tasks.values()))
                await asyncio.sleep(0)
                self.assertEqual(manager._protective_notice_delivered_at, {})
                NAMESPACE["_post_unthrottled_alert"](manager, "retry", "intent",
                    condition_key=(1, 189, 29, "exact-ref"), repeat_seconds=900)
                await asyncio.gather(*list(manager._active_risk_alert_tasks.values()))
                await asyncio.sleep(0)
                self.assertEqual(len(calls), 2)
                NAMESPACE["_post_unthrottled_alert"](manager, "repeat", "intent",
                    condition_key=(1, 189, 29, "exact-ref"), repeat_seconds=900)
                self.assertEqual(len(manager._active_risk_alert_tasks), 0)
            finally:
                release.set()
                await asyncio.gather(*list(getattr(manager, "_active_risk_alert_tasks", {}).values()),
                                     return_exceptions=True)
                await asyncio.sleep(0)

    async def test_slow_transport_does_not_block_clock_and_repeated_keys_are_bounded(self):
        release = threading.Event()
        calls = []
        def slow_post(*args, **kwargs):
            calls.append(args[0])
            release.wait(2)
            return False
        native = types.SimpleNamespace(post=slow_post, alerts_channel=lambda: "C_ALERTS", error_channel=lambda: "C_ERRORS")
        package = types.ModuleType("exitmgr"); package.alerting = native
        manager = types.SimpleNamespace(config=types.SimpleNamespace(alerts_channel="C_CONFIG"))
        with patch.dict(sys.modules, {"exitmgr": package}), redirect_stdout(io.StringIO()):
            try:
                start = time.monotonic()
                for _ in range(20):
                    NAMESPACE["_post_unthrottled_alert"](manager, "blind", "stops-withheld")
                NAMESPACE["_post_unthrottled_alert"](manager, "duplicate", "duplicate-resting-close")
                await asyncio.sleep(0.04)
                self.assertLess(time.monotonic()-start, 0.3)
                self.assertEqual(len(manager._active_risk_alert_tasks), 2)
                self.assertEqual(sorted(calls), ["blind", "duplicate"])
                release.set()
                await asyncio.gather(*list(manager._active_risk_alert_tasks.values()))
                await asyncio.sleep(0)
                self.assertEqual(manager._active_risk_alert_tasks, {})
            finally:
                release.set()
                await asyncio.gather(*list(getattr(manager, "_active_risk_alert_tasks", {}).values()), return_exceptions=True)
                await asyncio.sleep(0)

    async def test_distinct_stop_reasons_survive_while_identical_body_coalesces(self):
        release = threading.Event()
        calls = []
        def slow_post(body, *args, **kwargs):
            calls.append(body)
            release.wait(2)
            return True
        package = types.ModuleType("exitmgr")
        package.alerting = types.SimpleNamespace(post=slow_post, alerts_channel=lambda: "C_ALERTS", error_channel=lambda: "C_ERRORS")
        manager = types.SimpleNamespace(config=types.SimpleNamespace(alerts_channel="C_CONFIG"))
        with patch.dict(sys.modules, {"exitmgr": package}):
            try:
                for body in ("blind positions", "stop order withheld", "entry gate withheld", "blind positions"):
                    NAMESPACE["_post_unthrottled_alert"](manager, body, "stops-withheld")
                self.assertEqual(len(manager._active_risk_alert_tasks), 3)
                release.set()
                await asyncio.gather(*list(manager._active_risk_alert_tasks.values()))
                await asyncio.sleep(0)
                self.assertEqual(sorted(calls), ["blind positions", "entry gate withheld", "stop order withheld"])
                self.assertEqual(manager._active_risk_alert_tasks, {})
            finally:
                release.set()
                await asyncio.gather(*list(getattr(manager, "_active_risk_alert_tasks", {}).values()), return_exceptions=True)
                await asyncio.sleep(0)

    async def test_distinct_pending_deliveries_are_capped_and_error_cleanup_runs(self):
        release = threading.Event()
        def failing_post(*args, **kwargs):
            release.wait(2)
            raise TimeoutError("slow fixture")
        package = types.ModuleType("exitmgr")
        package.alerting = types.SimpleNamespace(post=failing_post, alerts_channel=lambda: "C_ALERTS", error_channel=lambda: "C_ERRORS")
        manager = types.SimpleNamespace(config=types.SimpleNamespace(alerts_channel="C_CONFIG"))
        with patch.dict(sys.modules, {"exitmgr": package}), redirect_stdout(io.StringIO()) as output:
            try:
                for number in range(8):
                    NAMESPACE["_post_unthrottled_alert"](manager, "alert", str(number))
                self.assertEqual(len(manager._active_risk_alert_tasks), 4)
                self.assertIn("four active-risk deliveries pending", output.getvalue())
                release.set()
                await asyncio.gather(*list(manager._active_risk_alert_tasks.values()))
                await asyncio.sleep(0)
                self.assertEqual(manager._active_risk_alert_tasks, {})
            finally:
                release.set()
                await asyncio.gather(*list(getattr(manager, "_active_risk_alert_tasks", {}).values()), return_exceptions=True)
                await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
