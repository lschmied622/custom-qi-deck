
# wf_logging.py
# Zentrales Logging-Modul für die Wall-Following- und Ladezustandsmaschine.
# Nutzt Python logging + Rolling File Handler sowie optionale CSV-Protokolle.

from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Optional, Any, Callable, Dict
import csv
import time
import os
import uuid

# ---------- Konfiguration ----------

@dataclass
class LogConfig:
    name: str = "wall_following"
    level: int = logging.INFO
    log_file: str = "wall_following.log"
    events_csv: str = "wall_following_events.csv"
    status_csv: str = "wall_following_status.csv"
    max_bytes: int = 2_000_000
    backup_count: int = 4
    console: bool = True
    to_file: bool = True

_run_id: str = None
_logger: Optional[logging.Logger] = None
_cfg: LogConfig = LogConfig()

def start_new_session(cfg: Optional[LogConfig] = None) -> str:
    """Initialisiert eine neue Logging-Session und liefert eine Run-ID."""
    global _run_id, _cfg
    if cfg is not None:
        _set_cfg(cfg)
    _run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    logger = get_logger()
    logger.info("SESSION START | run_id=%s", _run_id)
    # CSV-Header (falls nicht vorhanden) vorbereiten
    _ensure_csv_headers()
    return _run_id

def _set_cfg(cfg: LogConfig) -> None:
    global _cfg
    _cfg = cfg

def get_logger() -> logging.Logger:
    """Gibt den globalen Logger zurück (lazy init)."""
    global _logger, _cfg
    if _logger is not None:
        return _logger

    # Level via ENV überschreiben (optional)
    env_level = os.getenv("WF_LOG_LEVEL", "").upper()
    level = getattr(logging, env_level, _cfg.level) if env_level else _cfg.level

    logger = logging.getLogger(_cfg.name)
    logger.setLevel(level)
    logger.propagate = False  # keine Doppel-Logs

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S")

    if _cfg.console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    if _cfg.to_file:
        fh = RotatingFileHandler(_cfg.log_file, maxBytes=_cfg.max_bytes, backupCount=_cfg.backup_count, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    _logger = logger
    return _logger

def _ensure_csv_headers() -> None:
    # Events
    if _cfg.to_file and not os.path.exists(_cfg.events_csv):
        with open(_cfg.events_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts","run_id","type","prev_state","new_state","reason","details"])  # details als JSON-ähnlicher String
    # Status
    if _cfg.to_file and not os.path.exists(_cfg.status_csv):
        with open(_cfg.status_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts","run_id","state","front_m","side_m","battery_low","dt_in_state_s"])  # Minimal-Status

def _now_str() -> str:
    return time.strftime("%H:%M:%S", time.localtime())

def _csv_write(path: str, row: list[Any]) -> None:
    if not _cfg.to_file:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)

# ---------- Öffentliche API ----------

def log_state_change(prev_state: Any, new_state: Any, reason: str = "", **details: Any) -> None:
    """Loggt Zustandswechsel (Konsole/Datei) und schreibt in events_csv."""
    logger = get_logger()
    extras = " | ".join(f"{k}={v}" for k, v in details.items()) if details else ""
    logger.info("FSM: %s -> %s | %s%s", getattr(prev_state, "name", prev_state), getattr(new_state, "name", new_state), reason, (" | " + extras) if extras else "")
    _csv_write(_cfg.events_csv, [_now_str(), _run_id, "STATE_CHANGE", getattr(prev_state, "name", prev_state), getattr(new_state, "name", new_state), reason, extras])

def log_event(kind: str, msg: str, **details: Any) -> None:
    """Freie Ereignisse (z. B. Trigger, Safety-Stop, Sensorfehler)."""
    logger = get_logger()
    extras = " | ".join(f"{k}={v}" for k, v in details.items()) if details else ""
    logger.info("%s: %s%s", kind.upper(), msg, (" | " + extras) if extras else "")
    _csv_write(_cfg.events_csv, [_now_str(), _run_id, kind.upper(), "", "", msg, extras])

def log_status(state: Any, front_m: Optional[float], side_m: Optional[float], battery_low: Optional[bool], dt_in_state_s: Optional[float] = None) -> None:
    """Regelmäßiger Status-Log (Konsole/Datei) und CSV."""
    logger = get_logger()
    sname = getattr(state, "name", state)
    logger.info("STATUS: %s, Front=%.2f m, Side=%.2f m, BatteryLow=%s, dt=%.1f s",
                sname,
                float(front_m) if front_m is not None else float("nan"),
                float(side_m) if side_m is not None else float("nan"),
                battery_low,
                float(dt_in_state_s) if dt_in_state_s is not None else float("nan"))
    _csv_write(_cfg.status_csv, [_now_str(), _run_id, sname, front_m, side_m, battery_low, dt_in_state_s])

def instrument_wall_following(wf: Any, reason_provider: Optional[Callable[[Any, Any], str]] = None) -> None:
    """Monkey-Patch der Methode 'state_transition' des FSM-Objekts 'wf', um Zustandswechsel automatisch zu loggen.
    Erwartet, dass 'wf' Attribute hat: 'state', 'state_transition', 'state_change_time'.
    """
    if not hasattr(wf, "state_transition") or not hasattr(wf, "state"):
        return  # keine Instrumentierung möglich

    original = wf.state_transition

    def wrapped(new_state: Any, *args, **kwargs):
        prev = getattr(wf, "state", None)
        res = original(new_state, *args, **kwargs)
        # Nach erfolgreichem Übergang: loggen
        try:
            reason = reason_provider(prev, new_state) if reason_provider else ""
        except Exception:
            reason = ""
        try:
            wf.state_change_time = time.time()
        except Exception:
            pass
        log_state_change(prev, new_state, reason=reason)
        return res

    setattr(wf, "state_transition", wrapped)
