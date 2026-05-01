"""TPMS discovery probe.

Runs once at startup, fires a battery of candidate Mode 22 (UDS read-by-id)
and Mode 01 queries through the existing ELM327, and dumps raw responses
to a JSONL file under ~/log/ for offline analysis. No interpretation here.

Output:
  ~/log/tpms-probe-YYYYMMDD-HHMMSS-MAC.jsonl
  ~/log/tpms-probe.runs            (one JSON line per completed run)
  ~/log/tpms-probe.force           (touch to force a re-probe; deleted on use)

Safety: only Mode 01 and Mode 22 are permitted. Mode 22 is read-only on
all known ECUs. The whole probe is bounded by a wall-clock deadline and
swallows per-query exceptions so one bad PID can't kill the run.
"""

import json
import os
import os.path
import time
import traceback
from datetime import datetime

import obd


RUNS_PATH_NAME = "tpms-probe.runs"
FORCE_FLAG_NAME = "tpms-probe.force"
RUNS_THRESHOLD_PER_VIN = 50

CANDIDATES = [
    {"mode": "22", "pid": "0DA0", "desc": "candidate: Toyota TPMS LF pressure", "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA1", "desc": "candidate: Toyota TPMS RF pressure", "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA2", "desc": "candidate: Toyota TPMS LR pressure", "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA3", "desc": "candidate: Toyota TPMS RR pressure", "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA4", "desc": "candidate: Toyota TPMS spare/aux",   "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA5", "desc": "candidate: Toyota TPMS extended",    "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA6", "desc": "candidate: Toyota TPMS extended",    "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA7", "desc": "candidate: Toyota TPMS extended",    "tag": "toyota_tpms"},
    {"mode": "22", "pid": "0DA8", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DA9", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DAA", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DAB", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DAC", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DAD", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DAE", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "0DAF", "desc": "candidate: Toyota TPMS sweep",       "tag": "toyota_tpms_sweep"},
    {"mode": "22", "pid": "F40D", "desc": "candidate: TPMS rolling/sensor id",  "tag": "toyota_tpms"},
    {"mode": "22", "pid": "1100", "desc": "candidate: generic TPMS register",   "tag": "generic"},
    {"mode": "22", "pid": "1101", "desc": "candidate: generic TPMS register",   "tag": "generic"},
    {"mode": "22", "pid": "1102", "desc": "candidate: generic TPMS register",   "tag": "generic"},
    {"mode": "22", "pid": "1103", "desc": "candidate: generic TPMS register",   "tag": "generic"},
    {"mode": "01", "pid": "2D",   "desc": "candidate: Mode01 PID 0x2D",         "tag": "mode01_sweep"},
    {"mode": "01", "pid": "2E",   "desc": "candidate: Mode01 PID 0x2E",         "tag": "mode01_sweep"},
    {"mode": "01", "pid": "2F",   "desc": "candidate: Mode01 PID 0x2F",         "tag": "mode01_sweep"},
]

ALLOWED_MODES = {"01", "22"}


def _bt_mac_for_filename(mac):
    return mac.replace(":", "").upper()


def _is_shutdown_pending():
    return os.path.exists("/run/systemd/shutdown/scheduled")


def should_run_probe(log_dir, vin, force=False):
    """Decide whether to run the probe this trip.

    Run if force=True, if the force-flag file exists (consumes it), or if
    fewer than RUNS_THRESHOLD_PER_VIN completed runs are recorded for this VIN.
    """
    if force:
        return True

    force_path = os.path.join(log_dir, FORCE_FLAG_NAME)
    if os.path.exists(force_path):
        try:
            os.remove(force_path)
        except OSError:
            pass
        return True

    runs_path = os.path.join(log_dir, RUNS_PATH_NAME)
    if not os.path.exists(runs_path):
        return True

    count = 0
    try:
        with open(runs_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("vin") == vin:
                    count += 1
    except OSError:
        return True
    return count < RUNS_THRESHOLD_PER_VIN


def _build_command(mode, pid, desc):
    cmd_bytes = bytes.fromhex(mode + pid)
    return obd.OBDCommand(
        name=f"PROBE_{mode}{pid}",
        desc=desc,
        command=cmd_bytes,
        _bytes=0,
        decoder=lambda messages: messages,
        ecu=obd.ECU.ALL,
        fast=False,
    )


def _serialize_messages(response):
    out = []
    for m in (response.messages or []):
        data = getattr(m, "data", None) or b""
        ecu_addr = None
        for attr in ("tx_id", "ecu"):
            v = getattr(m, attr, None)
            if v is not None:
                try:
                    ecu_addr = hex(int(v))
                except (TypeError, ValueError):
                    ecu_addr = str(v)
                break
        try:
            raw = m.raw() if hasattr(m, "raw") else None
        except Exception:
            raw = None
        out.append({
            "ecu_addr": ecu_addr,
            "data_hex": " ".join(f"{b:02X}" for b in data),
            "raw": raw,
        })
    return out


def _is_negative_response(messages_serialized):
    for m in messages_serialized:
        data = m.get("data_hex", "")
        if data.startswith("7F"):
            return True
    return False


def _query_one(connection, candidate):
    mode = candidate["mode"]
    pid = candidate["pid"]
    assert mode in ALLOWED_MODES, f"refusing mode {mode}"
    cmd = _build_command(mode, pid, candidate["desc"])
    t0 = time.monotonic()
    error = None
    raw_messages = []
    is_null = True
    try:
        response = connection.query(cmd, force=True)
        is_null = response.is_null()
        raw_messages = _serialize_messages(response)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    duration_ms = round((time.monotonic() - t0) * 1000, 1)
    return {
        "type": "query",
        "ts": datetime.now().isoformat(),
        "mode": mode,
        "pid": pid,
        "desc": candidate["desc"],
        "tag": candidate["tag"],
        "raw_messages": raw_messages,
        "duration_ms": duration_ms,
        "is_null": is_null,
        "is_negative_response": _is_negative_response(raw_messages),
        "error": error,
        "parsed_or_null": None,
    }


def _run_at_ma(connection, fp, max_seconds=10, max_lines=5000):
    """Optional Phase 2: dump raw CAN traffic via ELM327 'AT MA' (monitor all).

    Reaches into python-obd's private serial port to break out of monitor
    mode without resetting the adapter. Best-effort; wrapped by caller.
    """
    interface = getattr(connection, "interface", None)
    if interface is None:
        return {"lines_captured": 0, "duration_sec": 0.0, "error": "no interface"}

    port = getattr(interface, "_ELM327__port", None)
    if port is None:
        return {"lines_captured": 0, "duration_sec": 0.0, "error": "no underlying port"}

    t0 = time.monotonic()
    deadline = t0 + max_seconds
    lines = 0
    error = None
    try:
        port.write(b"AT MA\r")
        port.flush()
        buf = b""
        while time.monotonic() < deadline and lines < max_lines:
            chunk = port.read(64) if hasattr(port, "read") else b""
            if not chunk:
                continue
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                text = line.decode("ascii", errors="replace").strip()
                if not text:
                    continue
                fp.write(json.dumps({
                    "type": "at_ma",
                    "ts": datetime.now().isoformat(),
                    "line": text,
                }) + "\n")
                lines += 1
                if lines >= max_lines:
                    break
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        try:
            port.write(b" ")
            port.flush()
            time.sleep(0.2)
            try:
                port.read(4096)
            except Exception:
                pass
        except Exception:
            pass
    return {
        "lines_captured": lines,
        "duration_sec": round(time.monotonic() - t0, 2),
        "error": error,
    }


def _record_run(log_dir, vin, hits):
    runs_path = os.path.join(log_dir, RUNS_PATH_NAME)
    try:
        with open(runs_path, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "vin": vin,
                "hits": hits,
            }) + "\n")
    except OSError:
        pass


def run_probe(connection, vin, bt_mac, log_dir, log_event_fn,
              max_seconds=30, enable_at_ma=False):
    """Run the discovery probe. Returns dict with attempts/hits/duration/output_path."""

    if connection.status() == obd.OBDStatus.NOT_CONNECTED:
        log_event_fn("tpms_probe_skipped", reason="not_connected", vin=vin)
        return {"attempts": 0, "hits": 0, "duration_sec": 0.0, "output_path": None}

    if _is_shutdown_pending():
        log_event_fn("tpms_probe_skipped", reason="shutdown_pending", vin=vin)
        return {"attempts": 0, "hits": 0, "duration_sec": 0.0, "output_path": None}

    mac_slug = _bt_mac_for_filename(bt_mac)
    output_path = os.path.join(
        log_dir, f"tpms-probe-{datetime.now():%Y%m%d-%H%M%S}-{mac_slug}.jsonl"
    )

    elm_version = ""
    try:
        v = connection.query(obd.commands.ELM_VERSION)
        if not v.is_null():
            elm_version = str(v.value)
    except Exception:
        pass

    attempts = 0
    hits = 0
    t0 = time.monotonic()
    deadline = t0 + max_seconds

    with open(output_path, "w") as fp:
        fp.write(json.dumps({
            "type": "header",
            "ts": datetime.now().isoformat(),
            "vin": vin,
            "bt_mac": bt_mac,
            "elm_version": elm_version,
            "candidates_total": len(CANDIDATES),
            "at_ma_enabled": bool(enable_at_ma),
            "max_seconds": max_seconds,
        }) + "\n")
        fp.flush()

        for i, candidate in enumerate(CANDIDATES):
            if time.monotonic() > deadline:
                break
            if i % 5 == 0 and _is_shutdown_pending():
                break
            row = _query_one(connection, candidate)
            attempts += 1
            if (not row["is_null"]
                    and not row["is_negative_response"]
                    and row["error"] is None):
                hits += 1
            fp.write(json.dumps(row) + "\n")
            fp.flush()

        if enable_at_ma and time.monotonic() < deadline:
            try:
                summary = _run_at_ma(connection, fp,
                                     max_seconds=min(10, int(deadline - time.monotonic())))
                log_event_fn("tpms_probe_at_ma_capture", **summary)
            except Exception:
                log_event_fn("tpms_probe_error",
                             phase="at_ma",
                             traceback=traceback.format_exc())

    duration_sec = round(time.monotonic() - t0, 2)
    _record_run(log_dir, vin, hits)
    return {
        "attempts": attempts,
        "hits": hits,
        "duration_sec": duration_sec,
        "output_path": output_path,
    }
