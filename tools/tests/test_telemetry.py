import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from test_init import init

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_receiver():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("lke_receiver", REPO_ROOT / "init" / "telemetry" / "receiver.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


receiver = load_receiver()


def kv(key, **value):
    return {"key": key, "value": value}


def metrics_payload(session):
    point = {
        "timeUnixNano": "1700000000000000000",
        "asDouble": 0.25,
        "attributes": [kv("session.id", stringValue=session), kv("model", stringValue="opus")],
    }
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [kv("service.name", stringValue="claude-code")]},
                "scopeMetrics": [
                    {"metrics": [{"name": "claude_code.cost.usage", "unit": "USD", "sum": {"dataPoints": [point]}}]}
                ],
            }
        ]
    }


def logs_payload(session):
    record = {
        "timeUnixNano": "1700000001000000000",
        "body": {"stringValue": "claude_code.tool_result"},
        "attributes": [
            kv("session.id", stringValue=session),
            kv("event.name", stringValue="claude_code.tool_result"),
            kv("duration_ms", intValue="42"),
        ],
    }
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}]}


def post(port, path, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", json.dumps(payload).encode(), {"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(request).status


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ReceiverTest(unittest.TestCase):
    def test_points_and_events_land_in_the_folder_of_their_session(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / ".logs"
            server = receiver.make_server(logs, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]
            try:
                self.assertEqual(post(port, "/v1/metrics", metrics_payload("s-1")), 200)
                self.assertEqual(post(port, "/v1/metrics", metrics_payload("s-2")), 200)
                self.assertEqual(post(port, "/v1/logs", logs_payload("s-1")), 200)
            finally:
                server.shutdown()
            (metric,) = lines(logs / "s-1" / "metrics.jsonl")
            (event,) = lines(logs / "s-1" / "events.jsonl")
            self.assertEqual(sorted(p.name for p in logs.iterdir()), ["s-1", "s-2"])
            self.assertFalse((logs / "s-2" / "events.jsonl").exists())
        self.assertEqual((metric["name"], metric["value"], metric["kind"]), ("claude_code.cost.usage", 0.25, "sum"))
        self.assertEqual(metric["attributes"]["model"], "opus")
        self.assertEqual(metric["attributes"]["service.name"], "claude-code")
        self.assertEqual(event["event"], "claude_code.tool_result")
        self.assertEqual(event["attributes"]["duration_ms"], 42)

    def test_a_record_without_a_session_is_kept_apart_and_a_hostile_id_stays_inside_the_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / ".logs"
            receiver.write_rows(logs, "metrics.jsonl", [("../../escape", {"a": 1}), (receiver.UNATTRIBUTED, {"a": 2})])
            names = sorted(p.name for p in logs.iterdir())
            self.assertFalse((Path(temp).parent / "escape").exists())
        self.assertEqual(names, ["_.._escape", "_unattributed"])

    def test_a_malformed_body_is_refused_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / ".logs"
            server = receiver.make_server(logs, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    post(server.server_address[1], "/v1/metrics", {"resourceMetrics": "nope"})
            finally:
                server.shutdown()
            self.assertEqual(caught.exception.code, 400)
            self.assertFalse(logs.exists())


class ProfileTelemetryTest(unittest.TestCase):
    def test_telemetry_is_on_unless_the_profile_turns_it_off(self):
        with tempfile.TemporaryDirectory() as temp:
            profiles = Path(temp)
            for name, extra in (("on", ""), ("off", "telemetry = false\n")):
                folder = profiles / name
                folder.mkdir()
                (folder / "profile.toml").write_text(f'skills = ["bmad"]\n{extra}', encoding="utf-8")
            on, off = (init.load_profile(name, profiles).telemetry for name in ("on", "off"))
        self.assertEqual((on, off), (True, False))


class InstallTelemetryTest(unittest.TestCase):
    def test_settings_are_merged_once_and_existing_values_win(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            settings = project / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"env": {"OTEL_METRIC_EXPORT_INTERVAL": "60000"}, "model": "x"}))
            first = init.install_telemetry(project, refresh=True)
            second = init.install_telemetry(project, refresh=False)
            merged = json.loads(settings.read_text())
            installed = sorted(p.name for p in (project / "_bmad" / "telemetry").iterdir())
        self.assertIn(".claude/settings.json", first)
        self.assertEqual(second, [])
        self.assertEqual(merged["model"], "x")
        self.assertEqual(merged["env"]["OTEL_METRIC_EXPORT_INTERVAL"], "60000")
        self.assertEqual(merged["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertEqual(len(merged["hooks"]["SessionStart"]), 1)
        self.assertEqual(installed, sorted(init.TELEMETRY_FILES))

    def test_a_join_does_not_overwrite_the_shared_files(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            init.install_telemetry(project, refresh=True)
            shared = project / "_bmad" / "telemetry" / "receiver.py"
            shared.write_text("changed")
            init.install_telemetry(project, refresh=False)
            self.assertEqual(shared.read_text(), "changed")


if __name__ == "__main__":
    unittest.main()
