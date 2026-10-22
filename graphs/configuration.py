from __future__ import annotations

from dataclasses import dataclass, field, fields
from langchain_core.runnables import ensure_config, RunnableConfig
from typing import Annotated, Any, Literal, Self


@dataclass(kw_only=True)
class IndexConfiguration:

    user_id: str = field(metadata={"description": "Unique identifier for the user."})
    embedding_model: str = field(default="text-embedding-3-small")
    search_kwargs: dict[str, Any] = field(default_factory=dict)
    index_name: str = field(default="langchain_index")
    retriever_provider: Literal["elastic-local", "elastic-cloud"] = field(default="elastic-local")

    

    @classmethod
    def from_runnable_config(cls: Self, config: RunnableConfig | None = None) -> Self:

        config = ensure_config(config)
        configurable = config.get("configurable") or {}
        _fields = {f.name for f in fields(cls) if f.init}
        return cls(**{k: v for k, v in configurable.items() if k in _fields})




@dataclass(kw_only=True)
class Configuration(IndexConfiguration):

    query_generate_model: str = field(default="gpt-3.5-mini")
    answer_model: str = field(default="gpt-3.5-mini")
