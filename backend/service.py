import asyncio
import os
from langchain_core.messages import HumanMessage, AIMessage
from .settings import Settings


class RagService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def version_filter(version):
        if version == "legacy":
            return {"bool": {"must_not": [{"exists": {"field": "metadata.version"}}]}}
        return {"term": {"metadata.version": version}}

    def config(self, document_ids=None, versions=None):
        configurable = {
            "user_id": self.settings.user_id, "index_name": self.settings.index_name,
            "embedding_model": self.settings.embedding_model,
            "retriever_provider": self.settings.provider,
            "answer_model": self.settings.chat_model,
            "query_generate_model": self.settings.query_model,
        }
        if document_ids is not None:
            configurable["search_kwargs"] = {"k": 5, "filter": [{"terms": {"metadata.document_id": document_ids}}]}
            if versions is not None:
                configurable["search_kwargs"]["filter"].append({"bool": {
                    "minimum_should_match": 1,
                    "should": [{"bool": {"filter": [
                        {"term": {"metadata.document_id": key}}, self.version_filter(versions[key])
                    ]}} for key in document_ids],
                }})
        return {"configurable": configurable}

    def index(self, docs):
        from graphs.index_graph import graph
        asyncio.run(graph.ainvoke({"docs": docs}, config=self.config()))

    def chat(self, messages, document_ids, versions=None):
        from graphs.graph import graph
        converted = [(HumanMessage if item.role == "user" else AIMessage)(content=item.content) for item in messages]
        state = asyncio.run(graph.ainvoke({"messages": converted, "queries": []}, config=self.config(document_ids, versions)))
        citations = []
        for doc in state.get("retrieved_docs", []):
            if doc.metadata.get("document_id") not in document_ids:
                raise ValueError("Retrieved evidence is outside the selected documents.")
            if versions is not None and doc.metadata.get("version", "legacy") != versions[doc.metadata["document_id"]]:
                raise ValueError("Retrieved evidence belongs to an inactive version.")
            citation = {"documentId": doc.metadata["document_id"], "name": doc.metadata.get("source", "Unknown source"), "excerpt": doc.page_content}
            if isinstance(doc.metadata.get("page"), int):
                citation["page"] = doc.metadata["page"]
            citations.append(citation)
        from graphs.utils import get_message_text
        return {"answer": get_message_text(state["messages"][-1].content), "citations": citations}

    def delete(self, document_id, version=None):
        from elasticsearch import Elasticsearch, NotFoundError
        options = {"request_timeout": 15, "max_retries": 0}
        if self.settings.provider == "elastic-cloud":
            options["api_key"] = os.environ["ELASTICSEARCH_API_KEY"]
        else:
            options["basic_auth"] = (os.environ["ELASTICSEARCH_USER"], os.environ["ELASTICSEARCH_PASSWORD"])
        url = os.environ.get("RAG_ELASTICSEARCH_URL") or os.environ["ELASTICSEARCH_URL"]
        with Elasticsearch(url, **options) as client:
            try:
                filters = [
                    {"term": {"metadata.document_id": document_id}},
                    {"term": {"metadata.user_id": self.settings.user_id}},
                ]
                if version is not None:
                    filters.append(self.version_filter(version))
                result = client.delete_by_query(index=self.settings.index_name, refresh=True, query={"bool": {"filter": [
                    *filters,
                ]}})
                if result.get("failures") or result.get("timed_out"):
                    raise RuntimeError("Elasticsearch deletion did not complete.")
            except NotFoundError:
                pass


def friendly_error(error: Exception) -> str:
    # Do not leak exception bodies, credentials or remote request headers.
    from elastic_transport import ConnectionError, ConnectionTimeout
    from elasticsearch import ApiError
    if isinstance(error, (ConnectionError, ConnectionTimeout)):
        return "Cannot connect to Elasticsearch. Start the service and check RAG_ELASTICSEARCH_URL in backend/.env."
    if isinstance(error, ApiError):
        return "Elasticsearch request failed. Check credentials, permissions, and index settings."
    if isinstance(error, KeyError):
        return "Missing service configuration. Check the project .env and backend/.env."
    try:
        import openai
        if isinstance(error, openai.APIError):
            return "Model or embedding request failed. Check your API key, model name, quota, and network."
    except ImportError:
        pass
    if isinstance(error, UnicodeError):
        return "Unrecognized text encoding. Save the file as UTF-8 and upload it again."
    if isinstance(error, ValueError):
        return "Cannot process this file. Check its format, page count, text length, and OCR language settings."
    return "Processing failed. Check the file format, OCR dependencies, and backend logs."
