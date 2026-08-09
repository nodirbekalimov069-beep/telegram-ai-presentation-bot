import pytest

from modules.ai.contracts import (
    AIContractError,
    ContentGenerationRequest,
    GeneratedContent,
    build_content_messages,
    validate_generated_content,
)
from modules.ai.providers.mock import MockAIProvider
from modules.presentations.engine import build_engine_payload


def test_engine_payload_prefers_expanded_content_over_outline_key_points() -> None:
    outline = {
        "slides": [
            {
                "number": 1,
                "title": "Tarixiy hikoyalar",
                "purpose": "Mavzuni tanishtirish",
                "key_points": ["Tarixiy hikoya nima?"],
                "sources": [],
            }
        ]
    }
    content = {
        "slides": [
            {
                "number": 1,
                "title": "Tarixiy hikoyalar",
                "body_blocks": [
                    {
                        "heading": "Ta’rif",
                        "body": (
                            "Tarixiy hikoya o‘tmishdagi voqealarni badiiy bayon orqali "
                            "tushuntiradi va davr ruhini ochib beradi."
                        ),
                    },
                    {
                        "heading": "Ahamiyati",
                        "body": (
                            "U faktlarni insoniy tajriba bilan bog‘lab, tarixiy tafakkur "
                            "va manbaga tanqidiy munosabatni rivojlantiradi."
                        ),
                    },
                ],
                "speaker_notes": "Talabalarga tarixiy manba va badiiy talqin farqini tushuntiring.",
            }
        ]
    }

    payload = build_engine_payload(
        title="Tarixiy hikoyalar",
        language="uz",
        direction="Tarix",
        design_style="historical",
        plan_code="premium_plus",
        outline=outline,
        content=content,
    )

    slide = payload["slides"][0]
    assert slide["role"] == "cover"
    assert slide["bullets"] == [
        "Ta’rif — Tarixiy hikoya o‘tmishdagi voqealarni badiiy bayon orqali "
        "tushuntiradi va davr ruhini ochib beradi.",
        "Ahamiyati — U faktlarni insoniy tajriba bilan bog‘lab, tarixiy tafakkur "
        "va manbaga tanqidiy munosabatni rivojlantiradi.",
    ]
    assert slide["speaker_notes"].startswith("Talabalarga")


def test_content_generation_messages_use_a_separate_full_content_mode() -> None:
    request = ContentGenerationRequest(
        title="Tarixiy hikoyalar",
        language="uz",
        direction="Tarix",
        purpose="ta'lim uchun",
        audience="oliy ta'lim",
        knowledge_level="advanced",
        outline={"title": "Tarixiy hikoyalar", "slides": [{"number": 1}]},
    )

    messages = build_content_messages(request)

    assert "FULL CONTENT GENERATION MODE" in messages[0]["content"]
    assert '"audience":"oliy ta\'lim"' in messages[1]["content"]


def test_generated_content_requires_consecutive_slide_numbers() -> None:
    content = GeneratedContent.model_validate(
        {
            "title": "Tarixiy hikoyalar",
            "slides": [
                {
                    "number": 2,
                    "title": "Kirish",
                    "body_blocks": [
                        {"heading": "Ta’rif", "body": "A" * 80},
                        {"heading": "Ahamiyat", "body": "B" * 80},
                    ],
                    "speaker_notes": "C" * 160,
                }
            ],
        }
    )

    with pytest.raises(AIContractError):
        validate_generated_content(content, expected_slide_count=1)


def test_uzbek_content_quality_rejects_known_spelling_and_generic_copy() -> None:
    content = GeneratedContent.model_validate(
        {
            "title": "Tarixiy Xikoyalar",
            "slides": [
                {
                    "number": 1,
                    "title": "Tarixiy Xikoyalarga Kirish",
                    "body_blocks": [
                        {
                            "heading": "Ahamiyati",
                            "body": "Tarixiy xikoyalar ma'lumotlarni etkazishda muhim rol o'ynaydi va jamiyat uchun foydali hisoblanadi.",
                        },
                        {
                            "heading": "Ta'limdagi o'rni",
                            "body": "Bu xikoyalar talabalar uchun muhim rol o'ynaydi va bilimlarni etkazishda yordam beradi.",
                        },
                    ],
                    "speaker_notes": "Tarixiy xikoyalar juda muhim rol o'ynaydi. Ular ma'lumotlarni etkazadi va talabalar uchun foydali bo'ladi. Mazkur slaydni sodda tarzda tushuntiring. Keyingi qismda ayni umumiy fikrni yana takrorlang va yangi dalil qo'shmang.",
                }
            ],
        }
    )

    with pytest.raises(AIContractError, match=r"Uzbek spelling|generic"):
        validate_generated_content(content, expected_slide_count=1, language="uz")


@pytest.mark.asyncio
async def test_mock_provider_expands_outline_into_explanatory_content() -> None:
    request = ContentGenerationRequest(
        title="Tarixiy hikoyalar",
        language="uz",
        direction="Tarix",
        purpose="ta'lim uchun",
        audience="oliy ta'lim",
        knowledge_level="advanced",
        outline={
            "title": "Tarixiy hikoyalar",
            "slides": [
                {
                    "number": 1,
                    "title": "Kirish",
                    "purpose": "Mavzuni tushuntirish",
                    "key_points": ["Tarixiy hikoya nima?", "Uning ahamiyati"],
                }
            ],
        },
    )

    content = await MockAIProvider().generate_content(request)

    assert len(content.slides) == 1
    assert len(content.slides[0].body_blocks) == 2
    assert all(len(block.body) >= 50 for block in content.slides[0].body_blocks)
