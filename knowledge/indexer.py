"""Построение pgvector-индекса для RAG.

Индексирует register_map.jsonl, fault_bitmap_map.jsonl и PDF-документы
из knowledge_base/equipment/{kb_path}/docs/.

Использование:
    python -m knowledge.indexer --all
    python -m knowledge.indexer --kb-path cummins_kta50_pcc3300
"""
import argparse
import logging
from pathlib import Path

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


def _get_vector_store(kb_path: str):
    """PGVectorStore для конкретной KB-папки."""
    from llama_index.vector_stores.postgres import PGVectorStore

    table_name = _table_name(kb_path)

    return PGVectorStore.from_params(
        database=_parse_db_name(settings.analytics_db_url),
        host=_parse_host(settings.analytics_db_url),
        password=_parse_password(settings.analytics_db_url),
        port=_parse_port(settings.analytics_db_url),
        user=_parse_user(settings.analytics_db_url),
        table_name=table_name,
        embed_dim=settings.embedding_dim,
    )


def index_equipment(kb_path: str, progress_cb=None) -> int:
    """Построить или пересобрать индекс для kb_path. Возвращает количество документов.

    progress_cb(step: str, total: int) — вызывается на каждом этапе (thread-safe).
    """
    from llama_index.core import VectorStoreIndex, Document, StorageContext
    from llama_index.core import Settings as LlamaSettings

    def _cb(step: str, total: int = 0):
        if progress_cb:
            progress_cb(step, total)

    from llama_index.core.node_parser import SentenceSplitter

    LlamaSettings.embed_model = _get_embed_model()
    LlamaSettings.llm = None
    # nomic-embed-text: 8192 token ctx. LlamaIndex uses tiktoken (GPT tokens),
    # которые ~2-3× меньше реальных токенов nomic. Ставим chunk_size=256 — запас 10×.
    LlamaSettings.transformations = [SentenceSplitter(chunk_size=256, chunk_overlap=32)]

    base_path = settings.knowledge_base_path / "equipment" / kb_path
    if not base_path.exists():
        raise FileNotFoundError(f"Папка не найдена: {base_path}")

    documents: list[Document] = []

    reg_path = base_path / "register_map.jsonl"
    if reg_path.exists():
        _cb("Загрузка register_map…")
        docs = _docs_from_register_map(reg_path, kb_path)
        documents.extend(docs)
        logger.info("register_map.jsonl: %d документов", len(docs))
        _cb(f"Регистры: {len(docs)} записей", len(documents))

    fault_path = base_path / "fault_bitmap_map.jsonl"
    if fault_path.exists():
        _cb("Загрузка fault_bitmap_map…", len(documents))
        docs = _docs_from_fault_bitmap(fault_path, kb_path)
        documents.extend(docs)
        logger.info("fault_bitmap_map.jsonl: %d документов", len(docs))
        _cb(f"Fault-биты: {len(docs)} записей", len(documents))

    docs_dir = base_path / "docs"
    if docs_dir.exists():
        _cb("Обработка PDF-документов…", len(documents))
        docs = _docs_from_pdfs(docs_dir, kb_path)
        documents.extend(docs)
        logger.info("PDF-документы: %d чанков", len(docs))
        _cb(f"PDF-чанки: {len(docs)} страниц", len(documents))

    if not documents:
        logger.warning("Нет документов для индексации: %s", kb_path)
        return 0

    _cb(f"Построение векторного индекса ({len(documents)} документов)…", len(documents))
    vector_store = _get_vector_store(kb_path)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context,
        show_progress=True,
    )

    logger.info("Индекс построен: %s | документов=%d", kb_path, len(documents))
    return len(documents)


def _docs_from_register_map(path: Path, kb_path: str) -> list:
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
                # notes_ru приоритетнее description (чище, на русском)
                desc = rec.get("notes_ru") or rec.get("description", "")
                text = (
                    f"Регистр {rec['addr']} ({rec.get('reg_type', 'holding')}): "
                    f"{rec.get('name', '')}. "
                    f"Единица: {rec.get('unit', '')}. "
                    f"Описание: {desc}. "
                    f"Тип данных: {rec.get('data_type', '')}. "
                    f"Множитель: {rec.get('multiplier', 1)}."
                )[:_CHUNK_MAX_CHARS]
                docs.append(Document(
                    text=text,
                    metadata={"type": "register", "addr": rec["addr"], "kb_path": kb_path},
                ))
            except (json.JSONDecodeError, KeyError):
                pass
    return docs


def _docs_from_fault_bitmap(path: Path, kb_path: str) -> list:
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
                )[:_CHUNK_MAX_CHARS]
                docs.append(Document(
                    text=text,
                    metadata={
                        "type": "fault",
                        "addr": rec["addr"],
                        "bit": rec["bit"],
                        "severity": rec.get("severity", "warning"),
                        "kb_path": kb_path,
                    },
                ))
            except (json.JSONDecodeError, KeyError):
                pass
    return docs


_CHUNK_MAX_CHARS = 1500   # ~375 токенов — большой запас для nomic-embed-text (8192 ctx)
_CHUNK_OVERLAP   = 100


def _split_text(text: str) -> list[str]:
    """Разбить длинный текст на перекрывающиеся чанки."""
    if len(text) <= _CHUNK_MAX_CHARS:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_MAX_CHARS
        chunks.append(text[start:end])
        start = end - _CHUNK_OVERLAP
    return chunks


def _docs_from_pdfs(docs_dir: Path, kb_path: str) -> list:
    """Чанкинг PDF-документов по странице (с разбивкой длинных страниц)."""
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
                for chunk_idx, chunk in enumerate(_split_text(text)):
                    docs.append(Document(
                        text=chunk,
                        metadata={
                            "type": "manual",
                            "source": pdf_path.name,
                            "page": i + 1,
                            "chunk": chunk_idx,
                            "kb_path": kb_path,
                        },
                    ))
        except Exception as e:
            logger.warning("Ошибка обработки %s: %s", pdf_path.name, e)

    return docs


def _table_name(kb_path: str) -> str:
    import re
    raw = f"kb_{kb_path}".lower()
    return re.sub(r"[^a-z0-9_]", "_", raw)[:60]


# ── URL parsing helpers ───────────────────────────────────────────────────────

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
    parser.add_argument("--all", action="store_true", help="Индексировать все KB-папки")
    parser.add_argument("--kb-path", help="Папка equipment/ (например: cummins_kta50_pcc3300)")
    args = parser.parse_args()

    if args.all:
        eq_root = settings.knowledge_base_path / "equipment"
        for kb_dir in sorted(eq_root.iterdir()):
            if not kb_dir.is_dir():
                continue
            try:
                count = index_equipment(kb_dir.name)
                print(f"✓ {kb_dir.name}: {count} документов")
            except Exception as e:
                print(f"✗ {kb_dir.name}: {e}", file=sys.stderr)
    elif args.kb_path:
        count = index_equipment(args.kb_path)
        print(f"✓ {args.kb_path}: {count} документов")
    else:
        parser.print_help()
        sys.exit(1)
