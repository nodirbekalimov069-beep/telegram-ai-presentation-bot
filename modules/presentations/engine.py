"""Python-to-Node presentation contract, renderer adapter and PPTX QA."""

import asyncio
import copy
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

from modules.templates.catalog import select_template


class PresentationRenderError(RuntimeError):
    pass


# PptxGenJS currently pulls an image-size release whose ICNS/JXL/HEIF decoders have
# upstream-unfixed CPU denial-of-service advisories. Reject those formats before Node.
_HEIF_BRANDS = {
    b"avif",
    b"avis",
    b"heic",
    b"heis",
    b"heix",
    b"heim",
    b"hevc",
    b"hevx",
    b"mif1",
    b"msf1",
}


def _is_vulnerable_image_format(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith((b"icns", b"\xff\x0a")) or header[4:8] == b"JXL ":
        return True
    return len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in _HEIF_BRANDS


@dataclass(frozen=True, slots=True)
class PptxQAReport:
    slide_count: int
    issues: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


def _rgb(value: str) -> str:
    return value.removeprefix("#").upper()


def _source(value: str) -> dict[str, str] | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return {"label": parsed.netloc, "url": value}


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


def _secure_image_paths(payload: dict[str, Any], *, render_root: Path) -> dict[str, Any]:
    safe_payload = copy.deepcopy(payload)
    root = render_root.resolve()
    slides = safe_payload.get("slides")
    if not isinstance(slides, list):
        raise PresentationRenderError("slides must be a list")
    for slide in slides:
        if not isinstance(slide, dict):
            raise PresentationRenderError("each slide must be an object")
        if "image_paths" not in slide:
            continue
        image_paths = slide["image_paths"]
        if not isinstance(image_paths, list):
            raise PresentationRenderError("image_paths must be a list")
        checked: list[str] = []
        for raw_path in image_paths:
            if not isinstance(raw_path, str):
                raise PresentationRenderError("each image path must be a string")
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                raise PresentationRenderError("image path is outside render workdir")
            if not resolved.is_file():
                raise PresentationRenderError("image path is not a regular file")
            if _is_vulnerable_image_format(resolved):
                raise PresentationRenderError("unsupported image format")
            checked.append(str(resolved))
        slide["image_paths"] = checked
    return safe_payload


def build_engine_payload(
    *,
    title: str,
    language: str,
    direction: str,
    design_style: str,
    plan_code: str,
    outline: dict[str, Any],
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template = select_template(
        direction=direction,
        requested_style=design_style,
        plan_code=plan_code,
    )
    slides: list[dict[str, Any]] = []
    content_slides = content.get("slides", []) if isinstance(content, dict) else []
    outline_slides = outline.get("slides", [])
    for index, item in enumerate(outline_slides):
        expanded = content_slides[index] if index < len(content_slides) else {}
        if not isinstance(expanded, dict):
            expanded = {}
        sources = [result for value in item.get("sources", []) if (result := _source(str(value)))]
        key_points = [
            str(value).strip() for value in item.get("key_points", []) if str(value).strip()
        ]
        body_blocks = expanded.get("body_blocks", [])
        expanded_points = [
            f"{str(block.get('heading', '')).strip()} — {str(block.get('body', '')).strip()}"
            for block in body_blocks
            if isinstance(block, dict)
            and str(block.get("heading", "")).strip()
            and str(block.get("body", "")).strip()
        ]
        inferred_role = (
            "cover"
            if index == 0
            else "conclusion"
            if index == len(outline_slides) - 1
            else "content"
        )
        slide: dict[str, Any] = {
            "role": str(item.get("role") or inferred_role),
            "title": str(expanded.get("title") or item.get("title", "Untitled"))[:180],
            "bullets": (expanded_points or key_points)[:8],
            "sources": sources[:10],
        }
        if expanded_points:
            slide["content_blocks"] = [
                {
                    "heading": str(block["heading"]).strip()[:90],
                    "body": str(block["body"]).strip()[:420],
                }
                for block in body_blocks
                if isinstance(block, dict)
                and str(block.get("heading", "")).strip()
                and str(block.get("body", "")).strip()
            ][:4]
        expanded_notes = str(expanded.get("speaker_notes", "")).strip()
        if expanded_notes:
            slide["speaker_notes"] = expanded_notes[:10_000]
        elif plan_code.casefold() in {"premium", "premium_plus"}:
            purpose = str(item.get("purpose", "")).strip()
            notes = [purpose, *key_points]
            slide["speaker_notes"] = "\n".join(value for value in notes if value)[:10_000]
        # A visual hint without structured numbers must never become a fabricated chart.
        # Charts/tables/layouts are added only when the validated AI contract contains data.
        structured_chart = item.get("chart")
        if isinstance(structured_chart, dict):
            slide["chart"] = _without_none(structured_chart)
        structured_table = item.get("table")
        if isinstance(structured_table, dict):
            slide["table"] = _without_none(structured_table)
        structured_layout = item.get("special_layout")
        if isinstance(structured_layout, dict):
            slide["layout"] = _without_none(structured_layout)
        slides.append(slide)

    if not slides:
        raise ValueError("outline must contain at least one slide")
    palette = template.palette
    return {
        "title": title[:140],
        "language": language,
        "theme": {
            "palette": {
                "background": _rgb(palette["background"]),
                "foreground": _rgb(palette["text"]),
                "primary": _rgb(palette["accent"]),
                "secondary": _rgb(palette["text"]),
                "accent": _rgb(palette["accent"]),
            },
            "typography": {
                "heading": template.heading_font,
                "body": template.body_font,
            },
        },
        "plan_code": plan_code.upper(),
        "slides": slides,
    }


class NodePresentationRenderer:
    def __init__(
        self,
        engine_directory: Path,
        *,
        node_binary: str = "node",
        timeout_seconds: float = 180,
    ) -> None:
        self._directory = engine_directory.expanduser().resolve()
        self._node_binary = node_binary
        self._timeout = timeout_seconds
        if not (self._directory / "render.mjs").is_file():
            raise FileNotFoundError(self._directory / "render.mjs")

    async def render(self, payload: dict[str, Any], output: Path) -> Path:
        output_path = output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = _secure_image_paths(payload, render_root=output_path.parent)
        descriptor, input_name = tempfile.mkstemp(
            prefix="presentation-",
            suffix=".json",
            dir=output_path.parent,
        )
        input_path = Path(input_name)
        try:
            with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(safe_payload, handle, ensure_ascii=False)
            process = await asyncio.create_subprocess_exec(
                self._node_binary,
                str(self._directory / "render.mjs"),
                str(input_path),
                str(output_path),
                cwd=self._directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._timeout
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise PresentationRenderError("Node renderer timed out") from exc
            if process.returncode != 0:
                detail = stderr.decode(errors="replace")[-2000:]
                raise PresentationRenderError(f"Node renderer failed: {detail}")
            if not output_path.is_file() or output_path.stat().st_size == 0:
                detail = stdout.decode(errors="replace")[-500:]
                raise PresentationRenderError(f"renderer did not create output: {detail}")
            return output_path
        finally:
            input_path.unlink(missing_ok=True)


def validate_pptx(path: Path, *, expected_slides: int) -> PptxQAReport:
    issues: list[str] = []
    slide_names: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                issues.append("PPTX ZIP contains a corrupt member")
            slide_names = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            if len(slide_names) != expected_slides:
                issues.append(f"expected {expected_slides} slides, found {len(slide_names)}")
            for name in slide_names:
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError:
                    issues.append(f"invalid XML in {name}")
                    continue
                texts = [node.text for node in root.iter() if node.tag.endswith("}t") and node.text]
                if not texts:
                    issues.append(f"empty slide: {name}")
    except (OSError, zipfile.BadZipFile):
        issues.append("file is not a valid PPTX ZIP archive")
    return PptxQAReport(slide_count=len(slide_names), issues=tuple(issues))
