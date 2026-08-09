"""LibreOffice PDF conversion and structural PDF quality checks."""

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


class PdfConversionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PdfQAReport:
    page_count: int
    issues: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


class LibreOfficeConverter:
    def __init__(
        self,
        *,
        binary: str | Path = "soffice",
        timeout_seconds: float = 180,
    ) -> None:
        binary_value = str(binary)
        resolved = shutil.which(binary_value)
        if resolved is None and Path(binary_value).is_file():
            resolved = str(Path(binary_value).expanduser().resolve())
        if resolved is None:
            raise FileNotFoundError(binary_value)
        self._binary = resolved
        self._timeout = timeout_seconds

    async def convert(self, pptx_path: Path, output_pdf: Path) -> Path:
        source = pptx_path.expanduser().resolve(strict=True)
        output = output_pdf.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        generated = output.parent / f"{source.stem}.pdf"
        output.unlink(missing_ok=True)
        if generated != output:
            generated.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile:
            profile_uri = Path(profile).resolve().as_uri()
            process = await asyncio.create_subprocess_exec(
                self._binary,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output.parent),
                str(source),
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
                raise PdfConversionError("LibreOffice conversion timed out") from exc
        if process.returncode != 0:
            detail = (stderr or stdout).decode(errors="replace")[-2000:]
            raise PdfConversionError(f"LibreOffice conversion failed: {detail}")
        if not generated.is_file() or generated.stat().st_size == 0:
            detail = stdout.decode(errors="replace")[-1000:]
            raise PdfConversionError(f"LibreOffice did not create a PDF: {detail}")
        if generated != output:
            generated.replace(output)
        return output


def validate_pdf(path: Path, *, expected_pages: int | None) -> PdfQAReport:
    issues: list[str] = []
    page_count = 0
    try:
        reader = PdfReader(str(path), strict=True)
        page_count = len(reader.pages)
        if page_count <= 0:
            issues.append("PDF has no pages")
        if expected_pages is not None and page_count != expected_pages:
            issues.append(f"expected {expected_pages} PDF pages, found {page_count}")
        dimensions: list[tuple[float, float]] = []
        for index, page in enumerate(reader.pages, start=1):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            if width <= 0 or height <= 0:
                issues.append(f"page {index} has invalid dimensions")
            dimensions.append((round(width, 2), round(height, 2)))
        if len(set(dimensions)) > 1:
            issues.append("PDF pages have inconsistent dimensions")
    except Exception as exc:
        issues.append(f"invalid PDF: {type(exc).__name__}")
    return PdfQAReport(page_count=page_count, issues=tuple(issues))


def validate_text_pdf(
    path: Path,
    *,
    expected_phrases: tuple[str, ...],
    minimum_characters: int = 100,
) -> PdfQAReport:
    """Validate a document PDF structurally and against its extracted text."""
    report = validate_pdf(path, expected_pages=None)
    issues = list(report.issues)
    if issues:
        return report
    try:
        reader = PdfReader(str(path), strict=True)
        extracted = " ".join((page.extract_text() or "") for page in reader.pages)
        normalized = " ".join(extracted.casefold().split())
        if len(normalized) < minimum_characters:
            issues.append("PDF extracted text is empty or too short")
        for phrase in expected_phrases:
            expected = " ".join(phrase.casefold().split())
            if expected not in normalized:
                issues.append("PDF extracted text is missing expected document content")
    except Exception as exc:
        issues.append(f"PDF text extraction failed: {type(exc).__name__}")
    return PdfQAReport(page_count=report.page_count, issues=tuple(issues))
