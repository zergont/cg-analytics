# API-гайд для внешнего UI — cg-analytics

**Базовый URL:** `http://<host>:<port>` (дефолт порт — см. config.yml)  
**Формат:** JSON, UTF-8  
**CORS:** если нужен — добавить `CORSMiddleware` в `main.py`  
**Авторизация:** нет (внутренняя сеть)

---

## 1. Онлайн: текущее состояние машин

### `GET /api/machines`

Список всех наблюдаемых машин с текущим состоянием.  
**Поллинг рекомендуется каждые 10–30 с.**

```json
[
  {
    "router_sn":     "DGU-001",
    "equip_type":    "diesel",
    "panel_id":      1,
    "name":          "ДГА №1",
    "manufacturer":  "Cummins",
    "model":         "KTA50",
    "status":        "running",        // running | stopped

    "run_state":       3,
    "run_state_label": "Работа",       // Стоп | Прогрев | Работа | Разгрузка | Охлаждение на х.х. | Переход на х.х.

    "severity_level":  "норма",        // норма | внимание | тревога
    "status_text":     "Работа, нагрузка 42%, все параметры в норме. 3ч 20м.",
    "status_updated":  "2026-06-06T10:35:00Z",   // когда обновлялся статус-текст

    "coking_risk":     "GREEN",        // GREEN | YELLOW | RED

    "_links": {
      "segments": "/api/machine/DGU-001/diesel/1/segments",
      "calendar":  "/online/calendar/DGU-001/diesel/1"
    }
  }
]
```

**Что откуда:**
| Поле | Источник | Частота обновления |
|---|---|---|
| `run_state` | телеметрия (Modbus) | каждые 30 с |
| `severity_level` | детерминированный аналитический блок | каждые 30 с |
| `coking_risk` | накопленный риск закоксовки | каждые 30 с |
| `status_text` | ИИ-оператор (Qwen) | каждые 5 мин при изменении |
| `status_updated` | timestamp генерации статус-текста | каждые 5 мин |

**Цветовая схема `severity_level`:**
- `норма` → зелёный / нейтральный
- `внимание` → жёлтый (WARNING-тревоги)
- `тревога` → красный (ALARM / SHUTDOWN)

**`status_text` может быть `null`** — первые ~1 мин после старта наблюдения, пока планировщик не сделал первый тик.

---

### `GET /api/online/status`

Расширенная версия с прогрессом batch-добора и лагом связи.  
Та же частота поллинга. Добавляет поля:

```json
{
  "engine_live":   true,        // движок запущен прямо сейчас
  "lag_sec":       15,          // отставание от now (< 90 с = онлайн)
  "cursor_ts":     "...",       // t_end последнего зафиксированного сегмента
  "processed_to":  "...",       // куда дошёл batch-добор
  "batch_end_ts":  "...",       // правая граница batch-добора
  "t_start_open":  "..."        // начало текущего открытого сегмента
}
```

Используйте `/api/machines` для простого дашборда, `/api/online/status` — если нужен прогресс-бар исторического добора.

---

## 2. Архив: история сегментов (календарь)

### `GET /api/machine/{sn}/{type}/{panel}/segments`

История сегментов машины — для построения календаря.

**Параметры:**
| Параметр | Тип | Описание |
|---|---|---|
| `year` | int | Год фильтра (вместе с `month`) |
| `month` | int | Месяц фильтра (1–12) |
| `limit` | int | Макс. число сегментов, дефолт 200 |

**Пример:** `/api/machine/DGU-001/diesel/1/segments?year=2026&month=6`

```json
[
  {
    "id":          1234,
    "t_start":     "2026-06-06T06:00:00Z",
    "t_end":       "2026-06-06T15:00:00Z",
    "is_open":     false,

    "run_state":       3,
    "run_state_label": "Работа",
    "duration_sec":    32400,
    "cause_close":     "DAILY_BOUNDARY",   // RUN_STATE_CHANGE | DAILY_BOUNDARY | OPERATOR_STOP

    "severity":        "WARNING",          // SHUTDOWN | ALARM | WARNING | INFO | null
    "analytics_version": "3.3.1",

    "has_report":  true,    // есть Markdown-отчёт аналитики
    "has_claude":  true,    // есть Claude-заключение
    "has_qwen":    true,    // есть Qwen-очеловечивание

    "_links": {
      "detail": "/api/segment/1234",
      "view":   "/online/segment/1234"
    }
  }
]
```

Сегменты отдаются от **новых к старым**. Если `year`/`month` не указаны — возвращает последние `limit` сегментов.

**Для построения календаря:**
- Группируйте по дате `t_start` (в локальном часовом поясе)
- `severity` → цвет ячейки дня: SHUTDOWN/ALARM → красный, WARNING → жёлтый, null → зелёный
- `has_qwen: true` → показывать значок ИИ-анализа

---

## 3. Детальный анализ сегмента

### `GET /api/segment/{id}`

Полный отчёт по сегменту для страницы детального анализа.

```json
{
  "id":            1234,
  "router_sn":     "DGU-001",
  "equip_type":    "diesel",
  "panel_id":      1,
  "t_start":       "2026-06-06T06:00:00Z",
  "t_end":         "2026-06-06T15:00:00Z",
  "is_open":       false,

  "run_state":       3,
  "run_state_label": "Работа",
  "duration_sec":    32400,
  "cause_close":     "DAILY_BOUNDARY",
  "severity":        "WARNING",
  "analytics_version": "3.3.1",

  "report_md": "# Аналитический отчёт...\n\n## Сводка\n...",

  "analysis": {
    "status":        "done",
    "conclusion_md": "## Сводка\nШтатная работа...",   // сухое заключение Claude
    "humanized_md":  "ДГУ работал штатно...",           // очеловеченное Qwen
    "created_at":    "2026-06-06T15:05:00Z",
    "updated_at":    "2026-06-06T15:06:00Z"
  },

  // Только для открытых сегментов (is_open: true):
  "status_text": "Работа, нагрузка 42%...",

  "_links": {
    "view":     "/online/segment/1234",
    "segments": "/api/machine/DGU-001/diesel/1/segments"
  }
}
```

**Что показывать в UI:**

| Блок | Поле | Описание |
|---|---|---|
| Карточка сегмента | `t_start`, `t_end`, `duration_sec`, `run_state_label` | Время и режим |
| Индикатор | `severity` | Цвет: красный/жёлтый/зелёный |
| Аналитика | `report_md` | Рендерить как Markdown |
| ИИ-заключение | `analysis.humanized_md` | Основной текст для оператора |
| Сырое Claude | `analysis.conclusion_md` | По запросу / для инженера |

**Значения `analysis.status`:**
- `pending` — ожидает в очереди
- `processing` — обрабатывается прямо сейчас
- `done` — готово
- `error` — ошибка
- `null` (поле `analysis: null`) — анализ ещё не запускался

---

## Типичные сценарии

### Главный экран (список машин + онлайн)
```
1. GET /api/machines                 → список машин, текущий статус
2. Поллинг каждые 15 с
3. Показываем: имя, run_state_label, severity_level (цвет), status_text
4. Кнопка «История» → переход к календарю машины
```

### Календарь истории машины
```
1. GET /api/machine/{sn}/{type}/{panel}/segments?year=2026&month=6
2. Строим сетку месяца, группируем по дате t_start
3. Цвет ячейки по максимальному severity дня
4. Значок 🤖 если has_qwen
5. Клик по ячейке → GET /api/segment/{id} → страница детального анализа
```

### Страница детального анализа сегмента
```
1. GET /api/segment/{id}
2. Карточка: время, режим, длительность, severity
3. Блок «Аналитика»: рендерим report_md
4. Блок «ИИ-заключение»: показываем humanized_md (если has_qwen)
   - Если analysis.status = pending/processing → спиннер + поллинг каждые 10 с
   - Если null → «Анализ не запускался»
```

---

## Примечания

- Все временны́е метки в **UTC** (ISO 8601 с `Z`)
- `report_md` — синтаксис Markdown, нужен рендерер (marked.js и т.п.)
- `humanized_md` — plain text, без Markdown
- `cause_close`:
  - `RUN_STATE_CHANGE` — смена режима (напр. Работа → Стоп)
  - `DAILY_BOUNDARY` — суточный рез в 09:00
  - `OPERATOR_STOP` — ручная остановка
- Открытый сегмент (`is_open: true`) — всегда один на машину; `t_end: null`
