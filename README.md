# cg-analytics

Модуль интеллектуальной аналитики телеметрии генераторных установок.  
Часть экосистемы **Честная Генерация**.

## Принцип работы

1. Ежедневно в 00:05 МСК планировщик запускает pipeline для каждой активной ГУ
2. Загружаются данные телеметрии за прошедшие сутки из основной БД
3. Вычисляются агрегаты и детектируются отклонения (fault-биты, выходы за пороги)
4. RAG по картам регистров и РЭ извлекает релевантный контекст
5. Агент (Claude Sonnet) анализирует данные через tool use и формирует отчёт
6. Отчёт сохраняется в аналитическую БД и отображается в Web UI на порту 8090

```
PostgreSQL (основная БД)          knowledge_base/
   history / events                  equipment/{Производитель}/{Модель}/
        │                              register_map.jsonl
        ▼                              fault_bitmap_map.jsonl
  [ cg-analytics ]  ◄── RAG ──        enum_map.json
   APScheduler + Agent                docs/*.pdf
        │
        ▼
  Analytics DB (pgvector)
        │
        ▼
    Web UI :8090
```

---

## Установка на Ubuntu 22.04 / 24.04

### Быстрый старт (рекомендуется)

```bash
git clone <url-репозитория> /opt/cg-analytics
cd /opt/cg-analytics
bash install.sh
```

Скрипт сам проверит Python, установит зависимости, скопирует конфиг, применит схему БД
и предложит запустить индексацию и systemd-сервис.

---

### Ручная установка (пошагово)

### 1. Системные зависимости

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev \
                   postgresql postgresql-contrib libpq-dev git
```

### 2. Клонирование репозитория

```bash
sudo mkdir -p /opt/cg-analytics
sudo chown $USER:$USER /opt/cg-analytics
git clone <url-репозитория> /opt/cg-analytics
cd /opt/cg-analytics
```

### 3. Python окружение и зависимости

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Настройка аналитической БД

```bash
# Создать пользователя и БД
sudo -u postgres psql <<'SQL'
CREATE USER analytics WITH PASSWORD 'yourpassword';
CREATE DATABASE analytics OWNER analytics;
\c analytics
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
SQL
```

### 5. Установка Ollama и модели эмбеддингов

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text
```

> **LMStudio** — альтернатива Ollama. Запустите сервер на порту 1234,
> установите `EMBEDDING_BASE_URL=http://localhost:1234` в `.env`.

### 6. Конфигурация

```bash
cp config.example.yml config.yml
nano config.yml   # заполните все значения
```

Обязательные параметры:

| Путь в YAML | Описание |
|---|---|
| `databases.source` | Строка подключения к основной БД телеметрии |
| `databases.analytics` | Строка подключения к аналитической БД |
| `anthropic.api_key` | API-ключ Anthropic |

`config.yml` добавлен в `.gitignore` и не попадает в репозиторий.

### 7. Применение схемы БД

```bash
source .venv/bin/activate
python -c "import asyncio; from db.analytics import init_db; asyncio.run(init_db())"
```

### 8. Синхронизация реестра оборудования

Открыть Web UI → **Настройки** → «Синхронизировать с основной БД».  
Или через CLI:

```bash
python -c "
import asyncio
from db.analytics import upsert_equipment
from db.source import get_active_equipment
async def sync():
    for eq in await get_active_equipment():
        await upsert_equipment(eq)
        print(eq['router_sn'], eq['model'])
asyncio.run(sync())
"
```

### 9. Добавление оборудования в knowledge_base

```bash
mkdir -p knowledge_base/equipment/Cummins/KTA50/docs

# Скопировать карты регистров (те же файлы что в telemetry2)
cp /path/to/register_map.jsonl    knowledge_base/equipment/Cummins/KTA50/
cp /path/to/fault_bitmap_map.jsonl knowledge_base/equipment/Cummins/KTA50/
cp /path/to/enum_map.json          knowledge_base/equipment/Cummins/KTA50/

# Скопировать PDF-документы (РЭ, карты уставок и т.д.)
cp /path/to/manual.pdf knowledge_base/equipment/Cummins/KTA50/docs/
```

### 10. Первичная индексация knowledge base

```bash
source .venv/bin/activate
python -m knowledge.indexer --all
# Или для одной модели:
python -m knowledge.indexer --manufacturer Cummins --model KTA50
```

### 11. Тестовый запуск

```bash
source .venv/bin/activate
python main.py
# Открыть http://localhost:8090
```

### 12. Регистрация systemd-сервиса

```bash
# Создать системного пользователя
sudo useradd --system --no-create-home --shell /sbin/nologin cg-analytics
sudo chown -R cg-analytics:cg-analytics /opt/cg-analytics

# Установить сервис
sudo cp cg-analytics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cg-analytics
sudo systemctl start cg-analytics

# Проверить статус
sudo systemctl status cg-analytics
sudo journalctl -u cg-analytics -f
```

---

## Обновление

```bash
cd /opt/cg-analytics
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart cg-analytics
```

## Добавление новой модели оборудования

1. Создать папку `knowledge_base/equipment/{Производитель}/{Модель}/`
2. Скопировать `register_map.jsonl`, `fault_bitmap_map.jsonl`, `enum_map.json`
3. Добавить PDF-документы в `docs/`
4. Запустить переиндексацию: Web UI → **База знаний** → «Переиндексировать»
5. В основном UI заполнить поля `manufacturer` и `model` для оборудования
6. Web UI → **Настройки** → «Синхронизировать с основной БД»

## Структура проекта

```
cg-analytics/
├── main.py               # FastAPI + точка входа
├── scheduler.py          # APScheduler, запуск в 00:05 UTC
├── config.py             # Настройки из .env
├── agent/
│   ├── loop.py           # Agentic loop (Anthropic API)
│   ├── tools.py          # Определения инструментов
│   ├── executor.py       # Выполнение инструментов
│   ├── charts.py         # Генерация графиков (matplotlib)
│   └── prompt.py         # Формирование промптов
├── pipeline/
│   ├── runner.py         # Оркестратор pipeline
│   ├── aggregator.py     # Агрегация телеметрии
│   └── detector.py       # Детектирование отклонений
├── knowledge/
│   ├── loader.py         # Загрузка карт регистров
│   ├── indexer.py        # Построение pgvector-индекса
│   └── retriever.py      # RAG-запросы
├── db/
│   ├── source.py         # Чтение из основной БД
│   ├── analytics.py      # Аналитическая БД
│   └── schema.sql        # Схема аналитической БД
├── web/
│   ├── routes.py         # FastAPI роуты
│   └── templates/        # Jinja2 шаблоны
├── knowledge_base/
│   └── equipment/        # {Производитель}/{Модель}/
├── requirements.txt
├── .env.example
└── cg-analytics.service  # systemd unit
```
