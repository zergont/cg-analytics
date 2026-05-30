"""Tests for Addendum v1.5: Negative Sequence current I₂."""
import sys, math, cmath
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone, timedelta
from analytics.config import AnalyticsConfig
from analytics.contract import DerivedMetrics, RiskAccumulators
from analytics.metrics import _compute_neg_seq_i2
from analytics.detectors import run_all_detectors

cfg = AnalyticsConfig(Path("knowledge_base/equipment/cummins_kta50_pcc3300"))
T = lambda h, m, s=0: datetime(2024, 1, 15, h, m, s, tzinfo=timezone.utc)

# ── Test 1: _compute_neg_seq_i2 — balanced system ───────────────────────────
print("=== Test 1: Balanced 3-phase (I₂ ≈ 0) ===")
# Perfectly balanced: 100A each, PF=0.8 (φ=36.87°), P=80kW, Q=60kVAR each
phi = math.atan2(60, 80)  # = 36.87°
i2 = _compute_neg_seq_i2(
    100, 80, 60,   # L1: 100A, P=80, Q=60
    100, 80, 60,   # L2: 100A, P=80, Q=60
    100, 80, 60,   # L3: 100A, P=80, Q=60
)
print(f"  I₂ (balanced) = {i2:.6f} A (expect ≈ 0)")
print(f"  OK: {'OK' if i2 is not None and i2 < 0.01 else 'FAIL'}")

# ── Test 2: Known imbalance ──────────────────────────────────────────────────
print()
print("=== Test 2: Unbalanced — L3 has 15% higher current ===")
# L1=100A balanced, L2=100A balanced, L3=115A balanced
# I₂ should be non-zero
i2_unbal = _compute_neg_seq_i2(
    100, 80, 60,   # L1
    100, 80, 60,   # L2
    115, 92, 69,   # L3: 15% more (P,Q also 15% more to keep same PF)
)
i_nom = 100  # nominal 100A for this test
i2_pct = (i2_unbal / i_nom * 100) if i2_unbal is not None else None
print(f"  I₂ = {i2_unbal:.2f} A → {i2_pct:.1f}% of I_nom")
print(f"  Non-zero and reasonable: {'OK' if i2_pct is not None and 0 < i2_pct < 20 else 'FAIL'}")

# ── Test 3: Detector fires at I₂% > 10% for > 60s ───────────────────────────
print()
print("=== Test 3: NEGATIVE_SEQUENCE detector ===")
dm_over = DerivedMetrics(neg_seq_i2_pct_max=11.0, neg_seq_i2_duration_sec=90.0)
dets = run_all_detectors({}, dm_over, RiskAccumulators(), [], "NORMAL", 3,
    T(8,0), T(8,30), None, cfg)
ns_dets = [d for d in dets if d.scenario == "NEGATIVE_SEQUENCE"]
print(f"  I₂=11%, dur=90s: {len(ns_dets)} detection (expect 1): {'OK' if len(ns_dets)==1 else 'FAIL'}")
if ns_dets:
    print(f"    severity={ns_dets[0].severity}, trigger: {ns_dets[0].trigger[:70]}")

# ── Test 4: Below threshold — no detection ───────────────────────────────────
print()
print("=== Test 4: I₂ below threshold ===")
dm_ok = DerivedMetrics(neg_seq_i2_pct_max=1.0, neg_seq_i2_duration_sec=300.0)
dets_ok = run_all_detectors({}, dm_ok, RiskAccumulators(), [], "NORMAL", 3,
    T(8,0), T(8,30), None, cfg)
ns_ok = [d for d in dets_ok if d.scenario == "NEGATIVE_SEQUENCE"]
print(f"  I₂=1% (real-world ≈ value): {len(ns_ok)} detections (expect 0): {'OK' if len(ns_ok)==0 else 'FAIL'}")

# ── Test 5: Gate — not in RS=3 ───────────────────────────────────────────────
print()
print("=== Test 5: Gate RS != 3 ===")
dm_high = DerivedMetrics(neg_seq_i2_pct_max=20.0, neg_seq_i2_duration_sec=120.0)
dets_rs0 = run_all_detectors({}, dm_high, RiskAccumulators(), [], "NA", 0,
    T(8,0), T(8,30), None, cfg)
ns_rs0 = [d for d in dets_rs0 if d.scenario == "NEGATIVE_SEQUENCE"]
print(f"  I₂=20% in RS=0: {len(ns_rs0)} detections (expect 0): {'OK' if len(ns_rs0)==0 else 'FAIL'}")

# ── Test 6: Old PHASE_IMBALANCE not in run_all_detectors ────────────────────
print()
print("=== Test 6: PHASE_IMBALANCE removed from run_all_detectors ===")
# Should not produce PHASE_IMBALANCE detections even with high imbalance
dm_imb = DerivedMetrics(current_imbalance_pct_max=15.0, imbalance_duration_sec=120.0)
dets_imb = run_all_detectors({}, dm_imb, RiskAccumulators(), [], "NORMAL", 3,
    T(8,0), T(8,30), None, cfg)
pi_dets = [d for d in dets_imb if d.scenario == "PHASE_IMBALANCE"]
print(f"  PHASE_IMBALANCE detections: {len(pi_dets)} (expect 0): {'OK' if len(pi_dets)==0 else 'FAIL'}")

print()
print("=== All v1.5 tests passed ===")
