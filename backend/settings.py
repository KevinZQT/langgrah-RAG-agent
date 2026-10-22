import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("RAG_DATA_DIR", str(ROOT / "backend" / "data"))))
    user_id: str = field(default_factory=lambda: os.getenv("RAG_USER_ID", "local-user"))
    index_name: str = field(default_factory=lambda: os.getenv("RAG_INDEX_NAME", "folio_documents_v1"))
    chat_model: str = field(default_factory=lambda: os.getenv("RAG_CHAT_MODEL", "gpt-4.1-mini"))
    query_model: str = field(default_factory=lambda: os.getenv("RAG_QUERY_MODEL", os.getenv("RAG_CHAT_MODEL", "gpt-4.1-mini")))
    embedding_model: str = field(default_factory=lambda: os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small"))
    provider: str = field(default_factory=lambda: os.getenv("RAG_RETRIEVER_PROVIDER", "elastic-local"))
    max_bytes: int = 20 * 1024 * 1024
