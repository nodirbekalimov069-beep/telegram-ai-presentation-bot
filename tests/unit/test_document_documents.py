from pathlib import Path

from docx import Document

from modules.ai.document_contracts import (
    DocumentGenerationRequest,
    DocumentSelection,
    materialize_document_selection,
    normalized_document_body,
)
from modules.service_orders.document_documents import (
    DocumentDocumentRenderer,
    document_artifact_parity_score,
    extracted_document_docx_body,
)
import modules.service_orders.document_documents as document_documents
from modules.service_orders.documents import validate_docx_security
from scripts.local_document_e2e import _pdf_matches_docx


def _document():  # type: ignore[no-untyped-def]
    request = DocumentGenerationRequest(
        document_type="application",
        details=(
            "Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, "
            "2026-yil 15-avgustdan uch kunlik ta’til so‘rayman."
        ),
        language="uz",
    )
    return materialize_document_selection(
        DocumentSelection(
            document_type="application",
            language="uz",
            source_indices=[0, 1],
        ),
        request=request,
    )


def test_document_renderer_creates_secure_editable_canonical_docx(tmp_path: Path) -> None:
    value = _document()
    path = DocumentDocumentRenderer().render(value, tmp_path, order_id=88)

    assert path.name == "hujjat_88.docx"
    validate_docx_security(path)
    assert extracted_document_docx_body(path) == normalized_document_body(value)
    document = Document(path)
    visible = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Toshkent davlat universiteti rektoriga." in visible
    assert "Ali Valiyev" in visible
    assert value.limitation_notice in visible
    assert "Imzo:" not in visible
    assert "Muhr:" not in visible
    assert "http://" not in visible and "https://" not in visible


def test_document_artifact_parity_accepts_exact_canonical_representation(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    value = _document()
    canonical = normalized_document_body(value)
    monkeypatch.setattr(document_documents, "extracted_document_docx_body", lambda _: canonical)
    monkeypatch.setattr(document_documents, "extracted_document_pdf_text", lambda _: canonical)

    assert (
        document_artifact_parity_score(
            value, docx_path=Path("document.docx"), pdf_path=Path("document.pdf")
        )
        == 1.0
    )


def test_document_artifact_parity_rejects_any_pdf_additional_content(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    value = _document()
    canonical = normalized_document_body(value)
    monkeypatch.setattr(document_documents, "extracted_document_docx_body", lambda _: canonical)

    additions = (
        f"UNAUTHORIZED PREFIX {canonical}",
        f"{canonical} UNAUTHORIZED SUFFIX",
        canonical.replace(" Men,", " UNAUTHORIZED INSERTED PARAGRAPH Men,"),
        f"{canonical} UNAUTHORIZED EXTRA PAGE",
    )
    for extracted_pdf in additions:
        monkeypatch.setattr(
            document_documents, "extracted_document_pdf_text", lambda _, text=extracted_pdf: text
        )
        assert (
            document_artifact_parity_score(
                value, docx_path=Path("document.docx"), pdf_path=Path("document.pdf")
            )
            == 0.0
        )


def test_local_e2e_pdf_oracle_requires_exact_normalized_docx_body() -> None:
    canonical = "Canonical first paragraph. Canonical second paragraph."

    assert _pdf_matches_docx(
        "  Canonical first paragraph.\nCanonical second paragraph.  ", canonical
    )
    for extracted_pdf in (
        f"UNAUTHORIZED PREFIX {canonical}",
        f"{canonical} UNAUTHORIZED SUFFIX",
        canonical.replace(" Canonical second", " UNAUTHORIZED INSERT Canonical second"),
        f"{canonical}\fUNAUTHORIZED EXTRA PAGE",
    ):
        assert not _pdf_matches_docx(extracted_pdf, canonical)
