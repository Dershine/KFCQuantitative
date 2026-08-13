from __future__ import annotations

from io import BytesIO

import httpx
from pypdf import PdfReader


class DocumentLoader:
    """Downloads a provider-supplied document URL; it does not discover or crawl pages."""

    def __init__(self, max_bytes: int, timeout_seconds: float = 20.0):
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    def load_text(self, url: str) -> str:
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "KFCQuant/0.1 research-client"})
            response.raise_for_status()
            content = response.content
            if len(content) > self.max_bytes:
                raise ValueError(f"document exceeds {self.max_bytes} bytes")
            content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            reader = PdfReader(BytesIO(content))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        response.encoding = response.encoding or "utf-8"
        return response.text.strip()
