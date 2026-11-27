#!/usr/bin/env python3
"""
lanaudio.py - LAN-AUDIO: LAN audio rooms, with attitude
=======================================================

Everything playing on the host PC is streamed in sync to every member PC in
the room. Every PC runs the same agent, and any PC can host or listen.

v1.2 "stable stream"
--------------------
* One persistent TCP connection per member carries audio, control and clock
  sync. TCP retransmits anything the network drops, so nothing is lost.
* A deep, auto-growing playout buffer absorbs Wi-Fi stalls and retransmits.
* If the connection drops, the member keeps playing from its buffer and the
  host replays what was missed on reconnect, so short drops are inaudible.
* Default codec is lossless 24-bit PCM at the source's native sample rate
  (no host-side resampling). Float capture and float playback end to end.
* High-quality windowed-sinc interpolation for drift correction.
* Windows tuning: MMCSS "Pro Audio" threads, high process priority, 1 ms
  timers and power throttling disabled.

Install (on every PC, Windows, Python 3.9+)
-------------------------------------------
    pip install numpy rich PyAudioWPatch
    pip install opuslib            # optional, needs opus.dll on PATH

Host
----
    lanaudio serve                                   # terminal 1, keep it open
    lanaudio group create office                     # terminal 2 ...
    lanaudio group add office 192.168.1.20 --name Rahul    # prints Rahul's token
    lanaudio room start office

Member (one-time token setup, then join by group name any time)
---------------------------------------------------------------
    lanaudio serve
    lanaudio token add LA1.xxxxx...
    lanaudio groups
    lanaudio join office

Ports: TCP 47800 (everything), 127.0.0.1:47799 (local API).
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import functools
import hashlib
import hmac
import ipaddress
import json
import math
import os
import platform
import queue
import random
import secrets
import select
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

try:
    import numpy as np
except ImportError:
    sys.exit("Missing dependency. Run:  pip install numpy")

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.exit("Missing dependency. Run:  pip install rich")


VERSION = "1.2.0"
PEER_DEFAULT_PORT = 47800
DEFAULT_PORT = int(os.environ.get("LANAUDIO_PORT", PEER_DEFAULT_PORT))
CH = 2                            # Network channels
FRAME_MS = 20                     # One audio frame on the wire
RING_SECONDS = 6                  # Member-side buffer capacity
HISTORY_S = 3.0                   # Host keeps this much audio to replay after a reconnect
AUTO_START_MS = 500               # Starting latency in auto mode (covers typical Wi-Fi stalls)
AUTO_STEP_MS = 150
HEADROOM_LOW_MS = 70              # Auto mode raises latency below this headroom
MIN_LATENCY_MS, MAX_LATENCY_MS = 60, 1500
LAT_SLEW = 0.002                  # Latency changes glide at 0.2 % speed (inaudible)
HB_S = 1.0
PEER_TIMEOUT_S = 5.0
RECONNECT_MAX_S = 5.0
DEVICE_RECHECK_S = 10.0
APPROVAL_TIMEOUT_S = 90
OUT_BUFFER_S = 0.04               # Output callback size (bigger = fewer underruns)
HARD_RESYNC_S = 0.030             # Jump instead of slewing above this error
MAX_SLEW = 0.003                  # Max playback-rate nudge (0.3 %)
LOOP_TAU = 5.0                    # Drift loop time constant (s): slow = no wow/flutter
ERR_ALPHA = 0.02                  # Per-callback smoothing of the timing error
BLOCKED_KEYWORDS = ("hands-free", "handsfree", "ag audio")
TOKEN_PREFIX = "LA1."
CODECS = ("pcm24", "pcm16", "opus")

F_JSON, F_AUDIO, F_PING, F_PONG = 1, 2, 3, 4
FH = struct.Struct("!BI")                 # frame type, payload length
AUDIO_HDR = struct.Struct("!IQqH")        # seq, pos, ts_us, frames
PING = struct.Struct("!d")
PONG = struct.Struct("!ddd")


# --------------------------------------------------------------------------- #
# Config (groups + their member tokens, my tokens, devices, identity)
# --------------------------------------------------------------------------- #
_cfg_lock = threading.RLock()


def home_dir() -> str:
    d = os.environ.get("LANAUDIO_HOME")
    if not d:
        if os.name == "nt":
            d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "lanaudio")
        else:
            d = os.path.join(os.path.expanduser("~"), ".config", "lanaudio")
    os.makedirs(d, exist_ok=True)
    return d


def cfg_path() -> str:
    return os.path.join(home_dir(), "config.json")


def load_cfg() -> dict:
    with _cfg_lock:
        try:
            with open(cfg_path(), encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError):
            cfg = {}
        changed = "agent_id" not in cfg
        cfg.setdefault("name", socket.gethostname())
        cfg.setdefault("agent_id", secrets.token_hex(4))
        cfg.setdefault("groups", {})
        cfg.setdefault("tokens", [])
        cfg.setdefault("devices", [])
        cfg.setdefault("offsets", {})
        cfg.setdefault("volume", 1.0)
        cfg.setdefault("advertise_ip", "")
        for members in cfg["groups"].values():
            for m in members:
                if "id" not in m:
                    m["id"] = secrets.token_hex(4)
                    changed = True
                if "secret" not in m:
                    m["secret"] = secrets.token_urlsafe(16)
                    changed = True
        if changed:
            save_cfg(cfg)
        return cfg


def save_cfg(cfg: dict) -> None:
    with _cfg_lock:
        p = cfg_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, p)


def parse_peer(spec: str) -> Tuple[str, int]:
    spec = spec.strip()
    ip, port = spec, PEER_DEFAULT_PORT
    if spec.count(":") == 1:
        ip, p = spec.split(":")
        port = int(p)
    ipaddress.IPv4Address(ip)
    if not 2 <= port <= 65534:
        raise ValueError("port out of range")
    return ip, port


def is_ip_spec(spec: str) -> bool:
    try:
        parse_peer(spec)
        return True
    except ValueError:
        return False


def clamp_latency(ms) -> int:
    return int(max(MIN_LATENCY_MS, min(MAX_LATENCY_MS, int(ms))))


# --------------------------------------------------------------------------- #
# Windows real-time tuning
# --------------------------------------------------------------------------- #
_tls = threading.local()


def boost_thread() -> None:
    """Register the calling thread with MMCSS 'Pro Audio' (once per thread)."""
    if os.name != "nt" or getattr(_tls, "boosted", False):
        return
    _tls.boosted = True
    try:
        import ctypes
        avrt = ctypes.windll.avrt
        avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
        idx = ctypes.c_ulong(0)
        avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(idx))
    except Exception:
        pass


def tune_process() -> List[str]:
    """Make this process hard to starve. Returns what was applied."""
    done = []
    try:
        sys.setswitchinterval(0.001)          # Audio threads get the GIL back quickly
        done.append("1 ms GIL switch")
    except Exception:
        pass
    if os.name != "nt":
        return done
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        proc = k.GetCurrentProcess()
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        if k.SetPriorityClass(proc, 0x00000080):           # HIGH_PRIORITY_CLASS
            done.append("high priority")
        try:
            if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
                done.append("1 ms timers")
        except Exception:
            pass

        class PowerThrottling(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        st = PowerThrottling(1, 0x1 | 0x4, 0)                 # execution speed + timer resolution: OFF
        k.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        if k.SetProcessInformation(proc, 4, ctypes.byref(st), ctypes.sizeof(st)):
            done.append("no power throttling")
    except Exception:
        pass
    return done


# --------------------------------------------------------------------------- #
# Network helpers
# --------------------------------------------------------------------------- #
def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def host_ips(cfg: Optional[dict] = None) -> List[str]:
    cfg = cfg or load_cfg()
    ips = [cfg.get("advertise_ip") or "", local_ip()]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    out: List[str] = []
    for ip in ips:
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)
    return out or ["127.0.0.1"]


class Conn:
    """JSON lines for the handshake, then length-prefixed binary frames.

    One reader thread at a time; any thread may send (sends are locked).
    Reads wait with select(), so the socket timeout only applies to sends.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.lock = threading.Lock()
        for opt, val in ((socket.TCP_NODELAY, 1),):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
            except OSError:
                pass
        for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, 1 << 20)
            except OSError:
                pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        sock.settimeout(10.0)

    def _more(self, deadline: float) -> bool:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        r, _, _ = select.select([self.sock], [], [], left)
        if not r:
            return False
        chunk = self.sock.recv(262144)
        if not chunk:
            raise ConnectionError("connection closed")
        self.buf += chunk
        if len(self.buf) > 32 << 20:
            raise ConnectionError("receive buffer overflow")
        return True

    # -- handshake (JSON lines) ------------------------------------------- #
    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self.lock:
            self.sock.sendall(data)

    def recv(self, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                break
            if len(self.buf) > 1 << 20:
                raise ConnectionError("oversized message")
            if not self._more(deadline):
                return None
        line = bytes(self.buf[:i])
        del self.buf[:i + 1]
        try:
            msg = json.loads(line)
        except ValueError:
            raise ConnectionError("garbled message")
        if not isinstance(msg, dict):
            raise ConnectionError("garbled message")
        return msg

    # -- session (binary frames) ------------------------------------------ #
    def send_frame(self, kind: int, payload: bytes) -> None:
        with self.lock:
            self.sock.sendall(FH.pack(kind, len(payload)) + payload)

    def send_json(self, obj: dict) -> None:
        self.send_frame(F_JSON, json.dumps(obj, separators=(",", ":")).encode())

    def recv_frame(self, timeout: float) -> Optional[Tuple[int, bytes]]:
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buf) >= FH.size:
                kind, n = FH.unpack_from(self.buf)
                if n > 4 << 20:
                    raise ConnectionError("oversized frame")
                if len(self.buf) >= FH.size + n:
                    payload = bytes(self.buf[FH.size:FH.size + n])
                    del self.buf[:FH.size + n]
                    return kind, payload
            if not self._more(deadline):
                return None

    def close(self) -> None:
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass


def dial(ip: str, port: int, timeout: float = 3.0) -> Conn:
    return Conn(socket.create_connection((ip, port), timeout=timeout))


def proof(secret: str, nonce: str) -> str:
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def short_err(e: Exception) -> str:
    if isinstance(e, ConnectionRefusedError):
        return "agent not running"
    if isinstance(e, socket.timeout):
        return "no response"
    if isinstance(e, OSError) and e.errno in (101, 113, 10051, 10065):
        return "unreachable"
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError)):
        return "connection dropped"
    s = str(e) or e.__class__.__name__
    return s if len(s) < 60 else s[:57] + "..."


# --------------------------------------------------------------------------- #
# Tokens: one per (group, member). The host mints it, the member saves it once.
# --------------------------------------------------------------------------- #
def make_token(cfg: dict, group: str, member: dict, port: int) -> str:
    p = {"a": cfg["agent_id"], "n": cfg["name"], "h": host_ips(cfg), "p": port, "g": group,
         "m": member["id"], "s": member["secret"], "u": member["name"]}
    raw = json.dumps(p, separators=(",", ":")).encode()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_token(text: str) -> dict:
    s = text.strip().strip("'\"")
    i = s.find(TOKEN_PREFIX)
    if i < 0:
        raise ValueError("that doesn't look like a lanaudio token (they start with LA1.)")
    s = s[i + len(TOKEN_PREFIX):].split()[0].strip("'\"")
    try:
        p = json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
        tok = {"host_id": str(p["a"]), "host_name": str(p["n"]),
               "hosts": [str(ipaddress.IPv4Address(h)) for h in p["h"]], "port": int(p["p"]),
               "group": str(p["g"]), "id": str(p["m"]), "secret": str(p["s"]), "me": str(p["u"])}
    except Exception:
        raise ValueError("the token is damaged (copy the whole line, it's long)")
    if not tok["hosts"]:
        raise ValueError("the token has no host address")
    return tok


def token_label(tok: dict) -> str:
    return f"{tok['group']}@{tok['host_name']}"


def fetch_info(tok: dict, my_name: str, timeout: float = 2.0) -> dict:
    err = "offline"
    for ip in tok["hosts"]:
        try:
            c = dial(ip, tok["port"], timeout)
        except OSError as e:
            err = short_err(e)
            continue
        try:
            c.send({"t": "HELLO", "v": VERSION, "name": my_name, "port": 0})
            h = c.recv(3)
            if not h or h.get("t") != "HELLO":
                err = "not a lanaudio agent"
                continue
            c.send({"t": "INFO", "group": tok["group"], "mid": tok["id"],
                    "proof": proof(tok["secret"], str(h.get("nonce", "")))})
            r = c.recv(5)
            if r:
                r["ip"] = ip
                return r
        except (OSError, ConnectionError) as e:
            err = short_err(e)
        finally:
            c.close()
    return {"t": "OFFLINE", "reason": err}


def to_clipboard(s: str) -> bool:
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["clip"], input=s, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Audio backend (PyAudioWPatch, loaded lazily so the CLI works without it)
# --------------------------------------------------------------------------- #
class AudioUnavailable(RuntimeError):
    pass


_pa_module = None


def pa_mod():
    global _pa_module
    if _pa_module is None:
        if os.name != "nt" and not os.environ.get("LANAUDIO_FAKE_AUDIO"):
            raise AudioUnavailable("Audio needs Windows (WASAPI loopback).")
        try:
            import pyaudiowpatch as m
        except ImportError:
            raise AudioUnavailable("PyAudioWPatch is missing. Run: pip install PyAudioWPatch")
        _pa_module = m
    return _pa_module


def wasapi_devices(pa) -> dict:
    m = pa_mod()
    wasapi = pa.get_host_api_info_by_type(m.paWASAPI)
    default = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    outputs, loopbacks = [], []
    for d in pa.get_device_info_generator():
        if d.get("hostApi") != wasapi["index"]:
            continue
        if d.get("isLoopbackDevice", False):
            loopbacks.append(d)
        elif int(d.get("maxOutputChannels", 0)) > 0:
            outputs.append(d)
    return {"default": default, "outputs": outputs, "loopbacks": loopbacks}


def find_loopback(pa, default: dict, loopbacks: List[dict]) -> dict:
    name = default["name"].lower()
    for lb in loopbacks:
        lb_name = lb["name"].lower().replace("[loopback]", "").strip()
        if lb_name == name or lb_name.startswith(name) or name.startswith(lb_name):
            return lb
    return pa.get_default_wasapi_loopback()


def output_devices(pa) -> Tuple[List[dict], str]:
    info = wasapi_devices(pa)
    default = info["default"]["name"]
    out, seen = [], set()
    for d in info["outputs"]:
        n = d["name"]
        if n in seen or any(k in n.lower() for k in BLOCKED_KEYWORDS):
            continue
        seen.add(n)
        out.append(d)
    out.sort(key=lambda d: d["name"] != default)
    return out, default


def current_output_names() -> Optional[List[str]]:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_names"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "_names"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return None
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# DSP
# --------------------------------------------------------------------------- #
def map_channels(x: np.ndarray, dst: int) -> np.ndarray:
    src = x.shape[1]
    if src == dst:
        return x
    if dst == 1:
        return x.mean(axis=1, keepdims=True)
    if src == 1:
        return np.repeat(x, dst, axis=1)
    out = np.zeros((len(x), dst), np.float32)
    out[:, :min(src, dst)] = x[:, :min(src, dst)]
    return out


@functools.lru_cache(maxsize=1)
def opus_available() -> bool:
    try:
        import opuslib
        opuslib.Encoder(48000, CH, opuslib.APPLICATION_AUDIO)
        return True
    except Exception:
        return False


def codec_label(name: str, rate: int) -> str:
    khz = f"{rate / 1000:g} kHz"
    if name == "pcm24":
        return f"PCM 24-bit · {khz} · {rate * CH * 24 / 1e6:.2f} Mbps · lossless"
    if name == "pcm16":
        return f"PCM 16-bit · {khz} · {rate * CH * 16 / 1e6:.2f} Mbps · CD lossless"
    return f"Opus · {khz} · 510 kbps · transparent"


class Codec:
    def __init__(self, name: str, rate: int):
        if name not in CODECS:
            raise ValueError(f"unknown codec {name!r}")
        self.name, self.rate = name, rate
        self.enc = self.dec = None
        if name == "opus":
            if rate not in (8000, 12000, 16000, 24000, 48000):
                raise ValueError(f"Opus can't do {rate} Hz. Use pcm24, or set the host's output to 48 kHz.")
            import opuslib
            self.enc = opuslib.Encoder(rate, CH, opuslib.APPLICATION_AUDIO)
            for attr, val in (("bitrate", 510000), ("complexity", 10)):
                try:
                    setattr(self.enc, attr, val)
                except Exception:
                    pass
            self.dec = opuslib.Decoder(rate, CH)

    def encode(self, x: np.ndarray) -> bytes:
        x = np.clip(x, -1.0, 1.0)
        if self.name == "pcm24":
            v = (x.reshape(-1) * 8388607.0).astype("<i4")
            return v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        pcm = (x * 32767.0).astype("<i2").tobytes()
        return pcm if self.name == "pcm16" else self.enc.encode(pcm, len(x))

    def decode(self, payload: bytes, n: int) -> np.ndarray:
        if self.name == "pcm24":
            if len(payload) != n * CH * 3:
                raise ValueError("bad frame size")
            b = np.frombuffer(payload, np.uint8).reshape(-1, 3)
            a = np.zeros((len(b), 4), np.uint8)
            a[:, 1:] = b
            v = a.view("<i4").reshape(-1) >> 8
            return (v.astype(np.float32) / 8388608.0).reshape(-1, CH)
        pcm = payload if self.name == "pcm16" else self.dec.decode(payload, n)
        if len(pcm) != n * CH * 2:
            raise ValueError("bad frame size")
        return (np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0).reshape(-1, CH)


class Meter:
    """Peak level and a log-spaced spectrum for the visualizers."""

    def __init__(self, size: int = 2048):
        self.buf = np.zeros(size, np.float32)
        self.level = 0.0
        self.lock = threading.Lock()

    def feed(self, x: np.ndarray) -> None:
        if not len(x):
            return
        mono = x.mean(axis=1) if x.ndim == 2 else x
        n = len(mono)
        with self.lock:
            if n >= len(self.buf):
                self.buf[:] = mono[-len(self.buf):]
            else:
                self.buf[:-n] = self.buf[n:]
                self.buf[-n:] = mono
        self.level = max(float(np.abs(mono).max()), self.level * 0.92)

    def bands(self, nb: int = 24) -> List[float]:
        with self.lock:
            x = self.buf.copy()
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        edges = np.geomspace(2, len(spec) - 1, nb + 1).astype(int)
        vals = [float(spec[a:max(b, a + 1)].mean()) for a, b in zip(edges[:-1], edges[1:])]
        db = 20 * np.log10(np.array(vals) + 1e-9)
        return [round(float(v), 3) for v in np.clip((db - 5) / 45, 0, 1)]


class Sinc:
    """Kaiser-windowed sinc interpolation kernel (polyphase table)."""

    def __init__(self, cutoff: float, taps: int = 24, phases: int = 256, beta: float = 7.0):
        self.offs = np.arange(-taps // 2 + 1, taps // 2 + 1)
        self.center = int(np.where(self.offs == 0)[0][0])
        self.P = phases
        frac = np.arange(phases + 1) / phases
        t = self.offs[None, :] - frac[:, None]
        half = taps / 2.0
        win = np.i0(beta * np.sqrt(np.clip(1 - (t / half) ** 2, 0, 1))) / np.i0(beta)
        h = 2 * cutoff * np.sinc(2 * cutoff * t) * win
        h /= h.sum(axis=1, keepdims=True)
        self.table = h.astype(np.float32)


class Ring:
    """Sample store indexed by absolute stream position; stale slots read as silence."""

    def __init__(self, rate: int, seconds: int = RING_SECONDS):
        self.n = rate * seconds
        self.data = np.zeros((self.n, CH), np.float32)
        self.tag = np.full(self.n, -(1 << 62), np.int64)
        self.high = 0

    def write(self, pos: int, x: np.ndarray) -> None:
        absolute = pos + np.arange(len(x), dtype=np.int64)
        idx = absolute % self.n
        self.data[idx] = x
        self.tag[idx] = absolute
        self.high = max(self.high, pos + len(x))

    def read(self, pos: np.ndarray, k: Sinc) -> Tuple[np.ndarray, int]:
        i0 = np.floor(pos).astype(np.int64)
        ph = np.rint((pos - i0) * k.P).astype(np.int64)
        idx = i0[:, None] + k.offs[None, :]
        slot = idx % self.n
        ok = self.tag[slot] == idx
        vals = self.data[slot] * ok[..., None]
        y = np.einsum("nk,nkc->nc", k.table[ph], vals)
        return y.astype(np.float32), int((~ok[:, k.center]).sum())


# --------------------------------------------------------------------------- #
# Host side: one loopback capture, fanned out to every room's broadcast
# --------------------------------------------------------------------------- #
class Capture:
    """Loopback of the Windows default output at its native rate, float32.

    Every emitted sample has an absolute position. Its host time is
    epoch + pos / rate, where epoch slowly tracks the host's perf_counter.
    """

    def __init__(self, on_chunk: Callable, start_pos: int = 0):
        self.on_chunk = on_chunk
        self.pa = self.stream = self.keep = self.m = None
        self.acc: List[np.ndarray] = []
        self.acc_n = 0
        self.acc_pos = start_pos
        self.epoch: Optional[float] = None
        self.source = ""
        self.fmt = ""
        self.rate = 48000
        self.frame = 960
        self.error = ""
        self.failed = threading.Event()

    def start(self) -> None:
        self.m = m = pa_mod()
        self.pa = m.PyAudio()
        try:
            info = wasapi_devices(self.pa)
            lb = find_loopback(self.pa, info["default"], info["loopbacks"])
            self.source = info["default"]["name"]
            self.rate = int(lb["defaultSampleRate"])
            self.frame = self.rate * FRAME_MS // 1000
            max_ch = max(1, int(lb["maxInputChannels"]))
            self._keepalive(info["default"])
            err = None
            for fmt, dtype, scale, label in ((m.paFloat32, np.float32, 1.0, "float32"),
                                             (m.paInt16, np.int16, 32768.0, "int16")):
                for ch in dict.fromkeys([max_ch, 2, 1]):
                    if ch > max_ch:
                        continue
                    try:
                        self.dtype, self.scale, self.src_ch = dtype, scale, ch
                        self.stream = self.pa.open(
                            format=fmt, channels=ch, rate=self.rate, input=True,
                            input_device_index=lb["index"], frames_per_buffer=1024,
                            stream_callback=self._cb)
                        self.fmt = label
                        break
                    except Exception as e:
                        err = e
                if self.stream is not None:
                    break
            if self.stream is None:
                raise RuntimeError(f"can't open loopback ({err})")
            self.stream.start_stream()
        except Exception:
            self.stop()
            raise

    def _keepalive(self, dev: dict) -> None:
        """WASAPI loopback stalls when nothing renders. A silent stream keeps it ticking."""
        m = self.m
        try:
            ch = max(1, min(2, int(dev["maxOutputChannels"])))

            def cb(in_data, n, ti, status):
                return (bytes(2 * ch * n), m.paContinue)

            self.keep = self.pa.open(format=m.paInt16, channels=ch, rate=int(dev["defaultSampleRate"]),
                                     output=True, output_device_index=dev["index"],
                                     frames_per_buffer=2048, stream_callback=cb)
            self.keep.start_stream()
        except Exception:
            self.keep = None

    def _cb(self, in_data, frame_count, time_info, status):
        boost_thread()
        try:
            now = time.perf_counter()
            x = np.frombuffer(in_data, self.dtype).reshape(-1, self.src_ch)
            y = map_channels(x.astype(np.float32) / self.scale if self.scale != 1.0 else x.astype(np.float32), CH)
            m = len(y)
            if m == 0:
                return (None, self.m.paContinue)
            end = self.acc_pos + self.acc_n + m
            if self.epoch is None:
                self.epoch = now - end / self.rate
            e = now - (self.epoch + end / self.rate)
            if e > 0.2:                                   # Capture gap: skip ahead in time
                self.acc, self.acc_n = [], 0
                self.acc_pos = int(round((now - self.epoch) * self.rate)) - m
            elif e < -0.2:
                self.epoch = now - end / self.rate
            else:
                self.epoch += 0.002 * e                   # Slow drift tracking
            self.acc.append(y)
            self.acc_n += m
            if self.acc_n >= self.frame:
                buf = np.concatenate(self.acc)
                k = (len(buf) // self.frame) * self.frame
                for i in range(0, k, self.frame):
                    pos = self.acc_pos + i
                    self.on_chunk(pos, self.epoch + pos / self.rate, buf[i:i + self.frame])
                rest = buf[k:]
                self.acc = [rest] if len(rest) else []
                self.acc_n = len(rest)
                self.acc_pos += k
        except Exception as e:
            self.error = short_err(e)
            self.failed.set()
            return (None, self.m.paAbort)
        return (None, self.m.paContinue)

    def next_pos(self) -> int:
        return self.acc_pos + self.acc_n

    def ok(self) -> bool:
        if self.failed.is_set():
            return False
        try:
            return self.stream is not None and self.stream.is_active()
        except Exception:
            return False

    def stop(self) -> None:
        for s in (self.stream, self.keep):
            if s is not None:
                for fn in (s.stop_stream, s.close):
                    try:
                        fn()
                    except Exception:
                        pass
        self.stream = self.keep = None
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None


class CaptureHub:
    """Starts the capture with the first room, stops it with the last one."""

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self.subs: list = []
        self.lock = threading.Lock()
        self.capture: Optional[Capture] = None
        self.meter = Meter()
        self.error = ""
        self.stop_ev = threading.Event()

    def ensure(self) -> int:
        """Start capturing if needed. Returns the stream sample rate."""
        with self.lock:
            if self.capture is None:
                cap = Capture(self._dispatch)
                cap.start()
                self.capture, self.error = cap, ""
                self.stop_ev = threading.Event()
                threading.Thread(target=self._watch, args=(self.stop_ev,), daemon=True).start()
            return self.capture.rate

    def subscribe(self, bc: "Broadcast") -> None:
        with self.lock:
            self.subs = self.subs + [bc]

    def unsubscribe(self, bc: "Broadcast") -> None:
        with self.lock:
            self.subs = [s for s in self.subs if s is not bc]
            if not self.subs and self.capture is not None:
                self.stop_ev.set()
                self.capture.stop()
                self.capture = None

    def _dispatch(self, pos: int, ts: float, chunk: np.ndarray) -> None:
        self.meter.feed(chunk)
        for s in self.subs:
            s.push(pos, ts, chunk)

    def _watch(self, stop_ev: threading.Event) -> None:
        while not stop_ev.wait(1.0):
            failed = False
            with self.lock:
                cap = self.capture
                if cap is None or stop_ev.is_set():
                    return
                if cap.ok():
                    continue
                self.agent.event("warn", "Source capture hiccuped. Restarting it like nothing happened.")
                pos = cap.next_pos() + cap.rate
                cap.stop()
                new = Capture(self._dispatch, start_pos=pos)
                try:
                    new.start()
                    self.error = ""
                except Exception as e:
                    self.error = short_err(e)
                    failed = True
                self.capture = new
            if failed:
                stop_ev.wait(3)

    def status(self) -> dict:
        cap = self.capture
        return {"name": cap.source if cap else "", "ok": bool(cap and cap.ok()), "error": self.error,
                "rate": cap.rate if cap else 0, "fmt": cap.fmt if cap else "",
                "level": round(self.meter.level, 3), "bands": self.meter.bands()}


class Broadcast:
    """Encodes each frame once and keeps a few seconds of history for replay."""

    def __init__(self, codec: str, rate: int):
        self.codec = Codec(codec, rate)
        self.rate = rate
        self.frames: List[tuple] = []          # (seq, pos, ts, wire bytes)
        self.cond = threading.Condition()
        self.seq = 0
        self.keep = int(HISTORY_S * 1000 / FRAME_MS)

    def push(self, pos: int, ts: float, chunk: np.ndarray) -> None:
        try:
            data = AUDIO_HDR.pack(self.seq & 0xFFFFFFFF, pos, int(ts * 1e6), len(chunk)) + self.codec.encode(chunk)
        except Exception:
            return
        with self.cond:
            self.frames.append((self.seq, pos, ts, data))
            self.seq += 1
            if len(self.frames) > self.keep + 50:
                del self.frames[:len(self.frames) - self.keep]
            self.cond.notify_all()

    def start_seq(self, latency_s: float, have_pos: Optional[int] = None) -> int:
        """First frame a member still needs: unplayed frames, after what it already has."""
        with self.cond:
            if not self.frames:
                return self.seq
            now = time.perf_counter()
            live = next((f[0] for f in self.frames if f[2] + latency_s > now + 0.02), self.seq)
            if have_pos is not None:
                hit = next((f[0] for f in self.frames if f[1] >= have_pos), self.seq)
                return max(hit, live)
            return live


class MemberState:
    def __init__(self, name: str, ip: str, key: str, token: bool):
        self.name, self.ip, self.key, self.token = name, ip, key, token
        self.state = "joining"
        self.note = ""
        self.stats: dict = {}
        self.cmd_q: "queue.Queue" = queue.Queue()
        self.since = time.time()
        self.connected = 0.0
        self.gen = 0
        self.resumes = 0
        self.skips = 0
        self.left = False

    def to_dict(self) -> dict:
        s = self.stats if self.state == "live" else {}
        return {"name": self.name, "ip": self.ip, "key": self.key, "token": self.token, "state": self.state,
                "note": self.note, "since": self.since, "rtt": s.get("rtt"), "head": s.get("head"),
                "gaps": s.get("gaps"), "sync": s.get("sync"), "lvl": s.get("lvl", 0.0),
                "resumes": self.resumes}


class Room:
    """One live listening session for one group. A host can run several at once."""

    def __init__(self, agent: "Agent", group: str, latency_ms: Optional[int], codec: str):
        self.agent = agent
        self.group = group
        self.id = secrets.token_hex(3)
        self.session = random.randint(1, 0xFFFFFFFF)
        self.auto = latency_ms is None
        self.latency = AUTO_START_MS if self.auto else clamp_latency(latency_ms)
        self.codec = codec
        self.rate = 48000
        self.members: Dict[str, MemberState] = {}
        self.pending: Dict[str, dict] = {}
        self.kicked: set = set()
        self.approved: set = set()
        self.active = True
        self.since = time.time()
        self.lock = threading.RLock()
        self.bc: Optional[Broadcast] = None
        self.last_bump = 0.0

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        self.rate = self.agent.hub.ensure()
        try:
            self.bc = Broadcast(self.codec, self.rate)
        except Exception:
            if not self.agent.hub.subs:
                self.agent.hub.unsubscribe(None)
            raise
        self.agent.hub.subscribe(self.bc)
        threading.Thread(target=self._monitor, daemon=True).start()

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        for req in list(self.pending.values()):
            req["ev"].set()
        with self.bc.cond:
            self.bc.cond.notify_all()
        time.sleep(0.4)
        self.agent.hub.unsubscribe(self.bc)

    def _monitor(self) -> None:
        """Auto latency: grow the buffer when anyone's headroom gets thin."""
        while self.active:
            time.sleep(1.0)
            if not self.auto or self.latency >= MAX_LATENCY_MS:
                continue
            now = time.monotonic()
            if now - self.last_bump < 8:
                continue
            for m in list(self.members.values()):
                head = m.stats.get("head_tgt") if m.state == "live" else None
                if head is not None and now - m.connected > 6 and head < HEADROOM_LOW_MS:
                    new = self.set_latency(self.latency + AUTO_STEP_MS, auto=True)
                    self.last_bump = now
                    self.agent.event("warn", f"{m.name}'s link got shaky ({head:.0f} ms headroom). "
                                             f"'{self.group}' buffer raised to {new} ms. Smooth > fast.")
                    break

    def roster(self) -> List[dict]:
        return load_cfg()["groups"].get(self.group, [])

    def accept_msg(self) -> dict:
        return {"t": "ACCEPT", "room": self.id, "session": self.session, "latency": self.latency,
                "codec": self.codec, "rate": self.rate, "host": self.agent.name, "group": self.group}

    # -- joining ---------------------------------------------------------- #
    def handle_join(self, conn: Conn, ident: dict, ip: str, msg: dict) -> None:
        key, name = ident["key"], ident["name"]
        reason = None
        if not self.active:
            reason = "not live"
        elif key in self.kicked:
            reason = "kicked"
        elif self.codec == "opus" and not msg.get("opus"):
            reason = "no opus on your side"
        if reason:
            conn.send({"t": "REJECT", "reason": reason})
            return
        if ident["token"]:
            self.agent.event("knock", f"{name} flashed their token at '{self.group}'. Waved straight in.")
        elif key not in self.approved and not self._await_approval(conn, ident, ip):
            return
        with self.lock:
            if not self.active:
                conn.send({"t": "REJECT", "reason": "not live"})
                return
            m = self.members.get(key) or MemberState(name, ip, key, ident["token"])
            m.name, m.ip, m.left = name, ip, False
            self.members[key] = m
        have = msg.get("have") if msg.get("session") == self.session else None
        start = self.bc.start_seq(self.latency / 1000.0, int(have) if have is not None else None)
        conn.send(self.accept_msg())
        self.run_session(conn, m, start, resumed=have is not None)

    def _await_approval(self, conn: Conn, ident: dict, ip: str) -> bool:
        key, name = ident["key"], ident["name"]
        req = {"key": key, "name": name, "ip": ip, "since": time.time(), "ev": threading.Event(), "ok": None}
        with self.lock:
            old = self.pending.get(key)
            if old:
                old["ev"].set()
            self.pending[key] = req
        self.agent.event("knock", f"{name} ({ip}) is knocking on '{self.group}'. [A] let in, [D] nope.")
        deadline = time.monotonic() + APPROVAL_TIMEOUT_S
        try:
            conn.send({"t": "WAIT", "group": self.group})
            while not req["ev"].wait(2.0):
                if not self.active or time.monotonic() > deadline:
                    break
                conn.send({"t": "WAIT", "group": self.group})
        except OSError:
            req["ok"] = None
        finally:
            with self.lock:
                if self.pending.get(key) is req:
                    del self.pending[key]
        if req["ok"]:
            self.approved.add(key)
            return True
        if req["ok"] is False:
            reason = "host said no"
            self.agent.event("kick", f"{name} got turned away at the door. Brutal.")
        else:
            reason = "not live" if not self.active else "nobody answered the door"
        try:
            conn.send({"t": "REJECT", "reason": reason})
        except OSError:
            pass
        return False

    def find_pending(self, who: Optional[str]) -> Optional[dict]:
        with self.lock:
            reqs = sorted(self.pending.values(), key=lambda r: r["since"])
        if not who:
            return reqs[0] if reqs else None
        w = who.lower()
        return next((r for r in reqs if w in (r["name"].lower(), r["ip"], r["key"].lower())), None)

    # -- the live session: one persistent connection ----------------------- #
    def _writer(self, conn: Conn, m: MemberState, cur: int, stop: threading.Event, gen: int, res: dict) -> None:
        boost_thread()
        bc = self.bc
        last_hb = 0.0
        try:
            while not stop.is_set() and m.gen == gen:
                if not self.active:
                    conn.send_json({"t": "STOP"})
                    res["end"] = "ended"
                    return
                try:
                    while True:
                        cmd = m.cmd_q.get_nowait()
                        conn.send_json(cmd)
                        if cmd["t"] in ("KICK", "STOP"):
                            res["end"] = "kicked" if cmd["t"] == "KICK" else "ended"
                            return
                except queue.Empty:
                    pass
                with bc.cond:
                    if bc.seq <= cur:
                        bc.cond.wait(0.1)
                    fr = bc.frames
                    batch = []
                    if fr:
                        if cur < fr[0][0]:                    # Fell out of history: jump to live
                            m.skips += 1
                            cur = bc.start_seq(self.latency / 1000.0)
                            self.agent.event("warn", f"{m.name} fell way behind. Skipped ahead to keep up.")
                        batch = fr[max(0, cur - fr[0][0]):]
                for f in batch:
                    conn.send_frame(F_AUDIO, f[3])
                    cur = f[0] + 1
                now = time.monotonic()
                if now - last_hb >= HB_S:
                    conn.send_json({"t": "HB", "lat": self.latency})
                    last_hb = now
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            stop.set()

    def run_session(self, conn: Conn, m: MemberState, start_seq: int, resumed: bool) -> None:
        with self.lock:
            m.gen += 1
            gen = m.gen
            was_live = m.state == "live"
        m.state, m.note = "live", ""
        m.connected = time.monotonic()
        if resumed or was_live:
            m.resumes += 1
            self.agent.event("join", f"{m.name} reconnected to '{self.group}' and picked up where they left off.")
        else:
            m.since = time.time()
            self.agent.event("join", f"{m.name} slid into '{self.group}'. Vibes: immaculate.")
        stop = threading.Event()
        res = {"end": "lost"}
        w = threading.Thread(target=self._writer, args=(conn, m, start_seq, stop, gen, res), daemon=True)
        w.start()
        last_rx = time.monotonic()
        try:
            while not stop.is_set() and m.gen == gen:
                fr = conn.recv_frame(0.5)
                if fr is None:
                    if time.monotonic() - last_rx > PEER_TIMEOUT_S:
                        break
                    continue
                last_rx = time.monotonic()
                kind, payload = fr
                if kind == F_PING and len(payload) == PING.size:
                    t2 = time.perf_counter()
                    conn.send_frame(F_PONG, PONG.pack(PING.unpack(payload)[0], t2, time.perf_counter()))
                elif kind == F_JSON:
                    msg = json.loads(payload)
                    if msg.get("t") == "STATS":
                        m.stats = msg
                    elif msg.get("t") == "BYE":
                        m.left = True
                        res["end"] = "left"
                        break
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            stop.set()
            w.join(timeout=2)
            conn.close()
            if m.gen == gen:
                end = res["end"]
                m.state = end
                if end == "left":
                    self.agent.event("leave", f"{m.name} left '{self.group}'. Their loss, honestly.")
                elif end == "kicked":
                    self.agent.event("kick", f"{m.name} was escorted out of '{self.group}' by security.")
                elif end == "lost" and self.active:
                    self.agent.event("warn", f"{m.name}'s connection blinked. Their buffer covers it while they reconnect.")

    # -- controls ---------------------------------------------------------- #
    def kick(self, who: str) -> Optional[str]:
        w = who.lower()
        with self.lock:
            members = list(self.members.values())
        for m in members:
            if w in (m.name.lower(), m.ip, m.key.lower()) and m.state == "live":
                self.kicked.add(m.key)
                m.cmd_q.put({"t": "KICK"})
                return m.name
        for r in self.roster():
            if r["name"].lower() == w:
                self.kicked.add("t:" + r["id"])
                return r["name"]
        for m in members:
            if w in (m.name.lower(), m.ip, m.key.lower()):
                self.kicked.add(m.key)
                m.state = "kicked"
                return m.name
        return None

    def set_latency(self, ms: Optional[int], auto: Optional[bool] = None) -> int:
        """ms=None keeps the current value. auto=None keeps the current mode."""
        if auto is not None:
            self.auto = auto
        if ms is not None:
            self.latency = clamp_latency(ms)
        for m in list(self.members.values()):
            m.cmd_q.put({"t": "LAT", "ms": self.latency})
        return self.latency

    def live_count(self) -> int:
        return sum(m.state == "live" for m in list(self.members.values()))

    def status(self) -> dict:
        with self.lock:
            members = dict(self.members)
            pending = sorted(self.pending.values(), key=lambda r: r["since"])
        rows, seen = [], set()
        for r in self.roster():
            key = "t:" + r["id"]
            seen.add(key)
            m = members.get(key)
            if m is not None:
                d = m.to_dict()
                d["name"] = r["name"]
            else:
                d = {"name": r["name"], "ip": r.get("ip", ""), "key": key, "token": True,
                     "state": "kicked" if key in self.kicked else "away", "note": "", "since": None,
                     "rtt": None, "head": None, "gaps": None, "sync": None, "lvl": 0.0, "resumes": 0}
            rows.append(d)
        for k, m in members.items():
            if k not in seen:
                rows.append(m.to_dict())
        return {"id": self.id, "group": self.group, "latency": self.latency, "auto": self.auto,
                "codec": self.codec, "label": codec_label(self.codec, self.rate), "since": self.since,
                "members": rows,
                "pending": [{"key": p["key"], "name": p["name"], "ip": p["ip"], "since": p["since"]}
                            for p in pending]}


# --------------------------------------------------------------------------- #
# Member side: clock sync, deep buffer, scheduled multi-device playback
# --------------------------------------------------------------------------- #
class ClockSync:
    """NTP-style offset (host clock - local clock), keeping the lowest-RTT sample."""

    def __init__(self):
        self.samples: deque = deque(maxlen=20)
        self.offset: Optional[float] = None
        self.rtt: Optional[float] = None

    def on_pong(self, t1: float, t2: float, t3: float) -> None:
        t4 = time.perf_counter()
        rtt = (t4 - t1) - (t3 - t2)
        if not 0 <= rtt < 2:
            return
        self.samples.append((rtt, ((t2 - t1) + (t3 - t4)) / 2))
        best_rtt, best_off = min(self.samples)
        if self.offset is None or abs(best_off - self.offset) > 0.05:
            self.offset = best_off
        else:
            self.offset += 0.25 * (best_off - self.offset)
        self.rtt = best_rtt


class DeviceOut:
    """One output stream. Plays the sample due *now* and nudges its rate to stay on time."""

    def __init__(self, mem: "Membership", dev: dict, offset_ms: int, meter: Optional[Meter], rate: int):
        self.mem, self.dev, self.meter = mem, dev, meter
        self.name = dev["name"]
        self.offset_ms = offset_ms
        self.src_rate = rate
        self.stream = self.m = None
        self.rate, self.ch, self.step0 = rate, CH, 1.0
        self.kernel: Optional[Sinc] = None
        self.float_out = True
        self.rpos: Optional[float] = None
        self.err_f = 0.0
        self.resyncs = 0
        self.gaps = 0
        self.had_audio = False
        self.lat = 0.05
        self.error = ""

    def open(self, pa, m) -> None:
        self.m = m
        dev_rate = int(self.dev["defaultSampleRate"])
        max_ch = max(1, int(self.dev["maxOutputChannels"]))
        rates = list(dict.fromkeys([dev_rate, self.src_rate, 48000, 44100]))
        chans = [c for c in dict.fromkeys([min(CH, max_ch), max_ch, 2, 1]) if 1 <= c <= max_ch]
        err = None
        for fmt, is_float in ((m.paFloat32, True), (m.paInt16, False)):
            for rate in rates:
                for ch in chans:
                    try:
                        self.rate, self.ch, self.float_out = rate, ch, is_float
                        self.step0 = self.src_rate / rate
                        self.kernel = Sinc(0.46 * min(1.0, rate / self.src_rate))
                        s = pa.open(format=fmt, channels=ch, rate=rate, output=True,
                                    output_device_index=self.dev["index"],
                                    frames_per_buffer=int(rate * OUT_BUFFER_S), stream_callback=self._cb)
                        try:
                            self.lat = float(s.get_output_latency()) or 0.05
                        except Exception:
                            pass
                        s.start_stream()
                        self.stream = s
                        return
                    except Exception as e:
                        err = e
        raise RuntimeError(f"no supported format ({err})")

    def _cb(self, in_data, n, ti, status):
        boost_thread()
        try:
            x = self._render(n, ti)
        except Exception as e:
            self.error = short_err(e)
            x = np.zeros((n, self.ch), np.float32)
        x = np.clip(x, -1.0, 1.0)
        data = x.astype(np.float32).tobytes() if self.float_out else (x * 32767.0).astype(np.int16).tobytes()
        return (data, self.m.paContinue)

    def _render(self, n: int, ti) -> np.ndarray:
        now = time.perf_counter()
        lat = 0.0
        if isinstance(ti, dict):
            lat = ti.get("output_buffer_dac_time", 0.0) - ti.get("current_time", 0.0)
        if not 0.0 < lat < 1.0:
            lat = self.lat
        target = self.mem.pos_for(now + lat - self.offset_ms / 1000.0)
        if target is None:
            self.rpos = None
            self.had_audio = False
            return np.zeros((n, self.ch), np.float32)
        sr = self.src_rate
        err = 0.0 if self.rpos is None else self.rpos - target
        if self.rpos is None or abs(err) > 0.12 * sr or abs(self.err_f) > HARD_RESYNC_S * sr:
            self.rpos, self.err_f, err = target, 0.0, 0.0
            self.resyncs += 1
        self.err_f += ERR_ALPHA * (err - self.err_f)
        # Feed-forward the known latency glide, then a slow loop for clock drift.
        adj = -self.mem.glide - (self.err_f / sr) / LOOP_TAU
        adj = max(-MAX_SLEW, min(MAX_SLEW, adj))
        step = self.step0 * (1.0 + adj)
        pos = self.rpos + np.arange(n) * step
        self.rpos += n * step
        x, missing = self.mem.ring.read(pos, self.kernel)
        if missing:
            if self.had_audio:
                self.gaps += 1
            self.had_audio = False
        else:
            self.had_audio = True
        x *= self.mem.volume
        if self.meter is not None:
            self.meter.feed(x)
        return map_channels(x, self.ch)

    def dead(self) -> bool:
        try:
            return self.stream is None or not self.stream.is_active()
        except Exception:
            return True

    def close(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            for fn in (s.stop_stream, s.close):
                try:
                    fn()
                except Exception:
                    pass

    def status(self) -> dict:
        return {"name": self.name, "rate": self.rate, "ch": self.ch, "offset": self.offset_ms,
                "fmt": "float32" if self.float_out else "int16",
                "sync": round(self.err_f / self.src_rate * 1000, 2), "resyncs": max(0, self.resyncs - 1),
                "gaps": self.gaps, "ok": not self.dead(), "error": self.error}


class Player(threading.Thread):
    """Owns the output streams. Keeps playing across reconnects. Rebuilds on device changes."""

    def __init__(self, mem: "Membership", devices: List[str], offsets: Dict[str, int], rate: int):
        super().__init__(daemon=True)
        self.mem, self.want, self.offsets, self.rate = mem, list(devices or []), dict(offsets or {}), rate
        self.meter = Meter()
        self.stop_ev = threading.Event()
        self.outs: List[DeviceOut] = []
        self.missing: List[str] = []
        self.pa = None
        self.error = ""
        self.gaps_before = 0

    def _offset_for(self, name: str) -> int:
        n = name.lower()
        for k, ms in self.offsets.items():
            if k != "*" and k.lower() in n:
                return int(ms)
        return int(self.offsets.get("*", 0))

    def _open(self) -> None:
        m = pa_mod()
        self.pa = m.PyAudio()
        outs, default = output_devices(self.pa)
        by = {d["name"]: d for d in outs}
        want = [n for n in self.want if n] or [default]
        chosen = [by[n] for n in want if n in by]
        self.missing = [n for n in want if n not in by]
        if not chosen and default in by:
            chosen = [by[default]]
        if not chosen:
            raise RuntimeError("no output device available")
        outs_ok = []
        for d in chosen:
            o = DeviceOut(self.mem, d, self._offset_for(d["name"]), None if outs_ok else self.meter, self.rate)
            try:
                o.open(self.pa, m)
                outs_ok.append(o)
            except Exception as e:
                self.missing.append(d["name"])
                self.error = f"{d['name']}: {short_err(e)}"
        if not outs_ok:
            raise RuntimeError(self.error or "could not open any output device")
        self.outs = outs_ok
        self.error = ""

    def _close(self) -> None:
        self.gaps_before += sum(o.gaps for o in self.outs)
        for o in self.outs:
            o.close()
        self.outs = []
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None

    def gaps(self) -> int:
        return self.gaps_before + sum(o.gaps for o in self.outs)

    def run(self) -> None:
        while not self.stop_ev.is_set():
            try:
                self._open()
            except Exception as e:
                self.error = short_err(e)
                self._close()
                self.stop_ev.wait(3)
                continue
            next_check = time.monotonic() + DEVICE_RECHECK_S
            while not self.stop_ev.wait(0.5):
                if any(o.dead() for o in self.outs):
                    self.mem.agent.event("warn", "An output device bailed. Rebuilding the speakers.")
                    break
                if self.missing and time.monotonic() >= next_check:
                    next_check = time.monotonic() + DEVICE_RECHECK_S
                    names = current_output_names()
                    if names and any(n in names for n in self.missing):
                        self.mem.agent.event("info", "A missing device is back. Welcome home.")
                        break
            self._close()
            self.stop_ev.wait(0.3)

    def stop(self) -> None:
        self.stop_ev.set()
        self.join(timeout=3)
        self._close()


class Membership:
    """This agent listening to someone else's room (by token, or as a guest by IP)."""

    ALIVE = ("connecting", "live", "reconnecting", "waiting", "approval")

    def __init__(self, agent: "Agent", hosts: List[str], port: int, group: Optional[str] = None,
                 token: Optional[dict] = None, devices: Optional[List[str]] = None,
                 offsets: Optional[Dict[str, int]] = None, volume: Optional[float] = None):
        cfg = load_cfg()
        self.agent = agent
        self.hosts = list(hosts)
        self.host_ip, self.host_port = self.hosts[0], port
        self.group = group
        self.token = token
        self.host_name = token["host_name"] if token else self.hosts[0]
        self.devices = devices if devices is not None else cfg.get("devices", [])
        self.offsets = offsets if offsets is not None else cfg.get("offsets", {})
        self.volume = max(0.0, min(2.0, float(volume if volume is not None else cfg.get("volume", 1.0))))
        self.room_id = ""
        self.session: Optional[int] = None
        self.rate = 48000
        self.latency = AUTO_START_MS / 1000.0
        self.lat_eff: Optional[float] = None
        self.lat_t = time.perf_counter()
        self.glide = 0.0
        self.codec_name = "pcm24"
        self.codec: Optional[Codec] = None
        self.state = "connecting"
        self.note = ""
        self.since = time.time()
        self.left = self.kicked = False
        self.stop_ev = threading.Event()
        self.clock = ClockSync()
        self.ring = Ring(self.rate)
        self.player: Optional[Player] = None
        self.epoch: Optional[float] = None
        self.connected_at = 0.0
        self.rx = self.late = self.reconnects = 0
        self.heads: deque = deque()

    def alive(self) -> bool:
        return self.state in self.ALIVE

    def configure(self, msg: dict) -> None:
        session, rate = int(msg["session"]), int(msg.get("rate", 48000))
        if session != self.session or rate != self.rate:
            self.ring = Ring(rate)
            self.epoch = None
            self.clock = ClockSync()
            self.lat_eff = None
        self.rate = rate
        self.room_id = str(msg["room"])
        self.session = session
        self.latency = clamp_latency(msg.get("latency", AUTO_START_MS)) / 1000.0
        if self.lat_eff is None:
            self.lat_eff = self.latency
        self.codec_name = str(msg.get("codec", "pcm24"))
        self.codec = Codec(self.codec_name, rate)
        self.host_name = str(msg.get("host") or self.host_name)
        self.group = str(msg.get("group") or self.group or "")

    def ensure_player(self) -> None:
        p = self.player
        if p is not None and p.rate != self.rate:
            p.stop()
            p = None
        if p is None:
            self.player = Player(self, self.devices, self.offsets, self.rate)
            self.player.start()

    # -- audio path -------------------------------------------------------- #
    def _eff_latency(self) -> float:
        now = time.perf_counter()
        e, tgt = self.lat_eff, self.latency
        glide = 0.0
        if e is None:
            e = tgt
        elif e != tgt:
            step = LAT_SLEW * max(0.0, now - self.lat_t)
            if abs(tgt - e) <= step:
                e = tgt
            else:
                e, glide = (e + step, LAT_SLEW) if tgt > e else (e - step, -LAT_SLEW)
        self.lat_eff, self.lat_t, self.glide = e, now, glide
        return e

    def pos_for(self, t_local: float) -> Optional[float]:
        off, ep = self.clock.offset, self.epoch
        if off is None or ep is None:
            return None
        return (t_local + off - self._eff_latency() - ep) * self.rate

    def on_audio(self, payload: bytes) -> None:
        _, pos, ts_us, n = AUDIO_HDR.unpack_from(payload)
        x = self.codec.decode(payload[AUDIO_HDR.size:], n)
        ts = ts_us / 1e6
        self.epoch = ts - pos / self.rate
        self.ring.write(pos, x)
        self.rx += 1
        off = self.clock.offset
        now = time.perf_counter()
        if off is not None and time.monotonic() - self.connected_at > 3:
            arrival = now + off
            playing = ts + (self.lat_eff if self.lat_eff is not None else self.latency)
            self.heads.append((now, playing - arrival, ts + self.latency - arrival))
            if playing < arrival:
                self.late += 1

    def headroom_ms(self, target: bool = False) -> Optional[float]:
        """Smallest margin between a frame arriving and it being played, last 10 s.
        target=True measures against the room's target buffer (used by auto mode)."""
        now = time.perf_counter()
        while self.heads and now - self.heads[0][0] > 10:
            self.heads.popleft()
        if not self.heads:
            return None
        i = 2 if target else 1
        return round(min(h[i] for h in self.heads) * 1000, 1)

    def _pinger(self, conn: Conn, live: threading.Event) -> None:
        n, last_stats = 0, 0.0
        try:
            while live.is_set() and not self.stop_ev.is_set():
                conn.send_frame(F_PING, PING.pack(time.perf_counter()))
                n += 1
                now = time.monotonic()
                if now - last_stats >= 1.0:
                    conn.send_json(dict(self.stats(), t="STATS"))
                    last_stats = now
                self.stop_ev.wait(0.2 if n < 20 else 0.5)
        except (OSError, ConnectionError):
            pass

    # -- stats ------------------------------------------------------------ #
    def stats(self) -> dict:
        p = self.player
        dev = p.outs[0] if p and p.outs else None
        return {"rtt": round((self.clock.rtt or 0) * 1000, 2), "head": self.headroom_ms(),
                "head_tgt": self.headroom_ms(target=True),
                "gaps": p.gaps() if p else 0, "late": self.late,
                "sync": round(dev.err_f / self.rate * 1000, 2) if dev else None,
                "lvl": round(p.meter.level, 3) if p else 0.0, "dev": len(p.outs) if p else 0}

    def status(self) -> dict:
        p = self.player if self.state in ("live", "reconnecting") else None
        buffered = 0.0
        if p and p.outs and p.outs[0].rpos is not None:
            buffered = max(0.0, (self.ring.high - p.outs[0].rpos) / self.rate * 1000)
        d = {"state": self.state, "note": self.note, "host": self.host_name, "host_ip": self.host_ip,
             "group": self.group or "", "room": self.room_id, "token": self.token is not None,
             "latency": int(self.latency * 1000), "lat_eff": int((self.lat_eff or self.latency) * 1000),
             "codec": self.codec_name, "label": codec_label(self.codec_name, self.rate),
             "since": self.since, "volume": self.volume, "buffered": round(buffered),
             "reconnects": self.reconnects, "offset_ms": round((self.clock.offset or 0) * 1000, 2),
             "bands": p.meter.bands() if p else [0.0] * 24,
             "devices": [o.status() for o in (p.outs if p else [])],
             "missing": list(p.missing) if p else [], "player_error": p.error if p else ""}
        d.update(self.stats())
        return d

    # -- session ---------------------------------------------------------- #
    def run_session(self, conn: Conn) -> None:
        boost_thread()
        first = self.state != "reconnecting"
        self.state, self.note = "live", ""
        self.connected_at = time.monotonic()
        if first:
            self.since = time.time()
            self.agent.event("join", f"Tuned in to '{self.group}' on {self.host_name}. Ears on, judgement off.")
        else:
            self.reconnects += 1
            self.agent.event("join", f"Back on '{self.group}'. The buffer covered for us. Nobody noticed.")
        live = threading.Event()
        live.set()
        threading.Thread(target=self._pinger, args=(conn, live), daemon=True).start()
        last_rx = time.monotonic()
        try:
            while not self.stop_ev.is_set():
                fr = conn.recv_frame(0.5)
                if fr is None:
                    if time.monotonic() - last_rx > PEER_TIMEOUT_S:
                        raise ConnectionError("host went quiet")
                    continue
                last_rx = time.monotonic()
                kind, payload = fr
                if kind == F_AUDIO:
                    self.on_audio(payload)
                elif kind == F_PONG and len(payload) == PONG.size:
                    self.clock.on_pong(*PONG.unpack(payload))
                elif kind == F_JSON:
                    msg = json.loads(payload)
                    t = msg.get("t")
                    if t in ("HB", "LAT"):
                        ms = msg.get("lat", msg.get("ms"))
                        if ms is not None:
                            self.latency = clamp_latency(ms) / 1000.0
                    elif t == "KICK":
                        self.kicked = True
                        self.state = "kicked"
                        self.agent.event("kick", f"{self.host_name} kicked us out. Rude, but noted.")
                        return
                    elif t == "STOP":
                        self.state = "ended"
                        self.agent.event("info", f"{self.host_name} ended '{self.group}'. Show's over.")
                        return
            if self.left:
                try:
                    conn.send_json({"t": "BYE"})
                except OSError:
                    pass
                self.state = "left"
        except (OSError, ConnectionError, ValueError):
            if self.state == "live":
                self.state = "reconnecting"
                self.agent.event("warn", f"Link to {self.host_name} blinked. Playing from the buffer while we reconnect.")
            raise
        finally:
            live.clear()
            conn.close()

    def _connect(self) -> Tuple[Conn, dict]:
        err: Optional[Exception] = None
        for ip in list(self.hosts):
            if self.stop_ev.is_set():
                break
            try:
                conn = dial(ip, self.host_port, 2.0)
            except OSError as e:
                err = e
                continue
            try:
                conn.send(self.agent.hello())
                h = conn.recv(5)
                if not h or h.get("t") != "HELLO":
                    raise ConnectionError("not a lanaudio agent")
            except (OSError, ConnectionError) as e:
                conn.close()
                err = e
                continue
            self.host_ip = ip
            self.hosts.remove(ip)
            self.hosts.insert(0, ip)
            self.host_name = str(h.get("name") or self.host_name)
            return conn, h
        raise err or ConnectionError("no host address")

    def join_loop(self) -> None:
        try:
            self._join_loop()
        finally:
            if self.player is not None:
                self.player.stop()

    def _join_loop(self) -> None:
        backoff = 0.3
        while not self.stop_ev.is_set():
            conn = None
            try:
                conn, h = self._connect()
                msg = {"t": "JOIN", "port": self.agent.port, "group": self.group, "opus": opus_available(),
                       "session": self.session, "have": self.ring.high if self.session else None}
                if self.token:
                    msg.update(mid=self.token["id"], proof=proof(self.token["secret"], str(h.get("nonce", ""))))
                conn.send(msg)
                while True:
                    rep = conn.recv(15)
                    if rep is None:
                        raise ConnectionError("host didn't answer")
                    if rep.get("t") != "WAIT":
                        break
                    self.state, self.note = "approval", ""
                    self.group = str(rep.get("group") or self.group or "")
                    if self.stop_ev.is_set():
                        raise ConnectionError("left")
                if self.stop_ev.is_set():
                    conn.close()
                    return
                if rep.get("t") == "REJECT":
                    conn.close()
                    reason = str(rep.get("reason", "rejected"))
                    if reason == "not live":
                        if self.state != "reconnecting" or self.ring.high == 0:
                            self.state, self.note = "waiting", ""
                        self.stop_ev.wait(2)
                        backoff = 0.3
                        continue
                    if reason == "pick a group":
                        reason = "the host runs several rooms; add --group " + "/".join(rep.get("groups", []))
                    self.state, self.note = "rejected", reason
                    return
                if rep.get("t") != "ACCEPT":
                    raise ConnectionError("unexpected reply")
                self.configure(rep)
                self.ensure_player()
                backoff = 0.3
                self.run_session(conn)
                conn = None
            except (OSError, ConnectionError, ValueError, KeyError, TypeError) as e:
                if conn:
                    conn.close()
                self.note = short_err(e)
                if self.state in ("connecting", "live", "approval"):
                    self.state = "reconnecting"
            if self.left or self.kicked or self.state in ("ended", "rejected", "left", "kicked"):
                return
            self.stop_ev.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    def leave(self) -> None:
        self.left = True
        if self.state != "live":
            self.state = "left"
        self.stop_ev.set()


# --------------------------------------------------------------------------- #
# Agent: control server (one TCP port for everything) + local API for the CLI
# --------------------------------------------------------------------------- #
class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.server.fn(self.request, self.client_address)
        except Exception:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr, fn):
        self.fn = fn
        super().__init__(addr, _Handler)


class Agent:
    def __init__(self, port: int, name: Optional[str] = None):
        cfg = load_cfg()
        if name:
            cfg["name"] = name
            save_cfg(cfg)
        self.name = cfg["name"]
        self.id = cfg["agent_id"]
        self.port, self.api_port = port, port - 1
        self.events: deque = deque(maxlen=200)
        self.rooms: Dict[str, Room] = {}
        self.member: Optional[Membership] = None
        self.hub = CaptureHub(self)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.started = time.time()
        self.ip = local_ip()
        self.ctl = self.api = None
        self.serving = False
        self.tuning: List[str] = []

    def event(self, kind: str, text: str) -> None:
        self.events.append((time.time(), kind, text))

    def hello(self) -> dict:
        return {"t": "HELLO", "v": VERSION, "name": self.name, "id": self.id, "port": self.port}

    def live_rooms(self) -> List[Room]:
        return [r for r in list(self.rooms.values()) if r.active]

    def role(self) -> str:
        if self.live_rooms():
            return "host"
        if self.member is not None and self.member.alive():
            return "listener"
        return "idle"

    def start(self) -> None:
        try:
            self.ctl = _Server(("0.0.0.0", self.port), self._peer)
            self.api = _Server(("127.0.0.1", self.api_port), self._api)
        except OSError:
            self.stop()
            raise
        for target in (self.ctl.serve_forever, self.api.serve_forever):
            threading.Thread(target=target, daemon=True).start()
        self.serving = True
        self.tuning = tune_process()
        self.event("info", f"Agent online at {self.ip}:{self.port}. Looking cute, sounding cuter.")

    def stop(self) -> None:
        self.stopping.set()
        try:
            for room in self.live_rooms():
                room.stop()
            if self.member is not None and self.member.alive():
                self.member.leave()
                time.sleep(0.3)
        except Exception:
            pass
        for srv in (self.ctl, self.api):
            if srv is not None:
                try:
                    if self.serving:
                        srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass

    # -- peers ------------------------------------------------------------ #
    def _identify(self, hello: dict, msg: dict, nonce: str, ip: str) -> Tuple[Optional[dict], str, str]:
        group = str(msg.get("group") or "")
        mid = msg.get("mid")
        if mid:
            for m in load_cfg()["groups"].get(group, []):
                if m.get("id") == mid and hmac.compare_digest(str(msg.get("proof", "")),
                                                              proof(m["secret"], nonce)):
                    return {"key": "t:" + m["id"], "name": m["name"], "token": True}, group, ""
            return None, group, "token revoked"
        port = int(msg.get("port", PEER_DEFAULT_PORT))
        return {"key": f"{ip}:{port}", "name": str(hello.get("name") or ip), "token": False}, group, ""

    def _peer(self, sock, addr) -> None:
        ip = addr[0]
        conn = Conn(sock)
        try:
            hello = conn.recv(5)
            if not hello or hello.get("t") != "HELLO":
                return
            nonce = secrets.token_hex(8)
            conn.send(dict(self.hello(), nonce=nonce, role=self.role(),
                           rooms=[r.group for r in self.live_rooms()]))
            msg = conn.recv(10)
            if msg is None:
                return
            t = msg.get("t")
            if t not in ("JOIN", "INFO"):
                return
            ident, group, err = self._identify(hello, msg, nonce, ip)
            if err:
                conn.send({"t": "REJECT", "reason": err})
                return
            if t == "INFO":
                if not ident["token"]:
                    conn.send({"t": "REJECT", "reason": "token needed"})
                    return
                room = self.rooms.get(group)
                live = room is not None and room.active
                conn.send({"t": "INFO", "group": group, "host": self.name, "you": ident["name"], "live": live,
                           "latency": room.latency if live else None,
                           "label": codec_label(room.codec, room.rate) if live else None,
                           "here": room.live_count() if live else 0,
                           "total": len(load_cfg()["groups"].get(group, []))})
                return
            rooms = self.live_rooms()
            if group:
                room = self.rooms.get(group)
            elif len(rooms) == 1:
                room = rooms[0]
            elif rooms:
                conn.send({"t": "REJECT", "reason": "pick a group", "groups": [r.group for r in rooms]})
                return
            else:
                room = None
            if room is None or not room.active:
                conn.send({"t": "REJECT", "reason": "not live"})
                return
            room.handle_join(conn, ident, ip, msg)
        except (OSError, ConnectionError, ValueError, KeyError, TypeError):
            pass
        finally:
            conn.close()

    # -- local API -------------------------------------------------------- #
    def _api(self, sock, addr) -> None:
        conn = Conn(sock)
        try:
            req = conn.recv(5)
            if not req:
                return
            try:
                res = self._dispatch(req)
            except Exception as e:
                res = {"ok": False, "error": short_err(e)}
            conn.send(res)
        finally:
            conn.close()

    def _pick_rooms(self, group: Optional[str]) -> Tuple[List[Room], str]:
        rooms = self.live_rooms()
        if not rooms:
            return [], "You're not hosting anything."
        if group:
            hit = [r for r in rooms if r.group.lower() == group.lower()]
            return (hit, "") if hit else ([], f"No live room for '{group}'. Live: {', '.join(r.group for r in rooms)}")
        return rooms, ""

    def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return dict(self.status(), ok=True)
        if cmd == "room_start":
            return self._room_start(req)
        if cmd == "room_stop":
            rooms, err = self._pick_rooms(req.get("group"))
            if err:
                return {"ok": False, "error": err}
            for r in rooms:
                r.stop()
                self.event("info", f"'{r.group}' is over. Mic dropped.")
            with self.lock:
                for r in rooms:
                    if self.rooms.get(r.group) is r:
                        del self.rooms[r.group]
            return {"ok": True, "stopped": [r.group for r in rooms], "left": [r.group for r in self.live_rooms()]}
        if cmd == "room_kick":
            rooms, err = self._pick_rooms(req.get("group"))
            if err:
                return {"ok": False, "error": err}
            for r in rooms:
                who = r.kick(str(req.get("name", "")))
                if who:
                    return {"ok": True, "name": who, "group": r.group}
            return {"ok": False, "error": f"Nobody called '{req.get('name')}' in your rooms."}
        if cmd == "room_latency":
            rooms, err = self._pick_rooms(req.get("group"))
            if err:
                return {"ok": False, "error": err}
            auto = bool(req.get("auto"))
            ms = None if auto else int(req.get("ms", AUTO_START_MS))
            vals = [r.set_latency(ms, auto=auto) for r in rooms]
            return {"ok": True, "latency": vals[0], "auto": auto, "groups": [r.group for r in rooms]}
        if cmd == "room_approve":
            allow = bool(req.get("allow", True))
            for r in sorted(self.live_rooms(), key=lambda r: min([p["since"] for p in r.pending.values()] or [1e18])):
                p = r.find_pending(req.get("name"))
                if p:
                    p["ok"] = allow
                    p["ev"].set()
                    return {"ok": True, "name": p["name"], "group": r.group, "allow": allow}
            return {"ok": False, "error": "Nobody's knocking." if not req.get("name")
                    else f"'{req.get('name')}' isn't knocking."}
        if cmd == "join":
            return self._join(req)
        if cmd == "leave":
            mem = self.member
            if mem is None or not mem.alive():
                return {"ok": False, "error": "You're not in a room."}
            name, group = mem.host_name, mem.group
            mem.leave()
            self.event("leave", f"Left '{group or name}' without saying goodbye. Iconic.")
            return {"ok": True, "host": name, "group": group}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _room_start(self, req: dict) -> dict:
        group = str(req.get("group", "")).strip()
        codec = str(req.get("codec", "pcm24"))
        lat = req.get("latency")
        with self.lock:
            if self.member is not None and self.member.alive():
                return {"ok": False, "error": f"You're in {self.member.host_name}'s room. Leave first."}
            groups = load_cfg()["groups"]
            real = next((g for g in groups if g.lower() == group.lower()), None)
            if real is None:
                return {"ok": False, "error": f"No group called '{group}'. Create it: lanaudio group create {group}"}
            if real in self.rooms and self.rooms[real].active:
                return {"ok": False, "error": f"'{real}' is already live. One stage per group."}
            if codec not in CODECS:
                return {"ok": False, "error": f"Unknown codec {codec!r}."}
            if codec == "opus" and not opus_available():
                return {"ok": False, "error": "Opus isn't available here (pip install opuslib + opus.dll)."}
            room = Room(self, real, None if lat in (None, "auto") else int(lat), codec)
            room.start()
            self.rooms[real] = room
        self.event("info", f"'{real}' is ON AIR in {codec_label(codec, room.rate)}. Try not to play anything embarrassing.")
        return {"ok": True, "room": room.id, "group": real, "source": self.hub.status()["name"],
                "label": codec_label(codec, room.rate), "latency": room.latency, "auto": room.auto,
                "roster": [m["name"] for m in groups[real]], "live": [r.group for r in self.live_rooms()]}

    def _join(self, req: dict) -> dict:
        ip = req.get("ip")
        with self.lock:
            if self.live_rooms():
                return {"ok": False, "error": "You're hosting. Can't be on stage and in the crowd."}
            cur = self.member
            if cur is not None and cur.alive():
                return {"ok": False, "error": f"Already in '{cur.group or cur.host_name}'. Leave first."}
            kw = dict(devices=req.get("devices"), offsets=req.get("offsets"), volume=req.get("volume"))
            if ip:
                port = int(req.get("port", PEER_DEFAULT_PORT))
                if ip in (self.ip, "127.0.0.1") and port == self.port:
                    return {"ok": False, "error": "That's you. Joining yourself is a whole mood, but no."}
                mem = Membership(self, [ip], port, req.get("group") or None, None, **kw)
            else:
                spec = str(req.get("group") or "")
                toks = load_cfg()["tokens"]
                g, _, h = spec.partition("@")
                cand = [t for t in toks if (not g or t["group"].lower() == g.lower())
                        and (not h or t["host_name"].lower() == h.lower())]
                if not cand:
                    if spec:
                        return {"ok": False, "error": f"You don't have a token for '{spec}'. Ask the host for one, "
                                                      "then run: lanaudio token add <TOKEN>"}
                    return {"ok": False, "error": "You haven't added any tokens yet. Ask a host for one, "
                                                  "then run: lanaudio token add <TOKEN>"}
                if len(cand) > 1:
                    return {"ok": False, "error": "Which one? " + ", ".join(token_label(t) for t in cand)}
                tok = cand[0]
                mem = Membership(self, tok["hosts"], tok["port"], tok["group"], tok, **kw)
            self.member = mem
        threading.Thread(target=mem.join_loop, daemon=True).start()
        self.event("info", f"Knocking on {mem.host_name}'s door for '{mem.group or 'their room'}'...")
        return {"ok": True, "group": mem.group, "host": mem.host_name}

    def status(self) -> dict:
        mem = self.member
        rooms = self.live_rooms()
        d = {"name": self.name, "id": self.id, "ip": self.ip, "port": self.port, "version": VERSION,
             "uptime": time.time() - self.started, "role": self.role(), "rooms": [], "source": None,
             "member": None, "tuning": self.tuning, "events": [list(e) for e in list(self.events)[-12:]]}
        try:
            d["rooms"] = [r.status() for r in rooms]
            if rooms:
                d["source"] = self.hub.status()
            if mem is not None:
                d["member"] = mem.status()
        except Exception as e:
            d["error"] = short_err(e)
        return d


# --------------------------------------------------------------------------- #
# FX: the drama department
# --------------------------------------------------------------------------- #
console = Console(highlight=False)
ANIM = True
PROG = "lanaudio"
LOGO = "LAN-AUDIO"

SASS = {
    "serve": [
        "Ears on. Drama off.",
        "Your LAN just got a sound system. You're welcome.",
        "Listening harder than a manager in a 1:1.",
        "Lossless audio, flawless attitude.",
        "Packets dressed to impress. None left behind.",
        "Zero cables were harmed in this broadcast.",
        "Standing by like a very patient DJ.",
        "Ready when you are. Fashionably idle until then.",
    ],
    "host": ["You're ON AIR. The whole LAN is listening.", "Main character energy: detected.",
             "Broadcasting in 24-bit. Please act natural."],
    "listen": ["Vibing on someone else's playlist. Respectfully.", "Ears tuned. Judgement suspended.",
               "Somebody else is DJing. Relax."],
    "group_create": ["A new squad has entered the chat.", "Squad assembled. Matching jackets optional.",
                     "Fresh group, zero drama. For now."],
    "group_add": ["{name} is on the guest list. Velvet rope lifted.", "{name} got VIP access. Act natural.",
                  "{name} is in. Someone tell the bouncer."],
    "group_remove": ["{name} has been yeeted from {group}. Token shredded. No refunds.",
                     "{name} is out. Their pass just turned into a pumpkin.",
                     "{name} left the group chat. Spiritually and cryptographically."],
    "group_delete": ["'{group}' has been disbanded. Creative differences.",
                     "'{group}' is no more. The reunion tour is not confirmed."],
    "token_add": ["Pass saved. You never have to do this again. Iconic.",
                  "VIP pass stored. The bouncer knows your face now.",
                  "Token locked in. Main-character access unlocked."],
    "ping_fast": ["{ms} ms. Fast. Suspiciously fast.", "{ms} ms. Blink and you missed it.",
                  "{ms} ms. That packet had somewhere to be."],
    "ping_ok": ["{ms} ms. Respectable. We'll allow it.", "{ms} ms. Not bad for a LAN on a weekday."],
    "ping_slow": ["{ms} ms. Did the packet take the stairs?", "{ms} ms. Wi-Fi is having a moment."],
    "room_start": ["You're ON AIR. Try not to play anything embarrassing.",
                   "The stage is yours. Don't trip.", "Broadcast is live. No pressure. Okay, some pressure."],
    "room_stop": ["Mic dropped. Crowd stunned.", "That's a wrap. Tell your friends.",
                  "Lights off. Legends never stay too long."],
    "join": ["You're in. Ears ready, judgement suspended.", "Welcome to the party. Snacks not included.",
             "Synced and slaying."],
    "leave": ["Left without saying goodbye. Iconic.", "Irish exit executed flawlessly.",
              "You're out. The room will cope. Probably."],
    "kick": ["{name} has been escorted out by security.", "{name} got the boot. Fashionably.",
             "Bye, {name}. Don't let the packets hit you on the way out."],
    "let_in": ["{name} is in. The velvet rope has spoken.", "Door's open, {name}. Wipe your feet.",
               "{name} made the cut. Barely."],
    "deny": ["{name} got the hand. Not tonight.", "Velvet rope: 1. {name}: 0.",
             "{name} has been politely, devastatingly declined."],
    "devices": ["Found your speakers. They were hiding in the settings.", "Audio gear located. Looking good."],
    "doctor_ok": ["Clean bill of health. Go make some noise.", "Everything checks out. Suspiciously healthy."],
    "doctor_bad": ["A few things need love. Fix them and come back.", "Diagnosis: fixable drama."],
    "error": ["Well, that's awkward.", "Not today, sweetie.", "Something broke, and it wasn't my heart."],
}


def sass(key: str, **kw) -> str:
    return random.choice(SASS[key]).format(**kw)


def hexc(h: float, s: float = 0.75, v: float = 1.0) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def neon(phase: float, v: float = 1.0) -> str:
    return hexc(0.72 + 0.23 * math.sin(phase), 0.75, v)


PINK, CYAN, LIME, GOLD, RED, GREY = "#ff4fd8", "#3ef2ff", "#39ff14", "#ffd166", "#ff2a6d", "#8a8a9a"


def gradient(s: str, t: float = 0.0, speed: float = 2.0, spread: float = 0.18, bold: bool = True) -> Text:
    out = Text()
    for i, ch in enumerate(s):
        out.append(ch, style=("bold " if bold else "") + neon(i * spread - t * speed))
    return out


FONT = {
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "B": ["████ ", "█   █", "████ ", "█   █", "████ "],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "I": ["███", " █ ", " █ ", " █ ", "███"],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "U": ["█   █", "█   █", "█   █", "█   █", " ███ "],
    "Y": ["█   █", " █ █ ", "  █  ", "  █  ", "  █  "],
    "1": [" █ ", "██ ", " █ ", " █ ", "███"],
    "2": ["████ ", "    █", " ███ ", "█    ", "█████"],
    "3": ["████ ", "    █", " ███ ", "    █", "████ "],
    "-": ["    ", "    ", "████", "    ", "    "],
    "!": ["█", "█", "█", " ", "█"],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def big_rows(word: str) -> List[str]:
    rows = [""] * 5
    for i, ch in enumerate(word.upper()):
        g = FONT.get(ch, FONT[" "])
        for r in range(5):
            rows[r] += ("" if i == 0 else " ") + g[r]
    return rows


def big(word: str, t: float, speed: float = 2.0, shine: bool = True, color: Optional[str] = None) -> Text:
    rows = big_rows(word)
    w = len(rows[0])
    sx = (t * 32) % (w + 30) - 15
    out = Text()
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == " ":
                out.append(" ")
            elif shine and abs(c - sx - r) < 1.5:
                out.append(ch, style="bold #ffffff")
            else:
                out.append(ch, style="bold " + (color or neon(c * 0.12 + r * 0.25 - t * speed)))
        if r < 4:
            out.append("\n")
    return out


BLOCKS = " ▁▂▃▄▅▆▇█"


def wave(w: int, t: float, energy: float = 0.6) -> Text:
    out = Text()
    for x in range(w):
        v = (math.sin(x * 0.35 - t * 4) * math.sin(x * 0.11 + t * 1.3) + 1) / 2
        v = 0.15 + v * energy * 0.85
        out.append(BLOCKS[1 + min(7, int(v * 7.99))], style=neon(x * 0.06 - t * 1.5))
    return out


def ripple(w: int, h: int, t: float, energy: float = 0.5, core: str = "◉", core_style: str = "bold #ffffff") -> Text:
    cx, cy = (w - 1) / 2, (h - 1) / 2
    rmax = max(cx / 2.1, cy) + 1
    out = Text()
    for y in range(h):
        for x in range(w):
            d = math.hypot((x - cx) / 2.1, y - cy)
            if x == int(round(cx)) and y == int(round(cy)):
                out.append(core, style=core_style)
                continue
            if d < 0.9:
                out.append(" ")
                continue
            k = (math.cos(d * 1.1 - t * (2.5 + 5 * energy)) + 1) / 2 * max(0.0, 1 - d / rmax)
            ch = "●" if k > 0.62 else "•" if k > 0.42 else "·" if k > 0.22 else " "
            out.append(ch, style=neon(d * 0.4 - t * 1.5, 0.55 + 0.45 * k))
        if y < h - 1:
            out.append("\n")
    return out


def fake_bands(t: float, n: int = 24, energy: float = 0.35) -> List[float]:
    return [max(0.02, energy * (0.5 + 0.5 * math.sin(t * 2.2 + i * 0.55)) * (0.6 + 0.4 * math.sin(t * 0.7 + i)))
            for i in range(n)]


def spectrum(bands: List[float], height: int, t: float, bar_w: int = 2, gap: int = 1) -> Text:
    idle = fake_bands(t, len(bands), 0.12)
    bands = [max(float(v), i) for v, i in zip(bands, idle)]
    out = Text()
    for row in range(height - 1, -1, -1):
        for i, v in enumerate(bands):
            fill = max(0.0, min(1.0, v * height - row))
            ch = BLOCKS[int(fill * 8)] if fill < 1 else "█"
            out.append(ch * bar_w, style=hexc(0.5 + 0.45 * (row / max(1, height - 1)) + 0.02 * math.sin(i * 0.4 + t), 0.8))
            out.append(" " * gap)
        if row:
            out.append("\n")
    return out


def meter_bar(level: float, w: int = 8) -> Text:
    n = int(max(0.0, min(1.0, level)) * w + 0.5)
    out = Text()
    for i in range(w):
        out.append("▮" if i < n else "▯", style=neon(i * 0.25) if i < n else "#3a3a4a")
    return out


def buffer_bar(ms: float, target: float, w: int = 14) -> Text:
    """How full the playout buffer is, against the room latency."""
    k = int(max(0.0, min(1.0, ms / max(1.0, target))) * w + 0.5)
    color = LIME if ms > 0.5 * target else GOLD if ms > 0.25 * target else RED
    out = Text()
    for i in range(w):
        out.append("█" if i < k else "░", style=color if i < k else "#3a3a4a")
    return out


def typewriter(s: str, t: float, cps: float = 30) -> Text:
    k = int(t * cps)
    out = Text(s[:k], style="italic #ff9de2")
    if k < len(s) or int(t * 3) % 2 == 0:
        out.append("▌", style="#ff9de2")
    return out


def hhmmss(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def play(render: Callable[[float], object], duration: float, fps: int = 30, keep: bool = True) -> None:
    if not ANIM:
        if keep:
            console.print(render(duration))
        return
    start = time.perf_counter()
    with Live(render(0.0), console=console, auto_refresh=False, transient=not keep) as live:
        while True:
            t = time.perf_counter() - start
            if t >= duration:
                live.update(render(duration), refresh=True)
                break
            live.update(render(t), refresh=True)
            time.sleep(1.0 / fps)


def card(title: str, body, quip: Optional[str] = None, border: str = "#a06cff") -> None:
    parts = [body if not isinstance(body, str) else Text(body)]
    if quip:
        parts += [Text(""), Text(quip, style="italic #ff9de2")]
    console.print(Panel(Group(*parts), title=gradient(f" {title} ", 0.0), title_align="left",
                        border_style=border, padding=(1, 2), expand=False))


def fail(title: str, msg: str, hint: Optional[str] = None) -> int:
    junk = "▓▒░#@$%&?!"
    quip = sass("error")

    def frame(t: float):
        k = max(0.0, 1 - t / 0.55)
        ttl = "".join(random.choice(junk) if (c != " " and random.random() < 0.6 * k) else c for c in title)
        body = [Text(msg, style="#ffd9e2")]
        if hint:
            body += [Text(""), Text(hint, style="#c9c9d6")]
        body += [Text(""), Text(quip, style="italic #ff9de2")]
        p = Panel(Group(*body), title=Text(f" ✕ {ttl} ", style="bold " + (RED if int(t * 20) % 2 else PINK)),
                  title_align="left", border_style=RED if k == 0 or int(t * 20) % 2 else PINK,
                  padding=(1, 2), expand=False)
        return Padding(p, (0, 0, 0, random.choice((0, 1, 2)) if k > 0 else 0))

    play(frame, 0.65, fps=30)
    return 1


class Keys:
    """Non-blocking single-key reader (Windows console or POSIX tty)."""

    def __enter__(self):
        self.old = None
        self.pending: List[str] = []
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def get(self) -> Optional[str]:
        if os.name == "nt":
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    return None
                return ch
            return None
        if self.old is None:
            return None
        if not self.pending:
            r, _, _ = select.select([self.fd], [], [], 0)
            if r:
                self.pending = list(os.read(self.fd, 64).decode(errors="ignore"))
        return self.pending.pop(0) if self.pending else None


# -- little one-shot animations -------------------------------------------- #
def anim_scan(label: str, duration: float = 0.9) -> None:
    w = 34

    def frame(t):
        x = (t / duration) * (w + 6) - 3
        bar = Text()
        for i in range(w):
            d = x - i
            if 0 <= d < 1:
                bar.append("▌", style="bold #ffffff")
            elif 0 < d < 7:
                bar.append("▌", style=neon(i * 0.2, 1 - d / 8))
            else:
                bar.append("·", style="#3a3a4a")
        return Group(gradient(label, t), bar)

    play(frame, duration, keep=False)


def anim_blocks(label: str, duration: float = 0.8) -> None:
    n = 18

    def frame(t):
        k = int(min(1.0, t / duration) * n)
        bar = Text()
        for i in range(n):
            bar.append("▰" if i < k else "▱", style=neon(i * 0.3 - t * 3) if i < k else "#3a3a4a")
        return Group(gradient(label, t), bar)

    play(frame, duration, keep=False)


def anim_ticket(label: str, name: str, group: str, secret: str, duration: float = 1.5) -> None:
    code = secret[:30]
    junk = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&@"

    def frame(t):
        p = min(1.0, t / (duration * 0.8))
        k = int(p * len(code))
        txt = Text()
        for i, ch in enumerate(code):
            if i < k:
                txt.append(ch, style="bold " + neon(i * 0.25 - t * 3))
            else:
                txt.append(random.choice(junk), style="#5a5a6a")
        txt.append("…", style=GREY)
        body = Group(Text.assemble(("🎟  VIP PASS  ", "bold " + GOLD), ("admit one", "italic " + GREY)),
                     Text.assemble(("holder  ", GREY), (name, "bold #ffffff")),
                     Text.assemble(("group   ", GREY), (group, "bold " + PINK)), txt)
        sparkle = Text("".join(random.choice(" ·✦✧") if random.random() < 0.2 else " " for _ in range(44)), style=GOLD)
        return Group(gradient(label, t), sparkle,
                     Panel(body, border_style=neon(t * 4), box=box.DOUBLE, expand=False, padding=(0, 2)))

    play(frame, duration, keep=False)


def anim_carpet(name: str, duration: float = 1.0) -> None:
    w = 36

    def frame(t):
        p = min(1.0, t / (duration * 0.75))
        x = int((1 - (1 - p) ** 3) * (w - len(name) - 4))
        line = Text()
        line.append("✦ ", style=GOLD if int(t * 10) % 2 else PINK)
        line.append("═" * x, style="#ff2a6d")
        line.append(f" {name} ", style="bold #ffffff on #ff2a6d")
        sparkle = Text("".join(random.choice(" ·✦✧*") if random.random() < 0.25 else " " for _ in range(w)),
                       style=GOLD)
        return Group(gradient("Rolling out the red carpet...", t), sparkle, line)

    play(frame, duration, keep=False)


def anim_yeet(name: str, label: str = "Yeeting in progress...", duration: float = 0.9) -> None:
    def frame(t):
        p = min(1.0, t / duration)
        x = int(p ** 2 * 40)
        shade = int(255 * (1 - p * 0.8))
        line = Text(" " * x)
        line.append(name, style=f"bold #{shade:02x}{shade:02x}{shade:02x}")
        line.append("  ~>", style=PINK)
        return Group(gradient(label, t), line)

    play(frame, duration, keep=False)


def anim_ping(ip: str, duration: float = 1.3) -> None:
    w = 26

    def frame(t):
        p = (t / duration) * 2
        pos = int((p if p <= 1 else 2 - p) * (w - 1))
        track = Text()
        track.append("YOU ", style="bold " + CYAN)
        track.append("●", style=CYAN)
        for i in range(w):
            track.append("◆" if i == pos else "─", style=("bold " + PINK) if i == pos else "#4a4a5a")
        track.append("●", style=PINK)
        track.append(f" {ip}", style="bold " + PINK)
        return Group(gradient("ping..." if p <= 1 else "...pong!", t), track)

    play(frame, duration, keep=False)


def anim_countdown(source: str) -> None:
    def frame(t):
        if t < 1.05:
            n = 3 - int(t / 0.35)
            flash = (t % 0.35) < 0.08
            body = big(str(n), t, color="#ffffff" if flash else None, shine=False)
            sub = Text("warming up the airwaves", style="dim")
        else:
            body = big("ON AIR", t, color=RED if int(t * 8) % 2 else "#ffffff", shine=False)
            sub = Text(f"broadcasting from: {source}", style="bold " + PINK)
        return Align.center(Group(Text(""), Align.center(body), Text(""), Align.center(sub)))

    play(frame, 1.9, keep=False)


def anim_broadcast(group: str, names: List[str], duration: float = 1.2) -> None:
    w = 22

    def frame(t):
        rows = [gradient(f"Beaming the news to the '{group}' squad...", t)]
        for i, n in enumerate(names[:12]):
            p = max(0.0, min(1.0, (t - i * 0.08) / 0.7))
            pos = int(p * (w - 1))
            line = Text(f"{'ON AIR':>8} ", style="bold " + RED)
            for k in range(w):
                line.append("◆" if (k == pos and p < 1) else "─", style=PINK if k == pos else "#4a4a5a")
            line.append("► ", style=PINK)
            line.append(n, style="bold #ffffff" if p >= 1 else "dim")
            if p >= 1:
                line.append("  ✓ can join", style=LIME)
            rows.append(line)
        if not names:
            rows.append(Text("  (nobody has a token yet, guests can still knock)", style=GREY))
        return Group(*rows)

    play(frame, duration, keep=False)


def anim_confetti(msg: str, duration: float = 1.0) -> None:
    w, h = 46, 6
    rnd = random.Random(7)
    bits = [(rnd.uniform(0, w), rnd.uniform(-h, 0), rnd.uniform(4, 9), rnd.choice("✦✧•*+◆"),
             rnd.random()) for _ in range(46)]

    def frame(t):
        grid = [[" "] * w for _ in range(h)]
        cols = [[None] * w for _ in range(h)]
        for x, y0, sp, ch, hue in bits:
            y = int(y0 + t * sp)
            xi = int(x + math.sin(t * 5 + hue * 6) * 1.5) % w
            if 0 <= y < h:
                grid[y][xi] = ch
                cols[y][xi] = hexc(hue)
        out = Text()
        for y in range(h):
            for x in range(w):
                out.append(grid[y][x], style=cols[y][x] or "")
            out.append("\n")
        out.append_text(gradient(f"   {msg}", t))
        return out

    play(frame, duration, keep=False)


def anim_mic_drop(duration: float = 1.1) -> None:
    def frame(t):
        y = int(min(1.0, t / 0.45) ** 2 * 4)
        rows = [Text("") for _ in range(5)]
        if t < 0.5:
            rows[y] = Text("      🎤", style="bold")
        else:
            shake = " " * random.choice((4, 5, 6)) if t < 0.8 else "     "
            rows[4] = Text(shake + "💥 ", style="bold").append(
                "MIC DROP", style="bold " + (RED if int(t * 12) % 2 else GOLD))
        return Group(*rows)

    play(frame, duration, keep=False)


def anim_walk_out(label: str, duration: float = 1.1) -> None:
    def frame(t):
        x = int(min(1.0, t / duration) * 34)
        return Group(gradient(label, t), Text("🚪" + " " * x + "🚶", style="bold"))

    play(frame, duration, keep=False)


def anim_boot(name: str, duration: float = 1.1) -> None:
    def frame(t):
        if t < 0.45:
            x = int(t / 0.45 * 14)
            line = Text(" " * x + "🥾" + " " * (14 - x) + " " + name, style="bold")
        else:
            p = (t - 0.45) / (duration - 0.45)
            line = Text(" " * 15 + "🥾" + " " * int(1 + p * 30))
            line.append(name, style="bold " + (RED if int(t * 14) % 2 else "#ffffff"))
        return Group(gradient("Bouncer mode: engaged", t), line)

    play(frame, duration, keep=False)


def anim_rope(name: str, allow: bool, duration: float = 1.0) -> None:
    def frame(t):
        p = min(1.0, t / (duration * 0.7))
        gap = int(p * 12) if allow else int((1 - p) * 12)
        rope = Text("▐█", style=GOLD)
        rope.append("~" * max(0, 12 - gap), style=RED)
        rope.append(" " * gap)
        rope.append("▐█", style=GOLD)
        who = Text(("  " + " " * int(p * 10) if allow else "  ") + name,
                   style="bold " + (LIME if allow else GREY))
        label = "Lifting the velvet rope..." if allow else "Rope stays up..."
        return Group(gradient(label, t), rope, who)

    play(frame, duration, keep=False)


def anim_buffer_fill(label: str, duration: float = 1.2) -> None:
    """A tank filling up: the deep buffer arming itself."""
    w = 30

    def frame(t):
        p = min(1.0, t / (duration * 0.85))
        k = int(p * w)
        bar = Text("▕", style=GREY)
        for i in range(w):
            bar.append("█" if i < k else "░", style=neon(i * 0.15 - t * 3) if i < k else "#3a3a4a")
        bar.append("▏", style=GREY)
        bar.append(f" {int(p * 100):3d}%", style="bold " + (LIME if p >= 1 else GOLD))
        return Group(gradient(label, t), bar)

    play(frame, duration, keep=False)


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #
class NoAgent(Exception):
    pass


def api(agent_port: int, cmd: str, timeout: float = 10.0, **kw) -> dict:
    try:
        s = socket.create_connection(("127.0.0.1", agent_port - 1), timeout=2.0)
    except OSError:
        raise NoAgent()
    c = Conn(s)
    try:
        c.send(dict(kw, cmd=cmd))
        r = c.recv(timeout)
    except (OSError, ConnectionError):
        raise NoAgent()
    finally:
        c.close()
    if r is None:
        raise NoAgent()
    return r


def no_agent(port: int) -> int:
    return fail("Agent not running",
                f"Nothing is serving on port {port} on this PC.",
                f"Open a terminal, run `{PROG} serve`, keep it open, then try again in another terminal.")


STATE_LOOK = {
    "live": ("●", LIME, "vibing"),
    "joining": ("◌", GOLD, "joining"),
    "away": ("○", GREY, "not here yet"),
    "lost": ("↻", GOLD, "reconnecting"),
    "left": ("←", GREY, "left"),
    "kicked": ("✕", RED, "kicked"),
    "ended": ("■", GREY, "ended"),
}
EVENT_ICON = {"info": ("✦", CYAN), "join": ("+", LIME), "leave": ("←", GREY),
              "kick": ("✕", RED), "warn": ("!", GOLD), "knock": ("♪", GOLD)}
SPIN = "◐◓◑◒"


def fmt_ms(v, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f} ms"


def head_text(v) -> Text:
    if v is None:
        return Text("—", style=GREY)
    color = LIME if v >= 150 else GOLD if v >= HEADROOM_LOW_MS else RED
    return Text(f"{v:.0f} ms", style=color)


def gaps_text(v) -> Text:
    if v is None:
        return Text("—", style=GREY)
    return Text(str(v), style=LIME if v == 0 else GOLD if v < 5 else RED)


def latency_text(ms: int, auto: bool) -> str:
    return f"{'auto ' if auto else ''}{ms} ms buffer"


def events_panel(events: list, t: float, n: int = 5, width: Optional[int] = None) -> Panel:
    ev = Text()
    rows = events[-n:]
    now = time.time()
    for i, (ts, kind, text) in enumerate(rows):
        icon, color = EVENT_ICON.get(kind, ("•", GREY))
        fresh = now - ts < 3
        ev.append(time.strftime("%H:%M:%S", time.localtime(ts)) + "  ", style=GREY)
        if fresh:
            ev.append("✨" if int(t * 6) % 2 else "✦ ", style="bold " + GOLD)
        ev.append(icon + " ", style="bold " + color)
        ev.append(text, style=("bold " + neon(t * 3)) if fresh else "#e6e6f0")
        if i < len(rows) - 1:
            ev.append("\n")
    if not rows:
        ev.append("quiet... for now", style=GREY)
    return Panel(ev, title=Text(" activity ", style="bold " + CYAN), title_align="left",
                 border_style="#3a3a5a", width=width)


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #
def render_serve(agent: Agent, t: float):
    st = agent.status()
    role = st["role"]
    h = console.size.height
    blink = int(t * 2) % 2 == 0
    energy = {"idle": 0.15, "host": 1.0, "listener": 0.7}[role]

    info = Table.grid(padding=(0, 2))
    info.add_column(style=GREY, justify="right")
    info.add_column()
    head = Text("● " if blink else "○ ", style="bold " + LIME)
    head.append("CONNECTED TO LAN-AUDIO", style="bold " + LIME)
    info.add_row("", head)
    info.add_row("you", Text(st["name"], style="bold #ffffff"))
    info.add_row("address", Text(f"{st['ip']}:{st['port']}", style=CYAN))
    if role == "host":
        rooms = st["rooms"]
        live = sum(m["state"] == "live" for r in rooms for m in r["members"])
        knocks = sum(len(r["pending"]) for r in rooms)
        mode = Text("● ON AIR", style="bold " + (RED if blink else "#ffffff"))
        mode.append(f"  {', '.join(r['group'] for r in rooms)} · {live} vibing", style=PINK)
        if knocks:
            mode.append(f" · {knocks} knocking!", style="bold " + GOLD)
        lines = SASS["host"]
    elif role == "listener" and st["member"]:
        m = st["member"]
        mode = Text("♫ LISTENING" if m["state"] in ("live", "reconnecting") else "◌ STANDING BY", style="bold " + CYAN)
        mode.append(f"  {m['group'] or 'room'} @ {m['host']}", style=PINK)
        lines = SASS["listen"]
    else:
        mode = Text("idle", style=GOLD)
        mode.append(" · waiting for drama", style=GREY)
        lines = SASS["serve"]
    info.add_row("mode", mode)
    info.add_row("stream", Text("TCP · lossless · self-healing", style="#e6e6f0"))
    info.add_row("tuning", Text(", ".join(st.get("tuning") or []) or "default", style=GREY))
    info.add_row("uptime", Text(hhmmss(st["uptime"])))
    info.add_row("", typewriter(lines[int(t // 6) % len(lines)], t % 6))

    parts = [Align.center(big(LOGO, t)), Align.center(wave(51, t, 0.3 + 0.7 * energy)), Text("")]
    if h >= 24:
        core_style = "bold " + (RED if (role == "host" and blink) else "#ffffff")
        top = Table.grid(padding=(0, 3))
        top.add_row(ripple(29, 9 if h >= 36 else 7, t, energy, "◉", core_style), info)
        parts.append(Align.center(top))
    else:
        parts.append(Align.center(info))

    rows = [(f"{PROG} room start <group>", "host a room for a group"),
            (f"{PROG} join <group>", "join a group's room (needs its token)"),
            (f"{PROG} token add <TOKEN>", "one-time setup of a pass you got"),
            (f"{PROG} groups", "which groups am I in? who's live?"),
            (f"{PROG} --help", "everything else")]
    pad = max(len(c) for c, _ in rows) + 3
    howto = Text()
    howto.append("You're connected to lan-audio ✨\n", style="bold #ffffff")
    howto.append("Please start a NEW terminal to make a room or do other stuff:\n\n", style="bold " + GOLD)
    for c, what in rows:
        howto.append("  $ ", style=PINK)
        howto.append(c.ljust(pad), style="bold " + CYAN)
        howto.append(what + "\n", style=GREY)
    howto.rstrip()
    parts.append(Align.center(Panel(howto, border_style=neon(t * 1.5), title=gradient(" next steps ", t),
                                    padding=(0, 2), expand=False)))
    if h >= 34:
        parts.append(Align.center(events_panel(st["events"], t, 5, min(100, console.size.width - 2))))
    parts.append(Align.center(Text("Ctrl+C shuts the agent down (any room ends with it)", style="dim")))
    return Group(*parts)


def cmd_serve(args) -> int:
    agent = Agent(args.port, args.name)
    try:
        agent.start()
    except OSError as e:
        return fail("Port already taken", f"Couldn't open ports {args.port - 1}-{args.port}: {short_err(e)}",
                    f"Another agent is probably serving already. One diva per port. Try `{PROG} status` or --port.")
    try:
        if not ANIM:
            console.print(f"lan-audio agent online at {agent.ip}:{agent.port} as {agent.name}. "
                          "Open a NEW terminal to make a room or join one. Ctrl+C to stop.")
            last = 0.0
            while True:
                for ts, kind, text in list(agent.events):
                    if ts > last:
                        console.print(Text(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  [{kind}] {text}"))
                        last = ts
                time.sleep(0.5)
        start = time.perf_counter()
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(render_serve(agent, time.perf_counter() - start), refresh=True)
                time.sleep(1 / 12)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
    play(lambda t: Group(Text(""), big("BYE", t), Text(""),
                         Text("Agent offline. The LAN is quiet again.", style="italic #ff9de2")), 1.1)
    return 0


# --------------------------------------------------------------------------- #
# devices
# --------------------------------------------------------------------------- #
def list_outputs() -> Tuple[List[dict], str]:
    pa = pa_mod().PyAudio()
    try:
        return output_devices(pa)
    finally:
        pa.terminate()


def pick_devices(spec: str, outs: List[dict]) -> List[str]:
    spec = spec.strip().lower()
    if spec in ("all", "a", "*"):
        return [d["name"] for d in outs]
    nums = sorted({int(x) for x in spec.replace(" ", ",").split(",") if x})
    bad = [n for n in nums if n < 1 or n > len(outs)]
    if not nums or bad:
        raise ValueError(f"choose numbers between 1 and {len(outs)}")
    return [outs[n - 1]["name"] for n in nums]


def cmd_devices(args) -> int:
    try:
        outs, default = list_outputs()
    except AudioUnavailable as e:
        return fail("No audio here", str(e))
    cfg = load_cfg()
    if args.set is not None:
        try:
            cfg["devices"] = [] if args.set.strip().lower() in ("default", "") else pick_devices(args.set, outs)
        except ValueError as e:
            return fail("Bad device choice", str(e), f"Example: {PROG} devices --set 1,3   (or 'all', 'default')")
        save_cfg(cfg)
    anim_scan("Sniffing out your speakers...")
    chosen = cfg["devices"] or [default]
    tbl = Table(box=box.ROUNDED, border_style="#5a3a8a", header_style="bold " + CYAN,
                title=gradient(" output devices ", 0.0))
    for col in ("#", "device", "format", ""):
        tbl.add_column(col)
    for i, d in enumerate(outs, 1):
        tags = Text()
        if d["name"] == default:
            tags.append("★ windows default  ", style=GOLD)
        if d["name"] in chosen:
            tags.append("✓ plays rooms", style=LIME)
        tbl.add_row(str(i), Text(d["name"], style="bold #ffffff" if d["name"] in chosen else ""),
                    f"{int(d['defaultSampleRate'])} Hz · {int(d['maxOutputChannels'])} ch", tags)
    console.print(tbl)
    if args.set is not None:
        card("Saved", "Rooms you join will play on: " + ", ".join(chosen), "Taste: impeccable.")
    else:
        console.print(Text(f"  {sass('devices')}  Pick with: {PROG} devices --set 1,3", style="italic #ff9de2"))
    return 0


# --------------------------------------------------------------------------- #
# groups (host side) - adding someone mints their token
# --------------------------------------------------------------------------- #
def find_group(groups: dict, name: str) -> Optional[str]:
    return next((g for g in groups if g.lower() == name.lower()), None)


def show_token(cfg: dict, group: str, member: dict, port: int, fresh: bool) -> None:
    token = make_token(cfg, group, member, port)
    if fresh:
        anim_ticket("Minting a VIP pass...", member["name"], group, token[len(TOKEN_PREFIX):])
    copied = to_clipboard(f"{PROG} token add {token}")
    body = Text.assemble(
        ("Give this pass to ", "#e6e6f0"), (member["name"], "bold #ffffff"), (". On their PC, one time only:\n", "#e6e6f0"),
        ("  1. ", GREY), (f"{PROG} serve", "bold " + CYAN), ("            (keep it open)\n", GREY),
        ("  2. ", GREY), (f"{PROG} token add <the token below>\n", "bold " + CYAN),
        ("\nAfter that, any time this room is live:\n", "#e6e6f0"),
        ("     ", GREY), (f"{PROG} join {group}", "bold " + PINK),
        ("\n\nThis pass works for this group only. ", GREY),
        ("Remove them from the group to revoke it.", GREY))
    if copied:
        body.append("\n✓ Copied the full command to your clipboard. Paste it into Teams.", style="bold " + LIME)
    card(f"🎟 {member['name']}'s pass for {group}", body, sass("group_add", name=member["name"]))
    console.print(Text("token ▼  (one long line, copy all of it)", style="bold " + GOLD))
    console.print(Text(token, style="bold #ffffff"), soft_wrap=True)
    console.print()


def cmd_group(args) -> int:
    cfg = load_cfg()
    groups: Dict[str, List[dict]] = cfg["groups"]
    a = args.gcmd

    if a == "create":
        name = args.name.strip()
        if not name or "@" in name or " " in name:
            return fail("Bad name", "A group needs a name without spaces or '@'. Even a silly one is fine.")
        if find_group(groups, name):
            return fail("Group exists", f"'{name}' already exists.",
                        f"Add people: {PROG} group add {name} <ip> --name <who>")
        anim_blocks(f"Assembling the '{name}' squad...")
        groups[name] = []
        save_cfg(cfg)
        card("Group created", Text.assemble("Group ", (name, "bold " + CYAN), " is ready. Add people with:\n  ",
                                            (f"{PROG} group add {name} <ip> --name <who>", "bold " + PINK),
                                            ("\nEach person you add gets a one-time token.", GREY)),
             sass("group_create"))
        return 0

    if a == "list":
        if not groups:
            card("No groups yet", f"Start one: {PROG} group create office", "Lonely in here.")
            return 0
        names = [find_group(groups, args.group)] if args.group else list(groups)
        if None in names:
            return fail("No such group", f"There's no group called '{args.group}'.")
        for g in names:
            tbl = Table(box=box.ROUNDED, border_style="#5a3a8a", header_style="bold " + CYAN,
                        title=gradient(f" {g} · {len(groups[g])} ", 0.0), min_width=44)
            for col in ("name", "ip", "pass"):
                tbl.add_column(col)
            for m in groups[g]:
                tbl.add_row(Text(m["name"], style="bold #ffffff"), m.get("ip") or "any", f"🎟 {m['id']}")
            if not groups[g]:
                tbl.add_row(Text("(empty)", style=GREY), "", "")
            console.print(tbl)
        console.print(Text(f"  Re-show someone's pass: {PROG} group token <group> <name>", style="italic #ff9de2"))
        return 0

    g = find_group(groups, args.group)
    if g is None:
        return fail("No such group", f"There's no group called '{args.group}'.", f"Create it: {PROG} group create {args.group}")

    if a in ("add", "token") and getattr(args, "host_ip", None):
        try:
            cfg["advertise_ip"] = str(ipaddress.IPv4Address(args.host_ip))
        except ValueError:
            return fail("Bad --host-ip", f"'{args.host_ip}' isn't an IPv4 address.")
        save_cfg(cfg)

    if a == "add":
        ip = ""
        if args.peer:
            try:
                ip, _ = parse_peer(args.peer)
            except ValueError:
                return fail("Bad address", f"'{args.peer}' isn't an IPv4 address.",
                            f"Example: {PROG} group add {g} 192.168.1.20 --name Rahul")
        who = (args.name or ip).strip()
        if not who:
            return fail("Name needed", "Give them a name with --name (or an IP).",
                        f"Example: {PROG} group add {g} 192.168.1.20 --name Rahul")
        if any(m["name"].lower() == who.lower() for m in groups[g]):
            return fail("Name taken", f"Someone called '{who}' is already in '{g}'.",
                        f"Re-show their pass: {PROG} group token {g} {who}")
        anim_carpet(who)
        member = {"name": who, "ip": ip, "id": secrets.token_hex(4), "secret": secrets.token_urlsafe(16)}
        groups[g].append(member)
        save_cfg(cfg)
        show_token(cfg, g, member, args.port, fresh=True)
        return 0

    if a == "token":
        m = next((m for m in groups[g] if m["name"].lower() == args.who.lower()), None)
        if m is None:
            return fail("Not found", f"Nobody called '{args.who}' in '{g}'.", f"See who's there: {PROG} group list {g}")
        show_token(cfg, g, m, args.port, fresh=True)
        return 0

    if a == "remove":
        w = args.who.lower()
        hit = next((m for m in groups[g] if w in (m["name"].lower(), m.get("ip"))), None)
        if hit is None:
            return fail("Not found", f"Nobody matching '{args.who}' in '{g}'.", f"See who's there: {PROG} group list {g}")
        try:
            api(args.port, "room_kick", name=hit["name"], group=g, timeout=3)
        except NoAgent:
            pass
        anim_yeet(hit["name"], "Shredding their pass...")
        groups[g].remove(hit)
        save_cfg(cfg)
        card("Removed", f"{hit['name']} is out of '{g}' and their token no longer works.",
             sass("group_remove", name=hit["name"], group=g))
        return 0

    if a == "delete":
        anim_yeet(g)
        del groups[g]
        save_cfg(cfg)
        card("Group deleted", f"'{g}' is gone, along with every pass for it.", sass("group_delete", group=g))
        return 0
    return 1


# --------------------------------------------------------------------------- #
# tokens (member side)
# --------------------------------------------------------------------------- #
def status_cell(info: dict) -> Text:
    t = info.get("t")
    if t == "INFO":
        if info.get("live"):
            return Text(f"● LIVE · {info.get('here', 0)}/{info.get('total', 0)} vibing", style="bold " + LIME)
        return Text("○ not live right now", style=GREY)
    if t == "REJECT":
        return Text("✕ pass revoked", style=RED)
    return Text(f"? host offline ({info.get('reason', 'no answer')})", style=GOLD)


def cmd_groups(args) -> int:
    cfg = load_cfg()
    toks = cfg["tokens"]
    if not toks:
        card("No passes yet", Text.assemble("Ask a host to add you to a group. They'll send a token. Then run:\n",
                                            (f"  {PROG} token add <TOKEN>", "bold " + CYAN)),
             "Everybody starts somewhere.")
        return 0
    res: Dict[int, dict] = {}
    ths = [threading.Thread(target=lambda i=i, t=t: res.__setitem__(i, fetch_info(t, cfg["name"])), daemon=True)
           for i, t in enumerate(toks)]
    for th in ths:
        th.start()

    def frame(tt):
        return Group(gradient("Checking which squads you're in...", tt),
                     Text("".join("◆" if (i == int(tt * 12) % 20) else "·" for i in range(20)), style=PINK))

    if ANIM:
        start = time.perf_counter()
        with Live(frame(0), console=console, transient=True, auto_refresh=False) as live:
            while any(th.is_alive() for th in ths) or time.perf_counter() - start < 0.6:
                live.update(frame(time.perf_counter() - start), refresh=True)
                time.sleep(0.04)
    for th in ths:
        th.join()
    tbl = Table(box=box.ROUNDED, border_style="#5a3a8a", header_style="bold " + CYAN,
                title=gradient(" your groups ", 0.0))
    for col in ("group", "host", "you are", "right now", "join with"):
        tbl.add_column(col, no_wrap=col in ("right now", "join with"), overflow="ellipsis")
    dup = {t["group"].lower() for t in toks if sum(x["group"].lower() == t["group"].lower() for x in toks) > 1}
    for i, t in enumerate(toks):
        spec = token_label(t) if t["group"].lower() in dup else t["group"]
        tbl.add_row(Text(t["group"], style="bold " + PINK), t["host_name"], t["me"], status_cell(res.get(i, {})),
                    Text(f"{PROG} join {spec}", style=CYAN))
    console.print(tbl)
    live_n = sum(1 for r in res.values() if r.get("live"))
    console.print(Text("  " + ("Something's live. Go, go, go." if live_n else "Nothing live. Touch grass, check back later."),
                       style="italic #ff9de2"))
    return 0


def cmd_token(args) -> int:
    cfg = load_cfg()
    if args.tcmd == "list":
        return cmd_groups(args)
    if args.tcmd == "add":
        try:
            tok = read_token(" ".join(args.token))
        except ValueError as e:
            return fail("Bad token", str(e).capitalize() + ".", "Ask the host to re-send it: lanaudio group token <group> <you>")
        if tok["host_id"] == cfg["agent_id"]:
            return fail("That's your own pass", "You minted this token. Send it to the person you added, "
                                                 "they run it on their PC.")
        cfg["tokens"] = [t for t in cfg["tokens"]
                         if not (t["host_id"] == tok["host_id"] and t["group"].lower() == tok["group"].lower())]
        cfg["tokens"].append(tok)
        save_cfg(cfg)
        anim_ticket("Scanning your VIP pass...", tok["me"], tok["group"], tok["secret"])
        info = fetch_info(tok, cfg["name"])
        body = Text.assemble(("You're in ", "#e6e6f0"), (tok["group"], "bold " + PINK), (" on ", "#e6e6f0"),
                             (tok["host_name"], "bold #ffffff"), ("'s PC as ", "#e6e6f0"), (tok["me"], "bold #ffffff"),
                             ".\n\n", ("right now  ", GREY))
        body.append_text(status_cell(info))
        body.append("\njoin       ", style=GREY)
        body.append(f"{PROG} join {tok['group']}", style="bold " + CYAN)
        body.append("\ncheck      ", style=GREY)
        body.append(f"{PROG} groups", style="bold " + CYAN)
        if info.get("t") == "OFFLINE":
            body.append("\n\nCan't reach the host right now. Saved anyway; it'll work once they're online.",
                        style=GOLD)
        card("Pass saved", body, sass("token_add"))
        return 0
    if args.tcmd == "remove":
        g, _, h = args.group.partition("@")
        hit = [t for t in cfg["tokens"] if t["group"].lower() == g.lower()
               and (not h or t["host_name"].lower() == h.lower())]
        if not hit:
            return fail("No such pass", f"You don't have a pass for '{args.group}'.", f"See them: {PROG} groups")
        if len(hit) > 1:
            return fail("Which one?", "Several hosts have that group: " + ", ".join(token_label(t) for t in hit))
        anim_yeet(token_label(hit[0]), "Tossing the pass...")
        cfg["tokens"].remove(hit[0])
        save_cfg(cfg)
        card("Pass removed", f"You won't see '{token_label(hit[0])}' anymore.", "Clean breaks are healthy.")
        return 0
    return 1


# --------------------------------------------------------------------------- #
# ping
# --------------------------------------------------------------------------- #
def cmd_ping(args) -> int:
    try:
        ip, port = parse_peer(args.target)
    except ValueError:
        return fail("Bad address", f"'{args.target}' isn't an IPv4 address (optionally :port).")
    best, info, err = None, None, None
    for _ in range(3):
        try:
            c = dial(ip, port, 3.0)
            t0 = time.perf_counter()
            c.send({"t": "HELLO", "v": VERSION, "name": load_cfg()["name"], "port": 0})
            h = c.recv(3.0)
            dt = (time.perf_counter() - t0) * 1000
            c.close()
            if not h or h.get("t") != "HELLO":
                err = "something answered, but it isn't a lanaudio agent"
                continue
            info = h
            best = dt if best is None else min(best, dt)
        except (OSError, ConnectionError) as e:
            err = short_err(e)
    if info is None:
        return fail("No answer", f"{ip}:{port} didn't respond ({err}).",
                    "Either it's off, the agent isn't serving, or the firewall is being dramatic. "
                    f"Try `{PROG} doctor` on that PC.")
    anim_ping(ip)
    key = "ping_fast" if best < 5 else "ping_ok" if best < 30 else "ping_slow"
    rooms = info.get("rooms") or []
    body = Text.assemble(("name     ", GREY), (str(info.get("name")), "bold #ffffff"), "\n",
                         ("version  ", GREY), str(info.get("v")), "\n",
                         ("mode     ", GREY), (str(info.get("role", "?")), PINK), "\n",
                         ("live     ", GREY), (", ".join(rooms) or "nothing", LIME if rooms else GREY), "\n",
                         ("rtt      ", GREY), (f"{best:.1f} ms", "bold " + CYAN))
    if info.get("v") != VERSION:
        body.append(f"\n\n⚠ They run {info.get('v')}, you run {VERSION}. Everyone needs the same version.", style=GOLD)
    card(f"Pong from {ip}", body, sass(key, ms=f"{best:.1f}"))
    return 0


# --------------------------------------------------------------------------- #
# room (host)
# --------------------------------------------------------------------------- #
def member_table(room: dict, t: float) -> Table:
    tbl = Table(box=box.ROUNDED, border_style="#5a3a8a", header_style="bold " + CYAN, expand=False)
    for col, j in (("", "center"), ("member", "left"), ("status", "left"), ("rtt", "right"), ("headroom", "right"),
                   ("gaps", "right"), ("sync", "right"), ("flow", "left")):
        tbl.add_column(col, justify=j, no_wrap=True, overflow="ellipsis")
    for i, m in enumerate(room["members"]):
        glyph, color, label = STATE_LOOK.get(m["state"], ("?", GREY, m["state"]))
        if m["state"] in ("joining", "lost"):
            glyph = SPIN[int(t * 8 + i) % 4]
        live = m["state"] == "live"
        status = Text(label, style=color)
        if not m["token"]:
            status.append(" · guest", style=GREY)
        if live and m.get("resumes"):
            status.append(f" · ↻{m['resumes']}", style=GREY)
        flow = Text()
        if live:
            pos = int(t * 12 + i * 3) % 8
            for k in range(8):
                flow.append("●" if k == pos else "─", style=PINK if k == pos else "#4a4a5a")
            flow.append(" ")
            flow.append_text(meter_bar(m.get("lvl") or 0, 4))
        else:
            flow.append("· · · ·", style="#3a3a4a")
        tbl.add_row(Text(glyph, style="bold " + color), Text(m["name"], style="bold #ffffff" if live else GREY), status,
                    fmt_ms(m["rtt"]) if live else "—",
                    head_text(m["head"]) if live else Text("—", style=GREY),
                    gaps_text(m["gaps"]) if live else Text("—", style=GREY),
                    fmt_ms(m["sync"], 1) if live else "—", flow)
    if not room["members"]:
        tbl.add_row("", Text("nobody in this group yet", style=GREY), "", "", "", "", "", "")
    return tbl


def knock_panel(rooms: list, t: float):
    reqs = [(r["group"], p) for r in rooms for p in r["pending"]]
    if not reqs:
        return None
    body = Text()
    for i, (g, p) in enumerate(reqs[:4]):
        shake = " " if (i == 0 and int(t * 12) % 2) else ""
        body.append(shake + ("🔔 " if i == 0 else "   "), style="bold")
        body.append(p["name"], style="bold #ffffff")
        body.append(f" ({p['ip']}) wants into ", style="#e6e6f0")
        body.append(g, style="bold " + PINK)
        body.append(f"  · waiting {int(time.time() - p['since'])}s", style=GREY)
        if i == 0:
            body.append("\n   ")
            body.append(" A ", style="bold #14141f on " + LIME)
            body.append(" let in  ", style=GREY)
            body.append(" D ", style="bold #14141f on " + RED)
            body.append(" nope", style=GREY)
        body.append("\n")
    if len(reqs) > 4:
        body.append(f"   +{len(reqs) - 4} more at the door\n", style=GREY)
    body.rstrip()
    return Panel(body, title=Text(" KNOCK KNOCK ", style="bold #14141f on " + (GOLD if int(t * 4) % 2 else PINK)),
                 title_align="left", border_style=GOLD if int(t * 4) % 2 else PINK, expand=False)


def render_host(st: dict, t: float, flash: str = ""):
    rooms = st["rooms"]
    src = st.get("source") or {}
    blink = int(t * 2) % 2 == 0
    head = Text("● ON AIR ", style="bold " + (RED if blink else "#ffffff"))
    head.append_text(gradient(f" {len(rooms)} room{'s' if len(rooms) != 1 else ''} · "
                              + " · ".join(r["group"] for r in rooms), t, spread=0.05))
    if rooms:
        head.append(f"   {hhmmss(time.time() - min(r['since'] for r in rooms))}", style=GREY)
    srcl = Text("source: ", style=GREY)
    srcl.append(src.get("name") or "?", style="#e6e6f0")
    if src.get("rate"):
        srcl.append(f"  · {src['rate']} Hz {src.get('fmt', '')} capture", style=GREY)
    if rooms and not src.get("ok"):
        srcl.append("   capture restarting" + (f" ({src['error']})" if src.get("error") else ""), style=GOLD)
    parts = [head, srcl, Text(""), spectrum(src.get("bands") or [0.0] * 24, 5, t), Text("")]
    kp = knock_panel(rooms, t)
    if kp is not None:
        parts += [kp]
    worst = None
    for r in rooms:
        roster = [m for m in r["members"] if m["state"] not in ("kicked",)]
        live_n = sum(m["state"] == "live" for m in roster)
        sub = Text("▸ ", style="bold " + PINK)
        sub.append(r["group"], style="bold #ffffff")
        sub.append(f"  {r['label']} · {latency_text(r['latency'], r['auto'])} · {live_n}/{len(roster)} vibing   ",
                   style=GREY)
        sub.append(f"join: {PROG} join {r['group']}", style=CYAN)
        parts += [sub, member_table(r, t)]
        for m in r["members"]:
            if m["state"] == "live" and m.get("head") is not None:
                worst = m["head"] if worst is None else min(worst, m["head"])
    total = sum(1 for r in rooms for m in r["members"] if m["state"] not in ("kicked", "left", "away"))
    live = sum(1 for r in rooms for m in r["members"] if m["state"] == "live")
    gaps = sum((m.get("gaps") or 0) for r in rooms for m in r["members"] if m["state"] == "live")
    if kp is not None:
        quip = "Someone's at the door. Don't leave them hanging."
    elif worst is not None and worst < HEADROOM_LOW_MS:
        quip = "Someone's headroom is thin. Auto mode will widen the buffer, or press + yourself."
    elif live and gaps == 0 and (src.get("level") or 0) >= 0.005:
        quip = "Zero gaps. Every sample delivered. Chef's kiss."
    elif live and (src.get("level") or 0) < 0.005:
        quip = "It's quiet... too quiet. Play something?"
    elif live == 0:
        quip = "Empty dance floor. Send them the join command, they'll come."
    else:
        quip = f"{live}/{max(total, live)} vibing. The rest are fashionably late."
    parts.append(Text(quip, style="italic #ff9de2"))
    if flash:
        parts.append(Text(flash, style="bold " + GOLD))
    if console.size.height >= 36:
        parts.append(events_panel(st["events"], t, 4, min(100, console.size.width - 4)))
    keys = Text()
    for k, what in (("A", "let in"), ("D", "deny"), ("+/-", "buffer"), ("U", "auto buffer"),
                    ("S", "stop ALL rooms"), ("Q", "detach")):
        keys.append(f" {k} ", style="bold #14141f on " + CYAN)
        keys.append(f" {what}   ", style=GREY)
    parts += [Text(""), keys]
    return Padding(Group(*parts), (1, 2))


def room_dashboard(args) -> int:
    if not ANIM:
        try:
            st = api(args.port, "status")
        except NoAgent:
            return no_agent(args.port)
        if not st.get("rooms"):
            return fail("Not hosting", "There's no room running on this agent.")
        console.print(render_host(st, 0.0))
        return 0
    result, flash, flash_until = "detach", "", 0.0
    seen_knocks: set = set()
    start = time.perf_counter()
    try:
        with Keys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                try:
                    st = api(args.port, "status")
                except NoAgent:
                    result = "agent"
                    break
                if not st.get("rooms"):
                    result = "gone"
                    break
                t = time.perf_counter() - start
                knocks = {p["key"] for r in st["rooms"] for p in r["pending"]}
                if knocks - seen_knocks:
                    console.bell()
                    flash, flash_until = "🔔 Knock knock! Press A to let them in, D to say nope.", t + 4
                seen_knocks = knocks
                live.update(render_host(st, t, flash if t < flash_until else ""), refresh=True)
                k = (keys.get() or "").lower()
                if k == "q":
                    break
                if k == "s":
                    result = "stop"
                    break
                if k in ("a", "d"):
                    r = api(args.port, "room_approve", allow=(k == "a"))
                    if r.get("ok"):
                        flash = ("✓ " + sass("let_in", name=r["name"])) if k == "a" else ("✕ " + sass("deny", name=r["name"]))
                    else:
                        flash = r.get("error", "") + " Pressing buttons for fun, I see."
                    flash_until = t + 3
                if k in ("+", "=", "-", "_"):
                    ms = st["rooms"][0]["latency"] + (50 if k in ("+", "=") else -50)
                    r = api(args.port, "room_latency", ms=ms)
                    flash = f"Buffer set to {r.get('latency')} ms (manual). It glides there, no glitch." + (
                        " Tighter. Bold move." if k in ("-", "_") else " Extra cushion. Wi-Fi says thanks.")
                    flash_until = t + 3
                if k == "u":
                    api(args.port, "room_latency", auto=True)
                    flash, flash_until = "Auto buffer ON. I'll widen it the moment anyone's link wobbles.", t + 3
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    if result == "stop":
        return do_room_stop(args, None)
    if result == "agent":
        return no_agent(args.port)
    if result == "gone":
        card("Rooms closed", "No rooms are running anymore.", "Show's over, folks.")
        return 0
    card("Detached", Text.assemble("Your rooms are still ON AIR.\n", ("re-attach  ", GREY),
                                   (f"{PROG} room status", "bold " + CYAN), "\n", ("stop all   ", GREY),
                                   (f"{PROG} room stop", "bold " + CYAN)),
         "Leaving the booth, not the party.")
    return 0


def parse_latency(v: str):
    v = str(v).strip().lower()
    if v in ("auto", "a", ""):
        return "auto"
    ms = int(v.rstrip("ms"))
    return ms


def cmd_room(args) -> int:
    try:
        if args.rcmd == "start":
            group = args.group_opt or args.group
            if not group:
                return fail("Which group?", "Tell me which group this room is for.", f"Example: {PROG} room start office")
            try:
                lat = parse_latency(args.latency)
            except ValueError:
                return fail("Bad --latency", f"'{args.latency}' should be 'auto' or milliseconds, e.g. 400.")
            r = api(args.port, "room_start", group=group, latency=lat, codec=args.codec)
            if not r.get("ok"):
                return fail("Room didn't start", r.get("error", "unknown error"),
                            f"Groups live in `{PROG} group list`. Audio problems? `{PROG} doctor`.")
            anim_countdown(r.get("source") or "your default output")
            anim_broadcast(r["group"], r.get("roster", []))
            if args.detach or not ANIM:
                card(f"ON AIR · {r['group']}",
                     Text.assemble("Streaming ", (r.get("source") or "your default output", "bold #ffffff"), "\n",
                                   ("quality        ", GREY), (r.get("label", ""), "bold " + LIME), "\n",
                                   ("buffer         ", GREY), (latency_text(r["latency"], r["auto"]), "#e6e6f0"), "\n",
                                   ("members join   ", GREY), (f"{PROG} join {r['group']}", "bold " + PINK), "\n",
                                   ("guests knock   ", GREY), (f"{PROG} join {agent_ip(args)}", "bold " + PINK), "\n",
                                   ("dashboard      ", GREY), (f"{PROG} room status", "bold " + CYAN), "\n",
                                   ("stop all       ", GREY), (f"{PROG} room stop", "bold " + CYAN)),
                     sass("room_start"))
                return 0
            return room_dashboard(args)
        if args.rcmd == "status":
            st = api(args.port, "status")
            if st.get("rooms"):
                return room_dashboard(args)
            if st.get("member") and st["member"]["state"] in Membership.ALIVE:
                return listen_dashboard(args)
            card("Not hosting", f"No room running. Start one: {PROG} room start <group>",
                 "The stage is empty. Suspiciously empty.")
            return 0
        if args.rcmd == "stop":
            return do_room_stop(args, None if args.all else args.group)
        if args.rcmd == "kick":
            r = api(args.port, "room_kick", name=args.name, group=args.group)
            if not r.get("ok"):
                return fail("Kick failed", r.get("error", "unknown error"))
            anim_boot(r["name"])
            card("Kicked", f"{r['name']} is out of '{r['group']}' for this session.", sass("kick", name=r["name"]))
            return 0
        if args.rcmd == "latency":
            try:
                lat = parse_latency(args.ms)
            except ValueError:
                return fail("Bad value", f"'{args.ms}' should be 'auto' or milliseconds, e.g. 500.")
            if lat == "auto":
                r = api(args.port, "room_latency", auto=True, group=args.group)
            else:
                r = api(args.port, "room_latency", ms=lat, group=args.group)
            if not r.get("ok"):
                return fail("Couldn't change the buffer", r.get("error", "unknown error"))
            card("Buffer updated", f"{', '.join(r['groups'])}: {latency_text(r['latency'], r['auto'])}. "
                                   "Members glide to it without a glitch.",
                 "Bigger buffer = more delay, fewer gaps. Choose wisely.")
            return 0
        if args.rcmd in ("approve", "deny"):
            allow = args.rcmd == "approve"
            r = api(args.port, "room_approve", name=args.name, allow=allow)
            if not r.get("ok"):
                return fail("Nobody to " + ("let in" if allow else "deny"), r.get("error", "unknown error"))
            anim_rope(r["name"], allow)
            card("Let in" if allow else "Denied", f"{r['name']} → '{r['group']}'.",
                 sass("let_in" if allow else "deny", name=r["name"]))
            return 0
    except NoAgent:
        return no_agent(args.port)
    return 1


def agent_ip(args) -> str:
    try:
        st = api(args.port, "status", timeout=3)
        return st["ip"] if st["port"] == PEER_DEFAULT_PORT else f"{st['ip']}:{st['port']}"
    except (NoAgent, KeyError):
        return "<your-ip>"


def do_room_stop(args, group: Optional[str]) -> int:
    try:
        r = api(args.port, "room_stop", group=group)
    except NoAgent:
        return no_agent(args.port)
    if not r.get("ok"):
        return fail("Nothing to stop", r.get("error", "unknown error"))
    anim_mic_drop()
    body = Text.assemble(("stopped  ", GREY), (", ".join(r["stopped"]), "bold #ffffff"))
    if r.get("left"):
        body.append("\nstill live  ", style=GREY)
        body.append(", ".join(r["left"]), style="bold " + LIME)
    else:
        body.append("\nEvery room is closed. Everyone's been sent home.", style=GREY)
    card("Rooms closed" if len(r["stopped"]) > 1 else "Room closed", body, sass("room_stop"))
    return 0


# --------------------------------------------------------------------------- #
# join / leave (member)
# --------------------------------------------------------------------------- #
REJECT_LOOK = {
    "host said no": "The host looked at you through the peephole and said no. Ouch.",
    "nobody answered the door": "Nobody answered the door. The host might be away from their keyboard.",
    "kicked": "You were kicked from this room. Read the room.",
    "token revoked": "Your pass for this group was revoked. Ask the host for a new one.",
    "no opus on your side": "The room uses Opus. Install opuslib + opus.dll, or ask the host for the default codec.",
}


def render_listen(st: dict, t: float):
    m = st["member"]
    state = m["state"]
    playing = state in ("live", "reconnecting")
    head = Text("♫ LISTENING " if playing else "◌ STANDING BY ", style="bold " + (CYAN if int(t * 2) % 2 else PINK))
    head.append_text(gradient(f" {m['group'] or 'room'} @ {m['host']}", t, spread=0.05))
    if playing:
        head.append(f"   {hhmmss(time.time() - m['since'])}", style=GREY)
    parts = [head]
    if playing:
        parts.append(Text.assemble(("quality  ", GREY), (m["label"], "bold " + LIME)))
    spin = SPIN[int(t * 8) % 4]
    if state == "waiting":
        parts.append(Text(f"{spin} '{m['group']}' isn't live yet. Lurking until {m['host']} starts it...", style=GOLD))
    elif state == "approval":
        parts.append(Text(f"{spin} Knocked. Waiting for {m['host']} to open the door...", style=GOLD))
    elif state == "reconnecting":
        parts.append(Text(f"{spin} Link blinked. Still playing from the buffer while we reconnect"
                          + (f" ({m['note']})" if m["note"] else "") + "...", style=GOLD))
    elif state == "connecting":
        parts.append(Text(f"{spin} Connecting...", style=GOLD))
    parts += [Text(""), spectrum(m["bands"], 8, t), Text("")]

    if playing:
        buf = Text("buffer   ", style=GREY)
        buf.append_text(buffer_bar(m.get("buffered", 0), m["lat_eff"]))
        buf.append(f"  {m.get('buffered', 0)} / {m['lat_eff']} ms", style="bold #ffffff")
        if m["lat_eff"] != m["latency"]:
            buf.append(f"  → gliding to {m['latency']} ms", style=GOLD)
        parts.append(buf)
        stats = Table.grid(padding=(0, 3))
        cells = [("rtt", fmt_ms(m.get("rtt"))), ("headroom", fmt_ms(m.get("head"), 0)),
                 ("gaps", str(m.get("gaps", 0))), ("sync", fmt_ms(m.get("sync"), 2)),
                 ("reconnects", str(m.get("reconnects", 0))), ("volume", f"{int(m.get('volume', 1) * 100)}%")]
        stats.add_row(*[Text.assemble((k + " ", GREY), (v, "bold #ffffff")) for k, v in cells])
        parts.append(stats)
        dt = Table(box=box.SIMPLE_HEAD, header_style="bold " + CYAN, border_style="#5a3a8a", expand=False)
        for col in ("device", "format", "offset", "sync", "gaps", ""):
            dt.add_column(col, no_wrap=True)
        for d in m["devices"]:
            dt.add_row(Text(d["name"], style="bold #ffffff"), f"{d['rate']} Hz · {d['ch']} ch · {d['fmt']}",
                       f"+{d['offset']} ms", fmt_ms(d["sync"], 2), gaps_text(d["gaps"]),
                       Text("● playing", style=LIME) if d["ok"] else Text("✕ " + (d["error"] or "down"), style=RED))
        for n in m.get("missing", []):
            dt.add_row(Text(n, style=GREY), "", "", "", "", Text("○ waiting for it", style=GOLD))
        if m["devices"] or m.get("missing"):
            parts.append(dt)
        if m.get("player_error"):
            parts.append(Text(f"output: {m['player_error']}", style=GOLD))

    sync = abs(m.get("sync") or 0)
    hr = m.get("head")
    if state == "waiting":
        quip = "Patience is hot. You'll be pulled in the second it starts."
    elif state == "approval":
        quip = "Standing at the velvet rope. Look cool."
    elif state == "reconnecting":
        quip = "Nobody panic. The buffer's got this."
    elif not playing:
        quip = "Hold tight."
    elif hr is not None and hr < HEADROOM_LOW_MS:
        quip = "Your link is wobbly. The host's auto buffer will widen to protect you."
    elif m.get("gaps", 0) == 0:
        quip = f"Zero gaps, {sync:.2f} ms sync. Every single sample, delivered."
    else:
        quip = f"{m['gaps']} gap(s) so far. If it keeps happening, try Ethernet or a bigger buffer."
    keys = Text()
    for k, what in (("L", "leave"), ("Q", "detach, keeps going in the background")):
        keys.append(f" {k} ", style="bold #14141f on " + PINK)
        keys.append(f" {what}   ", style=GREY)
    parts += [Text(quip, style="italic #ff9de2"), Text(""), keys]
    return Padding(Group(*parts), (1, 2))


def end_card(m: Optional[dict]) -> int:
    state = (m or {}).get("state", "ended")
    host = (m or {}).get("host", "the host")
    if state == "kicked":
        return fail("Kicked", f"{host} removed you from the room.", "Rude, but noted.")
    if state == "rejected":
        note = m.get("note", "rejected")
        return fail("Not let in", REJECT_LOOK.get(note, note))
    if state == "left":
        card("Left", f"You left {host}'s room.", sass("leave"))
        return 0
    card("Room ended", f"{host} ended the room.", "Show's over. Applause optional.")
    return 0


def listen_dashboard(args) -> int:
    if not ANIM:
        try:
            st = api(args.port, "status")
        except NoAgent:
            return no_agent(args.port)
        if st.get("member"):
            console.print(render_listen(st, 0.0))
        return 0
    result, last = "detach", None
    start = time.perf_counter()
    try:
        with Keys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                try:
                    st = api(args.port, "status")
                except NoAgent:
                    result = "agent"
                    break
                m = st.get("member")
                if not m or m["state"] not in Membership.ALIVE:
                    result, last = "ended", m
                    break
                live.update(render_listen(st, time.perf_counter() - start), refresh=True)
                k = (keys.get() or "").lower()
                if k == "q":
                    break
                if k == "l":
                    result = "leave"
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    if result == "leave":
        return do_leave(args)
    if result == "agent":
        return no_agent(args.port)
    if result == "ended":
        return end_card(last)
    card("Detached", Text.assemble("Still connected in the background.\n", ("re-attach  ", GREY),
                                   (f"{PROG} room status", "bold " + CYAN), "\n", ("leave      ", GREY),
                                   (f"{PROG} leave", "bold " + CYAN)),
         "Out of sight, still in sync.")
    return 0


def knock_frame(target: str, t: float, waiting_host: bool, note: str):
    shake = " " if int(t * 12) % 2 else ""
    door = ["┌──────────┐", "│ ┌──────┐ │", "│ │      │ │", "│ │    ● │ │", "│ │      │ │", "└─┴──────┴─┘"]
    knocks = Text(("KNOCK " * (1 + int(t * 3) % 3)).strip(), style="bold " + (PINK if int(t * 6) % 2 else GOLD))
    art = Text()
    for i, row in enumerate(door):
        art.append(shake + row, style=neon(i * 0.3 - t * 2))
        if i == 2:
            art.append("   ")
            art.append_text(knocks)
        if i == 4 and waiting_host:
            art.append("   🔔 the host just got pinged", style=GOLD)
        art.append("\n")
    art.append_text(gradient("Waiting for the host to let you in..." if waiting_host
                             else f"Knocking on {target}'s door...", t))
    if note:
        art.append(f"\n{note}", style=GREY)
    return art


def cmd_join(args) -> int:
    target = (args.target or "").strip()
    ip, port, group = None, PEER_DEFAULT_PORT, args.group
    if target and is_ip_spec(target):
        ip, port = parse_peer(target)
    elif target:
        group = target
    offsets: Dict[str, int] = {}
    for o in args.offset or []:
        try:
            if "=" in o:
                k, v = o.rsplit("=", 1)
                offsets[k.strip()] = max(0, min(2000, int(v)))
            else:
                offsets["*"] = max(0, min(2000, int(o)))
        except ValueError:
            return fail("Bad offset", f"'{o}' should be MS or NAME=MS, e.g. --offset Realtek=180")
    devices = None
    if args.devices:
        try:
            devices = pick_devices(args.devices, list_outputs()[0])
        except AudioUnavailable as e:
            return fail("No audio here", str(e))
        except ValueError as e:
            return fail("Bad device choice", str(e), f"See the list: {PROG} devices")
    try:
        r = api(args.port, "join", ip=ip, port=port, group=group, devices=devices,
                offsets=offsets or None, volume=args.volume)
        if not r.get("ok"):
            return fail("Can't join", r.get("error", "unknown error"),
                        f"See your passes: {PROG} groups" if not ip else None)
        label = r.get("group") or r.get("host") or target
        m = None
        start = time.perf_counter()
        deadline = start + 12
        live = Live(console=console, auto_refresh=False, transient=True) if ANIM else None
        if live:
            live.start()
        try:
            while time.perf_counter() < deadline:
                st = api(args.port, "status")
                m = st.get("member")
                if m and m["state"] in ("live", "rejected", "ended", "kicked", "left", "waiting"):
                    break
                if m and m["state"] == "approval":
                    deadline = start + APPROVAL_TIMEOUT_S + 5
                if live:
                    live.update(knock_frame(r.get("host") or label, time.perf_counter() - start,
                                            bool(m and m["state"] == "approval"),
                                            "" if not m or m["state"] == "approval" else m.get("note", "")),
                                refresh=True)
                time.sleep(0.1)
        finally:
            if live:
                live.stop()
        if m and m["state"] == "waiting":
            card(f"'{m['group']}' isn't live yet",
                 Text.assemble((m["host"], "bold #ffffff"), " hasn't started the room. Your agent is waiting at the door "
                               "and will slide in the moment it goes live.\n\n",
                               ("watch   ", GREY), (f"{PROG} room status", "bold " + CYAN), "\n",
                               ("cancel  ", GREY), (f"{PROG} leave", "bold " + CYAN)),
                 "Patience is hot.")
            return 0
        if not m or m["state"] != "live":
            if m and m["state"] in ("rejected", "kicked", "ended"):
                return end_card(m)
            try:
                api(args.port, "leave")
            except NoAgent:
                pass
            return fail("Nobody answered", f"Couldn't reach {r.get('host') or label}"
                        + (f" ({m['note']})." if m and m.get("note") else "."),
                        f"Is the host running `{PROG} serve`? Check with `{PROG} groups` or `{PROG} ping <ip>`.")
        anim_buffer_fill(f"Filling the {m['latency']} ms safety buffer...")
        anim_confetti(f"You're in '{m['group']}'! {sass('join')}")
        if args.detach or not ANIM:
            card(f"Listening to {m['group']} @ {m['host']}", Text.assemble(
                ("quality  ", GREY), (m["label"], "bold " + LIME), "\n",
                ("buffer   ", GREY), f"{m['latency']} ms\n",
                ("watch    ", GREY), (f"{PROG} room status", "bold " + CYAN), "\n",
                ("leave    ", GREY), (f"{PROG} leave", "bold " + CYAN)),
                sass("join"))
            return 0
        return listen_dashboard(args)
    except NoAgent:
        return no_agent(args.port)


def do_leave(args) -> int:
    try:
        r = api(args.port, "leave")
    except NoAgent:
        return no_agent(args.port)
    if not r.get("ok"):
        return fail("Nowhere to leave", r.get("error", "unknown error"))
    anim_walk_out("Slipping out the back door...")
    card("Left", f"You left '{r.get('group') or r['host']}'.", sass("leave"))
    return 0


# --------------------------------------------------------------------------- #
# status / doctor / banner
# --------------------------------------------------------------------------- #
def cmd_status(args) -> int:
    try:
        st = api(args.port, "status")
    except NoAgent:
        return no_agent(args.port)
    anim_scan("Checking the vibes...", 0.6)
    body = Text.assemble(("name     ", GREY), (st["name"], "bold #ffffff"), "\n",
                         ("address  ", GREY), (f"{st['ip']}:{st['port']}", CYAN), "\n",
                         ("uptime   ", GREY), hhmmss(st["uptime"]), "\n",
                         ("tuning   ", GREY), (", ".join(st.get("tuning") or []) or "default", GREY), "\n",
                         ("mode     ", GREY), (st["role"], "bold " + PINK))
    for r in st.get("rooms", []):
        live = sum(m["state"] == "live" for m in r["members"])
        body.append(f"\nroom     {r['group']} · {live}/{len(r['members'])} live · "
                    f"{latency_text(r['latency'], r['auto'])} · {r['label']}"
                    + (f" · {len(r['pending'])} knocking" if r["pending"] else ""))
    m = st.get("member")
    if m and m["state"] in Membership.ALIVE:
        body.append(f"\nin       {m['group'] or 'room'} @ {m['host']} · {m['state']} · gaps {m.get('gaps', 0)}")
    card("Agent status", body, random.choice(SASS[{"host": "host", "listener": "listen"}.get(st["role"], "serve")]))
    return 0


def _run(cmd: List[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout or ""
    except Exception:
        return ""


def cmd_doctor(args) -> int:
    fixes: List[str] = []
    agent_up = {"v": False}

    def c_os():
        return ("ok", platform.platform()) if os.name == "nt" else ("bad", "Windows is required for audio (WASAPI).")

    def c_py():
        return ("ok" if sys.version_info >= (3, 9) else "bad", platform.python_version())

    def c_libs():
        return ("ok", f"numpy {np.__version__}, rich")

    def c_pa():
        try:
            pa_mod()
            return ("ok", "PyAudioWPatch loaded")
        except AudioUnavailable as e:
            fixes.append("pip install PyAudioWPatch")
            return ("bad", str(e))

    def c_loop():
        try:
            pa = pa_mod().PyAudio()
            try:
                info = wasapi_devices(pa)
                lb = find_loopback(pa, info["default"], info["loopbacks"])
                return ("ok", f"{info['default']['name']} · {int(lb['defaultSampleRate'])} Hz")
            finally:
                pa.terminate()
        except Exception as e:
            return ("bad", f"no loopback ({short_err(e)})")

    def c_outs():
        try:
            outs, _ = list_outputs()
            return ("ok" if outs else "bad", f"{len(outs)} output device(s)")
        except Exception as e:
            return ("bad", short_err(e))

    def c_opus():
        return ("ok", "available") if opus_available() else ("ok", "not installed (not needed, PCM is lossless)")

    def c_agent():
        try:
            st = api(args.port, "status", timeout=3)
            agent_up["v"] = True
            tun = ", ".join(st.get("tuning") or []) or "none"
            return ("ok", f"running as {st['name']} ({st['role']}) · tuning: {tun}")
        except NoAgent:
            fixes.append(f"{PROG} serve")
            return ("warn", "not running")

    def c_ports():
        if agent_up["v"]:
            return ("ok", f"{args.port}/tcp held by the agent")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("0.0.0.0", args.port))
            finally:
                s.close()
            return ("ok", f"{args.port}/tcp is free")
        except OSError as e:
            return ("bad", f"port busy ({short_err(e)})")

    def c_fw():
        if os.name != "nt":
            return ("warn", "skipped (not Windows)")
        out = _run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"])
        if "lanaudio" in out.lower():
            return ("ok", "inbound lanaudio rule found")
        fixes.append(f'netsh advfirewall firewall add rule name="lanaudio" dir=in action=allow protocol=TCP localport={args.port}')
        return ("warn", "no lanaudio rule (run the fix below as admin)")

    def c_link():
        if os.name != "nt":
            return ("warn", "skipped (not Windows)")
        out = _run(["netsh", "wlan", "show", "interfaces"])
        lines = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in out.splitlines()) if v}
        if lines.get("state", "").lower() == "connected":
            sig = lines.get("signal", "?")
            return ("warn", f"on Wi-Fi ({sig} signal). Ethernet is far steadier; auto buffer will compensate")
        return ("ok", "wired (or Wi-Fi not in use)")

    def c_power():
        if os.name != "nt":
            return ("warn", "skipped (not Windows)")
        out = _run(["powercfg", "/getactivescheme"])
        name = out.split("(")[-1].rstrip(")\n ") if "(" in out else "unknown"
        if "saver" in name.lower():
            fixes.append("powercfg /setactive SCHEME_MIN      (High performance)")
            return ("warn", f"power plan '{name}' can starve audio")
        return ("ok", f"power plan '{name}'")

    def c_ip():
        ips = host_ips()
        return ("ok", ", ".join(ips)) if ips != ["127.0.0.1"] else ("warn", "no LAN address found")

    checks = [("operating system", c_os), ("python", c_py), ("libraries", c_libs), ("PyAudioWPatch", c_pa),
              ("loopback source", c_loop), ("output devices", c_outs), ("opus codec", c_opus),
              ("agent", c_agent), ("port", c_ports), ("firewall", c_fw), ("network link", c_link),
              ("power plan", c_power), ("lan addresses", c_ip)]
    look = {"ok": ("✓", LIME), "warn": ("!", GOLD), "bad": ("✕", RED)}
    done: List[Tuple[str, str, str]] = []

    def view(current: Optional[str], t: float):
        out = Text()
        out.append_text(gradient("Running diagnostics. Please hold your applause.", t))
        out.append("\n")
        for name, lvl, detail in done:
            g, c = look[lvl]
            out.append(f"  {g} ", style="bold " + c)
            out.append(f"{name:<18}", style="#ffffff")
            out.append(detail + "\n", style=GREY if lvl == "ok" else c)
        if current:
            out.append(f"  {SPIN[int(t * 10) % 4]} ", style="bold " + PINK)
            out.append(f"{current:<18}", style="#ffffff")
            out.append("checking...", style="dim")
        return out

    start = time.perf_counter()
    if ANIM:
        with Live(view(None, 0), console=console, auto_refresh=False) as live:
            for name, fn in checks:
                res: dict = {}
                th = threading.Thread(target=lambda: res.update(r=fn()), daemon=True)
                th.start()
                t0 = time.perf_counter()
                while th.is_alive() or time.perf_counter() - t0 < 0.18:
                    live.update(view(name, time.perf_counter() - start), refresh=True)
                    time.sleep(0.04)
                lvl, detail = res.get("r", ("bad", "check crashed"))
                done.append((name, lvl, detail))
            live.update(view(None, time.perf_counter() - start), refresh=True)
    else:
        for name, fn in checks:
            lvl, detail = fn()
            done.append((name, lvl, detail))
        console.print(view(None, 0))
    bad = any(lvl == "bad" for _, lvl, _ in done)
    if fixes:
        fx = Text()
        for f in dict.fromkeys(fixes):
            fx.append("  $ ", style=PINK)
            fx.append(f + "\n", style="bold " + CYAN)
        fx.rstrip()
        card("Fixes", fx, sass("doctor_bad"), border=GOLD)
    else:
        console.print(Text("  " + sass("doctor_bad" if bad else "doctor_ok"), style="italic #ff9de2"))
    return 1 if bad else 0


def cmd_banner() -> int:
    play(lambda t: Group(big(LOGO, t), wave(51, t, 0.8),
                         Text("LAN audio rooms, with attitude.  v" + VERSION, style="italic #ff9de2")), 1.4)
    tbl = Table.grid(padding=(0, 3))
    for c, what in ((f"{PROG} serve", "start the agent (keep that terminal open)"),
                    (f"{PROG} doctor", "check this PC is ready"),
                    ("", ""),
                    ("HOST", ""),
                    (f"{PROG} group create <g>", "make a group"),
                    (f"{PROG} group add <g> <ip> --name <who>", "add someone, prints their token"),
                    (f"{PROG} group token|remove|list|delete", "manage people and passes"),
                    (f"{PROG} room start <g>", "go live (lossless, auto buffer)"),
                    (f"{PROG} room status", "dashboard: let people in, buffer, stop"),
                    (f"{PROG} room latency <ms|auto>", "set the playout buffer"),
                    (f"{PROG} room approve|deny|kick", "the same, from the command line"),
                    (f"{PROG} room stop [<g>]", "stop one room, or ALL rooms with no group"),
                    ("", ""),
                    ("MEMBER", ""),
                    (f"{PROG} token add <TOKEN>", "one-time setup of a pass"),
                    (f"{PROG} groups", "your groups and which are live"),
                    (f"{PROG} join <g>", "join a group's room by name"),
                    (f"{PROG} join <host-ip>", "no pass? knock and the host lets you in"),
                    (f"{PROG} leave", "leave the room"),
                    ("", ""),
                    (f"{PROG} devices [--set 1,3]", "pick output devices"),
                    (f"{PROG} ping <ip> · status", "check another agent / this agent")):
        tbl.add_row(Text(c, style=("bold " + GOLD) if c in ("HOST", "MEMBER") else "bold " + CYAN),
                    Text(what, style=GREY))
    console.print(tbl)
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help=f"this PC's agent port (default {DEFAULT_PORT})")
    common.add_argument("--no-anim", action="store_true", default=argparse.SUPPRESS, help="plain output")

    ap = argparse.ArgumentParser(prog=PROG, description="LAN-AUDIO: LAN audio rooms, with attitude.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"agent port (default {DEFAULT_PORT})")
    ap.add_argument("--no-anim", action="store_true", help="plain output, no animations")
    ap.add_argument("--version", action="version", version=f"lan-audio {VERSION}")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("serve", parents=[common], help="start the agent (keep it running)")
    p.add_argument("--name", help="how you appear to others (saved)")

    p = sub.add_parser("devices", parents=[common], help="list output devices, or pick them for rooms")
    p.add_argument("--set", metavar="N,N|all|default", help="devices rooms play on (saved)")

    g = sub.add_parser("group", parents=[common], help="host: manage groups and member passes")
    gs = g.add_subparsers(dest="gcmd", metavar="ACTION", required=True)
    gs.add_parser("create", parents=[common], help="create a group").add_argument("name")
    ga = gs.add_parser("add", parents=[common], help="add someone and print their one-time token")
    ga.add_argument("group")
    ga.add_argument("peer", nargs="?", metavar="IP", help="their IP (for your reference)")
    ga.add_argument("--name", help="display name")
    ga.add_argument("--host-ip", help="the IP members should use to reach YOU (if auto-detect picks the wrong one)")
    gt = gs.add_parser("token", parents=[common], help="show someone's token again")
    gt.add_argument("group")
    gt.add_argument("who")
    gt.add_argument("--host-ip", help="the IP members should use to reach YOU")
    gr = gs.add_parser("remove", parents=[common], help="remove someone (revokes their token)")
    gr.add_argument("group")
    gr.add_argument("who")
    gs.add_parser("list", parents=[common], help="show groups").add_argument("group", nargs="?")
    gs.add_parser("delete", parents=[common], help="delete a group").add_argument("group")

    t = sub.add_parser("token", parents=[common], help="member: set up passes you received")
    ts = t.add_subparsers(dest="tcmd", metavar="ACTION", required=True)
    ts.add_parser("add", parents=[common], help="save a token (one time)").add_argument("token", nargs="+")
    ts.add_parser("remove", parents=[common], help="forget a pass").add_argument("group", metavar="GROUP[@HOST]")
    ts.add_parser("list", parents=[common], help="same as `groups`")
    sub.add_parser("groups", parents=[common], help="member: your groups and which ones are live")

    sub.add_parser("ping", parents=[common], help="check another agent").add_argument("target", metavar="IP[:PORT]")

    r = sub.add_parser("room", parents=[common], help="host and manage rooms")
    rs = r.add_subparsers(dest="rcmd", metavar="ACTION", required=True)
    st = rs.add_parser("start", parents=[common], help="go live for a group")
    st.add_argument("group", nargs="?")
    st.add_argument("--group", dest="group_opt", help=argparse.SUPPRESS)
    st.add_argument("--latency", default="auto",
                    help=f"playout buffer: 'auto' (default, starts {AUTO_START_MS} ms and grows if needed) or ms")
    st.add_argument("--codec", choices=CODECS, default="pcm24",
                    help="pcm24 = lossless 24-bit (default), pcm16 = CD lossless, opus = 510 kbps for weak Wi-Fi")
    st.add_argument("--detach", action="store_true", help="don't open the live dashboard")
    rs.add_parser("status", parents=[common], help="live dashboard")
    sp = rs.add_parser("stop", parents=[common], help="stop one room, or every room if no group is given")
    sp.add_argument("group", nargs="?")
    sp.add_argument("--all", action="store_true", help="stop every room")
    kp = rs.add_parser("kick", parents=[common], help="remove a member")
    kp.add_argument("name")
    kp.add_argument("--group")
    lp = rs.add_parser("latency", parents=[common], help="set the playout buffer (ms or auto)")
    lp.add_argument("ms", metavar="MS|auto")
    lp.add_argument("--group")
    rs.add_parser("approve", parents=[common], help="let a knocking guest in").add_argument("name", nargs="?")
    rs.add_parser("deny", parents=[common], help="turn a knocking guest away").add_argument("name", nargs="?")

    j = sub.add_parser("join", parents=[common], help="join a group's room (or knock on an IP)")
    j.add_argument("target", nargs="?", metavar="GROUP|HOST-IP")
    j.add_argument("--group", help="with an IP: which of the host's rooms")
    j.add_argument("--devices", metavar="N,N|all", help="output devices for this session")
    j.add_argument("--offset", action="append", metavar="MS|NAME=MS",
                   help="delay a device (or all) to line up Bluetooth, e.g. Realtek=180")
    j.add_argument("--volume", type=float, help="0.0-2.0 (default from config, 1.0)")
    j.add_argument("--detach", action="store_true", help="don't open the live dashboard")

    sub.add_parser("leave", parents=[common], help="leave the room you're in")
    sub.add_parser("status", parents=[common], help="what this agent is doing")
    sub.add_parser("doctor", parents=[common], help="check this PC is ready")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    global ANIM, PROG
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_names"]:
        pa = pa_mod().PyAudio()
        try:
            print(json.dumps(sorted({d["name"] for d in wasapi_devices(pa)["outputs"]})))
        finally:
            pa.terminate()
        return 0
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    base = os.path.basename(sys.argv[0] or "")
    PROG = f"python {base}" if base.endswith(".py") else "lanaudio"
    parser = build_parser()
    parser.prog = PROG
    args = parser.parse_args(argv)
    ANIM = console.is_terminal and not args.no_anim and not os.environ.get("LANAUDIO_NO_ANIM")
    handlers = {"serve": cmd_serve, "devices": cmd_devices, "group": cmd_group, "token": cmd_token,
                "groups": cmd_groups, "ping": cmd_ping, "room": cmd_room, "join": cmd_join, "leave": do_leave,
                "status": cmd_status, "doctor": cmd_doctor}
    try:
        if not args.cmd:
            return cmd_banner()
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        console.print(Text("\nInterrupted. Fine, be that way.", style="italic #ff9de2"))
        return 130


if __name__ == "__main__":
    sys.exit(main())
