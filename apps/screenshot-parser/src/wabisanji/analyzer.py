"""Screenshot analysis: OCR and translation of Japanese game screenshots.

Fully local — no API keys. manga-ocr extracts dialogue text; a vision model
served by Ollama (https://ollama.com) reads the full screen (dialogue, menus,
HUD) and provides readings, translation, and vocabulary. Three analysis paths:

- analyze_vision:    Ollama vision model over the image alone
- analyze_manga_ocr: local manga-ocr — accurate raw dialogue text, no readings
- analyze_hybrid:    both — manga-ocr's text is passed to the vision model as
                     a hint to anchor the dialogue; the model verifies it
                     against the image, so OCR hallucinations get discarded
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import OllamaConfig

logger = logging.getLogger(__name__)

# Lazy-loaded manga-ocr model (~1GB download on first use)
_manga_ocr_model = None


class OllamaError(Exception):
    """A failed call to the local Ollama server."""


def _error_result(image_path: str, ocr_method: str, error: str, **extra: Any) -> dict[str, Any]:
    return {
        "file_path": image_path,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "extracted_text": [],
        "translation": "",
        "context": "",
        "vocabulary": [],
        "ocr_method": ocr_method,
        "error": error,
        **extra,
    }


def get_manga_ocr_model():
    """Lazy-load and cache the manga-ocr model.

    Raises ImportError if the optional `ocr` extra is not installed.
    """
    global _manga_ocr_model
    if _manga_ocr_model is None:
        from manga_ocr import MangaOcr  # deferred: pulls in torch

        logger.info("Initializing manga-ocr model (first run downloads ~1GB)")
        _manga_ocr_model = MangaOcr()
    return _manga_ocr_model


def encode_image_to_base64(image_path: str) -> str:
    """Encode an image file to base64 for the Ollama images field."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError(f"Unsupported image format: {path.suffix}. Supported: PNG, JPG, JPEG")

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def preprocess_image_for_ocr(image_path: str):
    """Grayscale + CLAHE contrast + 2x bicubic upscale, for manga-ocr accuracy."""
    import cv2
    import numpy as np
    from PIL import Image

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    try:
        img_array = np.array(Image.open(path))
        if len(img_array.shape) == 3:
            gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        else:
            gray = img_array

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)

        height, width = enhanced.shape
        upscaled = cv2.resize(enhanced, (width * 2, height * 2), interpolation=cv2.INTER_CUBIC)
        return Image.fromarray(upscaled)
    except Exception as e:
        raise ValueError(f"Failed to preprocess image {image_path}: {e}")


_VOCAB_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "reading": {"type": "string"},
        "meaning": {"type": "string"},
        "part_of_speech": {"type": "string"},
        "jlpt_level": {"type": ["string", "null"]},
        "category": {"type": "string", "enum": ["speech", "menu", "pokemon"]},
    },
    "required": ["word", "reading", "meaning", "part_of_speech", "jlpt_level", "category"],
}

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "extracted_text": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "reading": {"type": "string"},
                },
                "required": ["text", "reading"],
            },
        },
        "translation": {"type": "string"},
        "context": {"type": "string"},
        "vocabulary": {"type": "array", "items": _VOCAB_ITEM_SCHEMA},
    },
    "required": ["extracted_text", "translation", "context", "vocabulary"],
}

_READING_RULES = """Rules:
- All readings MUST be written in hiragana (e.g. たべる), NEVER in romaji.
- Vocabulary: content words only (nouns, verbs, adjectives, adverbs,
  expressions) in dictionary form. Skip bare particles (は, が, を...) and
  grammar-only fragments.
- Categorize each vocabulary word: "pokemon" for Pokémon species and
  character names, "menu" for UI/menu/battle-command/HUD terms, "speech"
  for everything from dialogue.
- For proper nouns (Pokémon, characters, places): give the official English
  name ONLY if you are certain of it; otherwise romanize the Japanese name
  (e.g. unknown ビードル → "Biidoru (Pokémon)"). Never guess a similar name.
- JLPT level is N5/N4/N3/N2/N1, or null if unsure.
- Be precise with readings — they will be used on flashcards."""

VISION_PROMPT = f"""This is a screenshot from a Japanese video game.

1. Extract ALL visible Japanese text (hiragana, katakana, kanji) — dialogue,
   menu options, button labels, HUD text — one entry per text segment, each
   with its full reading written in hiragana characters
   (e.g. 主人公 → しゅじんこう). Latin letters are forbidden in readings.
2. Translate the complete text into natural English.
3. Briefly describe the game scene (context).
4. List the vocabulary words a learner should study: word, hiragana reading,
   English meaning, part of speech, and JLPT level. Menu verbs like たたかう
   or にげる count — they are exactly what a beginner needs.

{_READING_RULES}"""

OCR_HINT = """

A dedicated OCR system read the main dialogue text as:

{hint}

Verify it against the image: correct it where it disagrees with what is
visible, and ignore it entirely if that text does not appear on screen.
Still extract menu options, HUD labels, and any other text the OCR missed.
Never mention the OCR text or your verification in any output field — the
translation field must contain only the English translation of the text
that is actually visible on screen."""


_JLPT_LEVELS = {"N5", "N4", "N3", "N2", "N1"}
_PARTICLES = {"は", "が", "を", "に", "の", "と", "も", "で", "へ", "や", "か", "ね", "よ"}

# Official kana -> English Pokémon species names (generated from PokeAPI data).
# LLMs pattern-match names they don't actually know (ビードル is Weedle, not
# Beedrill), so species meanings are overridden deterministically.
_POKEMON_NAMES: dict[str, str] | None = None


def _pokemon_names() -> dict[str, str]:
    global _POKEMON_NAMES
    if _POKEMON_NAMES is None:
        path = Path(__file__).parent / "data" / "pokemon_names.json"
        _POKEMON_NAMES = json.loads(path.read_text(encoding="utf-8"))
    return _POKEMON_NAMES


def _has_kana(s: str) -> bool:
    return any("぀" <= c <= "ヿ" for c in s)


def _is_kana(s: str) -> bool:
    return bool(s) and all("぀" <= c <= "ヿ" or c in "ー・" for c in s)


def _fix_reading(reading: str, word: str) -> str:
    """Models sometimes emit romaji despite the prompt; fall back to the word
    itself when it's already pure kana, otherwise drop the bad reading."""
    if _has_kana(reading):
        return reading
    return word if _is_kana(word) else ""


def _clean_vocab(items: list[dict], pokemon: bool = True) -> list[dict]:
    """Normalize model vocabulary output for card quality.

    `pokemon` turns on the species lookup; other games skip it so a word that
    happens to match a Pokémon name (ゴースト) isn't relabelled.
    """
    cleaned = []
    for item in items:
        word = (item.get("word") or "").strip().strip("。、．，…")
        if not word or word in _PARTICLES:
            continue
        jlpt = (item.get("jlpt_level") or "").strip().upper()
        reading = (item.get("reading") or "").strip().strip("。、．，…")
        category = (item.get("category") or "").strip().lower()
        if category not in ("speech", "menu", "pokemon"):
            category = "speech"
        meaning = (item.get("meaning") or "").strip()
        species = _pokemon_names().get(word.strip("♂♀")) if pokemon else None
        if species:
            meaning, category = f"{species} (Pokémon)", "pokemon"
        cleaned.append({
            "word": word,
            "reading": _fix_reading(reading, word),
            "meaning": meaning,
            "part_of_speech": (item.get("part_of_speech") or "").strip(),
            "jlpt_level": jlpt if jlpt in _JLPT_LEVELS else None,
            "category": category,
            "form": (item.get("form") or "").strip(),
        })
    return cleaned


def _ollama_chat(
    prompt: str,
    *,
    model: str,
    ollama: OllamaConfig,
    schema: dict,
    images: list[str] | None = None,
) -> dict[str, Any]:
    """Call the local Ollama server and return the schema-constrained JSON reply."""
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = images

    payload = {
        "model": model,
        "messages": [message],
        "stream": False,
        "format": schema,
        # Thinking models otherwise reason forever before the JSON
        "think": False,
        "options": {"temperature": 0, "num_ctx": 8192},
    }

    url = ollama.url.rstrip("/") + "/api/chat"
    try:
        response = requests.post(url, json=payload, timeout=ollama.timeout)
        if response.status_code == 400 and "think" in response.text.lower():
            # Model doesn't accept the think parameter; retry without it
            payload.pop("think")
            response = requests.post(url, json=payload, timeout=ollama.timeout)
    except requests.ConnectionError as e:
        raise OllamaError(
            f"Cannot reach Ollama at {ollama.url}. Is it running? "
            "Install it from https://ollama.com, then pull a model "
            f"(e.g. `ollama pull {model}`)."
        ) from e
    except requests.Timeout as e:
        raise OllamaError(
            f"Ollama timed out after {ollama.timeout}s (model={model}). "
            "First calls are slow while the model loads; try again or raise "
            "ollama.timeout in config.yaml."
        ) from e

    if response.status_code == 404:
        raise OllamaError(f"Model '{model}' not found. Run: ollama pull {model}")
    if response.status_code != 200:
        raise OllamaError(f"Ollama returned HTTP {response.status_code}: {response.text[:200]}")

    # Some model/template combinations (e.g. qwen3-vl on image requests) put
    # the constrained output in `thinking` and leave `content` empty
    reply = response.json().get("message", {})
    content = reply.get("content") or reply.get("thinking") or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise OllamaError(f"Model returned invalid JSON ({e}): {content[:200]!r}")


def _analyze_image(
    image_path: str,
    ollama: OllamaConfig,
    hint: str | None = None,
    method: str = "vision",
) -> dict[str, Any]:
    """Run the vision model over the full screenshot, optionally anchored by
    OCR'd dialogue text."""
    try:
        image_base64 = encode_image_to_base64(image_path)
    except (FileNotFoundError, ValueError) as e:
        return _error_result(image_path, method, str(e))

    prompt = VISION_PROMPT + (OCR_HINT.format(hint=hint) if hint else "")
    try:
        logger.info("Calling Ollama vision (model=%s) for %s", ollama.model, image_path)
        data = _ollama_chat(
            prompt,
            model=ollama.model,
            ollama=ollama,
            schema=VISION_SCHEMA,
            images=[image_base64],
        )
    except OllamaError as e:
        return _error_result(image_path, method, str(e))

    segments = [
        {
            "text": seg.get("text", "").strip(),
            "reading": seg.get("reading"),
            "confidence": None,  # local vision models can't self-score reliably
            "position": None,
        }
        for seg in data.get("extracted_text", [])
        if seg.get("text", "").strip()
    ]

    return {
        "file_path": image_path,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "extracted_text": segments,
        "translation": data.get("translation", ""),
        "context": data.get("context", ""),
        "vocabulary": _clean_vocab(data.get("vocabulary", [])),
        "ocr_method": method,
    }


def analyze_vision(image_path: str, ollama: OllamaConfig | None = None) -> dict[str, Any]:
    """Analyze a screenshot with the Ollama vision model alone."""
    return _analyze_image(image_path, ollama or OllamaConfig(), method="vision")


def analyze_manga_ocr(image_path: str) -> dict[str, Any]:
    """Extract Japanese text with local manga-ocr. No readings or positions."""
    if not os.path.exists(image_path):
        return _error_result(image_path, "manga-ocr", f"Image not found: {image_path}")

    try:
        preprocessed = preprocess_image_for_ocr(image_path)
    except (FileNotFoundError, ValueError) as e:
        return _error_result(image_path, "manga-ocr", f"Image preprocessing failed: {e}")

    try:
        mocr = get_manga_ocr_model()
    except ImportError:
        return _error_result(
            image_path, "manga-ocr",
            "manga-ocr not installed. Install with: pip install wabisanji-parser[ocr]",
        )
    except Exception as e:
        return _error_result(image_path, "manga-ocr", f"Failed to initialize manga-ocr: {e}")

    try:
        raw_text = mocr(preprocessed)
    except Exception as e:
        return _error_result(image_path, "manga-ocr", f"OCR extraction failed: {e}")

    segments = []
    if raw_text and raw_text.strip():
        segments.append({
            "text": raw_text.strip(),
            "reading": None,       # manga-ocr doesn't provide readings
            "confidence": 0.95,    # manga-ocr is highly accurate for game/manga text
            "position": None,      # no bounding boxes
        })

    return {
        "file_path": image_path,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "extracted_text": segments,
        "translation": "",
        "context": "",
        "vocabulary": [],
        "ocr_method": "manga-ocr",
    }


def analyze_hybrid(image_path: str, ollama: OllamaConfig | None = None) -> dict[str, Any]:
    """manga-ocr dialogue text as a hint + the vision model over the full image.

    The vision model reads everything on screen (dialogue, menus, HUD);
    manga-ocr's text anchors the dialogue when present, and the model discards
    it when it doesn't match the image (OCR hallucination on menu screens).
    Falls back to plain vision analysis if manga-ocr isn't installed.
    """
    ollama = ollama or OllamaConfig()
    if not os.path.exists(image_path):
        return _error_result(image_path, "hybrid", f"Image not found: {image_path}")

    manga_result = analyze_manga_ocr(image_path)
    manga_error = manga_result.get("error")
    if manga_error:
        logger.info("manga-ocr unavailable (%s); analyzing without OCR hint", manga_error)
    hint = "" if manga_error else "\n".join(
        seg["text"] for seg in manga_result["extracted_text"]
    ).strip()

    result = _analyze_image(image_path, ollama, hint=hint or None, method="hybrid")
    if manga_error and not result.get("error"):
        result["partial_errors"] = [f"manga-ocr: {manga_error}"]
    return result


def analyze(
    image_path: str,
    method: str = "hybrid",
    ollama: OllamaConfig | None = None,
) -> dict[str, Any]:
    """Dispatch to the configured analysis method."""
    if method == "vision":
        return analyze_vision(image_path, ollama=ollama)
    if method == "manga-ocr":
        return analyze_manga_ocr(image_path)
    if method == "hybrid":
        return analyze_hybrid(image_path, ollama=ollama)
    raise ValueError(f"Unknown analysis method: {method!r} (expected vision | manga-ocr | hybrid)")
