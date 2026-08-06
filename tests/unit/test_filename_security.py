import pytest

from modules.files.security import UnsafeFileType, sanitize_filename


def test_filename_sanitization_blocks_path_traversal_and_control_characters() -> None:
    result = sanitize_filename("../../Quarterly\x00 Report?.pptx", allowed_extensions={".pptx"})

    assert result == "Quarterly_Report.pptx"
    assert "/" not in result
    assert "\\" not in result


def test_filename_sanitization_uses_safe_fallback_for_non_ascii_only_name() -> None:
    result = sanitize_filename("../../тақдимот.pptx", allowed_extensions={".pptx"})

    assert result == "presentation.pptx"


def test_filename_sanitization_rejects_unapproved_extension() -> None:
    with pytest.raises(UnsafeFileType):
        sanitize_filename("payload.exe", allowed_extensions={".pptx", ".pdf"})


def test_filename_is_bounded_and_preserves_extension() -> None:
    result = sanitize_filename(f"{'a' * 200}.pdf", allowed_extensions={".pdf"}, max_length=80)

    assert len(result) == 80
    assert result.endswith(".pdf")
