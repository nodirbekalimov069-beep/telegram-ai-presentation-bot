import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import JSZip from "jszip";

import { renderPresentation, selectEditorialLayout } from "../src/render.mjs";

const theme = {
  palette: {
    background: "F7F1E5",
    foreground: "35261D",
    primary: "8F603B",
    secondary: "C89F72",
    accent: "B85C3C"
  },
  typography: { heading: "Aptos Display", body: "Aptos" }
};

test("editorial layout selection follows slide semantics rather than slide index", () => {
  assert.equal(selectEditorialLayout({ title: "Tarixiy manbalarni tekshirish" }, 2), "source-map");
  assert.equal(selectEditorialLayout({ title: "Zafarnoma misolida tahlil" }, 2), "case-study");
  assert.equal(selectEditorialLayout({ title: "Tadqiqot jarayoni" }, 2), "process");
  assert.notEqual(
    selectEditorialLayout({ title: "Tarixiy manbalarni tekshirish" }, 2),
    selectEditorialLayout({ title: "Zafarnoma misolida tahlil" }, 2)
  );
});

test("expanded blocks render heading and explanation separately with a historical semantic visual", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-blocks-"));
  const output = path.join(dir, "deck.pptx");
  const deck = {
    title: "Tarixiy hikoyalar",
    language: "uz",
    theme,
    plan_code: "PREMIUM_PLUS",
    slides: [
      {
        title: "Tarixiy hikoyalar",
        bullets: ["Manbaga asoslangan akademik tahlil"],
        content_blocks: [{ heading: "Yo‘nalish", body: "Tarixiy narrativni manba va talqin orqali o‘rganish." }],
        sources: []
      },
      {
        title: "Tarixiy manbalarni tekshirish",
        bullets: [],
        content_blocks: [
          {
            heading: "Tashqi tanqid",
            body: "Hujjatning muallifi, yaratilgan davri va asl nusxaga yaqinligi tekshiriladi."
          },
          {
            heading: "Ichki tanqid",
            body: "Muallif pozitsiyasi, maqsadi va voqea haqidagi dalillar boshqa manbalar bilan solishtiriladi."
          },
          {
            heading: "Xulosa",
            body: "Tasdiqlangan dalil talqindan ajratilib, narrativga faqat tekshirilgan shaklda kiritiladi."
          }
        ],
        sources: []
      }
    ]
  };

  await renderPresentation(deck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  const xml = await zip.file("ppt/slides/slide2.xml").async("string");

  assert.match(xml, /<a:t>Tashqi tanqid<\/a:t>/);
  assert.match(xml, /Hujjatning muallifi/);
  assert.doesNotMatch(xml, /Tashqi tanqid — Hujjatning muallifi/);
  assert.match(xml, /MANBA/);
  assert.match(xml, /TALQIN/);
  assert.match(xml, /NARRATIV/);
});

test("historical case studies embed curated public-domain imagery without AI branding", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-curated-history-"));
  const output = path.join(dir, "deck.pptx");
  const block = {
    heading: "Dalil",
    body: "Qo‘lyozma tasviri matn, material va vizual talqinni birgalikda tahlil qilish imkonini beradi."
  };
  const deck = {
    title: "Tarixiy hikoyalar",
    language: "uz",
    theme,
    plan_code: "PREMIUM_PLUS",
    slides: [
      { title: "Kirish", bullets: ["Manba"], content_blocks: [block], sources: [] },
      {
        title: "Amaliy tahlil: Sharafiddin Ali Yazdiyning «Zafarnoma»si",
        bullets: [],
        content_blocks: [block, { ...block, heading: "Kontekst" }],
        sources: [{ label: "commons.wikimedia.org", url: "https://commons.wikimedia.org/" }]
      },
      { title: "Metod", bullets: ["Tekshiruv"], content_blocks: [block], sources: [] },
      { title: "Xulosa", bullets: ["Natija"], content_blocks: [block], sources: [] }
    ]
  };

  await renderPresentation(deck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  assert.ok(Object.keys(zip.files).some((name) => /^ppt\/media\/image[^/]*\.jpg$/.test(name)));
  const slideXml = await zip.file("ppt/slides/slide2.xml").async("string");
  assert.doesNotMatch(slideXml, /PRESENTATION AI/);
  assert.match(slideXml, /AKADEMIK TAQDIMOT/);
});

test("renderer rejects vulnerable ICNS/JXL/HEIF parsers on direct calls", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-hostile-image-"));
  const hostile = path.join(dir, "hostile.bin");
  await writeFile(hostile, Buffer.from("icns000000000000", "ascii"));
  const block = {
    heading: "Dalil",
    body: "Tekshirilmagan binary fayl renderer parseriga uzatilmaydi; format signaturasi avval xavfsizlik nazoratidan o‘tadi."
  };
  const deck = {
    title: "Xavfsiz render",
    language: "uz",
    theme,
    plan_code: "PREMIUM_PLUS",
    slides: [
      { title: "Kirish", bullets: ["A"], content_blocks: [block], sources: [] },
      { title: "Tasvir", bullets: [], content_blocks: [block], image_paths: [hostile], sources: [] },
      { title: "Xulosa", bullets: ["B"], content_blocks: [block], sources: [] }
    ]
  };

  await assert.rejects(
    renderPresentation(deck, path.join(dir, "deck.pptx")),
    /unsupported image format/,
  );
});
