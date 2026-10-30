"""Проверки v5.1.26 «Заглушки без связи» (без БД и сети).

- 1. П10: синтетический хвостовой разрыв пустого окна помечен start_unknown
  (начало обрыва неизвестно, это граница окна); окно с данными — без пометки.
- 2. П10: «Последние данные до обрыва» в заглушке суточного реза — не граница
  окна, а запасной вариант (history до окна, last_data_ts движка). Случай #36125:
  ДЭС №3, рез 26.10 09:00 при открытом обрыве с 25.10 00:11:03 — было 09:00:00.
- 3. Автостарт corpus не берёт заглушки без связи (data_quality == 0) без
  записи анализа, как и движок: иначе после рестарта ИИ писал по ним
  заключение (#36065, CAUTION). Заказанные вручную (queued/processing) — берёт.
- 4. Догонка после простоя (ревью): резы закрываются раньше открытого окна —
  память свежести продвигается и по закрытым окнам, иначе заглушка печатала
  бы момент остановки сервиса.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import online.engine as eng  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from corpus import db as corpus_db  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
HB = 40290


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def M(d: int, h: int, m: int, s: int = 0, mo: int = 10) -> datetime:
    """Метка МСК → UTC."""
    return datetime(2026, mo, d, h, m, s, tzinfo=MSK).astimezone(U)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def mk_engine(last_data_ts=None):
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg, e.tz = CFG, MSK
    e.router_sn, e.equip_type, e.panel_id = "1126246373", "pcc", 1
    e.last_data_ts = last_data_ts
    return e


# ══ 1. Пометка синтетического разрыва ══════════════════════════════════════
T0, T1 = M(25, 9, 0), M(26, 9, 0)
_g_empty = eng._synth_connectivity_gap([], T0, T1, CFG)
check(_g_empty == {"gap_start": T0, "gap_end": None, "start_unknown": True},
      f"1а: пустое окно — разрыв от границы окна с пометкой start_unknown: {_g_empty}")
_g_data = eng._synth_connectivity_gap([h(HB, M(26, 6, 2, 10), 1.0)], M(25, 9, 0), T1, CFG)
check(_g_data == {"gap_start": M(26, 6, 2, 10), "gap_end": None},
      f"1б: данные в окне — разрыв от последней строки, без пометки: {_g_data}")
check(eng._synth_connectivity_gap([h(HB, M(26, 8, 59, 30), 1.0)], T0, T1, CFG) is None,
      "1в: свежие данные у конца окна — разрыва нет")

# ══ 2. Последние данные до обрыва ═══════════════════════════════════════════
# #36125: в окне ни строки, data_gaps по обрыву ещё нет (пишется задним
# числом в 26.10 18:56:06), движок помнит 25.10 00:11:03
_e = mk_engine(last_data_ts=M(25, 0, 11, 3))
check(_e._no_data_last_before(T0, [_g_empty]) == M(25, 0, 11, 3),
      f"2а: #36125 — 25.10 00:11:03 из памяти движка, а не 09:00 границы окна: "
      f"{_e._no_data_last_before(T0, [_g_empty])}")
# После рестарта посреди обрыва last_data_ts восстанавливается из observation —
# тот же путь; без него и без history строки нет вовсе (не граница окна)
check(mk_engine(None)._no_data_last_before(T0, [_g_empty]) is None,
      "2б: памяти нет — строки «последние данные» нет, граница окна не подставляется")
# Настоящий разрыв data_gaps, покрывающий начало, по-прежнему главный
_real = {"gap_start": M(25, 0, 11, 3), "gap_end": M(26, 18, 55, 59)}
check(mk_engine(M(26, 18, 56))._no_data_last_before(T0, [_g_empty, _real]) == M(25, 0, 11, 3)
      and mk_engine(M(26, 18, 56))._no_data_last_before(T0, [_real, _g_empty]) == M(25, 0, 11, 3),
      "2в: настоящий разрыв data_gaps — его начало, порядок разрывов не важен")
# Открытый настоящий разрыв (gap_end=None без пометки) — как раньше
_open_real = {"gap_start": M(25, 0, 11, 3), "gap_end": None}
check(mk_engine(None)._no_data_last_before(T0, [_open_real]) == M(25, 0, 11, 3),
      "2г: настоящий открытый разрыв без пометки — его начало")
# Запасной по history до окна раньше памяти движка
check(mk_engine(M(25, 0, 11, 3))._no_data_last_before(T0, [_g_empty], [h(HB, M(24, 23, 50), 1.0)])
      == M(24, 23, 50),
      "2д: строка history до окна — раньше памяти движка")
# Память движка позже начала сегмента не годится (поздние строки, критика M1)
check(mk_engine(M(25, 9, 30))._no_data_last_before(T0, [_g_empty]) is None,
      "2е: last_data_ts позже начала сегмента — не «последние данные до обрыва»")
# Разрыв с данными в окне (без пометки) работает как прежде
check(mk_engine(None)._no_data_last_before(M(26, 7, 0), [_g_data]) == M(26, 6, 2, 10),
      "2ж: синтетический разрыв от последней строки окна — его начало")

# ══ 3. Автостарт без заглушек ═══════════════════════════════════════════════
# Семантика SQL проверена на БД (только чтение) — scratchpad v5126/q_sql.py;
# здесь — что форма запроса та, что проверялась
_src = " ".join(inspect.getsource(corpus_db.get_unanalyzed_segments).split())
check("WHERE c.sa_id IS NOT NULL OR (c.characteristics_json->'data_quality') "
      "IS DISTINCT FROM '0'::jsonb" in _src,
      "3а: заглушка без записи отсекается, заказанная (queued/processing) — нет, "
      "null и отсутствие ключа — берутся")
check("(sa.id IS NULL OR sa.status IN ('queued', 'processing')) OFFSET 0" in _src
      and "::float" not in _src,
      "3б: фильтр после соединения (OFFSET 0), без приведения типа")

# ══ 4. Догонка после простоя: память свежести и по закрытым окнам ═══════════
# Сервис стоял, в observation 24.10 09:59:30; машина слала до 25.10 12:00, потом
# обрыв. Догонка закрывает окно 25.10 (данные до 12:00), затем пустое окно
# 26.10 — заглушка должна напечатать 25.10 12:00, а не момент остановки сервиса
_saved: list = []


async def _fake_save(sn, et, p, ts):
    _saved.append(ts)

eng.online_db.update_observation_last_data_ts = _fake_save
_e4 = mk_engine(last_data_ts=M(24, 9, 59, 30))
asyncio.run(_e4._advance_last_data_ts(M(25, 12, 0)))
check(_e4.last_data_ts == M(25, 12, 0) and _saved == [M(25, 12, 0)],
      f"4а: закрытое окно продвинуло память и записало её: {_e4.last_data_ts} {_saved}")
asyncio.run(_e4._advance_last_data_ts(M(25, 11, 0)))
asyncio.run(_e4._advance_last_data_ts(None))
check(_e4.last_data_ts == M(25, 12, 0) and len(_saved) == 1,
      "4б: только вперёд — старее и None не трогают память и БД")
check(_e4._no_data_last_before(M(26, 9, 0), [eng._synth_connectivity_gap([], M(26, 9, 0), M(27, 9, 0), CFG)])
      == M(25, 12, 0),
      "4в: заглушка следующего пустого окна — 25.10 12:00")

# last_ts_out: последний ts history окна раньше ts_to (с преамбулой, без строк
# за ts_to из окна подтверждения)
import analytics.segmenter as _sg  # noqa: E402

_orig_load, _orig_seg = eng._load_data, _sg.segment


async def _fake_load(sn, et, p, a, b, cfg):
    return ([h(HB, M(25, 8, 57), 1.0), h(HB, M(25, 12, 0), 1.0), h(HB, M(26, 9, 0, 30), 1.0)], [], [], [])

eng._load_data, _sg.segment = _fake_load, (lambda **kw: [])
try:
    _out: dict = {}
    asyncio.run(eng._run_segment("SN", "pcc", 1, "", M(25, 9, 0), M(26, 9, 0), CFG, None,
                                 load_seed=None, last_ts_out=_out))
    check(_out.get("last_ts") == M(25, 12, 0), f"4г: last_ts_out — 25.10 12:00: {_out}")

    async def _fake_load_empty(sn, et, p, a, b, cfg):
        return ([], [], [], [])
    eng._load_data = _fake_load_empty
    _out = {}
    asyncio.run(eng._run_segment("SN", "pcc", 1, "", M(26, 9, 0), M(27, 9, 0), CFG, None,
                                 load_seed=None, last_ts_out=_out))
    check("last_ts" in _out and _out["last_ts"] is None, f"4д: пустое окно — None: {_out}")
finally:
    eng._load_data, _sg.segment = _orig_load, _orig_seg
_cw = inspect.getsource(eng.OnlinePollEngine._close_window)
check(_cw.count("last_ts_out=_win_last") == 2 and "self._advance_last_data_ts(_win_last.get(\"last_ts\"))" in _cw,
      "4е: оба прогона закрытия окна отдают last_ts, закрытие продвигает память")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
