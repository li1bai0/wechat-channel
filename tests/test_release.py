import contextlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

TEMP = tempfile.TemporaryDirectory()
os.environ["WECHAT_BRIDGE_DATA_DIR"] = TEMP.name
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wechat_bridge.py"
spec = importlib.util.spec_from_file_location("bridge", SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def tearDownModule():
    if b._file_logger:
        for handler in b._file_logger.handlers:
            handler.close()
    TEMP.cleanup()


class ReleaseTests(unittest.TestCase):
    def make_bridge(self):
        bridge = b.Bridge.__new__(b.Bridge)
        bridge.account = {"base_url": "https://example.invalid", "token": "test", "user_id": "owner"}
        bridge.state = {"boss_chat": "owner", "context_token": "test", "seen": [], "seen_fps": []}
        bridge.save_state = Mock()
        bridge._throttle_send = Mock()
        return bridge

    def test_version_and_unknown_command(self):
        self.assertIn("2.2.2", b.version_info())
        with patch.object(b.sys, "argv", ["bridge", "typo"]):
            self.assertEqual(b.main(), 2)

    def test_check_update_numeric_order(self):
        response = io.BytesIO(json.dumps([{"name": "v2.9.0"}, {"name": "v2.10.0"},
                                         {"name": "v99.0.0-rc1"}]).encode())
        with patch.object(b.urllib.request, "urlopen", return_value=response), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.check_update(), 0)
        self.assertIn("2.10.0", output.getvalue())

    def test_cli_non_unicode_console(self):
        result = subprocess.run([b.sys.executable, str(SCRIPT), "typo"],
                                env=dict(os.environ, PYTHONIOENCODING="ascii"),
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("未知命令", result.stdout.decode("utf-8"))

    def test_bound_sender_only(self):
        bridge = self.make_bridge()
        bridge.send_weixin = Mock()
        bridge.handle_update({"from_user_id": "stranger", "item_list": [
            {"type": 1, "text_item": {"text": "状态"}}]})
        bridge.send_weixin.assert_not_called()

    def test_send_response_states(self):
        for response, result, state in [({"ret": 0}, "ok", "api_accepted"),
                                        ({"message_id": "server-message"}, "ok", "api_accepted"),
                                        ({"message_id": "server-message", "ret": -2}, "error", "rejected"),
                                        ({"message_id": None}, "error", "rejected"),
                                        ({"ret": -2}, "error", "rejected"),
                                        ({"errcode": -14}, "expired", "expired"),
                                        ({}, "error", "rejected")]:
            with self.subTest(response=response):
                bridge = self.make_bridge()
                with patch.object(b, "_req", return_value=response):
                    self.assertEqual(bridge._post_message([], "owner", None), result)
                self.assertEqual(bridge.state["last_send"]["status"], state)

    def test_send_timeout_is_unknown(self):
        bridge = self.make_bridge()
        with patch.object(b, "_req", side_effect=TimeoutError):
            self.assertEqual(bridge._post_message([], "owner", None), "error")
        self.assertEqual(bridge.state["last_send"]["status"], "unknown")

    def test_failed_send_is_queued_not_success(self):
        bridge = self.make_bridge()
        with patch.object(bridge, "_send_message", return_value="error"), \
             patch.object(b, "_save_to_retry_queue") as retry, patch.object(b.time, "sleep"):
            self.assertFalse(bridge.send_weixin("hello"))
            retry.assert_called_once()

    def test_debug_off_and_log_rotation(self):
        with patch.dict(b.SETTINGS, {"debug_events": False}), contextlib.redirect_stdout(io.StringIO()) as stream:
            b.log("hidden-heartbeat", debug=True)
            self.assertEqual(stream.getvalue(), "")
        b.log("visible-state")
        self.assertNotIn("hidden-heartbeat", b.LOG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(b._file_logger.handlers[0].backupCount, 3)

    def test_dedup_old_format_and_expiry(self):
        bridge = self.make_bridge()
        bridge.backend = "codex"
        bridge.send_weixin = Mock()
        bridge.send_typing = Mock()
        bridge.state["seen_fps"] = ["legacy"]
        message = {"from_user_id": "owner", "message_type": 1,
                   "item_list": [{"type": 1, "text_item": {"text": "状态"}}]}
        with patch.object(b.time, "time", return_value=1000):
            bridge.handle_update(message)
            bridge.handle_update(message)
        self.assertEqual(bridge.send_weixin.call_count, 1)
        with patch.object(b.time, "time", return_value=1301):
            bridge.handle_update(message)
        self.assertEqual(bridge.send_weixin.call_count, 2)

    def test_independent_home_launch(self):
        home = Path(TEMP.name) / "home"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text("", encoding="utf-8")
        proc = Mock()
        proc.poll.return_value = None
        with patch.dict(b.SETTINGS, {"codex_home": str(home)}), \
             patch.object(b, "_port_listening", side_effect=[False, True]), \
             patch.object(b.subprocess, "Popen", return_value=proc) as launch, patch.object(b.time, "sleep"):
            self.assertTrue(b._ensure_app_server())
            self.assertEqual(launch.call_args.kwargs["env"]["CODEX_HOME"], str(home))

    def test_missing_home_does_not_fall_back(self):
        with patch.dict(b.SETTINGS, {"codex_home": str(Path(TEMP.name) / "missing")}), \
             patch.object(b, "_port_listening", return_value=False), patch.object(b.subprocess, "Popen") as launch:
            self.assertFalse(b._ensure_app_server())
            launch.assert_not_called()

    def test_rate_limit_rechecks_after_wake(self):
        bridge = self.make_bridge()
        bridge._send_lock = threading.Lock()
        bridge._send_ts = [0.0] * b.SEND_RATE_LIMIT
        with patch.object(b.time, "monotonic", side_effect=[1.0, 61.0]), patch.object(b.time, "sleep") as sleep:
            b.Bridge._throttle_send(bridge)
        self.assertEqual(bridge._send_ts, [61.0])
        sleep.assert_called_once()

    def test_four_level_router(self):
        self.assertEqual(b.classify_message("你好"), "casual")
        self.assertEqual(b.classify_message("这个设置是什么意思？"), "medium")
        self.assertEqual(b.classify_message("请你帮我检查并修复微信桥，可以吗？"), "complex")
        self.assertEqual(b.classify_message("请分析：" + "甲" * 1300), "long_task")

    def test_public_defaults_have_no_private_identity(self):
        self.assertEqual(b.BACKEND_IDENTITY["codex"], "你的 Codex 助手")
        self.assertEqual(b.BACKEND_IDENTITY["claude"], "你的 Claude 助手")
        self.assertIn("只服务扫码绑定的用户", b.PRIMER)
    def test_greeting_uses_low_effort_model_queue(self):
        bridge = self.make_bridge()
        bridge.backend = "codex"
        bridge._running_task = {"active": False}
        bridge.reply_q = b.deque()
        bridge._q_cond = threading.Condition()
        bridge.send_typing = Mock()
        bridge.handle_update({"from_user_id": "owner", "message_type": 1,
                              "item_list": [{"type": 1, "text_item": {"text": "你好"}}]})
        self.assertEqual(list(bridge.reply_q), ["你好"])
        bridge.send_typing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
