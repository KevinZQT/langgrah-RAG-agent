import hashlib
import sqlite3
import tempfile
import threading
import unittest
import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.documents import Document

from backend.main import create_app
from backend.service import RagService
from backend.settings import Settings
from backend.storage import Catalog
from backend.tests.test_api import FakeService

TEST_ROOT = Path(__file__).resolve().parents[1] / "data" / "test-runs"


def temporary_directory():
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=TEST_ROOT)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.service = FakeService()
        self.app = create_app(self.settings, self.service)
        self.client = TestClient(self.app).__enter__()
        self.catalog = self.app.state.catalog

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def upload(self, content=b"original"):
        response = self.client.post("/documents", files={"file": ("test.txt", content)})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["document"]["id"]

    def update(self, key, content=b"replacement", name="test.txt"):
        return self.client.put(f"/documents/{key}/content", files={"file": (name, content)})

    def chat(self, key):
        return self.client.post("/chat", json={"conversation_id": str(uuid4()), "document_ids": [key],
                                              "messages": [{"role": "user", "content": "question"}]})

    def test_identical_upload_and_update_skip_indexing(self):
        key = self.upload()
        before = self.catalog.get(key)
        with patch.object(self.service, "index", side_effect=AssertionError("should skip")):
            duplicate = self.client.post("/documents", files={"file": ("renamed.txt", b"original")})
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(duplicate.json()["document"]["id"], key)
            result = self.update(key, b"original")
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()["unchanged"])
        self.assertEqual(len(self.catalog.all()), 1)
        self.assertEqual(self.catalog.get(key)["active_version"], before["active_version"])
        self.assertEqual(len(list(self.catalog.uploads.iterdir())), 1)

    def test_update_preserves_id_and_replaces_only_target(self):
        key = self.upload()
        other = self.upload(b"other")
        before = self.catalog.get(key)
        self.assertEqual(self.update(key, name="new.txt").status_code, 202)
        after = self.catalog.get(key)
        self.assertEqual(after["id"], key)
        self.assertEqual(after["addedAt"], before["addedAt"])
        self.assertNotEqual(after["active_version"], before["active_version"])
        self.assertEqual(after["file_hash"], hashlib.sha256(b"replacement").hexdigest())
        self.assertEqual(after["name"], "new.txt")
        self.assertFalse(Path(before["path"]).exists())
        self.assertEqual({doc.page_content for doc in self.service.docs}, {"replacement", "other"})
        self.assertEqual(self.chat(key).status_code, 200)
        self.assertEqual(self.service.versions, {key: after["active_version"]})
        self.assertEqual(self.catalog.get(other)["status"], "ready")

    def test_partial_index_failure_preserves_old_content_and_can_retry(self):
        key = self.upload()
        before = self.catalog.get(key)
        def partial(docs):
            self.service.docs.extend(docs)
            raise RuntimeError("secret-key")
        with patch.object(self.service, "index", side_effect=partial):
            self.assertEqual(self.update(key).status_code, 202)
        after = self.catalog.get(key)
        self.assertEqual(after["status"], "ready")
        self.assertEqual(after["active_version"], before["active_version"])
        self.assertEqual(Path(after["path"]).read_bytes(), b"original")
        self.assertNotIn("secret", after["error"])
        self.assertFalse(after["updating"])
        self.assertEqual([doc.page_content for doc in self.service.docs], ["original"])
        self.assertEqual(self.chat(key).status_code, 200)
        self.assertEqual(self.update(key).status_code, 202)
        self.assertEqual(self.catalog.get(key)["error"], "")

    def test_parse_failure_keeps_previous_version(self):
        key = self.upload()
        before = self.catalog.get(key)
        self.assertEqual(self.update(key, b"  ").status_code, 202)
        self.assertEqual(self.catalog.get(key)["active_version"], before["active_version"])
        self.assertEqual(self.chat(key).status_code, 200)

    def test_failed_first_upload_can_be_updated_without_deleting(self):
        self.service.fail_index = True
        key = self.upload()
        self.assertEqual(self.catalog.get(key)["status"], "error")
        self.service.fail_index = False
        self.assertEqual(self.update(key, b"original").status_code, 202)
        self.assertEqual(self.catalog.get(key)["status"], "ready")

    def test_cleanup_failure_does_not_undo_success_and_retries_after_restart(self):
        key = self.upload()
        old = self.catalog.get(key)
        self.service.fail_delete = True
        self.update(key)
        current = self.catalog.get(key)
        self.assertEqual(current["status"], "ready")
        self.assertNotEqual(current["active_version"], old["active_version"])
        self.assertEqual(len(self.catalog.versions(key, "obsolete")), 1)
        self.assertEqual(self.chat(key).status_code, 200)
        self.assertEqual(self.service.versions[key], current["active_version"])
        self.service.fail_delete = False
        with TestClient(create_app(self.settings, self.service)):
            pass
        self.assertEqual(self.catalog.versions(key, "obsolete"), [])
        self.assertFalse(Path(old["path"]).exists())
        self.assertTrue(Path(current["path"]).exists())

    def test_pending_update_keeps_chat_available_and_rejects_mutations(self):
        key = self.upload()
        old = self.catalog.get(key)
        started, release = threading.Event(), threading.Event()
        original_index = self.service.index
        def blocked(docs):
            started.set()
            if not release.wait(10):
                raise TimeoutError("test timeout")
            original_index(docs)
        results = []
        with patch.object(self.service, "index", side_effect=blocked):
            thread = threading.Thread(target=lambda: results.append(self.update(key)))
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertTrue(self.catalog.get(key)["updating"])
                self.assertEqual(self.chat(key).status_code, 200)
                self.assertEqual(self.service.versions[key], old["active_version"])
                self.assertEqual(self.update(key).status_code, 409)
                self.assertEqual(self.client.delete(f"/documents/{key}").status_code, 409)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 202)

    def test_restart_discards_pending_version_and_preserves_active(self):
        key = self.upload()
        active = self.catalog.get(key)
        path = self.catalog.uploads / f"{key}_interrupted.txt"
        path.write_bytes(b"interrupted")
        record = dict(active, path=str(path), file_hash="pending")
        self.catalog.stage(record, "interrupted")
        self.service.docs.append(Document(page_content="partial", metadata={"document_id": key, "version": "interrupted"}))
        with TestClient(create_app(self.settings, self.service)) as restarted:
            row = restarted.get("/documents").json()["documents"][0]
            self.assertEqual(row["status"], "ready")
            self.assertFalse(row["updating"])
            self.assertTrue(row["error"])
        self.assertEqual(self.catalog.get(key)["active_version"], active["active_version"])
        self.assertEqual([d.page_content for d in self.service.docs], ["original"])
        self.assertFalse(path.exists())

    def test_invalid_updates_leave_active_file_untouched(self):
        key = self.upload()
        before = self.catalog.get(key)
        self.assertEqual(self.update(key, b"").status_code, 400)
        self.assertEqual(self.update(key, b"bad", "file.exe").status_code, 415)
        self.assertEqual(self.update(str(uuid4())).status_code, 404)
        self.settings.max_bytes = 1
        self.assertEqual(self.update(key).status_code, 413)
        self.assertEqual(self.catalog.get(key), before)
        self.assertEqual(len(list(self.catalog.uploads.iterdir())), 1)

    def test_delete_cleans_retained_versions(self):
        key = self.upload()
        self.service.fail_delete = True
        self.update(key)
        self.service.fail_delete = False
        self.assertEqual(self.client.delete(f"/documents/{key}").status_code, 204)
        self.assertEqual(self.catalog.versions(key), [])
        self.assertEqual(list(self.catalog.uploads.iterdir()), [])
        self.assertEqual(self.service.docs, [])

    def test_filters_pair_each_document_with_its_own_version(self):
        filters = RagService(self.settings).config(["a", "b"], {"a": "legacy", "b": "v2"})["configurable"]["search_kwargs"]["filter"]
        scope = filters[1]["bool"]
        self.assertEqual(scope["minimum_should_match"], 1)
        self.assertEqual(scope["should"][0]["bool"]["filter"], [
            {"term": {"metadata.document_id": "a"}},
            {"bool": {"must_not": [{"exists": {"field": "metadata.version"}}]}}])
        self.assertEqual(scope["should"][1]["bool"]["filter"], [
            {"term": {"metadata.document_id": "b"}}, {"term": {"metadata.version": "v2"}}])


class MigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_idempotently_and_updates(self):
        with temporary_directory() as root:
            root = Path(root)
            (root / "uploads").mkdir()
            key = str(uuid4())
            path = root / "uploads" / f"{key}.txt"
            path.write_bytes(b"legacy")
            with sqlite3.connect(root / "catalog.sqlite3") as conn:
                conn.execute("""CREATE TABLE documents (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    kind TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL, addedAt TEXT NOT NULL,
                    excerpt TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
                    index_started INTEGER NOT NULL DEFAULT 0)""")
                conn.execute("INSERT INTO documents (id,name,kind,size,status,addedAt,path,index_started) VALUES (?,?,?,?,'ready',?,?,1)",
                             (key, "old.txt", "TXT", 6, "2026-01-01", str(path)))
            conn.close()
            catalog = Catalog(root)
            self.assertEqual(catalog.get(key)["active_version"], "legacy")
            self.assertEqual(len(Catalog(root).versions(key)), 1)
            service = FakeService()
            service.docs = [Document(page_content="legacy", metadata={"document_id": key})]
            with TestClient(create_app(Settings(data_dir=root), service)) as client:
                same = client.put(f"/documents/{key}/content", files={"file": ("old.txt", b"legacy")})
                self.assertEqual(same.status_code, 200)
                updated = client.put(f"/documents/{key}/content", files={"file": ("new.txt", b"new")})
                self.assertEqual(updated.status_code, 202)
            self.assertEqual([d.page_content for d in service.docs], ["new"])
            self.assertFalse(path.exists())


class IndexAdapterTests(unittest.TestCase):
    def test_graph_preserves_version_and_uses_repeatable_ids(self):
        from graphs.index_graph import graph
        calls = []
        class Retriever:
            async def aadd_documents(self, docs, **kwargs):
                calls.append((docs, kwargs))
        @contextmanager
        def retriever(configuration):
            yield Retriever()
        docs = [Document(page_content="paragraph " * 200, metadata={"document_id": "doc", "version": "abc123"})]
        config = RagService(Settings()).config()
        with patch("graphs.index_graph.make_retriever", retriever):
            for _ in range(2):
                asyncio.run(graph.ainvoke({"docs": docs}, config=config))
        chunks, options = calls[0]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len(options["ids"]), len(set(options["ids"])))
        self.assertEqual(options["ids"], calls[1][1]["ids"])
        self.assertTrue(options["refresh_indices"])
        self.assertTrue(all(d.metadata["version"] == "abc123" for d in chunks))

    def test_delete_limits_scope_to_user_document_and_version(self):
        with patch("elasticsearch.Elasticsearch") as client_class, patch.dict("os.environ", {
            "ELASTICSEARCH_USER": "test", "ELASTICSEARCH_PASSWORD": "test", "ELASTICSEARCH_URL": "http://localhost:9200"
        }):
            client = client_class.return_value.__enter__.return_value
            client.delete_by_query.return_value = {"failures": [], "timed_out": False}
            settings = Settings(provider="elastic-local")
            service = RagService(settings)
            service.delete("doc", version="old")
            filters = client.delete_by_query.call_args.kwargs["query"]["bool"]["filter"]
            self.assertEqual(filters, [{"term": {"metadata.document_id": "doc"}},
                                      {"term": {"metadata.user_id": settings.user_id}},
                                      {"term": {"metadata.version": "old"}}])
            service.delete("doc", version="legacy")
            self.assertEqual(client.delete_by_query.call_args.kwargs["query"]["bool"]["filter"][-1],
                             {"bool": {"must_not": [{"exists": {"field": "metadata.version"}}]}})

    def test_chat_rejects_stale_evidence(self):
        from unittest.mock import AsyncMock
        from backend.main import ChatMessage
        from langchain_core.messages import AIMessage
        stale = Document(page_content="old", metadata={"document_id": "doc", "version": "old"})
        with patch("graphs.graph.graph.ainvoke", new=AsyncMock(return_value={
            "retrieved_docs": [stale], "messages": [AIMessage(content="answer")]
        })):
            with self.assertRaises(ValueError):
                RagService(Settings()).chat([ChatMessage(role="user", content="question")], ["doc"], {"doc": "new"})


if __name__ == "__main__":
    unittest.main()
