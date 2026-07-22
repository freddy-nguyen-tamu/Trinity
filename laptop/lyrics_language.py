import re
import unicodedata

from lingua import Language, LanguageDetectorBuilder


LANGUAGE_BY_CODE = {
    "vi": Language.VIETNAMESE,
    "en": Language.ENGLISH,
    "ko": Language.KOREAN,
    "ja": Language.JAPANESE,
    "zh": Language.CHINESE,
    "th": Language.THAI,
    "id": Language.INDONESIAN,
    "ms": Language.MALAY,
    "tl": Language.TAGALOG,
    "fr": Language.FRENCH,
    "es": Language.SPANISH,
    "pt": Language.PORTUGUESE,
    "de": Language.GERMAN,
    "it": Language.ITALIAN,
    "ru": Language.RUSSIAN,
    "uk": Language.UKRAINIAN,
    "ar": Language.ARABIC,
    "hi": Language.HINDI,
}

LANGUAGE_ALIASES = {
    "fil": "tl",
    "iw": "he",
    "in": "id",
    "cmn": "zh",
    "yue": "zh",
}

LANGUAGE_DETECTOR = (
    LanguageDetectorBuilder
    .from_languages(*LANGUAGE_BY_CODE.values())
    .with_minimum_relative_distance(0.15)
    .build()
)

MIN_LANGUAGE_LETTERS = 120
MIN_LANGUAGE_CONFIDENCE = 0.78
MIN_LANGUAGE_MARGIN = 0.18


def normalize_language_code(value):
    value = str(value or "").strip().lower()
    if not value:
        return ""

    value = value.replace("_", "-")
    root = value.split("-", 1)[0]
    return LANGUAGE_ALIASES.get(root, root)


def clean_language_sample(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwww\.\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", text)
    text = re.sub(r"\[[^\]\r\n]{0,100}\]", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_text_language(text):
    sample = clean_language_sample(text)
    letters = sum(1 for char in sample if char.isalpha())

    if letters < MIN_LANGUAGE_LETTERS:
        return {
            "approved": False,
            "code": "",
            "confidence": 0.0,
            "margin": 0.0,
            "letters": letters,
            "reason": f"too_short:{letters}_letters",
        }

    sample = sample[:12000]

    confidence_values = LANGUAGE_DETECTOR.compute_language_confidence_values(sample)
    if not confidence_values:
        return {
            "approved": False,
            "code": "",
            "confidence": 0.0,
            "margin": 0.0,
            "letters": letters,
            "reason": "no_language_result",
        }

    top = confidence_values[0]
    second_value = confidence_values[1].value if len(confidence_values) > 1 else 0.0

    iso = top.language.iso_code_639_1
    code = iso.name.lower() if iso is not None else ""
    confidence = float(top.value)
    margin = confidence - float(second_value)

    approved = (
        code in LANGUAGE_BY_CODE
        and confidence >= MIN_LANGUAGE_CONFIDENCE
        and margin >= MIN_LANGUAGE_MARGIN
    )

    return {
        "approved": approved,
        "code": code,
        "confidence": confidence,
        "margin": margin,
        "letters": letters,
        "reason": (
            "approved"
            if approved
            else f"weak_language_result:confidence={confidence:.3f},margin={margin:.3f}"
        ),
    }


def strong_script_language_hint(text):
    # Return a language only when the script evidence is distinctive.
    text = unicodedata.normalize("NFC", str(text or ""))

    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    thai = len(re.findall(r"[\u0e00-\u0e7f]", text))
    hiragana_katakana = len(re.findall(r"[\u3040-\u30ff]", text))
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    arabic = len(re.findall(r"[\u0600-\u06ff]", text))
    devanagari = len(re.findall(r"[\u0900-\u097f]", text))
    vietnamese_distinctive = len(
        re.findall(
            r"[ăâđêôơưĂÂĐÊÔƠƯ"
            r"áàảãạấầẩẫậắằẳẵặ"
            r"éèẻẽẹếềểễệ"
            r"íìỉĩị"
            r"óòỏõọốồổỗộớờởỡợ"
            r"úùủũụứừửữự"
            r"ýỳỷỹỵ"
            r"ÁÀẢÃẠẤẦẨẪẬẮẰẲẴẶ"
            r"ÉÈẺẼẸẾỀỂỄỆ"
            r"ÍÌỈĨỊ"
            r"ÓÒỎÕỌỐỒỔỖỘỚỜỞỠỢ"
            r"ÚÙỦŨỤỨỪỬỮỰ"
            r"ÝỲỶỸỴ]",
            text,
        )
    )

    if hangul >= 3:
        return "ko"
    if thai >= 3:
        return "th"
    if hiragana_katakana >= 3:
        return "ja"
    if arabic >= 3:
        return "ar"
    if devanagari >= 3:
        return "hi"
    if vietnamese_distinctive >= 2:
        return "vi"
    if han >= 8 and hiragana_katakana == 0:
        return "zh"

    return ""


def infer_expected_language(title="", artist="", library_lyrics="", explicit_language=""):
    explicit = normalize_language_code(explicit_language)
    if explicit in LANGUAGE_BY_CODE:
        return explicit, "explicit_override"

    script_hint = strong_script_language_hint(f"{title} {artist}")
    if script_hint:
        return script_hint, "title_artist_script"

    if library_lyrics:
        detected = detect_text_language(library_lyrics)
        if detected["approved"]:
            return detected["code"], "verified_library_lyrics"

    return "", "unknown"


def validate_subtitle_language(candidate, subtitle_text, expected_language=""):
    if candidate.get("is_translated"):
        return {
            "approved": False,
            "reason": "translated_track_rejected",
            "detected": {},
        }

    track_language = normalize_language_code(
        candidate.get("source_language_code")
        or candidate.get("language_code")
    )

    if track_language not in LANGUAGE_BY_CODE:
        return {
            "approved": False,
            "reason": f"unsupported_or_missing_track_language:{track_language}",
            "detected": {},
        }

    detected = detect_text_language(subtitle_text)
    if not detected["approved"]:
        return {
            "approved": False,
            "reason": detected["reason"],
            "detected": detected,
        }

    if detected["code"] != track_language:
        return {
            "approved": False,
            "reason": (
                f"track_text_disagree:track={track_language},"
                f"text={detected['code']}"
            ),
            "detected": detected,
        }

    expected = normalize_language_code(expected_language)
    if expected and detected["code"] != expected:
        return {
            "approved": False,
            "reason": (
                f"wrong_expected_language:expected={expected},"
                f"actual={detected['code']}"
            ),
            "detected": detected,
        }

    return {
        "approved": True,
        "reason": "approved",
        "track_language": track_language,
        "detected": detected,
    }
