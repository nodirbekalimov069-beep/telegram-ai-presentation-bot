import pytest

from modules.ai.contracts import AIContractError
from modules.ai.independent_work_contracts import (
    IndependentWorkDocument,
    IndependentWorkGenerationRequest,
    IndependentWorkSection,
    build_independent_work_messages,
    materialize_and_validate_independent_work,
    validate_independent_work_against_request,
)


def _document() -> IndependentWorkDocument:
    paragraph = (
        "Mavzu nazariy tushunchalar, sabab va oqibatlar hamda amaliy vazifalar orqali "
        "izchil yoritiladi. Har bir xulosa tekshirilmagan tashqi manbaga tayangan qat’iy "
        "da’vo sifatida emas, ehtiyotkor tahliliy mulohaza sifatida ifodalanadi."
    )
    return IndependentWorkDocument(
        title="Algoritmlarning amaliy ahamiyati",
        subject="Informatika",
        language="uz",
        objective=(
            "Algoritmik fikrlashning asosiy tamoyillarini tushuntirish va ularning amaliy "
            "masalalarni yechishdagi o‘rnini tizimli ravishda ko‘rsatish."
        ),
        practical_tasks=[
            "Asosiy tushunchalarni tavsiflash",
            "Amaliy qo‘llanish yo‘llarini tahlil qilish",
        ],
        introduction=paragraph,
        sections=[
            IndependentWorkSection(
                heading="Nazariy asoslar",
                paragraphs=[paragraph, paragraph],
            ),
            IndependentWorkSection(
                heading="Amaliy qo‘llanish",
                paragraphs=[paragraph, paragraph],
            ),
        ],
        conclusion=paragraph,
        requirement_evidence=[],
        references=[],
    )


def test_contract_accepts_service_specific_document() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
        additional_requirements="Xulosada algoritmik fikrlash iborasini ishlat.",
    )
    evidence = (
        "Algoritmik fikrlash masalani bosqichma-bosqich tahlil qilish, xavflarni oldindan "
        "baholash va amaliy yechimni inson nazorati bilan tekshirishga yordam beradi."
    )
    document = _document().model_copy(
        update={
            "conclusion": f"{_document().conclusion} {evidence}",
            "requirement_evidence": [evidence],
        }
    )

    assert validate_independent_work_against_request(document, request) is document


def test_contract_rejects_wrong_subject_and_unverified_references() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
    )
    wrong_subject = _document().model_copy(update={"subject": "Tarix"})
    with pytest.raises(AIContractError, match="subject"):
        validate_independent_work_against_request(wrong_subject, request)

    with_reference = _document().model_copy(
        update={"references": ["A. Muallif. Uydirma kitob. 2025."]}
    )
    with pytest.raises(AIContractError, match="references"):
        validate_independent_work_against_request(with_reference, request)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Natija https://example.test manbasida berilgan.",
        "Tadqiqot DOI:10.1000/fake orqali tasdiqlangan.",
        "Natija [1] manbaga ko‘ra aniq.",
        "Ishtirokchilarning 87% qismi buni tasdiqlagan.",
        "Smith (2024) bu fikrni tasdiqlagan.",
        "Smith, 2024 bu fikrni tasdiqlagan.",
        "Natija [1234] manbaga ko‘ra aniq.",
        "87 ishtirokchidan 64 nafari natijani ma’qullagan.",
        "Natijalar 3:2 nisbat bilan taqsimlangan.",
    ],
)
def test_contract_rejects_unverified_citations_and_statistics(unsafe_text: str) -> None:
    payload = _document().model_dump()
    payload["conclusion"] = f"{payload['conclusion']} {unsafe_text}"
    with pytest.raises(ValueError, match="unverified"):
        IndependentWorkDocument.model_validate(payload)


def test_contract_rejects_document_that_omits_requested_topic() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Ma’lumotlar bazasi",
        language="uz",
        length="short",
    )

    with pytest.raises(AIContractError, match="topic"):
        validate_independent_work_against_request(_document(), request)


def test_contract_requires_requirement_evidence_present_in_artifact() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
        additional_requirements="Xulosada amaliy xavfsizlikni tushuntir.",
    )

    with pytest.raises(AIContractError, match="requirement evidence"):
        validate_independent_work_against_request(_document(), request)

    fabricated_text = (
        "Amaliy xavfsizlik qaror oldidan xavfni aniqlash, inson nazoratini saqlash va "
        "natijani mustaqil tekshirish orqali izchil ta’minlanishi lozim."
    )
    fabricated_evidence = _document().model_copy(update={"requirement_evidence": [fabricated_text]})
    with pytest.raises(AIContractError, match="artifact"):
        validate_independent_work_against_request(fabricated_evidence, request)

    exact_evidence = (
        "Amaliy xavfsizlik har bir qarorni inson nazorati ostida tekshirish va kutilmagan "
        "oqibatlarni joriy etishdan oldin baholash orqali ta’minlanadi."
    )
    case_changed_evidence = _document().model_copy(
        update={
            "conclusion": f"{_document().conclusion} {exact_evidence}",
            "requirement_evidence": [exact_evidence.upper()],
        }
    )
    with pytest.raises(AIContractError, match="artifact"):
        validate_independent_work_against_request(case_changed_evidence, request)


def test_contract_rejects_untrusted_numeric_claim_not_present_in_request() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
    )
    document = _document().model_copy(
        update={"conclusion": f"{_document().conclusion} Jarayon 42 bosqichdan iborat."}
    )

    with pytest.raises(AIContractError, match="numeric"):
        validate_independent_work_against_request(document, request)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "(Smith 2024) bu fikrni tasdiqlagan.",
        "Natija [1–2] manbalarda qayd etilgan.",
        "Natija [1; 2] manbalarda qayd etilgan.",
        "Ishtirokchilarning 87％ qismi tasdiqlagan.",
        "Natija 3∶2 nisbatda taqsimlangan.",
        "Tadqiqot 137 people bilan o‘tkazilgan.",
        "Tahlil 137 cases asosida bajarilgan.",
        "According to Smith, ushbu fikr to‘g‘ri.",
        "Karimovga ko‘ra ushbu fikr to‘g‘ri.",
        "Smith tadqiqoti bu fikrni tasdiqlaydi.",
        "Согласно Смиту, этот вывод доказан.",
        "Как пишет Смит, этот вывод доказан.",
        "Smith proved this claim in a study.",
        "Smith says “this result is proven”.",
        "Smith stated that this is true.",
        "As Smith wrote, this is true.",
        "Smith asserted that this is true.",
        "Как написал Смит, этот вывод верен.",
        "Смит доказал этот вывод.",
        "Смит утверждал, что вывод верен.",
        "Karimov fikricha bu to‘g‘ri.",
        "Karimov aytishicha bu to‘g‘ri.",
        "Karimov ta’kidlashicha bu to‘g‘ri.",
    ],
)
def test_contract_rejects_unicode_and_trusted_number_bypasses(unsafe_text: str) -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar 2024 137 87 3 2 1",
        language="uz",
        length="short",
    )
    document = _document().model_copy(
        update={
            "title": request.topic,
            "conclusion": f"{_document().conclusion} {unsafe_text}",
        }
    )

    with pytest.raises((AIContractError, ValueError)):
        validate_independent_work_against_request(document, request)


@pytest.mark.parametrize(
    "meta_claim",
    [
        "Talab bajarildi.",
        "All requirements have been fulfilled.",
        "All requirements were satisfied.",
        "Все требования выполнены.",
        "Все требования были учтены.",
    ],
)
def test_contract_rejects_meta_only_requirement_evidence(meta_claim: str) -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar",
        language="uz",
        length="short",
        additional_requirements="Amaliy xavfsizlikni tushuntir.",
    )
    padded_meta = f"{meta_claim} " + "Mazmun tekshirilgani haqidagi izoh. " * 4
    meta_only = _document().model_copy(
        update={
            "requirement_evidence": [padded_meta],
            "conclusion": f"{_document().conclusion} {padded_meta}",
        }
    )

    with pytest.raises((AIContractError, ValueError)):
        materialize_and_validate_independent_work(meta_only, request=request)


def test_contract_allows_numeric_topic_only_as_exact_title() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmlar 2024",
        language="uz",
        length="short",
    )
    document = _document().model_copy(update={"title": request.topic})

    assert validate_independent_work_against_request(document, request) is document


def test_prompt_keeps_user_data_untrusted_and_material() -> None:
    request = IndependentWorkGenerationRequest(
        subject="Iqtisodiyot",
        topic="Raqamli bozor",
        language="uz",
        length="medium",
        additional_requirements="Har bir bo‘limda kichik biznes misolini tahlil qil.",
    )
    messages = build_independent_work_messages(request)

    assert messages[0]["role"] == "system"
    assert "untrusted" in messages[0]["content"]
    assert "references MUST be an empty" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "kichik biznes" in messages[1]["content"]
