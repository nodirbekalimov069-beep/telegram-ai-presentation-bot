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

test("explicit roles override position and chart-table renders both visuals", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-role-chart-table-"));
  const output = path.join(dir, "deck.pptx");
  const block = {
    heading: "Dalil",
    body: "Diagramma miqdoriy o‘zgarishni, jadval esa ayni ma’lumotning tekshiriladigan tafsilotlarini birga ko‘rsatadi."
  };
  const deck = {
    title: "Role va kombinatsiya",
    language: "uz",
    theme,
    plan_code: "PREMIUM_PLUS",
    slides: [
      { role: "content", title: "Kirish kontenti", bullets: ["A"], content_blocks: [block], sources: [] },
      {
        role: "content",
        title: "Diagramma va jadval",
        bullets: [],
        content_blocks: [block],
        chart: { type: "bar", categories: ["A", "B"], series: [{ name: "Qiymat", values: [2, 3] }] },
        table: { headers: ["Mezon", "Qiymat"], rows: [["A", "2"], ["B", "3"]] },
        sources: []
      },
      { role: "content", title: "Oddiy yakun", bullets: ["B"], content_blocks: [block], sources: [] }
    ]
  };

  await renderPresentation(deck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  const first = await zip.file("ppt/slides/slide1.xml").async("string");
  const second = await zip.file("ppt/slides/slide2.xml").async("string");
  const secondRels = await zip.file("ppt/slides/_rels/slide2.xml.rels").async("string");
  const third = await zip.file("ppt/slides/slide3.xml").async("string");
  assert.doesNotMatch(first, /20 SLAYD/);
  assert.match(first, /AKADEMIK\s+•\s+01/);
  assert.match(second, /Mezon/);
  assert.match(secondRels, /relationships\/chart/);
  assert.doesNotMatch(third, /DALIL.*METOD.*NATIJA/s);
});

test("biochemistry decks use a molecular science motif instead of archival papers", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-biochemistry-"));
  const output = path.join(dir, "deck.pptx");
  const block = {
    heading: "Glyukoza",
    body: "Glyukoza hujayra nafas olishida ATP hosil bo‘lishiga xizmat qiladigan asosiy monosaxaridlardan biridir."
  };
  const deck = {
    title: "Uglevodlar biokimyosi",
    language: "uz",
    theme,
    plan_code: "PREMIUM_PLUS",
    slides: [
      { role: "cover", title: "Uglevodlar", bullets: ["Tuzilish"], content_blocks: [block], sources: [] },
      { role: "content", title: "Glyukozaning tuzilishi", bullets: [], content_blocks: [block, { ...block, heading: "Energiya" }], sources: [] },
      { role: "conclusion", title: "Xulosa", bullets: ["Natija"], content_blocks: [block], sources: [] }
    ]
  };

  await renderPresentation(deck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  const cover = await zip.file("ppt/slides/slide1.xml").async("string");
  const content = await zip.file("ppt/slides/slide2.xml").async("string");
  assert.match(cover, /BIOKIMYO TAQDIMOT/);
  assert.match(cover, /C₆H₁₂O₆/);
  assert.match(content, /BIOKIMYO/);
  assert.doesNotMatch(content, /MANBA.*TALQIN.*NARRATIV/s);
});

test("dark themes keep white surfaces and conclusion text readable", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-dark-contrast-"));
  const output = path.join(dir, "deck.pptx");
  const darkTheme = {
    palette: {
      background: "0F1324",
      foreground: "F8FAFC",
      primary: "7C3AED",
      secondary: "A78BFA",
      accent: "22D3EE"
    },
    typography: { heading: "Aptos Display", body: "Aptos" }
  };
  const deck = {
    title: "Runtime kontrast",
    language: "uz",
    theme: darkTheme,
    plan_code: "PREMIUM_PLUS",
    slides: [
      {
        role: "content",
        title: "Chart va jadval",
        bullets: [],
        content_blocks: [
          { heading: "Dark insight", body: "Qorong‘i kartadagi matn." },
          { heading: "Surface insight", body: "Oq kartadagi matn o‘qilishi kerak." }
        ],
        chart: { type: "bar", categories: ["A"], series: [{ name: "Qiymat", values: [2] }] },
        table: { headers: ["Mezon", "Qiymat"], rows: [["Readable cell", "2"]] },
        sources: [{ label: "example.com", url: "https://example.com/" }]
      },
      {
        role: "content",
        title: "SWOT",
        bullets: [],
        layout: {
          type: "swot",
          strengths: ["Readable bullet"],
          weaknesses: ["B"],
          opportunities: ["C"],
          threats: ["D"]
        },
        sources: []
      },
      {
        role: "conclusion",
        title: "Xulosa",
        bullets: ["Yakun"],
        content_blocks: [{ heading: "Natija", body: "Kontrast saqlanadi." }],
        sources: []
      }
    ]
  };

  await renderPresentation(deck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  const chartSlide = await zip.file("ppt/slides/slide1.xml").async("string");
  const swotSlide = await zip.file("ppt/slides/slide2.xml").async("string");
  const conclusion = await zip.file("ppt/slides/slide3.xml").async("string");
  const shapeFor = (xml, text) => {
    const textIndex = xml.indexOf(`<a:t>${text}</a:t>`);
    assert.ok(textIndex >= 0, `${text} must be rendered`);
    return xml.slice(xml.lastIndexOf("<p:sp>", textIndex), xml.indexOf("</p:sp>", textIndex));
  };

  assert.match(shapeFor(chartSlide, "Surface insight"), /<a:srgbClr val="102E33"\/>/);
  assert.match(shapeFor(chartSlide, "Readable cell"), /<a:srgbClr val="102E33"\/>/);
  assert.match(shapeFor(swotSlide, "Readable bullet"), /<a:srgbClr val="102E33"\/>/);
  assert.match(conclusion, /<a:srgbClr val="0F1324"\/>/);
  assert.match(shapeFor(conclusion, "Xulosa"), /<a:srgbClr val="FFFFFF"\/>/);
});
