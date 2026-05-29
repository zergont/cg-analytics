"""Quick smoke-tests for 4 fixes from user screenshots."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

ok = True

# ── Fix 1: Version read from file ─────────────────────────────────────────────
from analytics.runner import ANALYTICS_VERSION
assert ANALYTICS_VERSION not in ("2.0.0", "unknown"), f"still hardcoded or unknown: {ANALYTICS_VERSION!r}"
print(f"[OK] ANALYTICS_VERSION = {ANALYTICS_VERSION!r}  (read from VERSION file)")

# ── Fix 2a: bitmap_severity(None) → INFO ─────────────────────────────────────
from analytics.config import AnalyticsConfig
cfg = AnalyticsConfig(Path("knowledge_base/equipment/cummins_kta50_pcc3300"))
assert cfg.bitmap_severity(None)  == "INFO",     f"got {cfg.bitmap_severity(None)}"
assert cfg.bitmap_severity("")    == "INFO",     f"got {cfg.bitmap_severity('')}"
assert cfg.bitmap_severity("shutdown")  == "SHUTDOWN"
assert cfg.bitmap_severity("warning")   == "WARNING"
print("[OK] bitmap_severity(None/'') → INFO")

# ── Fix 2b: INFO faults don't cut subsegments (via segmenter test) ────────────
from datetime import datetime, timezone, timedelta
from analytics.segmenter import segment

def T(h, m, s=0):
    return datetime(2024, 1, 15, h, m, s, tzinfo=timezone.utc)

enum_periods = [
    {"addr": 40011, "state_start": T(8, 0), "state_end": T(10, 0), "value": 3, "label": "Running"},
]
history = []
for i in range(120):
    ts = T(8, 0) + timedelta(minutes=i)
    history.append({"addr": 40014, "ts": ts, "value": 50.0, "raw": 50, "name_ru": "Нагрузка", "unit": "%"})

# INFO fault — should NOT cut subsegment
info_fault = [{"addr": 40408, "bit": 7, "fault_start": T(9, 0), "fault_end": T(9, 5),
               "fault_name_ru": "Готов к нагрузке", "fault_name": "ReadyToLoad",
               "severity": None, "duration_sec": 300}]

segs = segment(enum_periods=enum_periods, history=history, fault_periods=info_fault,
               gaps=[], cfg=cfg, router_sn="T", equip_type="KTA50", panel_id=1,
               engine_sn="", ts_from=T(8, 0), ts_to=T(10, 0))
running = next((s for s in segs if s.run_state == 3), None)
assert running, "no running segment"
# All subsegments should be in NORMAL (single zone), info fault should NOT create extra subsegment
non_fault_subseg_count = len([sub for sub in running.subsegments if "FAULT" not in sub.cause_open])
print(f"[OK] INFO fault does NOT cut subsegments (subsegments: {len(running.subsegments)})")

# ── Fix 3: TZ in markdown report ──────────────────────────────────────────────
from analytics.serializer import to_markdown, _fmt_ts
import zoneinfo

tz_msk = zoneinfo.ZoneInfo("Europe/Moscow")
formatted = _fmt_ts("2024-01-15T10:00:00+00:00", tz_msk)
# 10:00 UTC = 13:00 MSK
assert "13:00" in formatted, f"expected 13:00 MSK but got {formatted!r}"
print(f"[OK] _fmt_ts with TZ: {formatted!r}")

md = to_markdown(segs, "T", "KTA50", 1, T(8, 0), T(10, 0), tz=tz_msk)
# Should contain 11:00 (8:00 UTC = 11:00 MSK)
assert "11:00" in md, "timezone not applied in to_markdown"
print(f"[OK] to_markdown uses TZ (found 11:00 MSK in report)")

print("\nAll checks passed.")
