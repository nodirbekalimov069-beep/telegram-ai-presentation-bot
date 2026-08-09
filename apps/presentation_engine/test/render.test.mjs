import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import JSZip from "jszip";

import { renderPresentation } from "../src/render.mjs";

const require = createRequire(import.meta.url);

const validDeck = {
  title: "Quarterly product review",
  language: "en-US",
  theme: {
    palette: {
      background: "F7F4ED",
      foreground: "17202A",
      primary: "006D77",
      secondary: "E29578",
      accent: "FFB703"
    },
    typography: { heading: "Aptos Display", body: "Aptos" }
  },
  plan_code: "PREMIUM",
  slides: [
    {
      title: "Revenue grew across every segment",
      bullets: ["Enterprise led expansion", "Retention remained above target"],
      chart: {
        type: "bar",
        categories: ["Q1", "Q2", "Q3"],
        series: [{ name: "Revenue", values: [12, 18, 25] }]
      },
      speaker_notes: "Emphasize the acceleration in Q3.",
      sources: [{ label: "Finance export", url: "https://example.com/finance" }]
    },
    {
      title: "The launch follows three deliberate stages",
      bullets: [],
      layout: {
        type: "timeline",
        items: [
          { label: "Pilot", description: "Validate" },
          { label: "Launch", description: "Scale" },
          { label: "Optimize", description: "Improve" }
        ]
      },
      table: {
        headers: ["Owner", "Status"],
        rows: [["Product", "Ready"], ["Sales", "Briefed"]]
      },
      sources: []
    }
  ]
};

test("installed image inspection dependency excludes vulnerable parsers", () => {
  const imageSize = require("image-size");
  const enabledTypes = new Set(imageSize.types ?? []);

  for (const vulnerableType of ["icns", "jxl", "heif"]) {
    assert.equal(enabledTypes.has(vulnerableType), false, `${vulnerableType} parser must be absent`);
  }
  assert.throws(
    () => imageSize(Buffer.from("untrusted image bytes")),
    /image dimension inspection is disabled/i,
  );
});

test("renders a real PPTX with one slide XML part per input slide", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-engine-"));
  const output = path.join(dir, "deck.pptx");

  await renderPresentation(validDeck, output);

  const zip = await JSZip.loadAsync(await readFile(output));
  const slides = Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name));
  assert.equal(slides.length, 2);

  const firstSlideXml = await zip.file("ppt/slides/slide1.xml").async("string");
  assert.match(firstSlideXml, /Revenue grew across every segment/);
  assert.ok(zip.file("ppt/charts/chart1.xml"), "chart remains editable PowerPoint chart XML");
  const chartXml = await zip.file("ppt/charts/chart1.xml").async("string");
  assert.match(chartXml, /<a:srgbClr val="17202A"\/>/, "chart labels use theme foreground");
  const timelineXml = await zip.file("ppt/slides/slide2.xml").async("string");
  const pilotPosition = timelineXml.indexOf("<a:t>Pilot</a:t>");
  const pilotShape = timelineXml.slice(timelineXml.lastIndexOf("<p:sp>", pilotPosition), pilotPosition);
  const pilotWidth = Number(pilotShape.match(/<a:ext cx="(\d+)"/)?.[1] ?? 0);
  assert.ok(pilotWidth >= 1_371_600, "timeline labels use at least 1.5 inches of width");
  assert.ok(zip.file("ppt/notesSlides/notesSlide1.xml"), "Premium notes are emitted");
});

test("accepts the database-backed MEDIUM plan code", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-engine-medium-"));
  const output = path.join(dir, "medium.pptx");
  const mediumDeck = structuredClone(validDeck);
  mediumDeck.plan_code = "MEDIUM";
  mediumDeck.slides = [mediumDeck.slides[0]];

  await renderPresentation(mediumDeck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  assert.ok(zip.file("ppt/slides/slide1.xml"));
});

test("free demo watermark does not advertise an unrelated Telegram handle", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-engine-free-demo-"));
  const output = path.join(dir, "free-demo.pptx");
  const freeDemoDeck = structuredClone(validDeck);
  freeDemoDeck.plan_code = "FREE_DEMO";
  freeDemoDeck.slides = [freeDemoDeck.slides[0]];

  await renderPresentation(freeDemoDeck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  const slideXml = await zip.file("ppt/slides/slide1.xml").async("string");
  assert.doesNotMatch(slideXml, /@presentation_bot/);
  assert.doesNotMatch(slideXml, /Created with/);
  assert.match(slideXml, /<a:t>Telegram AI Presentation Bot<\/a:t>/);
});

test("rejects content that would require body text below 18pt", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-engine-overflow-"));
  const output = path.join(dir, "overflow.pptx");
  const overflowing = structuredClone(validDeck);
  overflowing.slides = [{
    title: "Too much content",
    bullets: Array.from({ length: 12 }, (_, index) => `Bullet ${index + 1}: ${"dense content ".repeat(30)}`),
    sources: []
  }];

  await assert.rejects(
    renderPresentation(overflowing, output),
    /overflow/i
  );
});
