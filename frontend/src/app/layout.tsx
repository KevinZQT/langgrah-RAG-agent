import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "My Document Q&A",
  description: "Upload documents and ask questions about their contents.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}

