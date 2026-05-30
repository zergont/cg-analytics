"""Test v1.3 changes: freq transient metrics, LOAD_STEP ISO 8528-5 classification."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone, timedelta
from analytics.config import AnalyticsConfig
from analytics.contract import DerivedMetrics, RiskAccumulators
from analytics.metrics import _freq_transient_metrics
from analytics.detectors import run_all_detectors

cfg = AnalyticsConfig(Path("knowledge_base/equipment/cummins_kta50_pcc3300"))
t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

print("=== Test 1: freq_transient_metrics ===")
freq_series = [
    (t0, 50.0),
    (t0 + timedelta(seconds=1), 48.5),
    (t0 + timedelta(seconds=2), 47.5),   # min: 5% below 50
    (t0 + timedelta(seconds=3), 49.0),
    (t0 + timedelta(seconds=5), 49.8),   # recovered to within 0.5%
]
dip, rise, rec = _freq_transient_metrics(freq_series, 50.0, 0.5, t0)
print(f"  freq_dip_pct = {dip}  (expect ~5.0)")
print(f"  freq_rise_pct = {rise}  (expect None)")
print(f"  freq_recovery_sec = {rec}  (expect ~5.0)")

print()
print("=== Test 2: LOAD_STEP ISO G3 classification ===")
# G3 norm: drop<7%, recovery<3s
# Our case: dip=5% (ok), rec=5s (exceeds 3s) -> WARNING
dm = DerivedMetrics(dP_dt_max=60.0, freq_dip_pct=dip, freq_recovery_sec=rec)
dets = run_all_detectors(
    {}, dm, RiskAccumulators(), [], "NORMAL", 3,
    t0, t0 + timedelta(seconds=60), None, cfg
)
load_step = [d for d in dets if d.scenario == "LOAD_STEP"]
for d in load_step:
    print(f"  LOAD_STEP: severity={d.severity}")
    print(f"  violations={d.values.get('violations')}")
    print(f"  trigger: {d.trigger}")
if not load_step:
    print("  No LOAD_STEP (unexpected)")

print()
print("=== Test 3: LOAD_STEP in RS=0 should NOT fire ===")
dets0 = run_all_detectors(
    {}, dm, RiskAccumulators(), [], "NA", 0,
    t0, t0 + timedelta(seconds=60), None, cfg
)
ls0 = [d for d in dets0 if d.scenario == "LOAD_STEP"]
print(f"  LOAD_STEP in RS=0: {len(ls0)} detections (expect 0): {'OK' if len(ls0)==0 else 'FAIL'}")

print()
print("=== Test 4: LOAD_STEP within G3 norms -> INFO ===")
# G3 norm: drop<7%, recovery<3s
# Our case: dip=3% (ok), rec=2s (ok) -> INFO
dm_ok = DerivedMetrics(dP_dt_max=60.0, freq_dip_pct=3.0, freq_recovery_sec=2.0)
dets_ok = run_all_detectors(
    {}, dm_ok, RiskAccumulators(), [], "ELEVATED", 3,
    t0, t0 + timedelta(seconds=60), None, cfg
)
ls_ok = [d for d in dets_ok if d.scenario == "LOAD_STEP"]
for d in ls_ok:
    print(f"  LOAD_STEP: severity={d.severity} (expect INFO)")
    print(f"  violations={d.values.get('violations')}")

print()
print("=== Test 5: DerivedMetrics new fields in to_dict ===")
dm_full = DerivedMetrics(
    dP_dt_max=60.0,
    freq_dip_pct=5.0,
    freq_rise_pct=None,
    freq_recovery_sec=3.5,
)
d_dict = dm_full.to_dict()
assert "freq_dip_pct" in d_dict, "freq_dip_pct missing from to_dict"
assert "freq_rise_pct" in d_dict, "freq_rise_pct missing from to_dict"
assert "freq_recovery_sec" in d_dict, "freq_recovery_sec missing from to_dict"
print(f"  freq_dip_pct={d_dict['freq_dip_pct']}")
print(f"  freq_recovery_sec={d_dict['freq_recovery_sec']}")
print("  OK")

print()
print("=== All tests passed ===")
