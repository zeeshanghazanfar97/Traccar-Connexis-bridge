import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from adapter.__main__ import healthy, main, write_health
from adapter.bridge import (
    Bridge, BridgeError, ConnexisClient, HttpClient, RequestLimiter, StateStore,
    StopRequested, gps_timestamp, position_params,
)
from adapter.config import Config


def sample(**updates):
    value = {
        "plateNo": "APL-04",
        "gpsTime": "2026-11-05 08:19:09",
        "latitude": "24.8607",
        "longitude": "67.0011",
        "speed": "18.52",
        "io": [
            {"ioName": "IGNITION", "ioValue": "1"},
            {"ioName": "MAIN POWER", "ioValue": "12.8"},
            {"ioName": "GSM SIGNAL", "ioValue": 4},
        ],
    }
    value.update(updates)
    return value


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(base_url="https://provider.example", api_token="private-token")

    def test_naive_and_aware_times_represent_same_instant(self):
        values = ["2026-11-05 08:19:09", "2026-11-05T08:19:09+05:00", "2026-11-05T03:19:09Z"]
        expected = int(datetime(2026, 11, 5, 3, 19, 9, tzinfo=timezone.utc).timestamp() * 1000)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(gps_timestamp(value, self.config.gps_timezone), expected)

    def test_fractional_seconds_preserved(self):
        base = gps_timestamp("2026-11-05T03:19:09Z", timezone.utc)
        self.assertEqual(gps_timestamp("2026-11-05T03:19:09.123Z", timezone.utc), base + 123)

    def test_invalid_time_rejected(self):
        for value in [None, "2026-11-05", "bad", "1960-01-01T00:00:00Z"]:
            with self.subTest(value=value), self.assertRaises(BridgeError):
                gps_timestamp(value, timezone.utc)

    def test_payload_mapping(self):
        params = position_params(sample(), self.config)
        self.assertEqual(params["id"], "APL-04")
        self.assertEqual(params["lat"], 24.8607)
        self.assertAlmostEqual(params["speed"], 10)
        self.assertEqual(params["ignition"], "true")
        self.assertEqual(params["power"], 12.8)
        self.assertEqual(params["gsmSignal"], 4)

    def test_each_speed_unit(self):
        for unit, value in [("kmh", 18.52), ("knots", 10), ("mph", 11.50779448), ("mps", 5.14444444)]:
            with self.subTest(unit=unit):
                self.assertAlmostEqual(position_params(sample(speed=value), replace(self.config, speed_unit=unit))["speed"], 10, places=6)

    def test_ignition_boolean_numeric_and_text_forms(self):
        for value, expected in [(1, "true"), ("1", "true"), (True, "true"), ("on", "true"),
                                (0, "false"), ("0", "false"), (False, "false"), ("false", "false")]:
            with self.subTest(value=value):
                params = position_params(sample(io=[{"ioName": "IGNITION", "ioValue": value}]), self.config)
                self.assertEqual(params["ignition"], expected)

    def test_bad_coordinates_speed_and_io_are_rejected(self):
        for changes in [{"latitude": None}, {"latitude": 91}, {"longitude": -181},
                        {"latitude": "NaN"}, {"speed": "inf"}, {"speed": -1},
                        {"speed": False}, {"plateNo": ""}, {"io": "invalid"}]:
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                position_params(sample(**changes), self.config)

    def test_zero_coordinates_are_valid_and_missing_speed_defaults_to_zero(self):
        params = position_params(sample(latitude=0, longitude=0, speed=None, io=None), self.config)
        self.assertEqual((params["lat"], params["lon"], params["speed"]), (0, 0, 0))
        self.assertNotIn("ignition", params)


class ConfigurationTests(unittest.TestCase):
    def env(self, **changes):
        result = {"CONNEXIS_BASE_URL": "https://provider.example", "CONNEXIS_API_TOKEN": "Token $exact#value"}
        result.update(changes)
        return result

    def test_token_is_unchanged_and_excluded_from_repr(self):
        config = Config.from_env(self.env())
        self.assertEqual(config.api_token, "Token $exact#value")
        self.assertNotIn(config.api_token, repr(config))

    def test_plates_and_offset(self):
        config = Config.from_env(self.env(CONNEXIS_PLATES=" APL-04, B-2, APL-04 ", GPS_UTC_OFFSET="-03:30"))
        self.assertEqual(config.plates, ("APL-04", "B-2"))
        self.assertEqual(config.gps_timezone.utcoffset(None).total_seconds(), -12600)

    def test_request_limit_defaults_to_three_and_can_be_overridden(self):
        self.assertEqual(Config.from_env(self.env()).requests_per_minute, 3)
        self.assertEqual(Config.from_env(self.env(CONNEXIS_REQUESTS_PER_MINUTE="12")).requests_per_minute, 12)

    def test_request_limit_requires_a_positive_integer(self):
        for value in ["0", "-1", "3.5", "nan", "inf", "", "false"]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "CONNEXIS_REQUESTS_PER_MINUTE"):
                Config.from_env(self.env(CONNEXIS_REQUESTS_PER_MINUTE=value))

    def test_invalid_configuration_fails_at_startup(self):
        for changes in [{"POLL_INTERVAL": "0"}, {"REQUEST_TIMEOUT": "nan"}, {"GPS_UTC_OFFSET": "+05:99"},
                        {"VERIFY_TLS": "maybe"}, {"SPEED_UNIT": "kph"}, {"CONNEXIS_API_TOKEN": "bad\nvalue"},
                        {"CONNEXIS_BASE_URL": "https://user:password@example.com"},
                        {"CONNEXIS_ASSETS_PATH": "//other.example"}, {"MAX_BACKOFF": "1"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Config.from_env(self.env(**changes))


class StartupDiagnosticsTests(unittest.TestCase):
    def test_state_failure_reports_operation_path_and_original_error(self):
        config = Config(base_url="https://provider.example", api_token="private-token")
        errors = [sqlite3.OperationalError("unable to open database file"),
                  PermissionError(13, "Permission denied", "/data/state.sqlite3")]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch("sys.argv", ["adapter", "--once"]), \
                    patch("adapter.__main__.Config.from_env", return_value=config), \
                    patch("adapter.__main__.signal.signal"), \
                    patch("adapter.__main__.StateStore", side_effect=error), \
                    self.assertLogs("adapter", level="ERROR") as logs:
                self.assertEqual(main(), 1)
            message = "\n".join(logs.output)
            self.assertIn("opening state database /data/state.sqlite3", message)
            self.assertIn(type(error).__name__, message)
            self.assertIn(str(error), message)
            self.assertNotIn(config.api_token, message)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.state = StateStore(self.path)
        self.addCleanup(lambda: self.state.close())
        self.config = Config(base_url="https://provider.example", api_token="secret", state_path=self.path)
        self.bridge = Bridge(self.config, self.state)
        self.bridge.provider = Mock()
        self.bridge.provider.assets.return_value = ["APL-04"]
        self.bridge.provider.positions.return_value = [sample()]
        self.bridge.receiver = Mock()

    def test_duplicate_state_survives_restart_and_old_fixes_are_skipped(self):
        self.assertTrue(self.bridge.poll())
        self.state.close()
        self.state = StateStore(self.path)
        self.bridge.state = self.state
        self.assertTrue(self.bridge.poll())
        self.bridge.provider.positions.return_value = [sample(gpsTime="2026-11-05 08:19:08")]
        self.assertTrue(self.bridge.poll())
        self.assertEqual(self.bridge.receiver.request.call_count, 1)

    def test_failed_delivery_is_retried_without_advancing_state(self):
        self.bridge.receiver.request.side_effect = [BridgeError("HTTP 400"), b"OK"]
        with self.assertLogs(level="ERROR"):
            self.assertFalse(self.bridge.poll())
        self.assertEqual(self.state.last_sent("APL-04"), 0)
        self.assertTrue(self.bridge.poll())
        self.assertGreater(self.state.last_sent("APL-04"), 0)
        self.assertEqual(self.bridge.receiver.request.call_count, 2)

    def test_failed_earlier_fix_blocks_only_that_vehicle(self):
        self.bridge.provider.assets.return_value = ["APL-04", "B-2"]
        self.bridge.provider.positions.return_value = [sample(gpsTime="2026-11-05 08:20:00"), sample(), sample(plateNo="B-2")]
        self.bridge.receiver.request.side_effect = [BridgeError("HTTP 500"), b"OK"]
        with self.assertLogs(level="ERROR"):
            self.assertFalse(self.bridge.poll())
        self.assertEqual(self.state.last_sent("APL-04"), 0)
        self.assertGreater(self.state.last_sent("B-2"), 0)
        self.assertEqual(self.bridge.receiver.request.call_count, 2)

    def test_malformed_vehicle_does_not_block_valid_vehicle(self):
        self.bridge.provider.positions.return_value = [None, sample(latitude="bad"), sample()]
        with self.assertLogs(level="ERROR"):
            self.assertFalse(self.bridge.poll())
        self.bridge.receiver.request.assert_called_once()

    def test_dry_run_does_not_forward_or_advance_state(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(self.bridge.poll(dry_run=True))
        self.assertEqual(json.loads(output.getvalue())["id"], "APL-04")
        self.bridge.receiver.request.assert_not_called()
        self.assertEqual(self.state.last_sent("APL-04"), 0)

    def test_explicit_plates_skip_discovery_and_filter_extra_positions(self):
        self.bridge.config = replace(self.config, plates=("APL-04",))
        self.bridge.provider.positions.return_value = [sample(), sample(plateNo="OTHER")]
        self.assertTrue(self.bridge.poll())
        self.bridge.provider.assets.assert_not_called()
        self.bridge.provider.positions.assert_called_once_with(["APL-04"])
        self.bridge.receiver.request.assert_called_once()

    def test_multiple_fixes_are_forwarded_in_time_order(self):
        self.bridge.provider.positions.return_value = [sample(gpsTime="2026-11-05 08:20:00"), sample()]
        self.assertTrue(self.bridge.poll())
        timestamps = [int(parse_qs(call.kwargs["data"].decode())["timestamp"][0]) for call in self.bridge.receiver.request.call_args_list]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(len(timestamps), 2)

    def test_provider_schema_errors_are_safe(self):
        provider = ConnexisClient(self.config, self.state)
        provider.http = Mock()
        for body in [b"not json secret", b'{"status":401,"error":"secret"}', b'{"status":200,"data":{}}']:
            provider.http.request.return_value = body
            with self.assertRaises(BridgeError) as caught:
                provider.assets()
            self.assertNotIn("secret", str(caught.exception))

    def test_assets_accept_strings_and_objects(self):
        provider = ConnexisClient(self.config, self.state)
        provider.http = Mock()
        provider.http.request.return_value = b'{"status":200,"data":["APL-04",{"plateNo":"B-2"},"APL-04"]}'
        self.assertEqual(provider.assets(), ["APL-04", "B-2"])

    def test_health_requires_recent_success(self):
        path = Path(self.directory.name) / "health"
        self.assertFalse(healthy(path, 10))
        write_health(path)
        self.assertTrue(healthy(path, 10))
        path.write_text(str(time.time() - 30))
        self.assertFalse(healthy(path, 10))
        path.write_text("invalid")
        self.assertFalse(healthy(path, 10))


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def time(self):
        return self.now

    def wait(self, delay):
        self.waits.append(delay)
        self.now += delay
        return False


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.sqlite3"
        self.state = StateStore(self.path)
        self.addCleanup(lambda: self.state.close())
        self.clock = FakeClock()
        self.limiter = RequestLimiter(self.state, 3, clock=self.clock.time, wait=self.clock.wait)

    def test_only_three_attempts_fit_in_each_rolling_minute(self):
        attempts = []
        for _ in range(8):
            self.limiter.acquire()
            attempts.append(self.clock.now)
            self.clock.now += 15  # Even a short poll interval cannot bypass the cap.
        self.assertEqual(attempts, [1000, 1015, 1030, 1060, 1075, 1090, 1120, 1135])
        for now in attempts:
            self.assertLessEqual(sum(now - 60 < attempt <= now for attempt in attempts), 3)

    def test_discovery_locations_and_failed_requests_share_quota(self):
        config = Config(base_url="https://provider.example", api_token="secret")
        client = ConnexisClient(config, self.state)
        client.limiter = self.limiter
        client.http = Mock()
        attempts = []

        def send(*args, **kwargs):
            attempts.append(self.clock.now)
            if len(attempts) == 3:
                raise BridgeError("Network failure")
            return b'{"status":200,"data":["APL-04"]}'

        client.http.request.side_effect = send
        client.assets()
        client.positions(["APL-04"])
        with self.assertRaises(BridgeError):
            client.assets()
        client.positions(["APL-04"])
        self.assertEqual(attempts, [1000, 1000, 1000, 1060])

    def test_quota_survives_restart(self):
        for _ in range(3):
            self.limiter.acquire()
        self.state.close()
        self.state = StateStore(self.path)
        self.clock.now += 10
        restarted = RequestLimiter(self.state, 3, clock=self.clock.time, wait=self.clock.wait)
        restarted.acquire()
        self.assertEqual(self.clock.waits, [50])

    def test_concurrent_connections_cannot_overbook_quota(self):
        barrier = threading.Barrier(6)

        def reserve(_):
            state = StateStore(self.path)
            try:
                barrier.wait(timeout=5)
                return state.reserve_api_request(3, self.clock.now)
            finally:
                state.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            delays = list(pool.map(reserve, range(6)))
        self.assertEqual(sorted(delays), [0, 0, 0, 60, 60, 60])

    def test_lowering_limit_waits_until_enough_previous_requests_expire(self):
        for _ in range(3):
            self.limiter.acquire()
            self.clock.now += 10
        lower = RequestLimiter(self.state, 1, clock=self.clock.time, wait=self.clock.wait)
        lower.acquire()
        self.assertEqual(self.clock.now, 1080)
        self.assertEqual(self.clock.waits, [50])

    def test_higher_configured_limit_is_respected(self):
        limiter = RequestLimiter(self.state, 5, clock=self.clock.time, wait=self.clock.wait)
        for _ in range(5):
            limiter.acquire()
        self.assertEqual(self.clock.waits, [])
        limiter.acquire()
        self.assertEqual(self.clock.waits, [60])

    def test_idle_time_does_not_accumulate_extra_request_credits(self):
        self.limiter.acquire()
        self.clock.now += 600
        for _ in range(3):
            self.limiter.acquire()
        self.limiter.acquire()
        self.assertEqual(self.clock.waits, [60])
        count = self.state.connection.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0]
        self.assertEqual(count, 1)

    def test_shutdown_interrupts_wait_without_an_extra_request(self):
        for _ in range(3):
            self.limiter.acquire()
        self.limiter.wait = Mock(return_value=True)
        with self.assertRaises(StopRequested):
            self.limiter.acquire()
        self.limiter.wait.assert_called_once_with(60)
        count = self.state.connection.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0]
        self.assertEqual(count, 3)

    def test_stopped_client_does_not_reserve_a_request(self):
        self.limiter.stop_event.set()
        with self.assertRaises(StopRequested):
            self.limiter.acquire()
        self.assertEqual(self.state.connection.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0], 0)


class HttpIntegrationTests(unittest.TestCase):
    def test_real_http_poll_mapping_ack_and_redirect_rejection(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization"), None))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/leaked-token")
                    self.end_headers()
                else:
                    self.send({"status": 200, "data": ["APL-04"]})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((self.path, self.headers.get("Authorization"), body))
                self.send({"status": 200, "data": [sample()]} if self.path == "/api/v1/current_location" else {})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                url = f"http://127.0.0.1:{server.server_port}"
                config = Config(base_url=url, api_token="Token exact-value", traccar_url=url + "/receiver", requests_per_minute=100)
                state = StateStore(Path(directory) / "state.sqlite3")
                try:
                    bridge = Bridge(config, state)
                    self.assertTrue(bridge.poll())
                    self.assertTrue(bridge.poll())
                    self.assertEqual(requests[0][:2], ("/api/v1/assets/", "Token exact-value"))
                    self.assertEqual(json.loads(requests[1][2]), {"plateNo": ["APL-04"]})
                    received = [request for request in requests if request[0] == "/receiver/"]
                    self.assertEqual(len(received), 1)
                    self.assertIsNone(received[0][1])
                    self.assertEqual(parse_qs(received[0][2].decode())["ignition"], ["true"])
                    with self.assertRaisesRegex(BridgeError, "HTTP 302"):
                        HttpClient(2).request(url + "/redirect", headers={"Authorization": "secret"})
                    self.assertFalse(any(request[0] == "/leaked-token" for request in requests))
                finally:
                    state.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
