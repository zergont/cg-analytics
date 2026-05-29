"""Quick end-to-end test for segment() with mock data."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone, timedelta
from analytics.segmenter import segment
from analytics.config import AnalyticsConfig
from analytics.serializer import to_markdown

kb = Path("knowledge_base/equipment/cummins_kta50_pcc3300")
cfg = AnalyticsConfig(kb)

def T(h, m, s=0):
    return datetime(2024, 1, 15, h, m, s, tzinfo=timezone.utc)

enum_periods = [
    {"addr": 40011, "state_start": T(8, 0), "state_end": T(8, 5), "value": 2, "label": "Warmup"},
    {"addr": 40011, "state_start": T(8, 5), "state_end": T(10, 0), "value": 3, "label": "Running/Rated"},
    {"addr": 40011, "state_start": T(10, 0), "state_end": T(10, 5), "value": 5, "label": "Cooldown"},
]

load_addr = 40014
coolant_addr = 40064

history = []
# 8:05-9:00 — NORMAL zone (40%)
for i in range(55):
    ts = T(8, 5) + timedelta(minutes=i)
    history.append({"addr": load_addr, "ts": ts, "value": 40.0, "raw": 40, "name_ru": "Нагрузка", "unit": "%"})
# 9:00-10:00 — ELEVATED zone (80%)
for i in range(60):
    ts = T(9, 0) + timedelta(minutes=i)
    history.append({"addr": load_addr, "ts": ts, "value": 80.0, "raw": 80, "name_ru": "Нагрузка", "unit": "%"})
# coolant temp throughout
for i in range(130):
    ts = T(8, 0) + timedelta(minutes=i)
    history.append({"addr": coolant_addr, "ts": ts, "value": 85.0, "raw": 850, "name_ru": "T ОЖ", "unit": "°C"})

segments = segment(
    enum_periods=enum_periods,
    history=history,
    fault_periods=[],
    gaps=[],
    cfg=cfg,
    router_sn="TEST001",
    equip_type="KTA50",
    panel_id=1,
    engine_sn="ENG001",
    ts_from=T(8, 0),
    ts_to=T(10, 30),
)

print(f"Segments: {len(segments)}")
for seg in segments:
    print(f"  [{seg.run_state}] {seg.run_state_label}: {seg.t_start[:19]} -> {seg.t_end[:19]}")
    print(f"    duration={seg.duration_sec}s subsegments={len(seg.subsegments)} dq={seg.data_quality}")
    for sub in seg.subsegments:
        det_names = [d.scenario for d in sub.detections]
        print(f"    sub {sub.id}: zone={sub.load_zone} dur={sub.duration_sec}s chars={len(sub.characteristics)} det={det_names}")
        print(f"      open={sub.cause_open} close={sub.cause_close}")

print()
print("=== Markdown report (first 50 lines) ===")
md = to_markdown(segments, "TEST001", "KTA50", 1, T(8, 0), T(10, 30))
for line in md.split("\n")[:50]:
    print(line)
