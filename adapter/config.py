"""Environment configuration shared by the CLI and bridge."""

import math
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


def positive_number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number") from None
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


def positive_integer(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive integer") from None
    if str(result) != str(value).strip() or result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def parse_bool(value, name):
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "on"}:
        return True
    if normalized in {"false", "0", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, 1/0, or on/off")


def parse_offset(value):
    if not re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        raise ValueError("GPS_UTC_OFFSET must use +HH:MM or -HH:MM")
    hours, minutes = map(int, value[1:].split(":"))
    if hours > 23 or minutes > 59:
        raise ValueError("GPS_UTC_OFFSET is outside the valid range")
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(offset if value[0] == "+" else -offset)


def validate_url(value, name):
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"} and parts.hostname
            and not parts.username and not parts.password
            and not parts.query and not parts.fragment
        )
        parts.port  # Validate the port without including the URL in errors.
    except ValueError:
        valid = False
    if not valid or any(char.isspace() for char in value):
        raise ValueError(f"{name} must be an HTTP(S) URL without credentials, query, or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    base_url: str
    api_token: str = field(repr=False)
    traccar_url: str = "http://traccar:5055"
    assets_path: str = "/api/v1/assets/"
    locations_path: str = "/api/v1/current_location"
    plates: tuple = ()
    requests_per_minute: int = 3
    max_backoff: float = 300
    request_timeout: float = 20
    gps_timezone: timezone = timezone(timedelta(hours=5))
    speed_unit: str = "kmh"
    verify_tls: bool = True
    ca_file: str = ""
    state_path: Path = Path("/data/state.sqlite3")
    health_path: Path = Path("/tmp/adapter-health")
    health_max_age: float = 600

    @property
    def request_interval(self):
        return 60.0 / self.requests_per_minute

    @property
    def requests_per_poll(self):
        return 1 if self.plates else 2

    @property
    def poll_interval(self):
        # Location requests batch all selected vehicles in a single API call.
        return self.requests_per_poll * self.request_interval

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        token = env.get("CONNEXIS_API_TOKEN", "")
        if not token.strip() or "\r" in token or "\n" in token:
            raise ValueError("CONNEXIS_API_TOKEN must be a nonempty single-line header value")
        try:
            token.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError("CONNEXIS_API_TOKEN must contain only HTTP header characters") from None
        paths = {}
        for name, default in (("CONNEXIS_ASSETS_PATH", "/api/v1/assets/"),
                              ("CONNEXIS_LOCATIONS_PATH", "/api/v1/current_location")):
            path = env.get(name, default)
            if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path or any(c.isspace() for c in path):
                raise ValueError(f"{name} must be an absolute URL path without a query or fragment")
            paths[name] = path
        speed_unit = env.get("SPEED_UNIT", "kmh").lower()
        if speed_unit not in {"kmh", "mph", "mps", "knots"}:
            raise ValueError("SPEED_UNIT must be kmh, mph, mps, or knots")
        backoff = positive_number(env.get("MAX_BACKOFF", "300"), "MAX_BACKOFF")
        config = cls(
            base_url=validate_url(env.get("CONNEXIS_BASE_URL", ""), "CONNEXIS_BASE_URL"),
            api_token=token,
            traccar_url=validate_url(env.get("TRACCAR_URL", "http://traccar:5055"), "TRACCAR_URL"),
            assets_path=paths["CONNEXIS_ASSETS_PATH"],
            locations_path=paths["CONNEXIS_LOCATIONS_PATH"],
            plates=tuple(dict.fromkeys(p.strip() for p in env.get("CONNEXIS_PLATES", "").split(",") if p.strip())),
            requests_per_minute=positive_integer(
                env.get("CONNEXIS_REQUESTS_PER_MINUTE", "3"), "CONNEXIS_REQUESTS_PER_MINUTE"
            ),
            max_backoff=backoff,
            request_timeout=positive_number(env.get("REQUEST_TIMEOUT", "20"), "REQUEST_TIMEOUT"),
            gps_timezone=parse_offset(env.get("GPS_UTC_OFFSET", "+05:00")),
            speed_unit=speed_unit,
            verify_tls=parse_bool(env.get("VERIFY_TLS", "true"), "VERIFY_TLS"),
            ca_file=env.get("CONNEXIS_CA_FILE", ""),
            state_path=Path(env.get("STATE_PATH", "/data/state.sqlite3")),
            health_path=Path(env.get("HEALTH_PATH", "/tmp/adapter-health")),
            health_max_age=positive_number(env.get("HEALTH_MAX_AGE", "600"), "HEALTH_MAX_AGE"),
        )
        if backoff < config.poll_interval:
            raise ValueError(
                f"MAX_BACKOFF must be at least the calculated poll interval ({config.poll_interval:.1f}s)"
            )
        return config
