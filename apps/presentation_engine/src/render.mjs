import { access } from "node:fs/promises";
import path from "node:path";

import PptxGenJS from "pptxgenjs";

import { validatePresentation } from "./contract.mjs";

const SLIDE_W = 13.333;
const SLIDE_H = 7.5;
const MARGIN = 0.68;
const BODY_PT = 18;

function estimateLines(text, charsPerLine) {
  return String(text)
    .split(/\n/)
    .reduce(
      (count, paragraph) =>
        count + Math.max(1, Math.ceil(paragraph.length / charsPerLine)),
      0,
    );
}

function assertFits(deck) {
  deck.slides.forEach((slide, index) => {
    if (estimateLines(slide.title, 42) > 2) {
      throw new Error(
        `Text overflow on slide ${index + 1}: title exceeds two lines at 34pt`,
      );
    }
    const competing =
      Number(Boolean(slide.table)) +
      Number(Boolean(slide.chart)) +
      Number(Boolean(slide.layout)) +
      Number(Boolean(slide.image_paths?.length));
    const bulletWidth = competing ? 48 : 88;
    const bulletLines = slide.bullets.reduce(
      (sum, bullet) => sum + estimateLines(bullet, bulletWidth),
      0,
    );
    const bulletCapacity = competing ? 10 : 18;
    if (bulletLines > bulletCapacity) {
      throw new Error(
        `Text overflow on slide ${index + 1}: bullets cannot fit at ${BODY_PT}pt`,
      );
    }
    if (slide.table) {
      const cellLines = [
        ...slide.table.headers,
        ...slide.table.rows.flat(),
      ].reduce((sum, cell) => sum + estimateLines(cell, 24), 0);
      if (cellLines > 75) {
        throw new Error(
          `Text overflow on slide ${index + 1}: table cannot fit at ${BODY_PT}pt`,
        );
      }
    }
  });
}

function isMedicalDeck(deck) {
  const corpus = [deck.title, ...deck.slides.map((slide) => slide.title)].join(
    " ",
  );
  return /o['’‘]?sma|saraton|tibb|onkolog|cancer|tumou?r|diagnost|terapi/i.test(
    corpus,
  );
}

function visualTheme(deck) {
  if (!isMedicalDeck(deck)) {
    return {
      ...deck.theme,
      palette: {
        ...deck.theme.palette,
        dark: deck.theme.palette.foreground,
        surface: "FFFFFF",
        muted: "64748B",
        pale: "E8EEF3",
      },
    };
  }
  return {
    typography: { heading: "Cambria", body: "Arial" },
    palette: {
      background: "F3F8F8",
      foreground: "102E33",
      primary: "087F75",
      secondary: "59C3B8",
      accent: "E06B5F",
      dark: "082F35",
      surface: "FFFFFF",
      muted: "5F777A",
      pale: "DCEDEB",
    },
  };
}

function softShadow() {
  return {
    type: "outer",
    color: "183B3F",
    opacity: 0.13,
    blur: 2,
    angle: 45,
    distance: 1.2,
  };
}

function addFooter(slide, index, total, theme, dark = false) {
  const color = dark ? "A9CFCC" : theme.palette.muted;
  slide.addText("PRESENTATION AI", {
    x: MARGIN,
    y: 7.12,
    w: 2.4,
    h: 0.18,
    fontFace: theme.typography.body,
    fontSize: 9,
    bold: true,
    charSpacing: 1.5,
    color,
    margin: 0,
  });
  slide.addText(`${String(index + 1).padStart(2, "0")} / ${String(total).padStart(2, "0")}`, {
    x: 11.45,
    y: 7.1,
    w: 1.2,
    h: 0.2,
    align: "right",
    fontFace: theme.typography.body,
    fontSize: 10,
    color,
    margin: 0,
  });
}

function addHeader(slide, title, theme, index, sectionLabel) {
  slide.addText(`${sectionLabel}  •  ${String(index + 1).padStart(2, "0")}`, {
    x: MARGIN,
    y: 0.38,
    w: 3.4,
    h: 0.18,
    fontFace: theme.typography.body,
    fontSize: 10,
    bold: true,
    charSpacing: 1.7,
    color: theme.palette.primary,
    margin: 0,
  });
  slide.addText(title, {
    x: MARGIN,
    y: 0.68,
    w: 11.95,
    h: 0.72,
    fontFace: theme.typography.heading,
    fontSize: 29,
    bold: true,
    color: theme.palette.foreground,
    margin: 0,
    breakLine: false,
    fit: "shrink",
  });
}

function addVisualCluster(slide, theme, x, y, scale = 1, medical = false) {
  const cells = [
    [1.55, 1.25, 1.45, theme.palette.secondary],
    [0.25, 1.05, 1.05, theme.palette.primary],
    [2.72, 0.35, 0.88, theme.palette.accent],
    [3.05, 1.85, 1.15, theme.palette.secondary],
    [1.15, 2.65, 0.72, theme.palette.accent],
    [2.15, 2.85, 0.52, theme.palette.primary],
  ];
  for (const [dx, dy, size, color] of cells) {
    const filled = medical || size < 0.8;
    slide.addShape("ellipse", {
      x: x + dx * scale,
      y: y + dy * scale,
      w: size * scale,
      h: size * scale,
      fill: { color: filled ? color : theme.palette.dark },
      line: { color, width: 1.5 },
    });
    if (medical) {
      slide.addShape("ellipse", {
        x: x + (dx + size * 0.34) * scale,
        y: y + (dy + size * 0.34) * scale,
        w: size * 0.32 * scale,
        h: size * 0.32 * scale,
        fill: { color: theme.palette.dark },
        line: { color: theme.palette.dark },
      });
    }
  }
}

function addCover(slide, content, deck, theme, index, medical) {
  slide.background = { color: theme.palette.dark };
  slide.addShape("ellipse", {
    x: -1.25,
    y: -2.05,
    w: 6.2,
    h: 6.2,
    fill: { color: theme.palette.primary },
    line: { color: theme.palette.primary },
  });
  slide.addText(medical ? "TIBBIY TAQDIMOT" : "AKADEMIK TAQDIMOT", {
    x: 0.82,
    y: 1.1,
    w: 4.2,
    h: 0.24,
    fontFace: theme.typography.body,
    fontSize: 11,
    bold: true,
    charSpacing: 2.8,
    color: theme.palette.secondary,
    margin: 0,
  });
  slide.addText(deck.title, {
    x: 0.82,
    y: 1.55,
    w: 7.2,
    h: 1.55,
    fontFace: theme.typography.heading,
    fontSize: 42,
    bold: true,
    color: "FFFFFF",
    margin: 0,
    fit: "shrink",
    valign: "mid",
  });
  const subtitle = content.bullets.slice(0, 3).join("  •  ");
  slide.addText(subtitle, {
    x: 0.86,
    y: 3.35,
    w: 6.75,
    h: 0.9,
    fontFace: theme.typography.body,
    fontSize: 16,
    color: "C9E3E1",
    margin: 0,
    breakLine: false,
    fit: "shrink",
  });
  slide.addShape("roundRect", {
    x: 0.82,
    y: 5.55,
    w: 3.15,
    h: 0.55,
    rectRadius: 0.08,
    fill: { color: theme.palette.accent },
    line: { color: theme.palette.accent },
  });
  slide.addText(`${deck.slides.length} SLAYD  •  AKADEMIK`, {
    x: 1.02,
    y: 5.72,
    w: 2.75,
    h: 0.18,
    align: "center",
    fontFace: theme.typography.body,
    fontSize: 10,
    bold: true,
    charSpacing: 1.25,
    color: "FFFFFF",
    margin: 0,
  });
  addVisualCluster(slide, theme, 8.25, 1.05, 1.05, medical);
  addFooter(slide, index, deck.slides.length, theme, true);
}

function addCard(slide, { x, y, w, h, number, text }, theme) {
  slide.addShape("roundRect", {
    x,
    y,
    w,
    h,
    rectRadius: 0.07,
    fill: { color: theme.palette.surface },
    line: { color: theme.palette.pale, width: 1 },
    shadow: softShadow(),
  });
  slide.addShape("ellipse", {
    x: x + 0.25,
    y: y + 0.25,
    w: 0.5,
    h: 0.5,
    fill: { color: number % 3 === 0 ? theme.palette.accent : theme.palette.primary },
    line: { color: number % 3 === 0 ? theme.palette.accent : theme.palette.primary },
  });
  slide.addText(String(number).padStart(2, "0"), {
    x: x + 0.25,
    y: y + 0.405,
    w: 0.5,
    h: 0.14,
    align: "center",
    fontFace: theme.typography.body,
    fontSize: 9,
    bold: true,
    color: "FFFFFF",
    margin: 0,
  });
  slide.addText(text, {
    x: x + 0.25,
    y: y + 0.98,
    w: w - 0.5,
    h: h - 1.18,
    fontFace: theme.typography.body,
    fontSize: 17,
    bold: true,
    color: theme.palette.foreground,
    margin: 0,
    valign: "top",
    fit: "shrink",
  });
}

function addCardGrid(slide, bullets, theme) {
  const items = bullets.slice(0, 6);
  const columns = items.length <= 3 ? items.length : 3;
  const rows = Math.ceil(items.length / columns);
  const gap = 0.28;
  const w = (SLIDE_W - MARGIN * 2 - gap * (columns - 1)) / columns;
  const h = rows === 1 ? 3.55 : 2.15;
  items.forEach((text, index) => {
    addCard(
      slide,
      {
        x: MARGIN + (index % columns) * (w + gap),
        y: 1.75 + Math.floor(index / columns) * (h + gap),
        w,
        h,
        number: index + 1,
        text,
      },
      theme,
    );
  });
}

function addSplitLayout(slide, bullets, theme, medical) {
  const panelX = 7.52;
  slide.addShape("roundRect", {
    x: panelX,
    y: 1.58,
    w: 5.12,
    h: 4.9,
    rectRadius: 0.08,
    fill: { color: theme.palette.dark },
    line: { color: theme.palette.dark },
    shadow: softShadow(),
  });
  slide.addText(medical ? "HUJAYRA\nMIQYOSIDA" : "ASOSIY\nG'OYA", {
    x: panelX + 0.5,
    y: 5.35,
    w: 2.1,
    h: 0.56,
    fontFace: theme.typography.body,
    fontSize: 11,
    bold: true,
    charSpacing: 1.5,
    color: "BFE0DD",
    margin: 0,
  });
  addVisualCluster(slide, theme, panelX + 0.38, 1.75, 0.86, medical);
  bullets.slice(0, 4).forEach((text, index) => {
    slide.addShape("roundRect", {
      x: MARGIN,
      y: 1.63 + index * 1.12,
      w: 6.3,
      h: 0.88,
      rectRadius: 0.05,
      fill: { color: index === 0 ? theme.palette.pale : theme.palette.surface },
      line: { color: theme.palette.pale, width: 1 },
    });
    slide.addText(`${String(index + 1).padStart(2, "0")}  ${text}`, {
      x: MARGIN + 0.28,
      y: 1.91 + index * 1.12,
      w: 5.7,
      h: 0.25,
      fontFace: theme.typography.body,
      fontSize: 16,
      bold: index === 0,
      color: theme.palette.foreground,
      margin: 0,
      fit: "shrink",
    });
  });
}

function addProcessFromBullets(slide, bullets, theme) {
  const items = bullets.slice(0, 5);
  const startX = 0.92;
  const totalW = 11.45;
  const itemW = totalW / items.length;
  slide.addShape("line", {
    x: startX + 0.4,
    y: 3.1,
    w: totalW - 0.8,
    h: 0,
    line: { color: theme.palette.secondary, width: 4 },
  });
  items.forEach((text, index) => {
    const center = startX + index * itemW + itemW / 2;
    slide.addShape("ellipse", {
      x: center - 0.38,
      y: 2.72,
      w: 0.76,
      h: 0.76,
      fill: { color: index === items.length - 1 ? theme.palette.accent : theme.palette.primary },
      line: { color: "FFFFFF", width: 2 },
      shadow: softShadow(),
    });
    slide.addText(String(index + 1), {
      x: center - 0.28,
      y: 2.96,
      w: 0.56,
      h: 0.18,
      align: "center",
      fontFace: theme.typography.body,
      fontSize: 11,
      bold: true,
      color: "FFFFFF",
      margin: 0,
    });
    slide.addText(text, {
      x: startX + index * itemW + 0.05,
      y: index % 2 === 0 ? 1.67 : 3.78,
      w: itemW - 0.1,
      h: 1.1,
      align: "center",
      valign: index % 2 === 0 ? "bottom" : "top",
      fontFace: theme.typography.body,
      fontSize: 15,
      bold: true,
      color: theme.palette.foreground,
      margin: 0.02,
      fit: "shrink",
    });
  });
}

function addTable(slide, table, theme, options = {}) {
  const {
    x = 4.35,
    y = 1.72,
    w = 8.3,
    h = 4.75,
    fontSize = 15,
    rowH = 0.62,
  } = options;
  const rows = [
    table.headers.map((text) => ({
      text: String(text),
      options: {
        bold: true,
        color: "FFFFFF",
        fill: theme.palette.primary,
        valign: "mid",
      },
    })),
    ...table.rows.map((row, rowIndex) =>
      row.map((text) => ({
        text: String(text),
        options: {
          fill: rowIndex % 2 === 0 ? "FFFFFF" : theme.palette.background,
          color: theme.palette.foreground,
          valign: "mid",
        },
      })),
    ),
  ];
  slide.addTable(rows, {
    x,
    y,
    w,
    h,
    fontFace: theme.typography.body,
    fontSize,
    border: { type: "solid", color: theme.palette.pale, pt: 1 },
    margin: 0.09,
    rowH,
    autoFit: false,
  });
}

function addChart(slide, chart, theme) {
  const chartType = { bar: "bar", line: "line", pie: "pie" }[chart.type];
  const data = chart.series.map((series) => ({
    name: series.name,
    labels: chart.categories,
    values: series.values,
  }));
  slide.addShape("roundRect", {
    x: 5.12,
    y: 1.58,
    w: 7.53,
    h: 4.92,
    rectRadius: 0.07,
    fill: { color: theme.palette.surface },
    line: { color: theme.palette.pale, width: 1 },
    shadow: softShadow(),
  });
  slide.addChart(chartType, data, {
    x: 5.48,
    y: 1.95,
    w: 6.85,
    h: 4.2,
    showLegend: chart.series.length > 1,
    legendColor: theme.palette.foreground,
    legendFontFace: theme.typography.body,
    legendFontSize: 11,
    showTitle: false,
    showValue: true,
    dataLabelPosition: chart.type === "bar" ? "outEnd" : "bestFit",
    dataLabelColor: theme.palette.foreground,
    dataLabelFormatCode: "0",
    chartColors: [
      theme.palette.primary,
      theme.palette.secondary,
      theme.palette.accent,
    ],
    catAxisLabelColor: theme.palette.muted,
    catAxisLabelFontFace: theme.typography.body,
    catAxisLabelFontSize: 11,
    catAxisLineColor: theme.palette.pale,
    valAxisLabelColor: theme.palette.muted,
    valAxisLabelFontFace: theme.typography.body,
    valAxisLabelFontSize: 10,
    valAxisLineColor: theme.palette.pale,
    valGridLine: { color: theme.palette.pale, width: 1 },
    catGridLine: { style: "none" },
    showCatName: chart.type === "pie",
    showPercent: chart.type === "pie",
    showBorder: false,
  });
}

function addSideInsights(slide, bullets, theme) {
  bullets.slice(0, 4).forEach((text, index) => {
    slide.addShape("roundRect", {
      x: MARGIN,
      y: 1.65 + index * 1.13,
      w: 3.95,
      h: 0.88,
      rectRadius: 0.05,
      fill: { color: index === 0 ? theme.palette.dark : theme.palette.surface },
      line: { color: index === 0 ? theme.palette.dark : theme.palette.pale },
    });
    slide.addText(text, {
      x: MARGIN + 0.24,
      y: 1.91 + index * 1.13,
      w: 3.47,
      h: 0.3,
      fontFace: theme.typography.body,
      fontSize: 15,
      bold: true,
      color: index === 0 ? "FFFFFF" : theme.palette.foreground,
      margin: 0,
      fit: "shrink",
    });
  });
}

function addSequence(slide, layout, theme) {
  const items = layout.items;
  const x = MARGIN;
  const w = SLIDE_W - MARGIN * 2;
  const itemW = w / items.length;
  const y = layout.type === "timeline" ? 3.0 : 2.15;
  if (layout.type === "timeline") {
    slide.addShape("line", {
      x: x + 0.5,
      y: y + 0.28,
      w: w - 1,
      h: 0,
      line: { color: theme.palette.secondary, width: 4 },
    });
  }
  items.forEach((item, index) => {
    const segmentX = x + index * itemW;
    const itemX =
      layout.type === "timeline"
        ? segmentX + itemW / 2 - 0.28
        : segmentX + 0.1;
    const textX =
      layout.type === "timeline" ? segmentX + 0.05 : itemX + 0.18;
    const textW =
      layout.type === "timeline" ? itemW - 0.1 : itemW - 0.56;
    slide.addShape(layout.type === "timeline" ? "ellipse" : "roundRect", {
      x: itemX,
      y,
      w: layout.type === "timeline" ? 0.56 : itemW - 0.2,
      h: layout.type === "timeline" ? 0.56 : 1.05,
      fill: {
        color:
          index === items.length - 1
            ? theme.palette.accent
            : theme.palette.primary,
      },
      line: { color: "FFFFFF", width: 1.5 },
      shadow: softShadow(),
    });
    slide.addText(
      layout.type === "timeline" ? item.label : `${index + 1}. ${item.label}`,
      {
        x: textX,
        y: y + (layout.type === "timeline" ? 0.72 : 0.2),
        w: textW,
        h: 0.5,
        align: "center",
        fontFace: theme.typography.heading,
        fontSize: 18,
        bold: true,
        color:
          layout.type === "timeline" ? theme.palette.foreground : "FFFFFF",
        margin: 0,
        fit: "shrink",
      },
    );
    if (item.description) {
      slide.addText(item.description, {
        x: textX,
        y: y + (layout.type === "timeline" ? 1.28 : 1.25),
        w: textW,
        h: 0.92,
        align: "center",
        valign: "top",
        fontFace: theme.typography.body,
        fontSize: 15,
        color: theme.palette.muted,
        margin: 0.02,
        fit: "shrink",
      });
    }
  });
}

function addSwot(slide, layout, theme) {
  const groups = [
    ["Strengths", layout.strengths],
    ["Weaknesses", layout.weaknesses],
    ["Opportunities", layout.opportunities],
    ["Threats", layout.threats],
  ];
  groups.forEach(([heading, items], index) => {
    const col = index % 2;
    const row = Math.floor(index / 2);
    const x = MARGIN + col * 6.15;
    const y = 1.55 + row * 2.5;
    slide.addShape("roundRect", {
      x,
      y,
      w: 5.9,
      h: 2.18,
      rectRadius: 0.06,
      fill: { color: theme.palette.surface },
      line: { color: theme.palette.pale },
      shadow: softShadow(),
    });
    slide.addText(heading, {
      x: x + 0.28,
      y: y + 0.24,
      w: 2.6,
      h: 0.32,
      fontFace: theme.typography.heading,
      fontSize: 19,
      bold: true,
      color: theme.palette.primary,
      margin: 0,
    });
    slide.addText(
      items.map((text) => ({
        text,
        options: { bullet: { indent: 16 }, breakLine: true },
      })),
      {
        x: x + 0.28,
        y: y + 0.72,
        w: 5.3,
        h: 1.2,
        fontFace: theme.typography.body,
        fontSize: 15,
        color: theme.palette.foreground,
        valign: "top",
        margin: 0.03,
      },
    );
  });
}

async function addImages(slide, imagePaths, theme) {
  if (!imagePaths?.length) return;
  await Promise.all(imagePaths.map((imagePath) => access(imagePath)));
  const count = imagePaths.length;
  const gap = 0.22;
  const w = (7.35 - gap * (count - 1)) / count;
  imagePaths.forEach((imagePath, index) => {
    const x = 5.28 + index * (w + gap);
    slide.addShape("roundRect", {
      x: x - 0.04,
      y: 1.58,
      w: w + 0.08,
      h: 4.93,
      rectRadius: 0.06,
      fill: { color: theme.palette.surface },
      line: { color: theme.palette.pale },
      shadow: softShadow(),
    });
    slide.addImage({ path: imagePath, x, y: 1.62, w, h: 4.82 });
  });
}

function addConclusion(slide, content, deck, theme, index, medical) {
  slide.background = { color: theme.palette.dark };
  slide.addText("YAKUNIY XULOSA", {
    x: MARGIN,
    y: 0.62,
    w: 3.8,
    h: 0.22,
    fontFace: theme.typography.body,
    fontSize: 11,
    bold: true,
    charSpacing: 2.2,
    color: theme.palette.secondary,
    margin: 0,
  });
  slide.addText(content.title, {
    x: MARGIN,
    y: 1.08,
    w: 7.4,
    h: 1.1,
    fontFace: theme.typography.heading,
    fontSize: 36,
    bold: true,
    color: "FFFFFF",
    margin: 0,
    fit: "shrink",
  });
  content.bullets.slice(0, 4).forEach((text, bulletIndex) => {
    slide.addShape("roundRect", {
      x: MARGIN,
      y: 2.55 + bulletIndex * 0.92,
      w: 7.25,
      h: 0.68,
      rectRadius: 0.05,
      fill: { color: theme.palette.primary },
      line: { color: theme.palette.secondary },
    });
    slide.addText(`0${bulletIndex + 1}  ${text}`, {
      x: MARGIN + 0.25,
      y: 2.77 + bulletIndex * 0.92,
      w: 6.7,
      h: 0.22,
      fontFace: theme.typography.body,
      fontSize: 15,
      color: "FFFFFF",
      margin: 0,
      fit: "shrink",
    });
  });
  addVisualCluster(slide, theme, 8.55, 1.3, 0.92, medical);
  addFooter(slide, index, deck.slides.length, theme, true);
}

function notesFor(slide) {
  const sourceBlock = slide.sources.length
    ? `[Sources]\n${slide.sources
        .map((source) => `- ${source.label}: ${source.url}`)
        .join("\n")}`
    : "";
  return [slide.speaker_notes, sourceBlock].filter(Boolean).join("\n\n");
}

export async function renderPresentation(input, outputPath) {
  const deck = validatePresentation(input);
  assertFits(deck);
  const medical = isMedicalDeck(deck);
  const sectionLabel = medical ? "ONKOLOGIYA" : "AKADEMIK";
  const theme = visualTheme(deck);
  const pptx = new PptxGenJS();
  pptx.layout = "LAYOUT_WIDE";
  pptx.author = "Telegram AI Presentation Bot";
  pptx.subject = deck.title;
  pptx.title = deck.title;
  pptx.lang = deck.language;
  pptx.theme = {
    headFontFace: theme.typography.heading,
    bodyFontFace: theme.typography.body,
    lang: deck.language,
  };

  for (const [index, content] of deck.slides.entries()) {
    const slide = pptx.addSlide();
    const canBeCover =
      index === 0 &&
      !content.table &&
      !content.chart &&
      !content.layout &&
      !content.image_paths?.length;
    const isConclusion =
      index === deck.slides.length - 1 && deck.slides.length > 2;

    if (canBeCover) {
      addCover(slide, content, deck, theme, index, medical);
    } else if (isConclusion) {
      addConclusion(slide, content, deck, theme, index, medical);
    } else {
      slide.background = { color: theme.palette.background };
      addHeader(slide, content.title, theme, index, sectionLabel);
      if (content.image_paths?.length) {
        addSideInsights(slide, content.bullets, theme);
        await addImages(slide, content.image_paths, theme);
      } else if (content.chart) {
        addSideInsights(slide, content.bullets, theme);
        addChart(slide, content.chart, theme);
      } else if (content.table && content.layout) {
        if (content.layout.type === "swot") {
          addSwot(slide, content.layout, theme);
        } else {
          addSequence(slide, content.layout, theme);
        }
        addTable(slide, content.table, theme, {
          x: 4.1,
          y: 5.18,
          w: 8.55,
          h: 1.28,
          fontSize: 12,
          rowH: 0.34,
        });
      } else if (content.table) {
        addSideInsights(slide, content.bullets, theme);
        addTable(slide, content.table, theme);
      } else if (content.layout?.type === "swot") {
        addSwot(slide, content.layout, theme);
      } else if (content.layout) {
        addSequence(slide, content.layout, theme);
      } else if (index % 3 === 1) {
        addCardGrid(slide, content.bullets, theme);
      } else if (index % 3 === 2) {
        addSplitLayout(slide, content.bullets, theme, medical);
      } else {
        addProcessFromBullets(slide, content.bullets, theme);
      }
      addFooter(slide, index, deck.slides.length, theme);
    }

    if (["PREMIUM", "PREMIUM_PLUS"].includes(deck.plan_code)) {
      const notes = notesFor(content);
      if (notes) slide.addNotes(notes);
    }
    if (deck.plan_code === "FREE_DEMO" && index === deck.slides.length - 1) {
      slide.addText("Telegram AI Presentation Bot", {
        x: 9.5,
        y: 7.08,
        w: 3.25,
        h: 0.2,
        align: "right",
        fontFace: theme.typography.body,
        fontSize: 10,
        color: theme.palette.primary,
        bold: true,
        margin: 0,
      });
    }
  }

  const resolvedOutput = path.resolve(outputPath);
  await pptx.writeFile({ fileName: resolvedOutput });
  return resolvedOutput;
}
