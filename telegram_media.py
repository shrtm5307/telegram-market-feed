"""Extract text from Telegram image and PDF attachments without retaining files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "20"))
MAX_MEDIA_TEXT_CHARS = int(os.getenv("MAX_MEDIA_TEXT_CHARS", "12000"))
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "kor+eng")


def supported_media(message: Any) -> tuple[str | None, str | None, str]:
    """Return (kind, mime type, extension) for supported Telegram attachments."""
    if getattr(message, "photo", None):
        return "image", "image/jpeg", ".jpg"

    document = getattr(message, "document", None)
    mime_type = (getattr(document, "mime_type", "") or "").lower()
    if mime_type == "application/pdf":
        return "pdf", mime_type, ".pdf"
    if mime_type.startswith("image/"):
        return "image", mime_type, ".img"
    return None, None, ""


def _ocr_image(image: Any) -> str:
    import pytesseract
    from PIL import Image

    if not isinstance(image, Image.Image):
        image = Image.frombytes("RGB", (image.width, image.height), image.samples)
    image = image.convert("RGB")
    image.thumbnail((3000, 3000))
    return pytesseract.image_to_string(image, lang=OCR_LANGUAGES, config="--psm 6")


def extract_file_text(path: str | Path, kind: str) -> str:
    """Extract embedded PDF text, OCR scanned PDF pages, or OCR an image."""
    path = Path(path)
    if kind == "image":
        from PIL import Image

        with Image.open(path) as image:
            text = _ocr_image(image.copy())
        return text.strip()[:MAX_MEDIA_TEXT_CHARS]

    if kind != "pdf":
        return ""

    import pymupdf

    parts: list[str] = []
    with pymupdf.open(path) as document:
        for page_number in range(min(document.page_count, MAX_PDF_PAGES)):
            page = document.load_page(page_number)
            page_text = page.get_text("text").strip()
            if len(page_text) < 40:
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                from PIL import Image

                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                page_text = _ocr_image(image).strip()
            if page_text:
                parts.append(f"[페이지 {page_number + 1}]\n{page_text}")
            if sum(map(len, parts)) >= MAX_MEDIA_TEXT_CHARS:
                break
    return "\n\n".join(parts)[:MAX_MEDIA_TEXT_CHARS]


async def download_and_extract(message: Any) -> tuple[str | None, str]:
    """Download supported media to a temporary file, extract text, then discard it."""
    kind, mime_type, suffix = supported_media(message)
    if not kind:
        return None, ""

    document = getattr(message, "document", None)
    declared_size = getattr(document, "size", None)
    if declared_size and declared_size > MAX_ATTACHMENT_BYTES:
        return kind, "[첨부파일 크기 제한으로 텍스트 추출 생략]"

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-media-") as temp_dir:
            path = Path(temp_dir) / f"attachment{suffix}"
            downloaded = await message.download_media(file=str(path))
            if not downloaded or not path.is_file():
                return kind, "[첨부파일 다운로드 실패]"
            if path.stat().st_size > MAX_ATTACHMENT_BYTES:
                return kind, "[첨부파일 크기 제한으로 텍스트 추출 생략]"
            text = extract_file_text(path, kind)
            if text.strip():
                label = "이미지 OCR" if kind == "image" else "PDF 텍스트 추출"
                return kind, f"[{label}]\n{text.strip()}"
    except Exception:
        # Media failures should not prevent the rest of the channel feed from updating.
        pass

    label = "이미지 OCR" if kind == "image" else "PDF 텍스트 추출"
    return kind, f"[{label}: 추출된 글자 없음 또는 처리 실패]"
