# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Tests for Addendum TZ v1.1 changes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone, timedelta
from analytics.config import AnalyticsConfig
from analytics.segmenter import segment
from analytics.serializer import to_markdown

kb = Path("knowledge_base/equipment/cummins_kta50_pcc3300")

# ── Test 1: Config validation ───────────────────────────────────────────────
print("=== Test 1: Config validation ===")
try:
    cfg = AnalyticsConfig(kb)
    print("OK: Config loaded and validated")
    decay = cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec")
    hot_warmup = cfg.det("WARMUP_VIOLATION", "hot_start_warmup_sec")
    print(f"  thermal_decay_rate_per_sec = {decay}")
    print(f"  hot_start_warmup_sec = {hot_warmup}")
except Exception as e:
    print(f"FAIL: {e}")

print()

# ── Test 2: Invalid config rejection ───────────────────────────────────────
print("=== Test 2: Invalid config rejection ===")
import yaml, tempfile, os
from analytics.config import AnalyticsConfig as AC

bad_yaml_dir = tempfile.mkdtemp()
analytics_dir = Path(bad_yaml_dir) / "analytics"
analytics_dir.mkdir()

# Copy good configs then overwrite zones with bad one
for fname in ["mapping.yaml", "thresholds.yaml", "segmentation.yaml", "detectors.yaml", "fault_matrix.yaml"]:
    (analytics_dir / fname).write_bytes((kb / "analytics" / fname).read_bytes())

# Write broken zones.yaml (max < min)
bad_zones = {
    "load_zones": {
        "LOW": {"min_pct": 0, "max_pct": 30},
        "NORMAL": {"min_pct": 30, "max_pct": 20},  # max < min!
        "ELEVATED": {"min_pct": 70, "max_pct": 100},
        "OVERLOAD": {"min_pct": 100},
    },
    "hysteresis": {"pct": 5.0},
    "NA": {},
}
(analytics_dir / "zones.yaml").write_text(yaml.dump(bad_zones), encoding="utf-8")

try:
    AC(bad_yaml_dir)
    print("FAIL: Should have rejected bad config")
except ValueError as e:
    lines = str(e).split("\n")
    print(f"OK: Bad config rejected ({len(lines)} error lines)")
    for line in lines[:4]:
        print(f"  {line}")

# Cleanup
import shutil
shutil.rmtree(bad_yaml_dir)
print()

# ── Test 3: Thermal decay ──────────────────────────────────────────────────
print("=== Test 3: Thermal decay in accumulators ===")
from analytics.contract import RiskAccumulators
from analytics.accumulators import update_accumulators

cfg = AnalyticsConfig(kb)
acc = RiskAccumulators()

def T(h, m):
    return datetime(2024, 1, 15, h, m, tzinfo=timezone.utc)

# Simulate 2 hours in ELEVATED
acc = update_accumulators(acc, {}, T(8, 0), T(10, 0), "ELEVATED", 3, cfg)
elev_after = acc.thermal_risk.elevated_zone_sec
print(f"  After 2h in ELEVATED: elevated_zone_sec={elev_after:.0f}s (expect 7200)")
print(f"  Risk level: {acc.thermal_risk.risk_level} (expect RED)")

# Simulate 1 hour NOT in ELEVATED (decay)
acc = update_accumulators(acc, {}, T(10, 0), T(11, 0), "NORMAL", 3, cfg)
decay_rate = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec"))
expected_after_decay = max(0, 7200 - 3600 * decay_rate)
print(f"  After 1h in NORMAL (decay={decay_rate}): elevated_zone_sec={acc.thermal_risk.elevated_zone_sec:.0f}s (expect {expected_after_decay:.0f})")
print(f"  Risk level: {acc.thermal_risk.risk_level}")
print()

# ── Test 4: Inter-segment checks (warmup/cooldown) ─────────────────────────
print("=== Test 4: Inter-segment checks ===")
load_addr = 40014
coolant_addr = 40064

def make_ts(h, m, s=0):
    return datetime(2024, 1, 15, h, m, s, tzinfo=timezone.utc)

enum_periods = [
    {"addr": 40011, "state_start": make_ts(7, 55), "state_end": make_ts(8, 0), "value": 1, "label": "Start"},
    # SHORT warmup: only 30s (below 180s min for cold start)
    {"addr": 40011, "state_start": make_ts(8, 0), "state_end": make_ts(8, 0, 30), "value": 2, "label": "Warmup"},
    {"addr": 40011, "state_start": make_ts(8, 0, 30), "state_end": make_ts(10, 0), "value": 3, "label": "Running"},
    # No cooldown (RUN_STATE 5) before stop → COOLDOWN_VIOLATION
    {"addr": 40011, "state_start": make_ts(10, 0), "state_end": make_ts(12, 0), "value": 0, "label": "Stop"},
]

history = []
# Cold coolant at warmup start (15°C < 21°C threshold → cold start)
for i in range(10):
    ts = make_ts(8, 0) + timedelta(minutes=i)
    history.append({"addr": coolant_addr, "ts": ts, "value": 15.0, "raw": 150, "name_ru": "T ОЖ", "unit": "°C"})
# ELEVATED load during running (80%)
for i in range(115):
    ts = make_ts(8, 0, 30) + timedelta(minutes=i)
    history.append({"addr": load_addr, "ts": ts, "value": 80.0, "raw": 80, "name_ru": "Нагрузка", "unit": "%"})
    history.append({"addr": coolant_addr, "ts": ts, "value": 88.0, "raw": 880, "name_ru": "T ОЖ", "unit": "°C"})

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
    ts_from=make_ts(7, 50),
    ts_to=make_ts(12, 0),
)

print(f"  Segments: {len(segments)}")
for seg in segments:
    det_names = [d.scenario for d in seg.subsegments[0].detections] if seg.subsegments else []
    checks = [(c["check"], c["passed"]) for c in seg.sequence_checks]
    print(f"  [{seg.run_state}] {seg.run_state_label}: detections={det_names} checks={checks}")

# Expect WARMUP_VIOLATION in running segment and COOLDOWN_VIOLATION in stop segment
running = next((s for s in segments if s.run_state == 3), None)
stop = next((s for s in segments if s.run_state == 0), None)
if running:
    warmup_det = [d for d in running.subsegments[0].detections if d.scenario == "WARMUP_VIOLATION"]
    cold_ctx = next((c for c in running.sequence_checks if c["check"] == "cold_start_context"), None)
    print(f"  WARMUP_VIOLATION in running: {'OK' if warmup_det else 'MISSING'}")
    if cold_ctx:
        print(f"  cold_start_context: cold_start={cold_ctx.get('cold_start')}, T={cold_ctx.get('coolant_at_start_c')}°C")

if stop:
    cooldown_det = [d for d in stop.subsegments[0].detections if d.scenario == "COOLDOWN_VIOLATION"]
    print(f"  COOLDOWN_VIOLATION in stop: {'OK' if cooldown_det else 'MISSING'}")

print()
print("=== All tests done ===")
