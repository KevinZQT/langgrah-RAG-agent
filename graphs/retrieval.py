from typing import Generator
from contextlib import contextmanager
from langchain_openai import OpenAIEmbeddings
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStoreRetriever
from langchain_elasticsearch import ElasticsearchStore
from .configuration import IndexConfiguration

import os
from dotenv import load_dotenv

load_dotenv()


def make_text_encoder(model: str) -> Embeddings:

    return OpenAIEmbeddings(model=model)

@contextmanager
def make_retriever(
    configuration: IndexConfiguration
    ) -> Generator[VectorStoreRetriever, None, None]:

    settings = {}
    if configuration.retriever_provider == "elastic-local":
        settings = {
            "es_user": os.environ['ELASTICSEARCH_USER'],
            "es_password": os.environ["ELASTICSEARCH_PASSWORD"]
        }
    else:
        settings = {
            "es_api_key": os.environ["ELASTICSEARCH_API_KEY"]
        }

    embedding = make_text_encoder(configuration.embedding_model)

    vstore = ElasticsearchStore(
        **settings,
        es_url = os.environ.get("RAG_ELASTICSEARCH_URL") or os.environ["ELASTICSEARCH_URL"],
        index_name = configuration.index_name,
        embedding = embedding,
        metadata_mappings={"user_id": {"type": "keyword"}, "document_id": {"type": "keyword"}, "version": {"type": "keyword"}},
        es_params={"request_timeout": 10, "max_retries": 0},
    )

    search_kwargs = dict(configuration.search_kwargs)
    search_filter = list(search_kwargs.get("filter") or [])
    
    search_filter.append({
        "term": {"metadata.user_id": configuration.user_id}
    })
    search_kwargs["filter"] = search_filter
    try:
        yield vstore.as_retriever(search_kwargs = search_kwargs)
    finally:
        vstore.close()

