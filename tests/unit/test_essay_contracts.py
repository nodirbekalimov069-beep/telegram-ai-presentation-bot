import json

import pytest
from pydantic import ValidationError

from modules.ai.essay_contracts import (
    EssayDocument,
    EssayGenerationRequest,
    EssayReference,
    EssaySection,
    build_essay_messages,
    validate_essay_against_request,
)
from modules.ai.contracts import AIContractError


def _valid_document() -> EssayDocument:
    return EssayDocument(
        title="Sun’iy intellektning ta’limdagi o‘rni",
        language="uz",
        introduction=(
            "Sun’iy intellekt ta’lim jarayonida individual yondashuvni kuchaytiradi. "
            "Ushbu referatda imkoniyatlar, cheklovlar va mas’uliyatli qo‘llash tamoyillari tahlil qilinadi."
        ),
        sections=[
            EssaySection(
                heading="Ta’limdagi asosiy imkoniyatlar",
                paragraphs=[
                    "Moslashtirilgan o‘qitish tizimlari o‘quvchining sur’ati va ehtiyojiga qarab material tanlaydi. "
                    "Bu yondashuv o‘qituvchining pedagogik qarorini almashtirmaydi, balki ma’lumot bilan qo‘llab-quvvatlaydi.",
                    "Avtomatik tahlil vositalari takroriy vazifalarni qisqartirib, o‘qituvchiga mazmuniy fikr-mulohaza berish uchun vaqt yaratadi. "
                    "Natijalar inson nazorati va tushunarli mezonlar bilan tekshirilishi lozim.",
                ],
            ),
            EssaySection(
                heading="Xavflar va mas’uliyat",
                paragraphs=[
                    "Noto‘g‘ri yoki tarafkash ma’lumot o‘quv natijasiga salbiy ta’sir qilishi mumkin. "
                    "Shuning uchun maxfiylik, adolat va verifikatsiya jarayonlari oldindan belgilanishi zarur.",
                    "Sun’iy intellektdan foydalanish raqamli savodxonlik bilan birga olib borilganda, o‘quvchi tayyor javobni emas, dalilni baholashni o‘rganadi. "
                    "Bu akademik halollikni saqlashga yordam beradi.",
                ],
            ),
        ],
        conclusion=(
            "Sun’iy intellekt ta’lim sifatini oshirish imkonini beradi, biroq natija pedagogik maqsad, inson nazorati va xavfsiz boshqaruvga bog‘liq. "
            "Texnologiya o‘qituvchini almashtiruvchi emas, mas’uliyatli yordamchi sifatida qo‘llanishi kerak."
        ),
        references=[
            EssayReference(
                description="UNESCOning ta’limda sun’iy intellektdan foydalanish bo‘yicha tavsiyalari",
                verification_note="Nashr nomi va yilini UNESCO rasmiy katalogidan tekshiring.",
            )
        ],
    )


def test_essay_messages_propagate_requirements_and_forbid_invented_sources() -> None:
    request = EssayGenerationRequest(
        topic="Sun’iy intellektning ta’limdagi o‘rni",
        language="uz",
        length="medium",
        additional_requirements="Ko‘proq O‘zbekiston misollaridan foydalan",
    )

    messages = build_essay_messages(request)
    payload = json.loads(messages[1]["content"])

    assert payload["additional_requirements"] == "Ko‘proq O‘zbekiston misollaridan foydalan"
    assert "DOI" in messages[0]["content"]
    assert "uydir" in messages[0]["content"].casefold()
    assert "JSON" in messages[0]["content"]
    assert "empty JSON" in messages[0]["content"]


def test_essay_document_requires_academic_structure() -> None:
    document = _valid_document()

    assert len(document.sections) == 2
    assert document.introduction.startswith("Sun’iy intellekt")
    assert document.conclusion.endswith("kerak.")
    assert document.references[0].verification_note


def test_essay_contract_rejects_url_or_doi_in_unverified_reference() -> None:
    document = _valid_document()
    payload = document.model_dump()
    payload["references"][0]["description"] = "https://example.com DOI:10.1234/invented"

    with pytest.raises(ValidationError):
        EssayDocument.model_validate(payload)


def test_essay_contract_rejects_duplicate_section_headings() -> None:
    document = _valid_document()
    payload = document.model_dump()
    payload["sections"][1]["heading"] = payload["sections"][0]["heading"]
    with pytest.raises(ValidationError):
        EssayDocument.model_validate(payload)


def test_request_validator_rejects_unverified_bibliographic_entries() -> None:
    request = EssayGenerationRequest(
        topic="Sun’iy intellektning ta’limdagi o‘rni",
        language="uz",
        length="short",
    )

    with pytest.raises(AIContractError, match="unverified bibliographic"):
        validate_essay_against_request(_valid_document(), request)
