import io
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile
from contextlib import contextmanager
from unittest.mock import patch
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from backend.main import create_app, ChatMessage
from backend.settings import Settings
from backend.service import RagService
from backend.loaders import load_documents


class FakeService:
    def __init__(self):
        self.docs = []
        self.fail_index = False
        self.fail_delete = False
        self.selected = []

    def index(self, docs):
        if self.fail_index:
            raise RuntimeError("secret-api-key-should-not-appear")
        self.docs.extend(docs)

    def delete(self, document_id, version=None):
        if self.fail_delete:
            raise RuntimeError("secret-delete-error")
        self.docs = [doc for doc in self.docs if not (doc.metadata["document_id"] == document_id
                     and (version is None or doc.metadata.get("version", "legacy") == version))]

    def chat(self, messages, document_ids, versions=None):
        self.selected = document_ids
        self.versions = versions
        return {"answer": "Test answer [1]", "citations": [{"documentId": document_ids[0], "name": "test.txt", "excerpt": "Test text"}]}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parents[1] / "data" / "test-runs"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.service = FakeService()
        self.app = create_app(self.settings, self.service)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(self.test_root.resolve()))
        self.temp.cleanup()

    def upload(self, name="test.txt", content=b"RAG test content"):
        response = self.client.post("/documents", files={"file": (name, content)})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["document"]["id"]

    def test_upload_list_and_metadata(self):
        key = self.upload("../../test.txt")
        record = self.client.get("/documents").json()["documents"][0]
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["name"], "test.txt")
        self.assertNotIn("path", record)
        self.assertEqual(self.service.docs[0].metadata["document_id"], key)
        self.assertEqual(self.service.docs[0].metadata["source"], "test.txt")

    def test_failure_is_persisted_and_sanitized(self):
        self.service.fail_index = True
        self.upload()
        record = self.client.get("/documents").json()["documents"][0]
        self.assertEqual(record["status"], "error")
        self.assertTrue(record["error"])
        self.assertNotIn("secret", str(record))

    def test_empty_unsupported_and_large_files(self):
        self.assertEqual(self.client.post("/documents", files={"file": ("a.txt", b"")}).status_code, 400)
        self.assertEqual(self.client.post("/documents", files={"file": ("a.exe", b"bad")}).status_code, 415)
        self.settings.max_bytes = 4
        self.assertEqual(self.client.post("/documents", files={"file": ("a.txt", b"12345")}).status_code, 413)
        self.assertEqual(list(self.app.state.catalog.uploads.iterdir()), [])

    def test_chat_scope_and_validation(self):
        key = self.upload()
        body = {"conversation_id": str(uuid4()), "document_ids": [key], "messages": [{"role": "user", "content": "Question"}]}
        response = self.client.post("/chat", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.selected, [key])
        self.assertEqual(response.json()["citations"][0]["documentId"], key)
        body["messages"][0]["role"] = "system"
        self.assertEqual(self.client.post("/chat", json=body).status_code, 422)
        body["messages"][0]["role"] = "user"
        body["document_ids"] = [str(uuid4())]
        self.assertEqual(self.client.post("/chat", json=body).status_code, 404)

    def test_failed_document_cannot_be_queried(self):
        self.service.fail_index = True
        key = self.upload()
        response = self.client.post("/chat", json={"conversation_id": str(uuid4()), "document_ids": [key], "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 409)

    def test_delete_cleans_file_and_record(self):
        key = self.upload()
        self.assertEqual(self.client.delete(f"/documents/{key}").status_code, 204)
        self.assertEqual(self.client.get("/documents").json()["documents"], [])
        self.assertEqual(list(self.app.state.catalog.uploads.iterdir()), [])
        self.assertEqual(self.service.docs, [])

    def test_delete_failure_keeps_record_for_retry(self):
        key = self.upload()
        self.service.fail_delete = True
        response = self.client.delete(f"/documents/{key}")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret", response.text)
        self.assertIsNotNone(self.app.state.catalog.get(key))

    def test_catalog_survives_restart(self):
        self.upload()
        with TestClient(create_app(self.settings, self.service)) as client:
            self.assertEqual(client.get("/documents").json()["documents"][0]["status"], "ready")

    def test_docx_and_empty_text_loader(self):
        path = Path(self.temp.name)/"text.docx"
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Résumé content</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(load_documents(path, "text.docx", "id")[0].page_content, "Résumé content")
        empty = Path(self.temp.name)/"empty.txt"
        empty.write_text("  ")
        with self.assertRaises(ValueError): load_documents(empty, "empty.txt", "id")

    def test_pdf_text_extraction(self):
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        stream=b"BT /F1 12 Tf 20 250 Td (Hello PDF) Tj ET"
        objects.append(b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream")
        content=b"%PDF-1.4\n"; offsets=[0]
        for i,obj in enumerate(objects,1):
            offsets.append(len(content)); content+=f"{i} 0 obj\n".encode()+obj+b"\nendobj\n"
        xref=len(content)
        content+=b"xref\n0 6\n0000000000 65535 f \n"+b"".join(f"{p:010d} 00000 n \n".encode() for p in offsets[1:])
        content+=f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
        path=Path(self.temp.name)/"test.pdf"; path.write_bytes(content)
        docs=load_documents(path,"test.pdf","pdf-id")
        self.assertIn("Hello PDF",docs[0].page_content)
        self.assertEqual(docs[0].metadata["page"],1)
        self.assertEqual(docs[0].metadata["extraction_method"],"text")

    def test_real_graph_adapter_with_mock_external_services(self):
        import graphs.graph as graph_module
        seen = {}
        class Retriever:
            async def ainvoke(self, query, config):
                seen["query"] = query
                return [Document(page_content="evidence", metadata={"source":"test.pdf","document_id":"selected-id","page":2})]
        @contextmanager
        def make_retriever(config):
            seen["config"] = config
            yield Retriever()
        class Model:
            async def ainvoke(self, prompt, config):
                seen["prompt"] = prompt.to_messages()[0].content
                return AIMessage(content="Answer [1]")
        with patch.object(graph_module,"make_retriever",make_retriever), patch.object(graph_module,"init_chat_model",return_value=Model()):
            result=RagService(self.settings).chat([ChatMessage(role="user",content="question")],["selected-id"])
        self.assertEqual(seen["config"].search_kwargs["filter"],[{"terms":{"metadata.document_id":["selected-id"]}}])
        self.assertEqual(seen["query"],"question")
        self.assertIn("[1]",seen["prompt"])
        self.assertEqual(result["citations"][0]["page"],2)
        self.assertEqual(result["answer"],"Answer [1]")


if __name__ == "__main__":
    unittest.main()
