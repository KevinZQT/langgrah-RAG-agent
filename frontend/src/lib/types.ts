export type KnowledgeDocument = {
  id: string;
  name: string;
  kind: string;
  size: number;
  status: "ready" | "processing" | "pending" | "error";
  addedAt: string;
  updatedAt?: string;
  updating?: boolean;
  excerpt?: string;
  error?: string;
};

export type Citation = {
  documentId: string;
  name: string;
  page?: number;
  excerpt: string;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
};

export type Conversation = {
  id: string;
  title: string;
  messages: Message[];
};
