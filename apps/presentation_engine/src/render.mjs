import { access } from "node:fs/promises";
import path from "node:path";

import PptxGenJS from "pptxgenjs";

import { validatePresentation } from "./contract.mjs";

const SLIDE_W = 13.333;
const SLIDE_H = 7.5;
const MARGIN = 0.55;
const BODY_PT = 18;

function estimateLines(text, charsPerLine) {
  return String(text).split(/\n/).reduce((count, paragraph) => count + Math.max(1, Math.ceil(paragraph.length / charsPerLine)), 0);
}

function assertFits(deck) {
  deck.slides.forEach((slide, index) => {
    if (estimateLines(slide.title, 42) > 2) throw new Error(`Text overflow on slide ${index + 1}: title exceeds two lines at 34pt`);
    const competing = Number(Boolean(slide.table)) + Number(Boolean(slide.chart)) + Number(Boolean(slide.layout)) + Number(Boolean(slide.image_paths?.length));
    const bulletWidth = competing ? 48 : 88;
    const bulletLines = slide.bullets.reduce((sum, bullet) => sum + estimateLines(bullet, bulletWidth), 0);
    const bulletCapacity = competing ? 10 : 18;
    if (bulletLines > bulletCapacity) throw new Error(`Text overflow on slide ${index + 1}: bullets cannot fit at ${BODY_PT}pt`);
    if (slide.table) {
      const cellLines = [...slide.table.headers, ...slide.table.rows.flat()].reduce((sum, cell) => sum + estimateLines(cell, 24), 0);
      if (cellLines > 75) throw new Error(`Text overflow on slide ${index + 1}: table cannot fit at ${BODY_PT}pt`);
    }
    if (slide.layout?.type === "swot") {
      const lines = [slide.layout.strengths, slide.layout.weaknesses, slide.layout.opportunities, slide.layout.threats]
        .flat().reduce((sum, item) => sum + estimateLines(item, 28), 0);
      if (lines > 28) throw new Error(`Text overflow on slide ${index + 1}: SWOT content cannot fit at ${BODY_PT}pt`);
    }
  });
}

function addTitle(slide, title, theme) {
  slide.addText(title, {
    x: MARGIN, y: 0.35, w: SLIDE_W - MARGIN * 2, h: 0.78,
    fontFace: theme.typography.heading, fontSize: 34, bold: true,
    color: theme.palette.foreground, margin: 0, breakLine: false, fit: "shrink"
  });
  slide.addShape("line", { x: MARGIN, y: 1.18, w: SLIDE_W - MARGIN * 2, h: 0, line: { color: theme.palette.primary, width: 2 } });
}

function addBullets(slide, bullets, theme, fullWidth) {
  if (!bullets.length) return;
  const runs = bullets.map((text) => ({ text, options: { bullet: { indent: 18 }, breakLine: true } }));
  slide.addText(runs, {
    x: MARGIN, y: 1.48, w: fullWidth ? 12.23 : 4.55, h: 5.35,
    fontFace: theme.typography.body, fontSize: BODY_PT, color: theme.palette.foreground,
    valign: "top", margin: 0.08, breakLine: false, paraSpaceAfterPt: 10
  });
}

function addTable(slide, table, theme, y = 1.5, h = 2.35) {
  const rows = [table.headers, ...table.rows].map((row) => row.map(String));
  slide.addTable(rows, {
    x: 5.35, y, w: 7.43, h,
    fontFace: theme.typography.body, fontSize: BODY_PT,
    color: theme.palette.foreground, border: { type: "solid", color: theme.palette.primary, pt: 1 },
    fill: theme.palette.background, margin: 0.06,
    bold: false, rowH: 0.38,
    autoFit: false
  });
}

function addChart(slide, chart, theme, y = 4.05, h = 2.65) {
  const chartType = { bar: "bar", line: "line", pie: "pie" }[chart.type];
  const data = chart.series.map((series) => ({ name: series.name, labels: chart.categories, values: series.values }));
  slide.addChart(chartType, data, {
    x: 5.35, y, w: 7.43, h,
    showLegend: true, legendColor: theme.palette.foreground,
    legendFontFace: theme.typography.body, legendFontSize: 12,
    showTitle: false, showValue: chart.type === "pie",
    chartColors: [theme.palette.primary, theme.palette.secondary, theme.palette.accent],
    catAxisLabelColor: theme.palette.foreground,
    catAxisLabelFontFace: theme.typography.body, catAxisLabelFontSize: 12,
    catAxisLineColor: theme.palette.foreground,
    valAxisLabelColor: theme.palette.foreground,
    valAxisLabelFontFace: theme.typography.body, valAxisLabelFontSize: 12,
    valAxisLineColor: theme.palette.foreground,
    showCatName: chart.type === "pie", showPercent: chart.type === "pie",
    border: { color: theme.palette.primary, pt: 1 }
  });
}

function addSequence(slide, layout, theme) {
  const x = 5.35;
  const w = 7.43;
  const itemW = w / layout.items.length;
  const y = layout.type === "timeline" ? 2.65 : 2.05;
  if (layout.type === "timeline") slide.addShape("line", { x: x + 0.35, y: y + 0.26, w: w - 0.7, h: 0, line: { color: theme.palette.secondary, width: 3 } });
  layout.items.forEach((item, index) => {
    const segmentX = x + index * itemW;
    const itemX = layout.type === "timeline" ? segmentX + itemW / 2 - 0.26 : segmentX + 0.08;
    const textX = layout.type === "timeline" ? segmentX + 0.05 : itemX;
    const textW = layout.type === "timeline" ? itemW - 0.1 : itemW - 0.16;
    slide.addShape(layout.type === "timeline" ? "ellipse" : "roundRect", {
      x: itemX, y, w: layout.type === "timeline" ? 0.52 : itemW - 0.16, h: layout.type === "timeline" ? 0.52 : 0.8,
      fill: { color: theme.palette.primary }, line: { color: theme.palette.primary }, radius: 0.08
    });
    slide.addText(layout.type === "timeline" ? item.label : `${index + 1}. ${item.label}`, {
      x: textX, y: y + (layout.type === "timeline" ? 0.65 : 0.12),
      w: textW, h: 0.42,
      align: "center", fontFace: theme.typography.heading, fontSize: BODY_PT, bold: true, color: theme.palette.foreground, margin: 0
    });
    if (item.description) slide.addText(item.description, {
      x: textX, y: y + (layout.type === "timeline" ? 1.12 : 0.95),
      w: textW, h: 0.85,
      align: "center", valign: "top", fontFace: theme.typography.body, fontSize: BODY_PT, color: theme.palette.foreground, margin: 0.02
    });
  });
}

function addSwot(slide, layout, theme) {
  const groups = [["Strengths", layout.strengths], ["Weaknesses", layout.weaknesses], ["Opportunities", layout.opportunities], ["Threats", layout.threats]];
  groups.forEach(([heading, items], index) => {
    const col = index % 2;
    const row = Math.floor(index / 2);
    const x = 5.35 + col * 3.78;
    const y = 1.48 + row * 2.62;
    slide.addText(heading, { x, y, w: 3.58, h: 0.42, fontFace: theme.typography.heading, fontSize: 20, bold: true, color: theme.palette.primary, margin: 0 });
    slide.addText(items.map((text) => ({ text, options: { bullet: { indent: 16 }, breakLine: true } })), {
      x, y: y + 0.5, w: 3.58, h: 1.82, fontFace: theme.typography.body, fontSize: BODY_PT,
      color: theme.palette.foreground, valign: "top", margin: 0.04
    });
  });
}

async function addImages(slide, imagePaths) {
  if (!imagePaths?.length) return;
  const count = imagePaths.length;
  const w = (7.43 - 0.18 * (count - 1)) / count;
  await Promise.all(imagePaths.map((imagePath) => access(imagePath)));
  imagePaths.forEach((imagePath, index) => slide.addImage({ path: imagePath, x: 5.35 + index * (w + 0.18), y: 1.5, w, h: 2.2 }));
}

function notesFor(slide) {
  const sourceBlock = slide.sources.length
    ? `[Sources]\n${slide.sources.map((source) => `- ${source.label}: ${source.url}`).join("\n")}`
    : "";
  return [slide.speaker_notes, sourceBlock].filter(Boolean).join("\n\n");
}

export async function renderPresentation(input, outputPath) {
  const deck = validatePresentation(input);
  assertFits(deck);
  const pptx = new PptxGenJS();
  pptx.layout = "LAYOUT_WIDE";
  pptx.author = "Telegram AI Presentation Bot";
  pptx.subject = deck.title;
  pptx.title = deck.title;
  pptx.lang = deck.language;
  pptx.theme = {
    headFontFace: deck.theme.typography.heading,
    bodyFontFace: deck.theme.typography.body,
    lang: deck.language
  };

  for (const [index, content] of deck.slides.entries()) {
    const slide = pptx.addSlide();
    slide.background = { color: deck.theme.palette.background };
    addTitle(slide, content.title, deck.theme);
    const hasRightContent = Boolean(content.table || content.chart || content.layout || content.image_paths?.length);
    addBullets(slide, content.bullets, deck.theme, !hasRightContent);
    await addImages(slide, content.image_paths);
    if (content.layout?.type === "swot") addSwot(slide, content.layout, deck.theme);
    else if (content.layout) addSequence(slide, content.layout, deck.theme);
    if (content.table) {
      const tableY = content.layout ? 4.75 : content.chart ? 1.48 : 2.1;
      const tableH = content.layout ? 2.28 : content.chart ? 2.15 : 3.6;
      addTable(slide, content.table, deck.theme, tableY, tableH);
    }
    if (content.chart) addChart(slide, content.chart, deck.theme, content.table ? 4.0 : 1.48, content.table ? 2.75 : 5.2);

    if (["PREMIUM", "PREMIUM_PLUS"].includes(deck.plan_code)) {
      const notes = notesFor(content);
      if (notes) slide.addNotes(notes);
    }
    if (deck.plan_code === "FREE_DEMO" && index === deck.slides.length - 1) {
      slide.addText("Create yours with @presentation_bot", {
        x: 9.5, y: 7.08, w: 3.25, h: 0.2, align: "right",
        fontFace: deck.theme.typography.body, fontSize: 10, color: deck.theme.palette.primary,
        bold: true, margin: 0
      });
    }
  }

  const resolvedOutput = path.resolve(outputPath);
  await pptx.writeFile({ fileName: resolvedOutput });
  return resolvedOutput;
}
