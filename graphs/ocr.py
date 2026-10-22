"""Lazy serialized OCR, shared by image and scanned-PDF loaders."""
import os
from pathlib import Path
from threading import Lock
from langchain_core.documents import Document

_ocr = None
_lock = Lock()


def recognize_image(image) -> str:
    global _ocr
    with _lock:
        if _ocr is None:
            from paddleocr import PaddleOCR
            _ocr = PaddleOCR(
                lang=os.getenv("RAG_OCR_LANGUAGE", "en"), ocr_version="PP-OCRv5",
                device="cpu", use_doc_orientation_classify=False,
                use_doc_unwarping=False, use_textline_orientation=False,
            )
        return "\n".join(text for result in _ocr.predict(input=image)
                         for text in result["rec_texts"]).strip()


def load_image(path: str) -> list[Document]:
    image_path = Path(path)
    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path.name}")
    content = recognize_image(str(image_path))
    if not content:
        raise ValueError(f"No text recognized: {image_path.name}")
    return [Document(page_content=content, metadata={
        "source": image_path.name, "extraction_method": "ocr",
    })]
