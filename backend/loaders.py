from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile
from langchain_core.documents import Document

SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp"}


def load_documents(path: Path, source: str, document_id: str) -> list[Document]:
    suffix = path.suffix.lower()
    docs = []
    if suffix in {".txt", ".md"}:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
        docs = [Document(page_content=text, metadata={"extraction_method": "text"})]
    elif suffix == ".docx":
        with ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 30 * 1024 * 1024:
                raise ValueError("Uncompressed Word document text exceeds the size limit.")
            xml = archive.read(info)
        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
            raise ValueError("Documents containing XML entities are not supported.")
        root = ElementTree.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        text = "\n".join("".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in root.findall(".//w:p", ns))
        docs = [Document(page_content=text, metadata={"extraction_method": "docx"})]
    elif suffix == ".pdf":
        import pypdfium2 as pdfium
        from graphs.ocr import recognize_image
        import numpy as np
        pdf = pdfium.PdfDocument(str(path))
        try:
            if len(pdf) > 200:
                raise ValueError("PDF files must contain no more than 200 pages.")
            for number in range(len(pdf)):
                page = pdf[number]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range().strip()
                    finally:
                        text_page.close()
                    method = "text"
                    if not text:
                        width, height = page.get_size()
                        bitmap = page.render(scale=min(2, 3000 / max(width, height)))
                        try:
                            image = bitmap.to_pil().convert("RGB")
                            text = recognize_image(np.asarray(image)[:, :, ::-1].copy())
                        finally:
                            bitmap.close()
                        method = "ocr"
                    if text.strip():
                        docs.append(Document(page_content=text, metadata={"page": number + 1, "extraction_method": method}))
                finally:
                    page.close()
        finally:
            pdf.close()
    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        from graphs.ocr import load_image
        docs = load_image(str(path))
    else:
        raise ValueError("Unsupported file format.")
    docs = [doc for doc in docs if doc.page_content.strip()]
    if not docs:
        raise ValueError("No text extracted. Check the file contents and OCR language.")
    if sum(len(doc.page_content) for doc in docs) > 2_000_000:
        raise ValueError("Extracted text exceeds 2 million characters. Split the file.")
    for doc in docs:
        doc.metadata.update(source=source, document_id=document_id)
    return docs
