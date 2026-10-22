"use client";

import { useEffect, useRef, useState } from "react";
import type { Citation, KnowledgeDocument, Message } from "@/lib/types";

const statusText = { ready: "Ready", processing: "Processing", pending: "Pending", error: "Processing failed" };

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend/${path}`, options);
  if (response.ok && response.status === 204) return undefined as T;
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

export default function Home() {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [asking, setAsking] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [updateId, setUpdateId] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const conversationId = useRef("");

  async function loadDocuments() {
    try {
      const data = await request<{ documents: KnowledgeDocument[] }>("documents");
      setDocuments(data.documents);
      setSelectedIds(ids => ids.filter(id => data.documents.some(doc => doc.id === id)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load documents");
    } finally { setLoading(false); }
  }

  useEffect(() => { void loadDocuments(); }, []);
  const processing = documents.some(doc => doc.status === "processing" || doc.updating);
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(() => void loadDocuments(), 3000);
    return () => clearInterval(timer);
  }, [processing]);

  async function uploadDocument() {
    if (!file || uploading) return;
    if (file.size > 20 * 1024 * 1024) { setError("Files must be no larger than 20 MB"); return; }
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await request<{ document: KnowledgeDocument; unchanged: boolean }>(
        updateId ? `documents/${updateId}/content` : "documents",
        { method: updateId ? "PUT" : "POST", body: form },
      );
      setNotice(result.unchanged
        ? (result.document.updating ? "This file is already being processed." : "Identical content already exists. No processing needed.")
        : (updateId ? "Update started. The previous version remains available until the update succeeds." : "Upload accepted. Processing started."));
      setUpdateId("");
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await loadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally { setUploading(false); }
  }

  async function deleteDocument(doc: KnowledgeDocument) {
    if (!window.confirm(`Delete "${doc.name}"?`)) return;
    setDeletingId(doc.id);
    setError("");
    try {
      await request(`documents/${doc.id}`, { method: "DELETE" });
      setDocuments(items => items.filter(item => item.id !== doc.id));
      setSelectedIds(ids => ids.filter(id => id !== doc.id));
      setUpdateId(id => id === doc.id ? "" : id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally { setDeletingId(""); }
  }

  async function askQuestion(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!question.trim() || asking) return;
    const readyIds = selectedIds.filter(id => documents.some(doc => doc.id === id && doc.status === "ready"));
    if (!readyIds.length) { setError("Please select at least one ready document."); return; }
    const userMessage: Message = { id: crypto.randomUUID(), role: "user", content: question.trim() };
    const nextMessages = [...messages, userMessage];
    if (!conversationId.current) conversationId.current = crypto.randomUUID();
    setAsking(true);
    setError("");
    try {
      const data = await request<{ answer: string; citations: Citation[] }>("chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: conversationId.current,
          document_ids: readyIds,
          messages: nextMessages.map(({ role, content }) => ({ role, content })),
        }),
      });
      setMessages([...nextMessages, { id: crypto.randomUUID(), role: "assistant", content: data.answer, citations: data.citations }]);
      setQuestion("");
    } catch (err) {

      setError(err instanceof Error ? err.message : "Failed to send question");
    } finally { setAsking(false); }
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-4 sm:p-8">
      <header>
        <h1 className="text-2xl font-bold">My Document Q&A</h1>
        <p className="mt-2 text-sm text-gray-600">Upload documents, select your sources, and ask a question.</p>
      </header>
      {error && <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {notice && <p role="status" className="rounded border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700">{notice}</p>}

      <section className="space-y-4 rounded-lg border border-gray-200 bg-white p-5">
        <h2 className="text-lg font-semibold">1. Upload a document</h2>
        <label htmlFor="upload-target" className="block text-sm">Upload destination</label>
        <select id="upload-target" value={updateId} disabled={uploading} onChange={event => setUpdateId(event.target.value)} className="w-full rounded border border-gray-300 p-2 text-sm">
          <option value="">Add a new document</option>
          {documents.filter(doc => doc.status !== "processing" && !doc.updating).map(doc => (
            <option key={doc.id} value={doc.id}>Update: {doc.name}</option>
          ))}
        </select>
        <label htmlFor="file" className="block text-sm text-gray-600">PDF, images, TXT, Markdown, and Word. Maximum file size: 20 MB.</label>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <input ref={fileInput} id="file" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.txt,.md,.docx" disabled={uploading}
            onChange={event => setFile(event.target.files?.[0] ?? null)}
            className="min-w-0 flex-1 text-sm file:mr-3 file:rounded file:border file:border-gray-300 file:bg-gray-50 file:px-3 file:py-2" />
          <button onClick={uploadDocument} disabled={!file || uploading} className="rounded bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700">{uploading ? "Uploading…" : updateId ? "Update file" : "Upload"}</button>
        </div>
      </section>

      <section className="space-y-4 rounded-lg border border-gray-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">2. Select documents</h2>
          <button onClick={() => { setError(""); void loadDocuments(); }} className="text-sm text-blue-600">Refresh</button>
        </div>
        <p className="text-sm text-gray-500">Once processing is complete, select the documents you want to use.</p>
        {loading ? <p className="text-sm">Loading…</p> : documents.length === 0 ? <p className="text-sm text-gray-500">No documents yet. Upload a file to get started.</p> : (
          <ul className="divide-y divide-gray-100">
            {documents.map(doc => (
              <li key={doc.id} className="flex items-start gap-3 py-3">
                <input type="checkbox" aria-label={`Select ${doc.name}`} checked={selectedIds.includes(doc.id)} disabled={doc.status !== "ready" || asking}
                  onChange={event => setSelectedIds(ids => event.target.checked ? [...ids, doc.id] : ids.filter(id => id !== doc.id))} className="mt-1" />
                <div className="min-w-0 flex-1">
                  <p className="break-all text-sm">{doc.name}</p>
                  <p className="mt-1 text-xs text-gray-500">{doc.updating && doc.status === "ready" ? "Updating · previous version available" : statusText[doc.status]}</p>
                  {doc.updatedAt && <p className="mt-1 text-xs text-gray-500">Updated: {new Date(doc.updatedAt).toLocaleString()}</p>}
                  {doc.error && <p className="mt-1 text-sm text-red-600">{doc.error}</p>}
                </div>
                <button onClick={() => { setUpdateId(doc.id); fileInput.current?.focus(); }} disabled={uploading || doc.status === "processing" || doc.updating} className="shrink-0 text-sm text-blue-600">Update</button>
                <button onClick={() => void deleteDocument(doc)} disabled={Boolean(deletingId) || asking || doc.status === "processing" || doc.updating} className="shrink-0 text-sm text-red-600">{deletingId === doc.id ? "Deleting…" : "Delete"}</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-4 rounded-lg border border-gray-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">3. Ask a question</h2>
          <button disabled={asking || messages.length === 0} className="text-sm text-gray-500" onClick={() => { setMessages([]); conversationId.current = ""; }}>Clear chat</button>
        </div>
        <div aria-live="polite" className="space-y-3">
          {messages.length === 0 && <p className="text-sm text-gray-500">For example: What is this document about?</p>}
          {messages.map(message => (
            <div key={message.id} className={`rounded p-3 ${message.role === "user" ? "bg-blue-50" : "bg-gray-50"}`}>
              <p className="mb-2 text-sm font-semibold">{message.role === "user" ? "You" : "Assistant"}</p>
              <p className="whitespace-pre-wrap break-words text-sm leading-7">{message.content}</p>
              {message.citations?.map((source, index) => (
                <details key={index} className="mt-3 text-sm">
                  <summary className="cursor-pointer break-words text-blue-700">Source {index + 1}: {source.name}{source.page != null ? ` (page ${source.page})` : ""}</summary>
                  <p className="mt-2 whitespace-pre-wrap break-words text-gray-600">{source.excerpt}</p>
                </details>
              ))}
            </div>
          ))}
          {asking && <p role="status" className="text-sm text-gray-500">Searching your documents and preparing an answer…</p>}
        </div>
        <form onSubmit={askQuestion} className="space-y-3">
          <label htmlFor="question" className="block text-sm">Your question</label>
          <textarea id="question" value={question} disabled={asking} maxLength={8000} onChange={event => setQuestion(event.target.value)} rows={3} placeholder="Type your question here" className="w-full rounded border border-gray-300 p-3 text-sm" />
          <button type="submit" disabled={asking || !question.trim() || selectedIds.length === 0} className="rounded bg-blue-600 px-5 py-2 text-sm text-white hover:bg-blue-700">{asking ? "Answering…" : "Send"}</button>
        </form>
      </section>
      <p className="text-center text-xs text-gray-500">This chat is only saved on this page. Refreshing will clear it.</p>
    </main>
  );
}
