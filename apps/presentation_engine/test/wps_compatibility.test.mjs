import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import JSZip from "jszip";

import { renderPresentation } from "../src/render.mjs";

const historicalDeck = {
  title: "Tarixiy Xikoyalar",
  language: "uz-UZ",
  theme: {
    palette: {
      background: "F5EAD8",
      foreground: "3D2B1F",
      primary: "8B5E3C",
      secondary: "C89F72",
      accent: "B66A3C",
    },
    typography: { heading: "Cambria", body: "Arial" },
  },
  plan_code: "PREMIUM_PLUS",
  slides: [
    {
      title: "Tarixiy Xikoyalarga Kirish",
      bullets: ["Tarixiy xikoya nima?", "Ularning ahamiyati"],
      sources: [],
    },
    {
      title: "Tarixiy Xikoyalarning Turlari",
      bullets: ["Afsonalar", "Qissalar", "Hikoyalar"],
      sources: [],
    },
    {
      title: "Xulosa va Tavsiyalar",
      bullets: ["Tarixiy xikoyalarning ahamiyati", "Oliy ta'limda qo'llanilishi"],
      sources: [],
    },
  ],
};

test("historical decks use generic labels and WPS-safe solid conclusion fills", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "presentation-engine-wps-"));
  const output = path.join(dir, "historical.pptx");

  await renderPresentation(historicalDeck, output);
  const zip = await JSZip.loadAsync(await readFile(output));
  const coverXml = await zip.file("ppt/slides/slide1.xml").async("string");
  const bodyXml = await zip.file("ppt/slides/slide2.xml").async("string");
  const conclusionXml = await zip.file("ppt/slides/slide3.xml").async("string");

  assert.match(coverXml, /AKADEMIK TAQDIMOT/);
  assert.match(bodyXml, /AKADEMIK/);
  assert.doesNotMatch(`${coverXml}${bodyXml}${conclusionXml}`, /ONKOLOGIYA|HUJAYRA/);
  assert.doesNotMatch(conclusionXml, /<a:alpha\b/);
  assert.match(conclusionXml, /Xulosa va Tavsiyalar/);
});
