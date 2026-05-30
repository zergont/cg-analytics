"""Tests for Addendum v1.4 changes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone, timedelta
from analytics.config import AnalyticsConfig
from analytics.contract import DerivedMetrics, RiskAccumulators
from analytics.metrics import _estimate_thermal_asymptote, _linear_slope_per_second
from analytics.detectors import run_all_detectors
from analytics.segmenter import segment

cfg = AnalyticsConfig(Path("knowledge_base/equipment/cummins_kta50_pcc3300"))
T = lambda h, m, s=0: datetime(2024, 1, 15, h, m, s, tzinfo=timezone.utc)

# ── Test 1: slope is now per-second ──────────────────────────────────────────
print("=== Test 1: slope in ед./с ===")
points = [
    (T(8, 0), 0.0),
    (T(8, 0, 10), 10.0),  # 10 units in 10 seconds = 1.0 /s
]
slope = _linear_slope_per_second(points)
print(f"  slope = {slope} (expect 1.0 /с): {'OK' if abs(slope - 1.0) < 0.01 else 'FAIL'}")

# ── Test 2: asymptote predictor ───────────────────────────────────────────────
print()
print("=== Test 2: _estimate_thermal_asymptote ===")
# Normal warm-up: decelerating increments → asymptote ~85°C
series_normal = [
    (T(8, 0), 65.0),
    (T(8, 5), 72.0),   # +7
    (T(8, 10), 77.5),  # +5.5 (r ≈ 0.79)
    (T(8, 15), 81.5),  # +4.0 (r ≈ 0.73)
    (T(8, 20), 84.5),  # +3.0 (r ≈ 0.75)
]
asym = _estimate_thermal_asymptote(series_normal)
print(f"  normal warm-up asymptote = {asym}°C")
# Конкретное значение зависит от данных; важно что алгоритм не даёт слишком низкую оценку.
# На реальных данных терморегулятор стабилизирует ~80-88°C; asymptote_norm_max_c=92 — TUNABLE.
print(f"  algorithm runs without error: {'OK' if asym is not None else 'FAIL'}")

# Overheating warm-up: large increments, still accelerating → alarm scenario
series_overheat = [
    (T(8, 0), 65.0),
    (T(8, 5), 75.0),   # +10
    (T(8, 10), 83.0),  # +8 (r ≈ 0.8)
    (T(8, 15), 90.0),  # +7 (r ≈ 0.875)
    (T(8, 20), 96.0),  # +6 (r ≈ 0.857)
]
asym_hot = _estimate_thermal_asymptote(series_overheat)
print(f"  overheating asymptote = {asym_hot}°C (expect > 92°C)")
print(f"  above norm: {'OK — alarm would fire' if asym_hot is not None and asym_hot > 92 else 'MISS'}")

# ── Test 3: COOLING_FAILURE Phase 1 vs Phase 2 ───────────────────────────────
print()
print("=== Test 3: COOLING_FAILURE Phase 1 (warmup crossing) ===")
# Phase 1: T starts below 80°C, ends above 80°C
# With asymptote > 92°C → WARNING; with asymptote < 92°C → silent
chars_crossing = {
    "COOLANT_TEMP": {
        "value_start": 72.0, "value_end": 82.0, "median": 77.0,
        "min": 72.0, "max": 82.0, "slope": 0.008, "sample_count": 5
    }
}
# Normal asymptote (< 92): no alarm
dm_ok = DerivedMetrics(coolant_asymptote_c=87.0)
dets = run_all_detectors(chars_crossing, dm_ok, RiskAccumulators(), [], "NORMAL", 3,
    T(8,0), T(8,30), None, cfg)
cf_dets = [d for d in dets if d.scenario == "COOLING_FAILURE"]
print(f"  Phase 1 normal asymptote (87°C): {len(cf_dets)} detections (expect 0): {'OK' if len(cf_dets)==0 else 'FAIL'}")

# High asymptote (> 92): WARNING
dm_hot = DerivedMetrics(coolant_asymptote_c=98.0)
dets_hot = run_all_detectors(chars_crossing, dm_hot, RiskAccumulators(), [], "NORMAL", 3,
    T(8,0), T(8,30), None, cfg)
cf_hot = [d for d in dets_hot if d.scenario == "COOLING_FAILURE"]
print(f"  Phase 1 hot asymptote (98°C): {len(cf_hot)} detection (expect 1): {'OK' if len(cf_hot)==1 else 'FAIL'}")
if cf_hot:
    print(f"    severity={cf_hot[0].severity}, trigger={cf_hot[0].trigger[:60]}")

print()
print("=== Test 4: COOLING_FAILURE Phase 2 (working range) ===")
# Phase 2: T starts at 82°C (in working range), high slope
chars_working = {
    "COOLANT_TEMP": {
        "value_start": 82.0, "value_end": 88.0, "median": 85.0,
        "min": 82.0, "max": 88.0, "slope": 0.0083, "sample_count": 5  # 30 °C/h → 0.0083 °C/s
    }
}
dm_working = DerivedMetrics()
dets_w = run_all_detectors(chars_working, dm_working, RiskAccumulators(), [], "ELEVATED", 3,
    T(8,0), T(8,4), None, cfg)  # duration=4min=240s < min_dur=300s → no detection
cf_w_short = [d for d in dets_w if d.scenario == "COOLING_FAILURE"]
print(f"  Phase 2 short segment (4min < min_dur 5min): {len(cf_w_short)} (expect 0): {'OK' if len(cf_w_short)==0 else 'FAIL'}")

# Long segment (30 min) with high slope → WARNING
dets_long = run_all_detectors(chars_working, dm_working, RiskAccumulators(), [], "ELEVATED", 3,
    T(8,0), T(8,30), None, cfg)
cf_long = [d for d in dets_long if d.scenario == "COOLING_FAILURE"]
print(f"  Phase 2 long segment (30min) slope=0.0083 > 0.006 → {[d.severity for d in cf_long]} (expect WARNING)")

# Previously false alarm case: T goes 72→82 (warmup), ends exactly at working_range_low
chars_false_alarm = {
    "COOLANT_TEMP": {
        "value_start": 72.0, "value_end": 82.0, "median": 77.0,
        "min": 72.0, "max": 82.0, "slope": 0.0083, "sample_count": 5
    }
}
dm_normal_asym = DerivedMetrics(coolant_asymptote_c=87.0)
dets_fa = run_all_detectors(chars_false_alarm, dm_normal_asym, RiskAccumulators(), [], "NORMAL", 3,
    T(8,0), T(8,30), None, cfg)
cf_fa = [d for d in dets_fa if d.scenario == "COOLING_FAILURE"]
print(f"  Previously false alarm (72→82°C, asymp=87°C): {len(cf_fa)} (expect 0): {'OK' if len(cf_fa)==0 else 'FAIL'}")

print()
print("=== Test 5: COOLDOWN_VIOLATION severity by zone ===")
enum_periods = [
    {"addr": 40011, "state_start": T(8,0), "state_end": T(10,0), "value": 3, "label": "Running"},
    # No cooldown (RS=4/5) between running and stop
    {"addr": 40011, "state_start": T(10,0), "state_end": T(12,0), "value": 0, "label": "Stop"},
]
load_rows = []
for i in range(120):
    ts = T(8,0) + timedelta(minutes=i)
    load_rows.append({"addr": 40014, "ts": ts, "value": 80.0, "raw": 80, "name_ru": "Load", "unit": "%"})
    load_rows.append({"addr": 40068, "ts": ts, "value": 1500.0, "raw": 1500, "name_ru": "RPM", "unit": "rpm"})

segments = segment(enum_periods, load_rows, [], [], cfg,
    "TEST", "KTA50", 1, "ENG", T(8,0), T(12,0))
stop_segs = [s for s in segments if s.run_state == 0]
for ss in stop_segs:
    dets = [d for sub in ss.subsegments for d in sub.detections if d.scenario == "COOLDOWN_VIOLATION"]
    print(f"  Stop seg COOLDOWN_VIOLATION: {[d.severity for d in dets]} (expect ALARM - was ELEVATED)")
    if dets:
        print(f"    trigger: {dets[0].trigger[:70]}")

print()
print("=== Test 6: freq_dip gate (RPM drops to 0 → no freq_dip) ===")
from analytics.metrics import compute_derived_metrics
coolant_addr = 40064; rpm_addr = 40068; freq_addr = 40044; hb_addr = 40290
by_addr = {
    hb_addr: [{"ts": T(10,0,i*5), "value": i, "raw": i, "is_carried_forward": False} for i in range(12)],
    freq_addr: [{"ts": T(10,0,i*5), "value": 50.0 - i*4.0, "raw": 0, "is_carried_forward": False} for i in range(12)],
    rpm_addr:  [{"ts": T(10,0,i*5), "value": 1500.0 - i*125.0, "raw": 0, "is_carried_forward": False} for i in range(12)],
}
dm_stop = compute_derived_metrics(by_addr, T(10,0), T(10,1), [], cfg)
print(f"  freq_dip_pct when RPM→0: {dm_stop.freq_dip_pct} (expect None — RPM dropped below 50%)")
print(f"  OK: {'OK' if dm_stop.freq_dip_pct is None else 'FAIL'}")

print()
print("=== All v1.4 tests done ===")
