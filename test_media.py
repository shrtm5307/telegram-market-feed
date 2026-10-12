import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_media import extract_file_text


class MediaTextExtractionTests(unittest.TestCase):
    def test_extracts_text_from_digital_pdf(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Telegram report: HBM demand continues.")
            document.save(path)
            document.close()

            text = extract_file_text(path, "pdf")

        self.assertIn("HBM demand continues", text)

    def test_ocr_for_image_uses_korean_and_english_config(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            Image.new("RGB", (100, 50), "white").save(path)
            with patch("pytesseract.image_to_string", return_value="반도체 market"):
                text = extract_file_text(path, "image")

        self.assertIn("반도체 market", text)

    def test_unsupported_kind_returns_empty_text(self):
        self.assertEqual(extract_file_text("unused.bin", "video"), "")


if __name__ == "__main__":
    unittest.main()
