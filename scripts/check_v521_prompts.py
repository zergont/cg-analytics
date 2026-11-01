# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.2.1: снимки (system, user) до/после на реальных случаях (§4.11). Только чтение.

Строит пары (system, user) старым кодом (дерево `git archive` прошлой версии) и
новым для реальных случаев и печатает дифф с разметкой «ожидаемо / неожиданно»:
- заключение ДЭС №3 (#36223, сжатый вход), заключение ДГУ №1 (#36219);
- гейт ДЭС №3 — текущий открытый сегмент (состав — из строки, счётчики не читаются);
- совет — последний акт с разбором (по умолчанию #35941).

Ожидаемо различаются: блок «Правила системы» и строки миграции в system (у
нового — сохранённые промпты, прогнанные через миграцию В КОПИИ В ПАМЯТИ; CRLF →
LF), картина объекта во входе (у совета — вместо блока «Оборудование (реестр)»),
наработка — строкой кода в заключении (не во входе). Модель и шлюз не зовутся.

БД аналитики — только чтение (default_transaction_read_only); имена объектов —
таблица objects этой же БД (копия из телеметрии), телеметрия не читается.

Запуск:
  git archive HEAD | tar -x -C <old>          # дерево прошлой версии (+ config.yml)
  py -3 scripts/check_v521_prompts.py --dsn <файл DSN> --old <old> [--out <папка>]
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CASES = {
    "conclusion_des3_36223": 36223,
    "conclusion_dgu1_36219": 36219,
}
ACT_SEG = 35941


# ── Чтение БД (родитель) ─────────────────────────────────────────────────────

def _enc(v):
    if isinstance(v, datetime):
        return {"__dt__": v.isoformat()}
    return v


def _dec(o):
    if isinstance(o, dict) and set(o) == {"__dt__"}:
        return datetime.fromisoformat(o["__dt__"])
    return o


async def fetch(dsn: str) -> dict:
    import asyncpg
    c = await asyncpg.connect(dsn, server_settings={"default_transaction_read_only": "on"})
    try:
        out: dict = {"rows": {}, "now": datetime.now(timezone.utc)}
        ids = list(CASES.values()) + [ACT_SEG]
        for sid in ids:
            r = await c.fetchrow("SELECT * FROM auto_segments WHERE id = $1", sid)
            out["rows"][str(sid)] = dict(r) if r else None
        r = await c.fetchrow("SELECT * FROM auto_segments WHERE router_sn = '1126246373' AND equip_type = 'pcc' "
                             "AND panel_id = 1 AND t_end IS NULL ORDER BY t_start DESC LIMIT 1")
        out["rows"]["gate_open"] = dict(r) if r else None
        act = out["rows"][str(ACT_SEG)] or {}
        inc = act.get("incident_json")
        inc = json.loads(inc) if isinstance(inc, str) else inc
        cyc = []
        if isinstance(inc, dict) and (inc.get("window") or {}).get("from"):
            cyc = [dict(x) for x in await c.fetch("""
                SELECT id, run_state, t_start, t_end, characteristics_json FROM auto_segments
                WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND t_end IS NOT NULL
                  AND t_end > $4 AND t_start < $5 ORDER BY t_start""",
                act["router_sn"], act["equip_type"], act["panel_id"],
                datetime.fromisoformat(inc["window"]["from"]), datetime.fromisoformat(inc["stop_ts"]))]
        out["cycle"] = cyc
        out["registry"] = [dict(x) for x in await c.fetch("SELECT * FROM equipment_registry ORDER BY router_sn, equip_type, panel_id")]
        out["units"] = [dict(x) for x in await c.fetch("SELECT * FROM units ORDER BY id")]
        out["sensors"] = [dict(x) for x in await c.fetch("SELECT * FROM unit_sensors")]
        out["obs"] = [dict(x) for x in await c.fetch(
            "SELECT router_sn, equip_type, panel_id, last_data_ts, status FROM online_observations")]
        out["objects"] = {x["router_sn"]: x["name"] for x in await c.fetch("SELECT router_sn, name FROM objects") if x["name"]}
        out["settings"] = {x["key"]: x["value"] for x in await c.fetch(
            "SELECT key, value FROM app_settings WHERE key LIKE 'ai_task_%_prompt' OR key = 'ai_depth_profiles'")}
        return out
    finally:
        await c.close()


# ── Сборка пар (дочерний процесс: старый или новый код) ──────────────────────

def build(root: str, fixture: str, side: str) -> dict:
    sys.path.insert(0, root)
    os.chdir(root)
    data = json.loads(Path(fixture).read_text(encoding="utf-8"), object_hook=_dec)
    from llm import router
    import corpus.preprocessor as prep
    import corpus.worker as worker
    import online.status_assembler as sa
    import online.incident_gate as ig
    from online.manager import _LLM_VERDICT_SUFFIX
    new = side == "new"
    st = data["settings"]
    router.load_depth(st.get("ai_depth_profiles") or "")
    notes: dict = {}
    for t in router.TASKS:
        raw = st.get(f"ai_task_{t}_prompt")
        if raw is None:
            continue
        val = raw
        if new:
            from llm import prompts
            m, n = prompts.migrate_prompt_text(t, raw)      # миграция — на копии в памяти
            notes[t] = n
            val = raw if m is None else m
        router.apply_task(t, router.get_provider(t), val)

    def system(task: str, suffix: str = "") -> tuple[str, dict]:
        if new:
            from llm import prompts
            txt, meta = prompts.system_for(task, suffix=suffix)
            return txt, meta
        return router.get_prompt(task) + suffix, {}

    pics: dict = {}
    if new:
        from db.units import build_tree
        from llm import object_picture as op
        fresh = {(o["router_sn"], o["equip_type"], int(o["panel_id"])): o["last_data_ts"] for o in data["obs"]}
        status = {(o["router_sn"], o["equip_type"], int(o["panel_id"])): o.get("status") for o in data["obs"]}
        tree = build_tree(data["registry"], data["units"], data["sensors"], object_names=data["objects"],
                          freshness=fresh, observation_status=status)
        # Телеметрия здесь не читается: свежесть датчиков не известна (их и нет)
        ctx = {"tree": op.annotate(tree, seen=None), "names_ok": True}
        for r in data["registry"]:
            k = (r["router_sn"], r["equip_type"], int(r["panel_id"]))
            # заключение и совет — не живая свежесть, гейт — живая
            pics[k] = (op.render(ctx, *k, now=data["now"]), op.render(ctx, *k, now=data["now"], live=True))

    def pic(row, live=False):
        p = pics.get((row["router_sn"], row["equip_type"], int(row["panel_id"]))) if new else None
        return (p[1] if live else p[0]) if p else None

    out: dict = {"_notes": notes}
    # Заключения: system и вход как у цепочки (полный, иначе сжатый по пределу шлюза)
    for case, sid in CASES.items():
        row = dict(data["rows"][str(sid)])
        if pic(row):
            row["object_picture"] = pic(row)
        task = "seg_auto"
        sysm, meta = system(task)
        full = prep.build_claude_input(row)
        user, comp = full, None
        if worker._est_prompt_tokens(sysm, full) > worker._GW_MAX_PROMPT_TOKENS:
            c = worker._compact_model_input(row, sysm, full)
            if c:
                user, comp = c[0], c[2]
        out[case] = {"system": sysm, "user": user, "meta": meta, "compact": comp,
                     "est": worker._est_prompt_tokens(sysm, user),
                     "hours_line": worker._hours_line(row) if new else None}
    # Гейт ДЭС №3: состав — из открытой строки (время в режиме — фиксировано)
    g = dict(data["rows"]["gate_open"])
    struct = sa.build_structural_status(g)
    struct["time_in_mode_sec"] = 3600
    wa = g.get("warning_analyses")
    wa = json.loads(wa) if isinstance(wa, str) else (wa or [])
    prov = router.get_route(router.depth_for_gate(struct.get("severity_level"))).get("provider", "llm")
    sysm, meta = system("warning_claude", _LLM_VERDICT_SUFFIX if prov == "llm" else "")
    kw = {"object_picture": pic(g, live=True)} if new and pic(g, live=True) else {}
    user = sa.build_warning_prompt(struct, prev_analyses=[x for x in wa if isinstance(x, dict)],
                                   episode_timeline=[], **kw)
    out["gate_des3_open"] = {"system": sysm, "user": user, "meta": meta, "provider": prov,
                             "segment": g["id"], "est": worker._est_prompt_tokens(sysm, user)}
    # Совет: акт, сегменты цикла, оборудование из реестра (состояния регистров
    # на момент останова — из телеметрии, здесь не читаются: у обеих сторон пусто)
    a = dict(data["rows"][str(ACT_SEG)])
    inc = a.get("incident_json")
    inc = json.loads(inc) if isinstance(inc, str) else inc
    eq = next((r for r in data["registry"] if (r["router_sn"], r["equip_type"], r["panel_id"])
               == (a["router_sn"], a["equip_type"], a["panel_id"])), {})
    from analytics import binding
    from config import settings
    cfg = binding.build_config(settings.knowledge_base_path, controller_id=eq.get("controller_id"),
                               engine_id=eq.get("engine_id"), kb_path=eq.get("kb_path"))
    context = {"equipment": {"name": eq.get("name"),
                             "engine": " ".join(str(x) for x in (eq.get("manufacturer"), eq.get("model")) if x),
                             "engine_sn": eq.get("engine_sn"), "controller": cfg.controller_label}}
    kw = {"object_picture": pic(a)} if new and pic(a) else {}
    user = ig.build_incident_prompt(inc, a, data["cycle"], context, **kw)
    sysm, meta = system("incident")
    out["incident_act_35941"] = {"system": sysm, "user": user, "meta": meta,
                                 "est": worker._est_prompt_tokens(sysm, user)}
    return out


# ── Сравнение ────────────────────────────────────────────────────────────────

def _lines(s: str) -> list[str]:
    return s.replace("\r\n", "\n").split("\n")


def expected_sets() -> tuple[set[str], set[str]]:
    """(строки, которые могут появиться, строки, которые могут уйти)."""
    sys.path.insert(0, str(ROOT))
    from llm import prompts
    add: set[str] = {prompts.RULES_HEADER} | {f"- {prompts.rule_text(r)}" for r in prompts.RULES}
    rem: set[str] = set()
    for reps in prompts.REPLACEMENTS.values():
        for old, new, _ in reps:
            add |= set(new)
            rem |= set(old)
    return add, rem


def classify(case: str, part: str, old: str, new: str, pic_lines: set[str], add: set[str],
             rem: set[str]) -> tuple[list[str], list[str]]:
    """(ожидаемые, неожиданные) различия построчно."""
    ok, bad = [], []
    if old == new:
        return ok, bad
    if old.replace("\r\n", "\n").strip() == new.strip() and "\r" in old:
        return ["CRLF → LF"], bad
    a, b = _lines(old), _lines(new)
    a = [x.rstrip() for x in a]
    b = [x.rstrip() for x in b]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    eq_block = {"Оборудование (реестр):"}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for x in a[i1:i2]:
            if not x or x in rem or x in eq_block or (part == "user" and case.startswith("incident")
                                                      and x.startswith("  - ")):
                ok.append(f"- {x[:110]}")
            else:
                bad.append(f"- {x[:160]}")
        for x in b[j1:j2]:
            if not x or x in add or x in pic_lines:
                ok.append(f"+ {x[:110]}")
            else:
                bad.append(f"+ {x[:160]}")
    return ok, bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", help="файл с DSN БД аналитики")
    ap.add_argument("--old", help="дерево прошлой версии (git archive)")
    ap.add_argument("--out", default=str(ROOT / "exports" / "prompt_snap"))
    ap.add_argument("--build", nargs=3, metavar=("ROOT", "FIXTURE", "SIDE"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.build:
        res = build(*args.build)
        sys.stdout.buffer.write(json.dumps(res, ensure_ascii=False, default=str).encode("utf-8"))
        return 0
    if not (args.dsn and args.old):
        ap.error("нужны --dsn и --old")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = asyncio.run(fetch(Path(args.dsn).read_text(encoding="utf-8").strip()))
    fixture = out / "fixture.json"
    fixture.write_text(json.dumps(data, ensure_ascii=False, default=_enc), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    res = {}
    for side, root in (("old", args.old), ("new", str(ROOT))):
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--build", root, str(fixture), side],
                           capture_output=True, env=env, cwd=root)
        if p.returncode != 0:
            print(f"сборка «{side}» упала:\n{p.stderr.decode('utf-8', 'replace')[-3000:]}")
            return 2
        res[side] = json.loads(p.stdout.decode("utf-8"))
    add, rem = expected_sets()
    total_bad = 0
    print("Миграция (копия в памяти):")
    for t, notes in res["new"]["_notes"].items():
        print(f"  {t}: " + ("; ".join(notes) or "—"))
    for case in [k for k in res["new"] if not k.startswith("_")]:
        o, n = res["old"][case], res["new"][case]
        pic_lines = set()
        for line in _lines(n["user"]):
            if line.startswith(("Картина объекта", "Объект:", "Эта установка:", "Источники данных:",
                                "Соседи:", "Датчики объекта")):
                pic_lines.add(line)
        print(f"\n══ {case} ══ ~ток.: {o['est']} → {n['est']} (+{n['est'] - o['est']})"
              + (f" · сжатие: {o.get('compact')} → {n.get('compact')}" if o.get("compact") or n.get("compact") else ""))
        if n.get("meta"):
            print(f"  prompt_version: {n['meta'].get('version')} · правила {n['meta'].get('rule_ids')}"
                  + (f" · противоречия {n['meta'].get('conflicts')}" if n["meta"].get("conflicts") else ""))
        if n.get("hours_line"):
            print(f"  строка кода в заключении: {n['hours_line']}")
        if n.get("provider"):
            print(f"  маршрут: {n['provider']}, сегмент #{n.get('segment')}")
        for part in ("system", "user"):
            (out / "old").mkdir(exist_ok=True)
            (out / "new").mkdir(exist_ok=True)
            (out / "old" / f"{case}.{part}.txt").write_text(o[part], encoding="utf-8", newline="\n")
            (out / "new" / f"{case}.{part}.txt").write_text(n[part], encoding="utf-8", newline="\n")
            ok, bad = classify(case, part, o[part], n[part], pic_lines, add, rem)
            if "\r" in o[part] and "\r" not in n[part]:
                print(f"  {part}: переводы строк CRLF → LF (старый уходил модели с CRLF из формы)")
            if not ok and not bad:
                print(f"  {part}: без изменений")
                continue
            print(f"  {part}: ожидаемых строк {len(ok)}, неожиданных {len(bad)}")
            for x in ok[:40]:
                print(f"     {x}")
            for x in bad:
                print(f"   ! {x}")
            total_bad += len(bad)
    print(f"\nСнимки: {out}/old, {out}/new")
    print("ИТОГ: " + ("различаются только ожидаемые места" if not total_bad else f"неожиданных строк: {total_bad}"))
    return 0 if not total_bad else 1


if __name__ == "__main__":
    sys.exit(main())
