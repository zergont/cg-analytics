"""Проверки защиты от кривых меток времени (ts_guard, без БД и сети).

Решения владельца 30.10.2026 — docs/plans/design_time_guard.md. Образцы — по
прецедентам ДЭС №3 (1126246373/pcc/1), время МСК:

- 1. История: кадр застывших часов (первый опрос после загрузки роутера,
  подписан временем до выключения) отбрасывается по счётчику 40290 — внутри
  обрыва (30.09) и внутри живых данных (24.10, приём 26.10). Очередь db-writer
  и настоящее опоздание (40290 согласован), пачки без 40290 — остаются. Метки
  из будущего — отбрасываются. Из дублей (addr, ts) — копия, ближайшая к приёму.
- 2. Периоды enum: кривое начало — на момент записи, конец предшественника —
  туда же (30.09); предшественник до окна запрашивается (03.10); период, который
  кадр открыл и живой пакет тут же закрыл, исчезает; метка в будущем (09.10)
  выпрямляет вывернутый период. Нетронутые пары с перекрытием остаются.
- 3. Периоды fault: начало переносится, конец бита, снятого кадром, — нет.
- 4. Разрывы: конец в будущем — на момент записи, разрыв из будущего —
  отбрасывается, конец задним числом (настоящее опоздание) — как есть.
- 5. Обвязка source: SQL читает received_at / created_at / ts_bad, предшественник
  дочитывается только по нужде, куски истории сверяются вместе, сбой проверки
  не роняет чтение.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import source as src  # noqa: E402
from analytics import ts_guard as tg  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
HB = 40290


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def M(d: int, h: int, m: int, s: int = 0, mo: int = 10) -> datetime:
    """Метка МСК → UTC."""
    return datetime(2026, mo, d, h, m, s, tzinfo=MSK).astimezone(U)


def S(sec: float) -> timedelta:
    return timedelta(seconds=sec)


def row(addr, ts, value, lag=8.0, ra=None):
    """Строка history: received_at = ts + lag (или явно ra)."""
    return {"addr": addr, "ts": ts, "value": value, "raw": None, "name_ru": None, "unit": None,
            "received_at": ra if ra is not None else ts + S(lag)}


def keys(rows):
    return [(r["addr"], r["ts"]) for r in rows]


# ── 1. История ─────────────────────────────────────────────────────────────────
# 1а. 30.09: живые строки до 18:04:47, кадр с меткой 18:02:52 приходит 30.09
# 08:44:50 со счётчиком «из 30.09», затем живой поток с 08:44:49
live0 = M(29, 17, 50, mo=9)
pre = [row(HB, live0 + S(9 * i), 232_521_000 + 9 * i) for i in range(8)]
pre += [row(40068, live0 + S(9 * i), 0) for i in range(8)]
frame_ra = M(30, 8, 44, 50, mo=9)
frame = [row(HB, M(29, 18, 2, 52, mo=9), 232_518_240, ra=frame_ra),
         row(40068, M(29, 18, 2, 52, mo=9), 1496, ra=frame_ra + S(0.1)),
         row(40070, M(29, 18, 2, 53, mo=9), 54_429_480, ra=frame_ra + S(0.3)),
         row(40068, M(29, 18, 3, 1, mo=9), 1496, ra=frame_ra + S(0.5))]
live1 = [row(HB, M(30, 8, 44, 59, mo=9) + S(9 * i), 232_518_269 + 9 * i, lag=6) for i in range(4)]
rows = sorted(pre + frame + live1, key=lambda r: (r["ts"], r["addr"], r["received_at"]))
out, st = tg.filter_history(rows, HB)
check(st["frozen"] == 4 and st["future"] == 0, f"1а: кадр 30.09 — 4 строки отброшены: {st}")
check(not any(r["ts"] < M(30, 0, 0, mo=9) and r["ts"] > M(29, 18, 0, mo=9) and r["addr"] == 40068
              and r["value"] == 1496 for r in out), "1а: обороты 1496 кадра не в истории")
check(len(out) == len(rows) - 4 and all("received_at" not in r for r in out),
      "1а: остальные строки на месте, received_at убран")
check(keys(out) == sorted(keys(out), key=lambda k: (k[1], k[0])), "1а: порядок ts, addr сохранён")

# 1б. 24.10 (приём 26.10): кадр внутри живых данных, рядом живые 20:56:02 и 20:59:02;
# 20 пар дублей с живыми строками в ту же секунду пропадают вместе с кадром
t0 = M(24, 20, 56, 2)
live = [row(HB, t0 + S(9 * i), 234_228_000 + 9 * i) for i in range(40)]
live += [row(40034, t0 + S(9 * i), 160 + (i % 3)) for i in range(40)]
fr_ra = M(26, 18, 56, 5)
fr = [row(HB, M(24, 20, 58, 9), 234_390_547, ra=fr_ra), row(HB, M(24, 20, 58, 18), 234_390_556, ra=fr_ra + S(0.5))]
fr += [row(40034, M(24, 20, 58, 9), 148, ra=fr_ra + S(0.1)), row(40070, M(24, 20, 58, 9), 56_182_500, ra=fr_ra + S(0.2))]
dup_ts = t0 + S(9 * 14)            # кадр совпал с живой строкой по секунде
fr += [row(40034, dup_ts, 999, ra=fr_ra + S(0.3))]
rows = sorted(live + fr, key=lambda r: (r["ts"], r["addr"], r["received_at"]))
out, st = tg.filter_history(rows, HB)
check(st["frozen"] == 5 and st["dups"] == 0, f"1б: кадр 26.10 отброшен целиком, дублей нет: {st}")
check(all(r["value"] != 999 for r in out) and all(r["value"] != 56_182_500 for r in out),
      "1б: наработка +43 ч и значение-дубль кадра не попали в историю")

# 1в. Очередь db-writer (20.07): строки опаздывают на 15–27 мин, счётчик согласован
q0 = M(20, 16, 55, mo=7)
rows = [row(HB, q0 + S(10 * i), 226_000_000 + 10 * i, lag=900 + 4 * i) for i in range(180)]
rows += [row(40034, q0 + S(10 * i), 120, lag=900 + 4 * i) for i in range(180)]
rows.sort(key=lambda r: (r["ts"], r["addr"]))
rows = [row(HB, q0 - S(60), 225_999_940, lag=5)] + rows     # живая строка до очереди
out, st = tg.filter_history(rows, HB)
check(len(out) == len(rows) and st["frozen"] == 0, f"1в: очередь db-writer остаётся целиком: {st}")

# 1г. Настоящее опоздание (ДГУ 11.07, 4 ч): счётчик согласован с соседом
r0 = M(11, 13, 0, mo=7)
rows = [row(HB, r0 + S(10 * i), 229_720_000 + 10 * i) for i in range(8)]
rows += [row(HB, r0 + S(60), 229_720_060, ra=r0 + S(4 * 3600)), row(40068, r0 + S(60), 0, ra=r0 + S(4 * 3600))]
rows.sort(key=lambda r: (r["ts"], r["addr"], r["received_at"]))
out, st = tg.filter_history(rows, HB)
check(st["frozen"] == 0 and len(out) == len(rows) - 1 and st["dups"] == 1,
      f"1г: опоздание с верной меткой остаётся (дубль 40290 схлопнут к живой копии): {st}")
check([r for r in out if r["addr"] == HB and r["ts"] == r0 + S(60)][0]["value"] == 229_720_060,
      "1г: из дублей выбрана копия, ближайшая к приёму")

# 1д. Застрявшее сообщение медленной группы (24.10, 50 мин, без 40290) — остаётся
rows = [row(HB, M(24, 5, 40) + S(9 * i), 1000 + 9 * i) for i in range(5)]
rows += [row(40595, M(24, 11, 48, 9), 49.9, ra=M(24, 12, 38, 28))]
rows.sort(key=lambda r: (r["ts"], r["addr"]))
out, st = tg.filter_history(rows, HB)
check(len(out) == len(rows) and st["frozen"] == 0, f"1д: пачка без счётчика не трогается: {st}")

# 1е. Пачка со счётчиком, но без нормального соседа в окне — проверить нечем
rows = [row(HB, M(18, 2, 46, 36, mo=9), 232_492_553, ra=M(22, 7, 0, 2, mo=9)),
        row(40068, M(18, 2, 46, 36, mo=9), 1496, ra=M(22, 7, 0, 2, mo=9))]
out, st = tg.filter_history(rows, HB)
check(len(out) == 2 and st["frozen"] == 0, f"1е: без нормального соседа — как есть: {st}")

# 1ж. Метка в будущем (часы +2 ч, до 09.10) — отбрасывается всегда, и без счётчика
rows = [row(40034, M(9, 13, 0) + S(10 * i), 150, ra=M(9, 11, 0, 15) + S(10 * i)) for i in range(3)]
rows += [row(40034, M(9, 11, 0) + S(10 * i), 150) for i in range(3)]
rows.sort(key=lambda r: (r["ts"], r["addr"]))
out, st = tg.filter_history(rows, None)
check(st["future"] == 3 and len(out) == 3 and all(r["ts"] < M(9, 12, 0) for r in out),
      f"1ж: строки +2 ч отброшены: {st}")

# 1з. Обычные дубли — копия с наименьшим |lag|; при равенстве — пришедшая раньше
t = M(28, 10, 0)
rows = [row(40026, t, 81, lag=8), row(40026, t, 83, lag=3), row(40027, t, 5, lag=4), row(40027, t, 6, lag=4)]
rows.sort(key=lambda r: (r["ts"], r["addr"], r["received_at"]))
rows[2]["received_at"] = t + S(4)
rows[3]["received_at"] = t + S(4.5)    # вторая копия пришла на 0,5 с позже
out, st = tg.filter_history(rows, HB)
check(st["dups"] == 2 and [r["value"] for r in out] == [83, 5], f"1з: дубли (addr, ts): {out}, {st}")
rows = [row(40027, t, 5, ra=t + S(4)), row(40027, t, 6, ra=t + S(4))]
out, st = tg.filter_history(rows, HB)
check(len(out) == 1, "1з: дубли с одинаковым приёмом схлопнуты")

# 1и. Тип без счётчика — поздние пачки остаются; received_at неизвестен — норма
check(tg.counter_addr("pcc") == HB and tg.counter_addr("comap") is None, "1и: счётчик только у pcc")
rows = [row(40068, M(29, 18, 2, 52, mo=9), 1496, ra=M(30, 8, 44, 50, mo=9)),
        {"addr": 40068, "ts": M(30, 8, 45, mo=9), "value": 1500, "received_at": None}]
out, st = tg.filter_history(rows, None)
check(len(out) == 2 and st == {"future": 0, "frozen": 0, "dups": 0}, f"1и: {st}")


# ── 2. Периоды enum ─────────────────────────────────────────────────────────────
def P(addr, value, s, e, created=None, bad=False):
    return {"addr": addr, "value": value, "state_start": s, "state_end": e, "label": str(value),
            "name_ru": None, "created_at": created if created is not None else s + S(9), "ts_bad": bad}


def fx(rows, a, b):
    return tg.fix_periods(rows, a, b, start="state_start", end="state_end", key=lambda p: p["addr"])


F = M(29, 18, 2, 53, mo=9)            # метка кадра
C = M(30, 8, 45, 5, mo=9)             # момент записи периода
# 2а. 30.09: открыт стоп; кадр закрыл его на своей метке и открыл «Работу»
rows = [P(40011, 0, M(29, 18, 54, 36, mo=9) - S(3 * 3600), F), P(40011, 3, F, None, created=C, bad=True)]
out, need, moved = fx(rows, M(29, 15, 50, mo=9), M(30, 9, 0, mo=9))
st0 = [p for p in out if p["value"] == 0][0]
run = [p for p in out if p["value"] == 3][0]
check(st0["state_end"] == C and run["state_start"] == C and not need and len(moved) == 1,
      f"2а: стоп до записи кадра, «Работа» с неё: {out}, {need}, {moved}")
check(all("created_at" not in p and "ts_bad" not in p and "_fixed" not in p for p in out),
      "2а: служебные поля убраны")

# 2б. 03.10: окно с 02.10 09:00; предшественник кончился до окна — нужен
F3, C3 = M(1, 20, 8, 18), M(3, 8, 41, 10)
rows = [P(40011, 3, F3, M(3, 9, 56, 53), created=C3, bad=True)]
out, need, moved = fx(rows, M(2, 9, 0), M(3, 8, 50))
check(need == [(40011, F3)] and [(p["value"], p["state_start"]) for p in out] == [(3, C3)],
      f"2б: предшественник запрошен, в окне пока только «Работа» с записи кадра: {need}, {out}")
rows.append(P(40011, 0, M(1, 14, 32, 4), F3))
out, need, moved = fx(rows, M(2, 9, 0), M(3, 8, 50))
check(len(out) == 2 and out[0]["value"] in (0, 3) and not need
      and [p for p in out if p["value"] == 0][0]["state_end"] == C3
      and [p for p in out if p["value"] == 3][0]["state_start"] == C3,
      f"2б: с предшественником — стоп до 03.10 08:41:10: {out}")
out, need, _ = fx(rows[:1], M(3, 9, 0), M(4, 9, 0))
check(not need, "2б: окно после записи кадра — предшественник не нужен")

# 2в. Кадр открыл период, живой пакет закрыл его раньше момента записи кадра
E = C - S(10)
rows = [P(40237, 4, M(29, 10, 0, mo=9), F), P(40237, 3, F, E, created=C, bad=True), P(40237, 5, E, None)]
out, need, _ = fx(rows, M(29, 15, 50, mo=9), M(30, 9, 0, mo=9))
check([(p["value"], p["state_end"]) for p in sorted(out, key=lambda p: p["state_start"])] == [(4, E), (5, None)],
      f"2в: однокадровый период исчез, прежний тянется до живого пакета: {out}")

# 2г. 09.10: часы +2 ч — вывернутый период выпрямляется, конец предшественника — тоже
Ff, Cf, Er = M(9, 14, 3, 3), M(9, 12, 3, 19), M(9, 13, 6, 7)
rows = [P(40011, 4, M(9, 12, 0, 57), Ff), P(40011, 0, Ff, Er, created=Cf, bad=True), P(40011, 3, Er, None)]
out, need, moved = fx(rows, M(9, 9, 0), M(10, 9, 0))
got = [(p["value"], p["state_start"], p["state_end"]) for p in sorted(out, key=lambda p: p["state_start"])]
check(got == [(4, M(9, 12, 0, 57), Cf), (0, Cf, Er), (3, Er, None)], f"2г: 09.10 выпрямлено: {got}")

# 2д. Без кривых меток — ничего не меняется, свои перекрытия источника остаются
rows = [P(40010, 2, M(28, 1, 0), M(28, 5, 0)), P(40010, 0, M(28, 4, 0), None)]
out, need, moved = fx(rows, M(28, 0, 0), M(29, 0, 0))
check([(p["state_start"], p["state_end"]) for p in out] == [(M(28, 1, 0), M(28, 5, 0)), (M(28, 4, 0), None)]
      and not moved and not need, "2д: чистые данные как есть")

# 2е. Начало перенеслось за окно — период из окна уходит, предшественник покрывает окно
rows = [P(40011, 0, M(29, 12, 0, mo=9), F), P(40011, 3, F, None, created=C, bad=True)]
out, _, _ = fx(rows, M(29, 15, 50, mo=9), M(30, 6, 0, mo=9))
check([(p["value"], p["state_end"]) for p in out] == [(0, C)], f"2е: окно до записи кадра — один стоп: {out}")

# 2ж. Правка одного регистра не трогает другие: нулевой период 40010 остаётся
# и рядом с исправленным 40237 (лента читает каталог, нарезка — KB: вход нарезки
# не должен зависеть от того, что ещё попало в выборку) — ревью
z = P(40010, 2, M(29, 20, 0, mo=9), M(29, 20, 0, mo=9))
o1, _, _ = fx([z], M(29, 15, 50, mo=9), M(30, 9, 0, mo=9))
o2, _, _ = fx([z, P(40237, 3, F, None, created=C, bad=True), P(40237, 4, M(29, 9, 0, mo=9), F)],
              M(29, 15, 50, mo=9), M(30, 9, 0, mo=9))
check([p for p in o2 if p["addr"] == 40010] == o1 == [{k: v for k, v in z.items() if k not in ("created_at", "ts_bad")}],
      f"2ж: нулевой период нетронутого регистра одинаков с правкой соседа и без: {o1} / {o2}")

# 2з. Предшественник не запрашивается, если старое начало в окне (его вернула бы
# сама выборка) — нет лишнего второго запроса каждый цикл; ревью
rows = [P(40011, 3, M(30, 7, 0, mo=9), None, created=C, bad=True)]
_, need, _ = fx(rows, M(29, 15, 50, mo=9), M(30, 9, 0, mo=9))
check(need == [], f"2з: старое начало в окне — предшественник не нужен: {need}")


# ── 3. Периоды fault ────────────────────────────────────────────────────────────
def Fp(addr, bit, s, e, created=None, bad=False):
    return {"addr": addr, "bit": bit, "fault_start": s, "fault_end": e, "duration_sec": 0,
            "created_at": created if created is not None else s + S(9), "ts_bad": bad}


rows = [Fp(40408, 7, F, M(30, 15, 28, 29, mo=9), created=C, bad=True),
        Fp(40407, 7, M(29, 15, 44, 48, mo=9), F - S(1))]           # бит снят кадром
out, _, moved = tg.fix_periods(rows, M(29, 15, 50, mo=9), M(30, 9, 0, mo=9),
                               start="fault_start", end="fault_end", key=lambda f: (f["addr"], f["bit"]))
b7 = [f for f in out if f["addr"] == 40408][0]
b77 = [f for f in out if f["addr"] == 40407][0]
check(b7["fault_start"] == C and b77["fault_end"] == F - S(1) and len(moved) == 1,
      f"3: начало бита — на запись кадра, конец снятого кадром — как есть: {out}")


# ── 4. Разрывы ─────────────────────────────────────────────────────────────────
def G(s, e, created):
    return {"gap_start": s, "gap_end": e, "created_at": created}


rows = [G(M(9, 12, 1, 5), M(9, 14, 1, 5), M(9, 12, 1, 18)),          # фиктивный двухчасовой
        G(M(9, 15, 0), M(9, 17, 30), M(9, 15, 30, 10)),              # настоящий, конец +2 ч
        G(M(9, 18, 0), M(9, 18, 30), M(9, 16, 0)),                    # целиком из будущего
        G(M(24, 8, 48, 15), M(24, 11, 48, 9), M(24, 12, 38, 28)),     # конец задним числом
        G(M(28, 3, 0), M(28, 3, 5), M(28, 3, 5, 9))]                  # норма
out, fixed = tg.fix_gaps(rows, M(9, 0, 0), M(29, 0, 0))
got = [(g["gap_start"], g["gap_end"]) for g in out]
check(got == [(M(9, 12, 1, 5), M(9, 12, 1, 18)), (M(9, 15, 0), M(9, 15, 30, 10)),
              (M(24, 8, 48, 15), M(24, 11, 48, 9)), (M(28, 3, 0), M(28, 3, 5))] and len(fixed) == 3,
      f"4: разрывы: {got}, {fixed}")
check(all("created_at" not in g for g in out), "4: created_at убран")
out, _ = tg.fix_gaps([G(M(9, 12, 1, 5), M(9, 14, 1, 5), M(9, 12, 1, 18))], M(9, 12, 30), M(9, 13, 0))
check(out == [], "4: сжатый разрыв уходит из окна после него")


# ── 5. Обвязка source ──────────────────────────────────────────────────────────
class FakeConn:
    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.answers(sql, args)


def install(conn):
    @contextlib.asynccontextmanager
    async def _connection(pool, *a, **k):
        yield conn
    src.remote.connection = _connection
    src._get_pool = lambda: None


_orig_conn, _orig_pool = src.remote.connection, src._get_pool
try:
    # 5а. Предшественник 03.10 дочитывается вторым запросом по (addr, конец)
    def _ans(sql, args):
        if "unnest" in sql:
            return [P(40011, 0, M(1, 14, 32, 4), F3)]
        return [P(40011, 3, F3, None, created=C3, bad=True)]
    fc = FakeConn(_ans)
    install(fc)
    got = asyncio.run(src.get_enum_periods("1126246373", "pcc", 1, M(2, 9, 0), M(3, 8, 50), addrs=[40011]))
    check(len(fc.calls) == 2 and fc.calls[1][1][3] == [40011] and fc.calls[1][1][4] == [F3],
          f"5а: второй запрос — предшественник (40011, {F3}): {[c[1] for c in fc.calls]}")
    check([(p["value"], p["state_start"], p["state_end"]) for p in got]
          == [(0, M(1, 14, 32, 4), C3), (3, C3, None)], f"5а: стоп до записи кадра, затем «Работа»: {got}")
    sql0 = fc.calls[0][0]
    check("created_at" in sql0 and "ts_bad" in sql0 and "LEAST(e.state_start, e.created_at)" in sql0
          and "data_gaps g" in sql0 and fc.calls[0][1][6:] == (tg.SKEW_SEC, tg.RECONNECT_SEC),
          "5а: SQL читает created_at, ts_bad (с разрывом при переподключении), окно по LEAST")
    # Окно после вывернутого периода 09.10 (13:09–15:30): период с началом-меткой
    # 14:03:03 и концом 13:06:07 выбирается по началу в окне — иначе конец
    # «Разгрузки» на 14:03:03 не перенести
    check("OR (e.state_start >= $5 AND e.state_start < $6)" in sql0,
          "5а: окно добирает вывернутые периоды с началом в окне")

    # 5б. Чистое окно — один запрос, результат как из источника
    fc = FakeConn(lambda sql, args: [P(40011, 3, M(28, 1, 0), None)])
    install(fc)
    got = asyncio.run(src.get_enum_periods("1126246373", "pcc", 1, M(28, 0, 0), M(29, 0, 0), addrs=[40011]))
    check(len(fc.calls) == 1 and got == [{k: v for k, v in P(40011, 3, M(28, 1, 0), None).items()
                                          if k not in ("created_at", "ts_bad")}],
          f"5б: чистое окно — один запрос, как есть: {got}")

    # 5в. fault: длительность пересчитана после переноса — только у сдвинутого
    _other = dict(Fp(40406, 2, M(29, 15, 44, 48, mo=9), None), duration_sec=12345)
    fc = FakeConn(lambda sql, args: [Fp(40408, 7, F, M(30, 15, 28, 29, mo=9), created=C, bad=True), _other])
    install(fc)
    got = asyncio.run(src.get_fault_periods("1126246373", "pcc", 1, M(29, 15, 50, mo=9), M(30, 9, 0, mo=9)))
    g7 = [f for f in got if f["addr"] == 40408][0]
    g2 = [f for f in got if f["addr"] == 40406][0]
    check(g7["fault_start"] == C and g7["duration_sec"] == round((M(30, 15, 28, 29, mo=9) - C).total_seconds())
          and g2["duration_sec"] == 12345, f"5в: fault — начало и длительность сдвинутого, остальные как в SQL: {got}")
    check("ts_bad" in fc.calls[0][0] and "LEAST(f.fault_start, f.created_at)" in fc.calls[0][0], "5в: SQL fault")

    # 5г. Разрывы — created_at читается, конец в будущем переносится
    fc = FakeConn(lambda sql, args: [G(M(9, 12, 1, 5), M(9, 14, 1, 5), M(9, 12, 1, 18))])
    install(fc)
    got = asyncio.run(src.get_data_gaps("1126246373", "pcc", 1, M(9, 9, 0), M(10, 9, 0)))
    check(got == [{"gap_start": M(9, 12, 1, 5), "gap_end": M(9, 12, 1, 18)}] and "created_at" in fc.calls[0][0],
          f"5г: разрыв 09.10 сжат: {got}")

    # 5д. Куски истории сверяются вместе: кадр в одном куске, соседи — в другом
    _orig_fetch = src._fetch_whitelist_history

    async def _fake_fetch(sn, et, p, a, b, wl):
        if a < M(29, 18, 0, mo=9):
            return [r for r in pre if a <= r["ts"] < b]
        return [r for r in sorted(frame + live1, key=lambda r: (r["ts"], r["addr"])) if a <= r["ts"] < b]
    src._fetch_whitelist_history = _fake_fetch
    try:
        got = asyncio.run(src.get_whitelist_history_chunked(
            "1126246373", "pcc", 1, M(29, 17, 0, mo=9), M(30, 9, 0, mo=9), frozenset({HB, 40068, 40070})))
        check(not any(r["ts"].date() == M(29, 18, 2, 52, mo=9).date() and r.get("value") == 1496 for r in got)
              and len(got) == len(pre) + len(live1), f"5д: кадр отброшен по соседям из другого куска: {len(got)}")
    finally:
        src._fetch_whitelist_history = _orig_fetch
    check("received_at" in inspect.getsource(src._fetch_whitelist_history)
          and "ORDER BY ts ASC, addr ASC, received_at ASC" in inspect.getsource(src._fetch_whitelist_history),
          "5д: история читает received_at, порядок с третьим ключом")

    # 5е. Сбой проверки не роняет чтение: строки как есть, без received_at
    _orig_fh = tg.filter_history

    def _boom(*a, **k):
        raise RuntimeError("тест")
    tg.filter_history = _boom
    src.logger.disabled = True
    try:
        got = src._guard_history([row(HB, M(28, 1, 0), 1.0)], "SN", "pcc", 1)
        check(len(got) == 1 and "received_at" not in got[0], "5е: сбой проверки — строки как есть")
    finally:
        tg.filter_history = _orig_fh
        src.logger.disabled = False

    # 5е'. Сбой правки периодов не роняет чтение: периоды как в источнике
    _orig_fp = tg.fix_periods
    tg.fix_periods = _boom
    src.logger.disabled = True
    try:
        fc = FakeConn(lambda sql, args: [P(40011, 3, M(28, 1, 0), None)])
        install(fc)
        got = asyncio.run(src.get_enum_periods("SN", "pcc", 1, M(28, 0, 0), M(29, 0, 0), addrs=[40011]))
        check(len(got) == 1 and "ts_bad" not in got[0] and "created_at" not in got[0],
              f"5е': сбой правки периодов — как есть: {got}")
    finally:
        tg.fix_periods = _orig_fp
        src.logger.disabled = False

    # 5з. Узкая выборка без счётчика (обороты и частота акта): 40290 дочитывается
    # для проверки кадра и убирается из ответа; meta["max_ts"] — до проверки (ревью)
    calls: list = []

    async def _fake_fetch2(sn, et, p, a, b, wl):
        calls.append(wl)
        return sorted([r for r in pre + frame + live1 if r["addr"] in wl], key=lambda r: (r["ts"], r["addr"]))
    src._fetch_whitelist_history = _fake_fetch2
    try:
        meta: dict = {}
        got = asyncio.run(src.get_whitelist_history(
            "1126246373", "pcc", 1, M(29, 17, 0, mo=9), M(30, 9, 0, mo=9), frozenset({40068}), meta=meta))
        check(calls[-1] == frozenset({40068, HB}) and all(r["addr"] == 40068 for r in got)
              and not any(r["value"] == 1496 and r["ts"] < M(30, 0, 0, mo=9) for r in got),
              f"5з: кадр отброшен по дочитанному счётчику, счётчик из ответа убран: {calls[-1]}")
        check(meta.get("max_ts") == max(r["ts"] for r in pre + frame + live1 if r["addr"] in (40068, HB)),
              f"5з: meta max_ts — до проверки: {meta}")
        got = asyncio.run(src.get_whitelist_history_chunked(
            "1126246373", "pcc", 1, M(29, 17, 0, mo=9), M(30, 9, 0, mo=9), frozenset({40068})))
        check(calls[-1] == frozenset({40068, HB}) and all(r["addr"] == 40068 for r in got), "5з: то же в chunked")
    finally:
        src._fetch_whitelist_history = _orig_fetch

    # 5и. Живой цикл: отметка дочитки хвоста — и по отброшенным строкам, свежесть
    # — только по принятым (иначе при часах роутера впереди хвост растёт) — ревью
    import online.engine as _eng
    _uw = inspect.getsource(_eng.OnlinePollEngine._update_open_window)
    check(_uw.count("meta=_fetched") == 2 and "_fetched.get(\"max_ts\")" in _uw
          and "await self._advance_last_data_ts(_seen_ts)" in _uw,
          "5и: открытое окно — отметка кэша по прочитанному, свежесть по принятому")

    # 5ж. Точечные выборки не берут строки с меткой дальше SKEW от приёма
    for fn in (src.get_last_value_before, src.get_last_values_before, src.get_value_histogram):
        s = inspect.getsource(fn)
        check("abs(EXTRACT(EPOCH FROM received_at - ts))" in s and "ts_guard.SKEW_SEC" in s,
              f"5ж: {fn.__name__} отсекает кривые метки")
finally:
    src.remote.connection, src._get_pool = _orig_conn, _orig_pool

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
