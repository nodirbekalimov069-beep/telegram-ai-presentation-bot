import pytest
from pydantic import ValidationError

from modules.ai.contracts import AIContractError
from modules.ai.course_work_contracts import (
    CourseWorkChapter,
    CourseWorkDocument,
    CourseWorkGenerationRequest,
    build_course_work_messages,
    materialize_and_validate_course_work,
    requested_course_sections,
    validate_course_work_against_request,
)


def _paragraph(topic: str = "Algoritmik fikrlash") -> str:
    return (
        f"{topic} informatika fanidagi murakkab vazifalarni qismlarga ajratish, yechimni "
        "izchil loyihalash va natijani inson nazorati ostida tekshirish uchun qo‘llanadi. "
        "Mazkur yondashuv nazariy tushunchalarni amaliy jarayon bilan bog‘laydi, xavflarni "
        "oldindan baholashga yordam beradi va tekshirilmagan tashqi manbalarni dalil sifatida "
        "ko‘rsatmasdan ehtiyotkor xulosa chiqarishni ta’minlaydi."
    )


def _request(*, requirements: str | None = None) -> CourseWorkGenerationRequest:
    return CourseWorkGenerationRequest(
        subject="Informatika",
        topic="Algoritmik fikrlash",
        sections="Nazariy asoslar; Amaliy tahlil; Tavsiyalar",
        length="short",
        language="uz",
        additional_requirements=requirements,
    )


def _document(*, evidence: str | None = None) -> CourseWorkDocument:
    paragraph = _paragraph()
    conclusion = (
        "Algoritmik fikrlash bo‘yicha kurs ishida nazariya va amaliyot o‘rtasidagi bog‘liqlik "
        "izchil umumlashtiriladi. Yechimlarni qismlarga ajratish, natijani inson nazoratida "
        "tekshirish va cheklovlarni ochiq ko‘rsatish mas’uliyatli yondashuv sifatida asoslanadi."
    )
    if evidence:
        conclusion = (
            f"{conclusion} Mas’uliyatli avtomatlashtirish amaliy qarorlarda inson nazorati "
            "va mustaqil tekshiruv bilan bog‘lanadi."
            f"\n\n{evidence}"
        )
    return CourseWorkDocument(
        title="Algoritmik fikrlash",
        subject="Informatika",
        language="uz",
        objective=(
            "Algoritmik fikrlashning informatika fanidagi nazariy asoslarini tizimlashtirish, "
            "amaliy yechim yaratish bosqichlarini tushuntirish va xavfsiz qo‘llash mezonlarini "
            "aniqlash."
        ),
        tasks=[
            "Algoritmik fikrlashning asosiy tushunchalarini izchil tavsiflash",
            "Amaliy vazifani qismlarga ajratish va yechim jarayonini tahlil qilish",
            "Natijani tekshirish hamda mas’uliyatli qo‘llash bo‘yicha tavsiya berish",
        ],
        introduction=_paragraph(),
        chapters=[
            CourseWorkChapter(heading="Nazariy asoslar", paragraphs=[paragraph, paragraph]),
            CourseWorkChapter(heading="Amaliy tahlil", paragraphs=[paragraph, paragraph]),
            CourseWorkChapter(heading="Tavsiyalar", paragraphs=[paragraph, paragraph]),
        ],
        conclusion=conclusion,
        requirement_evidence=[evidence] if evidence else [],
        references=[],
    )


def test_requested_course_sections_are_bounded_and_deterministic() -> None:
    assert requested_course_sections(" Nazariy asoslar; Amaliy tahlil\nTavsiyalar ") == (
        "Nazariy asoslar",
        "Amaliy tahlil",
        "Tavsiyalar",
    )
    with pytest.raises(ValueError, match="sections"):
        requested_course_sections("Faqat bitta bo‘lim")
    with pytest.raises(ValueError, match="sections"):
        requested_course_sections(";".join(f"Bo‘lim {index}" for index in range(13)))


def test_course_work_contract_accepts_bound_safe_document() -> None:
    document = _document()
    assert validate_course_work_against_request(document, _request()) is document


def test_course_work_contract_is_strict_and_rejects_unverified_material() -> None:
    with pytest.raises(ValidationError):
        CourseWorkDocument.model_validate({**_document().model_dump(), "debug": True})
    with pytest.raises(ValidationError):
        CourseWorkDocument.model_validate(
            {**_document().model_dump(), "references": ["Smith, Fake Book, 2024"]}
        )


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Smith stated that this is proven.",
        "Согласно Смиту, этот вывод доказан.",
        "Karimov fikricha bu to‘g‘ri.",
        "Natija [1–2] manbalarda qayd etilgan.",
        "Tadqiqot 137 people bilan o‘tkazilgan.",
        "Manba https://example.test yoki DOI:10.1000/fake.",
    ],
)
def test_course_work_final_rescan_rejects_hallucination_bypasses(unsafe_text: str) -> None:
    document = _document().model_copy(
        update={"conclusion": f"{_document().conclusion} {unsafe_text}"}
    )
    with pytest.raises((AIContractError, ValueError)):
        validate_course_work_against_request(document, _request())


def test_course_work_rejects_untrusted_numbers_even_when_request_contains_them() -> None:
    request = _request().model_copy(update={"topic": "Algoritmik fikrlash 2026"})
    document = _document().model_copy(
        update={
            "title": request.topic,
            "conclusion": f"{_document().conclusion} Jarayon 2026 ishtirokchini qamradi.",
        }
    )
    with pytest.raises(AIContractError, match="numeric"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize(
    "quotation",
    [
        '"Learning always transforms every human perspective."',
        "“Learning always transforms every human perspective.”",
        "«Learning always transforms every human perspective.»",
        "„Learning always transforms every human perspective.“",
        "'Learning always transforms every human perspective.'",
        "‘Learning always transforms every human perspective.’",
    ],
)
def test_final_rescan_rejects_untrusted_model_authored_quote(quotation: str) -> None:
    document = _document().model_copy(update={"introduction": f"{_paragraph()} {quotation}"})
    with pytest.raises(AIContractError, match="quotation"):
        validate_course_work_against_request(document, _request())


@pytest.mark.parametrize("construction", ["model_copy", "model_validate"])
@pytest.mark.parametrize(
    "quotation",
    [
        "＂Learning always transforms every human perspective.＂",
        '＂Learning always transforms every human perspective."',
        '"Learning always transforms every human perspective.＂',
        "＇Learning always transforms every human perspective.＇",
        "＇Learning always transforms every human perspective.'",
        "'Learning always transforms every human perspective.＇",
    ],
)
def test_final_rescan_rejects_nfkc_compatible_quote_delimiters(
    construction: str, quotation: str
) -> None:
    payload = _document().model_dump()
    payload["introduction"] = f"{_paragraph()} {quotation}"
    document = (
        CourseWorkDocument.model_validate(payload)
        if construction == "model_validate"
        else _document().model_copy(update={"introduction": payload["introduction"]})
    )
    with pytest.raises(AIContractError, match="quotation"):
        validate_course_work_against_request(document, _request())


def test_requirement_evidence_must_be_semantically_bound_to_request() -> None:
    unrelated = (
        "Umumiy texnologik kuzatuv ehtiyotkor yondashuvni, inson nazoratini va natijani "
        "mustaqil tekshirishni tavsiya etadi."
    )
    document = _document(evidence=unrelated)
    request = _request(requirements="Alohida SWOT tahlili va tavsiyalar jadvali kiritilsin.")
    with pytest.raises(AIContractError, match="additional requirements"):
        validate_course_work_against_request(document, request)


def test_requested_language_must_match_model_authored_body() -> None:
    document = _document().model_copy(update={"language": "ru"})
    request = _request().model_copy(update={"language": "ru"})
    with pytest.raises(AIContractError, match="language"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ('"', '"'),
        ("“", "”"),
        ("«", "»"),
        ("„", "“"),
        ("'", "'"),
        ("‘", "’"),
        ("＂", "＂"),
        ("＇", "＇"),
    ],
)
def test_user_requested_quoted_phrase_is_allowed_only_when_content_is_exact(
    opening: str, closing: str
) -> None:
    request = _request(
        requirements='Xulosada "mas’uliyatli avtomatlashtirish" iborasini aynan ishlat.'
    )
    exact_quote = f"{opening}mas’uliyatli avtomatlashtirish{closing}"
    altered_quote = f"{opening}MAS’ULIYATLI   AVTOMATLASHTIRISH{closing}"
    evidence = (
        f"{exact_quote} iborasi xulosada amaliy xavfsizlik, inson nazorati "
        "va mustaqil tekshiruv tamoyili sifatida yoritildi."
    )
    document = _document(evidence=evidence)
    validated = validate_course_work_against_request(document, request)
    assert validated.requirement_evidence == [evidence]

    altered = document.model_copy(
        update={
            "conclusion": document.conclusion.replace(exact_quote, altered_quote),
            "requirement_evidence": [evidence.replace(exact_quote, altered_quote)],
        }
    )
    with pytest.raises(AIContractError, match="quotation"):
        validate_course_work_against_request(altered, request)


def test_isbn_and_numbers_in_model_authored_heading_are_rejected() -> None:
    document = _document()
    unsafe_heading = CourseWorkChapter(
        heading="Qo‘shimcha tahlil",
        paragraphs=[_paragraph(), _paragraph()],
    ).model_copy(update={"heading": "ISBN 978-X fictional handbook"})
    bypass = document.model_copy(update={"chapters": [*document.chapters, unsafe_heading]})
    with pytest.raises(AIContractError):
        validate_course_work_against_request(bypass, _request())


def _french_body() -> str:
    return (
        "Cette étude examine les pratiques pédagogiques avec une démarche cohérente et prudente. "
        "Elle décrit les relations entre les concepts, les contraintes du contexte et les choix "
        "méthodologiques sans présenter de sources externes comme des faits vérifiés. Les limites "
        "restent explicites tandis que la réflexion soutient une application responsable. "
        "analysis evidence solution"
    )


def _document_with_english_label_and_body(text: str, construction: str) -> CourseWorkDocument:
    payload = _document().model_dump()
    payload.update(
        {
            "language": "en",
            "objective": text,
            "tasks": [text, text, text],
            "introduction": text,
            "chapters": [
                {"heading": chapter.heading, "paragraphs": [text, text]}
                for chapter in _document().chapters
            ],
            "conclusion": text,
        }
    )
    validated_payload = CourseWorkDocument.model_validate(payload)
    if construction == "model_validate":
        return validated_payload
    return _document().model_copy(
        update={
            "language": "en",
            "objective": text,
            "tasks": [text, text, text],
            "introduction": text,
            "chapters": validated_payload.chapters,
            "conclusion": text,
        }
    )


@pytest.mark.parametrize("construction", ["model_copy", "model_validate"])
def test_english_language_rejects_french_body_with_marker_stuffing(construction: str) -> None:
    document = _document_with_english_label_and_body(_french_body(), construction)
    request = _request().model_copy(update={"language": "en"})
    with pytest.raises(AIContractError, match="language"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize("construction", ["model_copy", "model_validate"])
def test_english_language_rejects_spanish_body_with_repeated_marker_stuffing(
    construction: str,
) -> None:
    text = ("le la et " * 15) + (
        "analysis and approach conclusion evidence for human in of practice problem result "
        "solution the to with "
    ) * 2
    document = _document_with_english_label_and_body(text, construction)
    request = _request().model_copy(update={"language": "en"})
    with pytest.raises(AIContractError, match="language"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize("construction", ["model_copy", "model_validate"])
def test_english_language_rejects_unknown_gibberish_marker_stuffing(construction: str) -> None:
    text = ("zo za xi " * 15) + (
        "analysis and approach conclusion evidence for human in of practice problem result "
        "solution the to with "
    ) * 2
    document = _document_with_english_label_and_body(text, construction)
    request = _request().model_copy(update={"language": "en"})
    with pytest.raises(AIContractError, match="language"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize("construction", ["model_copy", "model_validate"])
def test_english_language_rejects_unique_unknown_gibberish_marker_stuffing(
    construction: str,
) -> None:
    invented = " ".join(f"zq{chr(97 + index // 26)}{chr(97 + index % 26)}x" for index in range(30))
    text = (
        invented
        + " "
        + (
            "analysis and approach conclusion evidence for human in of practice problem result "
            "solution the to with "
        )
        * 2
    )
    document = _document_with_english_label_and_body(text, construction)
    request = _request().model_copy(update={"language": "en"})
    with pytest.raises(AIContractError, match="language"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize(
    "evidence",
    [
        (
            "Renewable energy storage and grid integration are addressed here. Office filing, "
            "calendar scheduling, stationery sorting, and meeting-room labels form the remaining text."
        ),
        (
            "Renewable energy storage and grid integration appear together. Office filing, calendar "
            "scheduling, stationery sorting, meeting-room labels, and desk allocation dominate this paragraph."
        ),
    ],
)
def test_requirement_evidence_rejects_meta_and_keyword_stuffing(evidence: str) -> None:
    request = _request(
        requirements="Explain renewable energy storage and grid integration in practical terms."
    )
    document = _document(evidence=evidence)
    with pytest.raises(AIContractError, match="additional requirements"):
        validate_course_work_against_request(document, request)


@pytest.mark.parametrize(
    "evidence",
    [
        (
            "So‘ralgan energiya saqlash va tarmoq integratsiyasi bo‘yicha muhokama quyida "
            "taqdim etiladi. Energiya saqlash va tarmoq integratsiyasi amaliy tahlil hamda "
            "yechim orqali talabni qamrab oladi."
        ),
        (
            "Energiya saqlash va tarmoq integratsiyasi talabi shu matnda bajarilgan. Amaliy "
            "tahlil va yechim natijasi inson nazorati orqali ko‘rsatilgan."
        ),
        (
            "Energiya saqlash va tarmoq integratsiyasi talab ijrosini ushbu muhokamada "
            "ta’minlaydi. Amaliy tahlil va yechim talab mazmunini shu bo‘limda to‘liq "
            "qamrab oladi."
        ),
    ],
)
def test_requirement_evidence_rejects_uzbek_deictic_meta_claims(evidence: str) -> None:
    request = _request(
        requirements=("Energiya saqlash va tarmoq integratsiyasi bo‘yicha amaliy muhokama yozing.")
    )
    document = _document(evidence=evidence)
    with pytest.raises((AIContractError, ValueError)):
        validate_course_work_against_request(document, request)


def test_requirement_keywords_must_reach_authored_body_outside_evidence() -> None:
    request = _request(
        requirements=("Energiya saqlash va tarmoq integratsiyasi bo‘yicha amaliy muhokama yozing.")
    )
    evidence = (
        "Energiya saqlash tarmoq integratsiyasi amaliy muhokama tahlil yechim nazorat natija "
        "algoritmik fikrlash informatika jarayoni bilan izchil bog‘lanadi va ehtiyotkor xulosa beradi."
    )
    with pytest.raises(AIContractError, match="additional requirements"):
        validate_course_work_against_request(_document(evidence=evidence), request)


def test_requirement_key_salad_in_body_and_evidence_is_not_substantive_prose() -> None:
    request = _request(
        requirements=("Energiya saqlash va tarmoq integratsiyasi bo‘yicha amaliy muhokama yozing.")
    )
    evidence = (
        "Energiya saqlash tarmoq integratsiyasi bo‘yicha amaliy muhokama. Tahlil yechim "
        "nazorat natija algoritmik fikrlash informatika jarayoni ehtiyotkor xulosa."
    )
    body_salad = (
        "MUHOKAMA AMALIY INTEGRATSIYASI TARMOQ SAQLASH ENERGIYA. Natija nazorat yechim "
        "tahlil informatika algoritmik jarayon mezon ehtiyotkor yondashuv xulosa."
    )
    document = _document(evidence=evidence).model_copy(update={"objective": body_salad})

    with pytest.raises(AIContractError, match="substantive"):
        validate_course_work_against_request(document, request)


def test_requirement_key_salad_with_generic_relations_and_predicates_is_rejected() -> None:
    request = _request(
        requirements=("Energiya saqlash va tarmoq integratsiyasi bo‘yicha amaliy muhokama yozing.")
    )
    evidence = (
        "Energiya saqlash tarmoq integratsiyasi bo‘yicha va umumiy tartib muhimdir, davom "
        "etadi hamda amalga oshiriladi. Tahlil yechim nazorat natija ehtiyotkor xulosa."
    )
    body_salad = (
        "MUHOKAMA AMALIY INTEGRATSIYASI TARMOQ SAQLASH ENERGIYA BO‘YICHA va umumiy tartib "
        "muhimdir, davom etadi hamda amalga oshiriladi. Tahlil yechim nazorat natija."
    )
    document = _document(evidence=evidence).model_copy(update={"objective": body_salad})

    with pytest.raises(AIContractError, match="substantive"):
        validate_course_work_against_request(document, request)


def test_course_work_binds_subject_topic_sections_language_and_length() -> None:
    document = _document()
    with pytest.raises(AIContractError, match="subject"):
        validate_course_work_against_request(
            document.model_copy(update={"subject": "Tarix"}), _request()
        )
    with pytest.raises(AIContractError, match="topic"):
        validate_course_work_against_request(
            document.model_copy(update={"title": "Boshqa mavzu"}), _request()
        )
    with pytest.raises(AIContractError, match="section"):
        validate_course_work_against_request(
            document.model_copy(update={"chapters": document.chapters[:2]}), _request()
        )
    with pytest.raises(AIContractError, match="language"):
        validate_course_work_against_request(
            document.model_copy(update={"language": "en"}), _request()
        )


def test_course_work_materializes_substantive_requirement_evidence_verbatim() -> None:
    evidence = (
        "Mas’uliyatli avtomatlashtirish qaror oldidan xavfni baholash, inson nazoratini saqlash "
        "va yakuniy natijani mustaqil tekshirish bilan amalda ta’minlanadi."
    )
    request = _request(requirements="Xulosada mas’uliyatli avtomatlashtirishni tushuntir.")
    document = _document().model_copy(
        update={
            "conclusion": (
                f"{_document().conclusion} Mas’uliyatli avtomatlashtirish amaliy qarorlarda "
                "inson nazorati va mustaqil tekshiruv bilan bog‘lanadi."
            ),
            "requirement_evidence": [evidence],
        }
    )

    result = materialize_and_validate_course_work(document, request=request)

    assert result.conclusion.endswith(evidence)
    assert evidence in result.conclusion


def test_course_work_requirement_evidence_is_required_iff_requested() -> None:
    with pytest.raises(AIContractError, match="evidence"):
        validate_course_work_against_request(
            _document(), _request(requirements="Xavfsizlikni yoz.")
        )
    with pytest.raises(AIContractError, match="unexpected"):
        validate_course_work_against_request(
            _document(
                evidence=(
                    "Xavfsizlik yechimni ishga tushirishdan oldin tekshirish, inson nazoratini "
                    "saqlash va xatolarni qayta ko‘rib chiqish orqali ta’minlanadi."
                )
            ),
            _request(),
        )


def test_course_work_prompt_contains_all_inputs_and_fail_closed_policy() -> None:
    request = _request(requirements="Amaliy tavsiya alohida yozilsin.")
    messages = build_course_work_messages(request)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert request.subject in messages[1]["content"]
    assert request.topic in messages[1]["content"]
    assert request.sections in messages[1]["content"]
    assert request.additional_requirements in messages[1]["content"]
    assert "references MUST be an empty" in messages[0]["content"]
    assert "untrusted" in messages[0]["content"]
