"""Построение pgvector-индекса для RAG.

Индексирует register_map.jsonl, fault_bitmap_map.jsonl и PDF-документы
из knowledge_base/equipment/{manufacturer}/{model}/docs/.

Использование:
    python -m knowledge.indexer --all
    python -m knowledge.indexer --manufacturer Cummins --model KTA50
"""
import argparse
import logging
import os
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


def _get_embed_model():
    """Инициализация embedding-модели (Ollama или LMStudio)."""
    from llama_index.embeddings.ollama import OllamaEmbedding
    return OllamaEmbedding(
        model_name=settings.embedding_model,
        base_url=settings.embedding_base_url,
        request_timeout=120.0,
    )


def _get_vector_store(manufacturer: str, model: str):
    """PGVectorStore для конкретной модели оборудования."""
    from llama_index.vector_stores.postgres import PGVectorStore

    # Имя таблицы: kb_{manufacturer}_{model} (нормализованное)
    table_name = _table_name(manufacturer, model)

    return PGVectorStore.from_params(
        database=_parse_db_name(settings.analytics_db_url),
        host=_parse_host(settings.analytics_db_url),
        password=_parse_password(settings.analytics_db_url),
        port=_parse_port(settings.analytics_db_url),
        user=_parse_user(settings.analytics_db_url),
        table_name=table_name,
        embed_dim=settings.embedding_dim,
    )


def index_equipment(manufacturer: str, model: str) -> int:
    """Построить или пересобрать индекс для модели. Возвращает количество документов."""
    from llama_index.core import VectorStoreIndex, Document, StorageContext
    from llama_index.core import Settings as LlamaSettings

    LlamaSettings.embed_model = _get_embed_model()
    LlamaSettings.llm = None  # LLM не нужна для индексации

    base_path = settings.knowledge_base_path / "equipment" / manufacturer / model
    if not base_path.exists():
        raise FileNotFoundError(f"Папка не найдена: {base_path}")

    documents: list[Document] = []

    # 1. Регистры
    reg_path = base_path / "register_map.jsonl"
    if reg_path.exists():
        docs = _docs_from_register_map(reg_path, manufacturer, model)
        documents.extend(docs)
        logger.info("register_map.jsonl: %d документов", len(docs))

    # 2. Fault-биты
    fault_path = base_path / "fault_bitmap_map.jsonl"
    if fault_path.exists():
        docs = _docs_from_fault_bitmap(fault_path, manufacturer, model)
        documents.extend(docs)
        logger.info("fault_bitmap_map.jsonl: %d документов", len(docs))

    # 3. PDF-документы из docs/
    docs_dir = base_path / "docs"
    if docs_dir.exists():
        docs = _docs_from_pdfs(docs_dir, manufacturer, model)
        documents.extend(docs)
        logger.info("PDF-документы: %d чанков", len(docs))

    if not documents:
        logger.warning("Нет документов для индексации: %s/%s", manufacturer, model)
        return 0

    vector_store = _get_vector_store(manufacturer, model)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context,
        show_progress=True,
    )

    logger.info(
        "Индекс построен: %s/%s | документов=%d",
        manufacturer, model, len(documents)
    )
    return len(documents)


def _docs_from_register_map(path: Path, manufacturer: str, model: str) -> list:
    """Один документ на регистр."""
    import json
    from llama_index.core import Document

    docs = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                text = (
                    f"Регистр {rec['addr']} ({rec.get('reg_type', 'holding')}): "
                    f"{rec.get('name', '')}. "
                    f"Единица: {rec.get('unit', '')}. "
                    f"Описание: {rec.get('description', '')}. "
                    f"Тип данных: {rec.get('data_type', '')}. "
                    f"Множитель: {rec.get('multiplier', 1)}."
                )
                docs.append(Document(
                    text=text,
                    metadata={
                        "type": "register",
                        "addr": rec["addr"],
                        "manufacturer": manufacturer,
                        "model": model,
                    },
                ))
            except (json.JSONDecodeError, KeyError):
                pass
    return docs


def _docs_from_fault_bitmap(path: Path, manufacturer: str, model: str) -> list:
    """Один документ на fault-бит."""
    import json
    from llama_index.core import Document

    docs = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                text = (
                    f"Ошибка/предупреждение — регистр {rec['addr']}, бит {rec['bit']}: "
                    f"{rec.get('name', '')}. "
                    f"Описание: {rec.get('description', '')}. "
                    f"Серьёзность: {rec.get('severity', 'warning')}."
                )
                docs.append(Document(
                    text=text,
                    metadata={
                        "type": "fault",
                        "addr": rec["addr"],
                        "bit": rec["bit"],
                        "severity": rec.get("severity", "warning"),
                        "manufacturer": manufacturer,
                        "model": model,
                    },
                ))
            except (json.JSONDecodeError, KeyError):
                pass
    return docs


def _docs_from_pdfs(docs_dir: Path, manufacturer: str, model: str) -> list:
    """Чанкинг PDF-документов по странице."""
    from llama_index.core import Document

    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf не установлен, PDF-документы пропущены.")
        return []

    docs = []
    for pdf_path in docs_dir.glob("*.pdf"):
        try:
            reader = PdfReader(str(pdf_path))
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if len(text.strip()) < 50:
                    continue
                docs.append(Document(
                    text=text,
                    metadata={
                        "type": "manual",
                        "source": pdf_path.name,
                        "page": i + 1,
                        "manufacturer": manufacturer,
                        "model": model,
                    },
                ))
        except Exception as e:
            logger.warning("Ошибка обработки %s: %s", pdf_path.name, e)

    return docs


def _table_name(manufacturer: str, model: str) -> str:
    import re
    raw = f"kb_{manufacturer}_{model}".lower()
    return re.sub(r"[^a-z0-9_]", "_", raw)[:60]


# ── URL parsing helpers ───────────────────────────────────────────────────────
# postgresql://user:pass@host:port/dbname

def _parse_db_name(url: str) -> str:
    return url.split("/")[-1].split("?")[0]

def _parse_host(url: str) -> str:
    return url.split("@")[-1].split(":")[0].split("/")[0]

def _parse_port(url: str) -> int:
    try:
        return int(url.split("@")[-1].split(":")[1].split("/")[0])
    except (IndexError, ValueError):
        return 5432

def _parse_user(url: str) -> str:
    return url.split("://")[1].split(":")[0]

def _parse_password(url: str) -> str:
    try:
        return url.split("://")[1].split(":")[1].split("@")[0]
    except IndexError:
        return ""


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Построение RAG-индекса knowledge base")
    parser.add_argument("--all", action="store_true", help="Индексировать всё оборудование")
    parser.add_argument("--manufacturer", help="Производитель")
    parser.add_argument("--model", help="Модель")
    args = parser.parse_args()

    if args.all:
        eq_root = settings.knowledge_base_path / "equipment"
        for mfr_dir in sorted(eq_root.iterdir()):
            if not mfr_dir.is_dir():
                continue
            for model_dir in sorted(mfr_dir.iterdir()):
                if not model_dir.is_dir():
                    continue
                try:
                    count = index_equipment(mfr_dir.name, model_dir.name)
                    print(f"✓ {mfr_dir.name}/{model_dir.name}: {count} документов")
                except Exception as e:
                    print(f"✗ {mfr_dir.name}/{model_dir.name}: {e}", file=sys.stderr)
    elif args.manufacturer and args.model:
        count = index_equipment(args.manufacturer, args.model)
        print(f"✓ {args.manufacturer}/{args.model}: {count} документов")
    else:
        parser.print_help()
        sys.exit(1)
