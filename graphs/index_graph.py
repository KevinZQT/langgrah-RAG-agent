from langchain_core.runnables import RunnableConfig
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.graph import StateGraph, END
from .configuration import IndexConfiguration
from .state import IndexState
from .retrieval import make_retriever
from typing import Any
import hashlib


def prepare_docs(state: IndexState, config: RunnableConfig | None = None) -> dict[str, Any]:

    if not config:
        raise ValueError("Configuration required to run prepare_docs.")

    configuration = IndexConfiguration.from_runnable_config(config)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size = 800,
        chunk_overlap = 120,
        length_function = len,
        separators=[
            "\n\n",
            "\n",
            ". ",
            " ",
            ",",
            ""
        ],
        keep_separator = "end",
        add_start_index = True
    )
    user_id = configuration.user_id

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("User id must be non-empty string")

    docs = [
        Document(
            page_content = doc.page_content, 
            metadata = {**doc.metadata, "user_id": user_id}
            )
        for doc in state.docs
        if doc.page_content.strip()
    ]

 
    chunks = splitter.split_documents(docs)
    
    return {"docs": chunks}



async def index_docs(state: IndexState, config: RunnableConfig | None = None) -> dict[str, Any]:
    if not state.docs:
        return {"docs": "delete"}
    configuration = IndexConfiguration.from_runnable_config(config)
    with make_retriever(configuration) as retriever:

        # Explicit IDs make a retry within the same version overwrite its chunks.
        ids = [hashlib.sha256(
            f"{configuration.user_id}:{doc.metadata['document_id']}:{doc.metadata['version']}:{number}".encode()
        ).hexdigest() for number, doc in enumerate(state.docs)] if all(
            "document_id" in doc.metadata and "version" in doc.metadata for doc in state.docs
        ) else None
        await retriever.aadd_documents(state.docs, ids=ids, refresh_indices=True)

    return {"docs": "delete"}


builder = StateGraph(IndexState)

builder.add_node("prepare_docs", prepare_docs)
builder.add_node("index_docs", index_docs)
builder.set_entry_point("prepare_docs")
builder.add_edge("prepare_docs", "index_docs")
builder.add_edge("index_docs", END)

graph = builder.compile()

graph.name = "index_graph"
