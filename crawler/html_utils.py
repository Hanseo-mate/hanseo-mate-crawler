import base64
import logging
import os
import re
import uuid
from datetime import date, datetime
from typing import Optional

from bs4 import BeautifulSoup, Tag

from .config import ACTIVE_HTML_PARSER, IMAGE_BASE_URL, IMAGE_SAVE_DIRECTORY


ORIGIN_NOTICE_ID_PATTERN = re.compile(r"goView\('(?:[^']*)','([^']+)'")
DATA_IMAGE_PATTERN = re.compile(
    r"^data:image/(?P<extension>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
    flags=re.IGNORECASE | re.DOTALL,
)


def parse_iso_date(raw_value: str) -> date:
    return datetime.strptime(raw_value.strip(), "%Y-%m-%d").date()


def normalize_text(raw_text: str) -> str:
    return " ".join(raw_text.split())


def extract_onclick_args(onclick_value: str) -> list[str]:
    return re.findall(r"'([^']*)'", onclick_value)


def extract_origin_notice_id(onclick_value: str) -> str:
    match = ORIGIN_NOTICE_ID_PATTERN.search(onclick_value)
    if not match:
        raise ValueError(f"origin_notice_id 추출 실패: {onclick_value}")
    return match.group(1)


def is_hot_notice(number_cell: Tag) -> bool:
    hot_icon = number_cell.find("img", alt=re.compile(r"HOT"))
    return hot_icon is not None


def extract_notice_title(anchor: Tag) -> str:
    title_from_attribute = anchor.get("title", "").strip()
    if title_from_attribute:
        return normalize_text(title_from_attribute)
    return normalize_text(anchor.get_text(" ", strip=True))


def find_tag_end(html_text: str, lt_index: int) -> int:
    quote_char: Optional[str] = None
    cursor = lt_index + 1
    text_len = len(html_text)

    while cursor < text_len:
        char = html_text[cursor]

        if quote_char is None and (char == '"' or char == "'"):
            quote_char = char
        elif quote_char is not None and char == quote_char:
            quote_char = None
        elif quote_char is None and char == ">":
            return cursor

        cursor += 1

    return -1


def extract_tag_name(raw_tag: str) -> tuple[str, bool, bool]:
    stripped = raw_tag.strip()
    if not stripped:
        return "", False, False

    is_closing = stripped.startswith("/")
    if is_closing:
        stripped = stripped[1:].lstrip()

    if not stripped:
        return "", is_closing, False

    if stripped[0] in ("!", "?"):
        return "", is_closing, False

    name_chars: list[str] = []
    for char in stripped:
        if char.isalnum() or char in (":", "-", "_"):
            name_chars.append(char)
            continue
        break

    if not name_chars:
        return "", is_closing, False

    is_self_closing = stripped.rstrip().endswith("/")
    return "".join(name_chars).lower(), is_closing, is_self_closing


def extract_view_box_html_raw(html_text: str) -> Optional[str]:
    view_box_match = re.search(
        r'<div[^>]*class=["\'][^"\']*viewBox[^"\']*["\'][^>]*>',
        html_text,
        flags=re.IGNORECASE,
    )
    if view_box_match is None:
        return None

    cursor = view_box_match.end()
    depth = 1
    text_len = len(html_text)

    while cursor < text_len:
        lt_index = html_text.find("<", cursor)
        if lt_index == -1:
            return None

        if html_text.startswith("<!--", lt_index):
            comment_end = html_text.find("-->", lt_index + 4)
            if comment_end == -1:
                return None
            cursor = comment_end + 3
            continue

        gt_index = find_tag_end(html_text, lt_index)
        if gt_index == -1:
            return None

        raw_tag = html_text[lt_index + 1 : gt_index]
        tag_name, is_closing, is_self_closing = extract_tag_name(raw_tag)

        if tag_name in ("script", "style", "noscript", "textarea") and not is_closing:
            close_tag = f"</{tag_name}>"
            close_index = html_text.lower().find(close_tag, gt_index + 1)
            if close_index == -1:
                return None
            cursor = close_index + len(close_tag)
            continue

        if tag_name == "div" and not is_closing and not is_self_closing:
            depth += 1
        elif tag_name == "div" and is_closing:
            depth -= 1
            if depth == 0:
                return html_text[view_box_match.end() : lt_index]

        cursor = gt_index + 1

    return None


def parse_metadata_value(info_items: list[Tag], label: str) -> Optional[str]:
    for item in info_items:
        strong = item.find("strong")
        if strong is None:
            continue

        normalized_label = normalize_text(strong.get_text(strip=True))
        if normalized_label != label:
            continue

        strong.extract()
        return normalize_text(item.get_text(" ", strip=True))
    return None


def parse_attachments(article: Tag) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []

    for anchor in article.select("div.fieldBox dd a[href]"):
        href = anchor.get("href", "").strip()
        if not href or "synapView" in href:
            continue

        preview_icon = anchor.find(
            "img",
            src=lambda value: value is not None and "/images/board/btn_preview.gif" in value,
        )
        if preview_icon is not None:
            continue

        file_name = normalize_text(anchor.get_text(" ", strip=True))
        if not file_name:
            continue

        attachments.append(
            {
                "file_name": file_name,
                "file_url": href,
            }
        )

    return attachments


def normalize_image_extension(raw_extension: str) -> str:
    normalized = raw_extension.strip().lower()
    if normalized == "jpeg":
        return "jpg"
    if "+" in normalized:
        normalized = normalized.split("+", 1)[0]

    sanitized = re.sub(r"[^a-z0-9]", "", normalized)
    return sanitized or "bin"


def build_image_public_url(file_name: str) -> str:
    return f"{IMAGE_BASE_URL.rstrip('/')}/{file_name}"


def process_embedded_images(content_html: str) -> str:
    soup = BeautifulSoup(content_html, ACTIVE_HTML_PARSER)

    for image_tag in soup.find_all("img"):
        src = image_tag.get("src")
        if not src or not src.startswith("data:image/"):
            continue

        match = DATA_IMAGE_PATTERN.match(src)
        if match is None:
            logging.warning("임베디드 이미지 형식 파싱 실패: src_prefix=%s", src[:64])
            continue

        extension = normalize_image_extension(match.group("extension"))
        encoded_data = re.sub(r"\s+", "", match.group("data"))
        file_name = f"{uuid.uuid4().hex}.{extension}"
        file_path = os.path.join(IMAGE_SAVE_DIRECTORY, file_name)

        try:
            image_binary = base64.b64decode(encoded_data, validate=True)
            with open(file_path, "wb") as image_file:
                image_file.write(image_binary)
            image_tag["src"] = build_image_public_url(file_name)
            logging.info("임베디드 이미지 저장 완료: %s", file_path)
        except Exception as exc:
            logging.warning(
                "임베디드 이미지 처리 실패, 원본 src 유지: file_name=%s, error=%s",
                file_name,
                exc,
            )

    return soup.decode_contents().strip()
