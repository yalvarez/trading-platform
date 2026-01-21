from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

@dataclass
class Window:
    start: dtime
    end: dtime

def parse_windows(spec: str) -> list[Window]:
    out: list[Window] = []
    # Si es lista, úsala directamente; si es string, splitea por coma
    if isinstance(spec, list):
        parts = spec
    else:
        parts = (spec or "").split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        sh, sm = a.split(":")
        eh, em = b.split(":")
        out.append(Window(dtime(int(sh), int(sm)), dtime(int(eh), int(em))))
    return out

def in_windows(windows: list[Window], now: datetime | None = None) -> bool:
    now = now or datetime.now(NY)
    t = now.time()
    for w in windows:
        if w.start <= w.end:
            if w.start <= t <= w.end:
                return True
        else:
            # overnight window (rare)
            if t >= w.start or t <= w.end:
                return True
    return False
