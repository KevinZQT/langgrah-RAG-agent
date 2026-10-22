import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context) {
  const base = process.env.RAG_API_URL?.trim();
  if (!base) return NextResponse.json({ error: "The Python backend URL is not configured." }, { status: 503 });
  const { path } = await context.params;
  const valid = (path.length === 1 && (
    (path[0] === "documents" && ["GET", "POST"].includes(request.method)) ||
    (path[0] === "chat" && request.method === "POST")
  )) || (path.length === 2 && path[0] === "documents" && request.method === "DELETE")
    || (path.length === 3 && path[0] === "documents" && path[2] === "content" && request.method === "PUT");
  if (!valid) return NextResponse.json({ error: "Endpoint not found." }, { status: 404 });
  try {
    const target = `${base.replace(/\/$/, "")}/${path.map(encodeURIComponent).join("/")}`;
    const headers = new Headers({ Accept: "application/json" });
    const contentType = request.headers.get("content-type");
    if (contentType) headers.set("Content-Type", contentType);
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      signal: AbortSignal.timeout(120_000),
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      return NextResponse.json(
        { error: typeof detail?.error === "string" ? detail.error : `Backend request failed (${response.status}). Check the Python server logs.` },
        { status: response.status },
      );
    }
    if (response.status === 204) return new Response(null, { status: 204 });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json(
      { error: "Cannot connect to the Python backend, or the request timed out. Check the server and RAG_API_URL." },
      { status: 502 },
    );
  }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as DELETE };
