"""Image JD -> text via Robust Multi-Tier OCR Pipeline.

Tiers:
1. Image Preprocessing: Adaptive contrast enhancement, sharpening, and resolution normalization.
2. Local RapidOCR (if rapidocr_onnxruntime is installed).
3. Client-side OCR payload (Tesseract.js in browser).
4. Cloud OCR.Space Dual-Engine fallback (Engine 2 -> Engine 1).

Fails soft: returns None or best extracted string.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
import httpx
from app import config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _rapid_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    except Exception:
        return None


def preprocess_image_for_ocr(image_bytes: bytes) -> bytes:
    """Enhance image contrast and sharpness to fix screenshot OCR detection issues."""
    if not image_bytes:
        return image_bytes

    try:
        from PIL import Image, ImageEnhance

        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # 1. Resolution normalization
        w, h = img.size
        max_dim = max(w, h)
        if max_dim > 1800:
            scale = 1800.0 / max_dim
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        elif max_dim < 600 and max_dim > 0:
            scale = 1000.0 / max_dim
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        # 2. Contrast & Sharpness boost for clear character contours
        enhancer_c = ImageEnhance.Contrast(img)
        img = enhancer_c.enhance(1.35)

        enhancer_s = ImageEnhance.Sharpness(img)
        img = enhancer_s.enhance(1.25)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("Image preprocessing notice: %s", exc)
        return image_bytes


def _ocr_space_extract(image_bytes: bytes) -> str | None:
    """Extract text via OCR.Space free REST API with Engine 2 and Engine 1 fallback."""
    if not image_bytes:
        return None

    upload_bytes = preprocess_image_for_ocr(image_bytes)
    # Only the user's own configured key is ever used. There is intentionally
    # no "helloworld" demo-key fallback: screenshots must never be uploaded
    # to a third-party demo endpoint. Without a key, OCR returns None and the
    # intake flow asks the user to paste the JD text instead.
    api_keys = [config.OCR_SPACE_API_KEY]

    for key in api_keys:
        if not key:
            continue
        # Attempt Engine 2 first (best for screenshots), then Engine 1
        for engine_choice in ("2", "1"):
            try:
                r = httpx.post(
                    "https://api.ocr.space/parse/image",
                    files={"file": ("screenshot.jpg", upload_bytes, "image/jpeg")},
                    data={
                        "apikey": key,
                        "OCREngine": engine_choice,
                        "detectOrientation": "true",
                        "scale": "true",
                    },
                    timeout=14.0,
                )
                if r.status_code == 200:
                    data = r.json()
                    results = data.get("ParsedResults") or []
                    text = "\n".join((p.get("ParsedText") or "").strip() for p in results).strip()
                    if text and len(text) > 25:
                        return text
            except Exception as exc:
                logger.debug("OCR.Space notice (key %s, engine %s): %s", key[:4], engine_choice, exc)
                continue
    return None


def image_to_text(image_bytes: bytes | None, client_text: str = "") -> str | None:
    """Multi-tier OCR extraction: Local RapidOCR -> Client Browser OCR -> OCR.Space Dual-Engine."""
    clean_client = (client_text or "").strip()

    # 1. Try local RapidOCR if available in environment
    if image_bytes:
        engine = _rapid_engine()
        if engine is not None:
            try:
                processed_bytes = preprocess_image_for_ocr(image_bytes)
                result, _ = engine(processed_bytes)
                if result:
                    lines = [line[1] for line in result if len(line) > 1 and line[1].strip()]
                    local_text = "\n".join(lines).strip()
                    if len(local_text) > 25:
                        return local_text
            except Exception as exc:
                logger.debug("Local RapidOCR notice: %s", exc)

    # 2. If client-side OCR was already captured in the browser (e.g. via Tesseract.js), use it!
    if clean_client and len(clean_client) > 30:
        return clean_client

    # 3. Serverless cloud fallback: OCR.Space Dual-Engine API over HTTP
    if image_bytes:
        cloud_text = _ocr_space_extract(image_bytes)
        if cloud_text:
            return cloud_text

    # 4. Return whatever client text we have, or None
    return clean_client if clean_client else None
