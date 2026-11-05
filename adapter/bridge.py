"""Translate the API contract in the supplied guide to OsmAnd HTTP posts."""

import json
import logging
import math
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import parse_bool

LOG = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class BridgeError(Exception):
    """An operational error whose message is safe to log."""


class StopRequested(Exception):
    """Stop a poll cleanly when shutdown interrupts a rate-limit wait."""


class RequestLimiter:
    """Share a rolling 60-second request quota through the adapter state database."""

    def __init__(self, state, limit, stop_event=None, *, clock=time.time, wait=None):
        self.state = state
        self.limit = limit
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.clock = clock
        self.wait = wait if wait is not None else self.stop_event.wait

    def acquire(self):
        while not self.stop_event.is_set():
            delay = self.state.reserve_api_request(self.limit, self.clock())
            if delay <= 0:
                return
            LOG.info("Connexis request limit reached; waiting %.1f seconds", delay)
            if self.wait(delay):
                break
        raise StopRequested


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the provider's Authorization header to another URL.
        return None


class HttpClient:
    def __init__(self, timeout, verify_tls=True, ca_file=""):
        context = ssl.create_default_context(cafile=ca_file or None)
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = build_opener(NoRedirects(), HTTPSHandler(context=context))
        self.timeout = timeout

    def request(self, url, *, data=None, headers=None):
        request = Request(url, data=data, headers=headers or {})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise BridgeError(f"HTTP {response.status}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise BridgeError("Response exceeded the 5 MiB limit")
                return body
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise BridgeError(f"HTTP {code}; check credentials, endpoint, or Traccar device identifier") from None
        except (URLError, OSError, socket.timeout, HTTPException):
            # URLs and provider response bodies can contain credentials or locations.
            raise BridgeError("Network, timeout, or TLS error; check connectivity and certificates") from None


class ConnexisClient:
    def __init__(self, config, state, stop_event=None):
        self.config = config
        self.http = HttpClient(config.request_timeout, config.verify_tls, config.ca_file)
        self.limiter = RequestLimiter(state, config.requests_per_minute, stop_event)

    def request(self, path, payload=None):
        headers = {"Authorization": self.config.api_token, "Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        # Reserve before sending: failures also consume the provider's quota.
        self.limiter.acquire()
        raw = self.http.request(self.config.base_url + path, data=data, headers=headers)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError("Connexis returned invalid JSON") from None
        if not isinstance(result, dict) or result.get("status") not in (200, "200"):
            raise BridgeError("Connexis response did not contain status 200")
        if not isinstance(result.get("data"), list):
            raise BridgeError("Connexis data must be a list; check the provider response contract")
        return result["data"]

    def assets(self):
        rows = self.request(self.config.assets_path)
        plates = []
        for row in rows:
            plate = row.get("plateNo") if isinstance(row, dict) else row
            if not isinstance(plate, str) or not plate.strip():
                raise BridgeError("An asset must be a plate string or an object with plateNo")
            plates.append(plate.strip())
        return list(dict.fromkeys(plates))

    def positions(self, plates):
        return self.request(self.config.locations_path, {"plateNo": plates})


def number(value, name, minimum=None, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BridgeError(f"{name} must be a finite number") from None
    if isinstance(value, bool) or not math.isfinite(result):
        raise BridgeError(f"{name} must be a finite number")
    if (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        raise BridgeError(f"{name} is outside the valid range")
    return result


def gps_timestamp(value, default_timezone):
    if not isinstance(value, str):
        raise BridgeError("gpsTime must be an ISO date and time")
    try:
        # Explicit provider offsets win. The configured offset only fills naive times.
        if "T" not in value and " " not in value:
            raise ValueError
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_timezone)
        timestamp = int(dt.timestamp() * 1000)
        if timestamp <= 0:
            raise ValueError
        return timestamp
    except (ValueError, OverflowError, OSError):
        raise BridgeError("gpsTime must be a valid date and time after the Unix epoch") from None


def position_params(position, config):
    if not isinstance(position, dict):
        raise BridgeError("Position must be an object")
    plate = position.get("plateNo")
    if not isinstance(plate, str) or not plate.strip():
        raise BridgeError("Position is missing plateNo")
    timestamp = gps_timestamp(position.get("gpsTime"), config.gps_timezone)
    speed_value = position.get("speed")
    speed = number(0 if speed_value is None or speed_value == "" else speed_value, "speed", 0)
    factor = {"kmh": 1 / 1.852, "mph": 0.8689762419, "mps": 1.9438444924, "knots": 1}
    params = {
        "id": plate.strip(),
        # Epoch milliseconds preserve subsecond fixes and are accepted by OsmAnd.
        "timestamp": timestamp,
        "lat": number(position.get("latitude"), "latitude", -90, 90),
        "lon": number(position.get("longitude"), "longitude", -180, 180),
        "speed": speed * factor[config.speed_unit],
        "valid": "true",
    }
    io_rows = position.get("io") or []
    if not isinstance(io_rows, list):
        raise BridgeError("io must be a list")
    for item in io_rows:
        if not isinstance(item, dict):
            raise BridgeError("Each io entry must be an object")
        name, value = item.get("ioName"), item.get("ioValue")
        if value is None:
            continue
        if name == "IGNITION":
            try:
                params["ignition"] = str(parse_bool(value, "IGNITION")).lower()
            except ValueError as exc:
                raise BridgeError(str(exc)) from None
        elif name == "MAIN POWER":
            params["power"] = number(value, "MAIN POWER", 0)
        elif name == "GSM SIGNAL":
            params["gsmSignal"] = number(value, "GSM SIGNAL")
    return params


class StateStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("CREATE TABLE IF NOT EXISTS last_sent (plate TEXT PRIMARY KEY, timestamp INTEGER NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS api_requests (requested_at REAL NOT NULL)")
        self.connection.commit()

    def reserve_api_request(self, limit, now):
        # A write lock makes checking and reserving atomic across processes sharing
        # this volume, including --once/--list-assets and a restarted service.
        # Wall-clock timestamps survive restarts; an in-memory monotonic clock cannot.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("DELETE FROM api_requests WHERE requested_at <= ?", (now - 60,))
            timestamps = self.connection.execute(
                "SELECT requested_at FROM api_requests ORDER BY requested_at"
            ).fetchall()
            if len(timestamps) >= limit:
                # Also handles reducing the configured limit while requests remain.
                return timestamps[len(timestamps) - limit][0] + 60 - now
            self.connection.execute("INSERT INTO api_requests (requested_at) VALUES (?)", (now,))
        return 0

    def last_sent(self, plate):
        row = self.connection.execute("SELECT timestamp FROM last_sent WHERE plate = ?", (plate,)).fetchone()
        return row[0] if row else 0

    def mark_sent(self, plate, timestamp):
        with self.connection:
            self.connection.execute(
                "INSERT INTO last_sent (plate, timestamp) VALUES (?, ?) "
                "ON CONFLICT(plate) DO UPDATE SET timestamp = MAX(last_sent.timestamp, excluded.timestamp)",
                (plate, timestamp),
            )

    def close(self):
        self.connection.close()


class Bridge:
    def __init__(self, config, state, stop_event=None):
        self.config = config
        self.state = state
        self.provider = ConnexisClient(config, state, stop_event)
        # Disabling Connexis TLS validation must not disable it for Traccar.
        self.receiver = HttpClient(config.request_timeout)

    def poll(self, dry_run=False):
        plates = list(self.config.plates) or self.provider.assets()
        if not plates:
            LOG.warning("Connexis returned no vehicles")
            return True
        positions = self.provider.positions(plates)
        if not positions:
            LOG.warning("Connexis returned no current positions for %d requested vehicles", len(plates))
        allowed = set(plates)
        pending = []
        failed = False
        for index, position in enumerate(positions):
            try:
                params = position_params(position, self.config)
                if params["id"] in allowed:
                    pending.append(params)
            except BridgeError as exc:
                LOG.error("Invalid position at response index %d: %s", index, exc)
                failed = True
        # If the provider returns several fixes, process oldest first per vehicle.
        failed_plates = set()
        for params in sorted(pending, key=lambda item: (item["id"], item["timestamp"])):
            plate, timestamp = params["id"], params["timestamp"]
            # Do not advance past a failed earlier fix for this vehicle.
            if plate in failed_plates:
                continue
            if timestamp <= self.state.last_sent(plate):
                continue
            if dry_run:
                print(json.dumps(params, sort_keys=True), flush=True)
                continue
            try:
                self.receiver.request(
                    self.config.traccar_url + "/",
                    data=urlencode(params).encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except BridgeError as exc:
                LOG.error("Traccar submission failed for %r: %s", plate, exc)
                failed_plates.add(plate)
                failed = True
                continue
            # Commit only after Traccar acknowledges. Disk errors must stop the process.
            self.state.mark_sent(plate, timestamp)
            LOG.info("Sent %r | GPS %s | speed %.2f knots", plate,
                     datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat(), params["speed"])
        return not failed
