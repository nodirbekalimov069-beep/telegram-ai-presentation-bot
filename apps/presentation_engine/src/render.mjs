import { access, open } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import PptxGenJS from "pptxgenjs";

import { validatePresentation } from "./contract.mjs";

const SLIDE_W = 13.333;
const SLIDE_H = 7.5;
const MARGIN = 0.68;
const BODY_PT = 18;
const HISTORICAL_ASSET_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../assets/historical",
);
const HEIF_BRANDS = new Set([
  "avif", "avis", "heic", "heis", "heix", "heim",
  "hevc", "hevx", "mif1", "msf1",
]);

async function assertSafeImageFormat(imagePath) {
  const handle = await open(imagePath, "r");
  try {
    const header = Buffer.alloc(16);
    const { bytesRead } = await handle.read(header, 0, header.length, 0);
    const first = header.subarray(0, bytesRead);
    const vulnerable =
      first.subarray(0, 4).toString("ascii") === "icns" ||
      (first[0] === 0xff && first[1] === 0x0a) ||
      first.subarray(4, 8).toString("ascii") === "JXL " ||
      (first.subarray(4, 8).toString("ascii") === "ftyp" &&
        HEIF_BRANDS.has(first.subarray(8, 12).toString("ascii")));
    if (vulnerable) throw new Error("unsupported image format");
  } finally {
    await handle.close();
  }
}

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
    if (!slide.layout) {
      const bulletWidth = competing ? 48 : 88;
      const visibleTexts = slide.content_blocks?.length
        ? slide.content_blocks.flatMap((block) => [block.heading, block.body])
        : slide.bullets;
      const bulletLines = visibleTexts.reduce(
        (sum, text) => sum + estimateLines(text, bulletWidth),
        0,
      );
      const bulletCapacity = competing ? 18 : 30;
      if (bulletLines > bulletCapacity) {
        throw new Error(
          `Text overflow on slide ${index + 1}: visible content cannot fit readably`,
        );
      }
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

export function selectEditorialLayout(content, index) {
  const title = String(content.title || "").toLocaleLowerCase("uz");
  if (/jarayon|bosqich|usul|saqlash|tarqatish|loyiha|innovats|raqamli/.test(title)) return "process";
  if (/misol|case|tahlil|zafarnoma|temur|jadid/.test(title)) return "case-study";
  if (/manba|arxiv|hujjat|ishonchl|tekshir|tadqiqot/.test(title)) return "source-map";
  if (/farq|taqqos|haqiqat|mif|etika|turlari/.test(title)) return "comparison";
  return index % 2 === 0 ? "editorial-split" : "cards";
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
  slide.addText("AKADEMIK TAQDIMOT", {
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

function addSourceLine(slide, content, theme, dark = false) {
  if (!content.sources?.length) return;
  const labels = content.sources
    .slice(0, 2)
    .map((source) => source.label.replace(/^www\./, ""))
    .join("  •  ");
  slide.addText(`MANBA  •  ${labels}`, {
    x: 3.35,
    y: 7.12,
    w: 5.8,
    h: 0.18,
    align: "center",
    fontFace: theme.typography.body,
    fontSize: 8.5,
    charSpacing: 0.7,
    color: dark ? "A9CFCC" : theme.palette.muted,
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

function addHistoricalArchiveVisual(
  slide,
  theme,
  x,
  y,
  scale = 1,
  labels = ["MANBA", "TALQIN", "NARRATIV"],
) {
  slide.addShape("roundRect", {
    x,
    y,
    w: 4.45 * scale,
    h: 4.1 * scale,
    rectRadius: 0.06,
    fill: { color: theme.palette.dark },
    line: { color: theme.palette.dark },
  });
  slide.addShape("rect", {
    x: x + 0.5 * scale,
    y: y + 0.55 * scale,
    w: 2.25 * scale,
    h: 2.65 * scale,
    rotate: -5,
    fill: { color: "F6E7CA" },
    line: { color: theme.palette.secondary, width: 1.2 },
  });
  slide.addShape("rect", {
    x: x + 0.75 * scale,
    y: y + 0.38 * scale,
    w: 2.25 * scale,
    h: 2.65 * scale,
    rotate: 3,
    fill: { color: "FFF9ED" },
    line: { color: theme.palette.accent, width: 1.2 },
  });
  for (let line = 0; line < 5; line += 1) {
    slide.addShape("line", {
      x: x + 1.02 * scale,
      y: y + (0.92 + line * 0.34) * scale,
      w: (line === 4 ? 1.15 : 1.52) * scale,
      h: 0,
      line: {
        color: line === 0 ? theme.palette.accent : "9B8066",
        width: line === 0 ? 2 : 1,
      },
    });
  }
  slide.addShape("ellipse", {
    x: x + 2.45 * scale,
    y: y + 2.38 * scale,
    w: 0.64 * scale,
    h: 0.64 * scale,
    fill: { color: theme.palette.accent },
    line: { color: "F6D9B7", width: 1.5 },
  });
  labels.forEach((label, index) => {
    const labelY = y + (0.55 + index * 0.78) * scale;
    slide.addText(label, {
      x: x + 3.15 * scale,
      y: labelY,
      w: 1.05 * scale,
      h: 0.22 * scale,
      fontFace: theme.typography.body,
      fontSize: 10.5 * scale,
      bold: true,
      charSpacing: 1.3,
      color: index === 2 ? "FFFFFF" : "D9B78D",
      margin: 0,
    });
    if (index < labels.length - 1) {
      slide.addShape("line", {
        x: x + 3.42 * scale,
        y: labelY + 0.36 * scale,
        w: 0,
        h: 0.28 * scale,
        line: {
          color: theme.palette.secondary,
          width: 1.5,
          beginArrowType: "none",
          endArrowType: "triangle",
        },
      });
    }
  });
}

function contentBlocksFor(content) {
  if (content.content_blocks?.length) return content.content_blocks;
  return content.bullets.map((text) => {
    const [heading, ...rest] = String(text).split(/\s+[—–-]\s+/);
    return {
      heading: rest.length ? heading : String(text),
      body: rest.length ? rest.join(" — ") : "",
    };
  });
}

function curatedHistoricalImages(content, medical) {
  if (content.image_paths?.length) return content.image_paths;
  if (medical) return [];
  const title = String(content.title || "").toLocaleLowerCase("uz");
  if (/zafarnoma/.test(title)) {
    return [path.join(HISTORICAL_ASSET_ROOT, "zafarnama.jpg")];
  }
  if (/temuriy.*qo['’‘]?lyozma|qo['’‘]?lyozma.*temuriy/.test(title)) {
    return [path.join(HISTORICAL_ASSET_ROOT, "timurid-quran.jpg")];
  }
  return [];
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
  const subtitle = contentBlocksFor(content)
    .slice(0, 3)
    .map((block) => block.heading)
    .join("  •  ");
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
  if (medical) {
    addVisualCluster(slide, theme, 8.25, 1.05, 1.05, true);
  } else {
    addHistoricalArchiveVisual(slide, theme, 8.25, 1.2, 1.02);
  }
  addFooter(slide, index, deck.slides.length, theme, true);
}

function addCard(slide, { x, y, w, h, number, block }, theme) {
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
  slide.addText(String(number).padStart(2, "0"), {
    x: x + 0.25,
    y: y + 0.24,
    w: 0.5,
    h: 0.22,
    fontFace: theme.typography.body,
    fontSize: 9.5,
    bold: true,
    charSpacing: 1.1,
    color: number % 3 === 0 ? theme.palette.accent : theme.palette.primary,
    margin: 0,
  });
  slide.addText(block.heading, {
    x: x + 0.25,
    y: y + 0.62,
    w: w - 0.5,
    h: h > 3 ? 0.6 : 0.45,
    fontFace: theme.typography.heading,
    fontSize: h > 3 ? 19 : 16,
    bold: true,
    color: theme.palette.foreground,
    margin: 0,
    fit: "shrink",
  });
  if (block.body) {
    slide.addText(block.body, {
      x: x + 0.25,
      y: y + (h > 3 ? 1.42 : 1.14),
      w: w - 0.5,
      h: h - (h > 3 ? 1.72 : 1.37),
      fontFace: theme.typography.body,
      fontSize: h > 3 ? 15.5 : 13.5,
      color: theme.palette.muted,
      margin: 0,
      valign: "top",
      fit: "shrink",
      breakLine: false,
    });
  }
}

function addCardGrid(slide, blocks, theme) {
  const items = blocks.slice(0, 4);
  const columns = items.length === 4 ? 2 : Math.min(3, items.length);
  const rows = Math.ceil(items.length / columns);
  const gap = 0.28;
  const w = (SLIDE_W - MARGIN * 2 - gap * (columns - 1)) / columns;
  const h = rows === 1 ? 3.75 : 2.18;
  items.forEach((block, index) => {
    addCard(
      slide,
      {
        x: MARGIN + (index % columns) * (w + gap),
        y: 1.72 + Math.floor(index / columns) * (h + gap),
        w,
        h,
        number: index + 1,
        block,
      },
      theme,
    );
  });
}

function addSplitLayout(slide, blocks, theme, medical, mode = "editorial-split") {
  const panelX = 7.78;
  const labels = mode === "case-study"
    ? ["DALIL", "KONTEKST", "XULOSA"]
    : ["MANBA", "TALQIN", "NARRATIV"];
  if (medical) {
    slide.addShape("roundRect", {
      x: panelX,
      y: 1.58,
      w: 4.85,
      h: 4.9,
      rectRadius: 0.08,
      fill: { color: theme.palette.dark },
      line: { color: theme.palette.dark },
    });
    addVisualCluster(slide, theme, panelX + 0.3, 1.72, 0.82, true);
  } else {
    addHistoricalArchiveVisual(slide, theme, panelX + 0.18, 1.72, 1.02, labels);
  }
  const items = blocks.slice(0, 3);
  items.forEach((block, index) => {
    const y = 1.66 + index * 1.56;
    slide.addText(String(index + 1).padStart(2, "0"), {
      x: MARGIN,
      y,
      w: 0.45,
      h: 0.22,
      fontFace: theme.typography.body,
      fontSize: 9.5,
      bold: true,
      charSpacing: 1.1,
      color: theme.palette.primary,
      margin: 0,
    });
    slide.addText(block.heading, {
      x: MARGIN + 0.62,
      y: y - 0.03,
      w: 6.18,
      h: 0.36,
      fontFace: theme.typography.heading,
      fontSize: 17.5,
      bold: true,
      color: theme.palette.foreground,
      margin: 0,
      fit: "shrink",
    });
    slide.addText(block.body, {
      x: MARGIN + 0.62,
      y: y + 0.45,
      w: 6.18,
      h: 0.72,
      fontFace: theme.typography.body,
      fontSize: 14.5,
      color: theme.palette.muted,
      margin: 0,
      valign: "top",
      fit: "shrink",
    });
    if (index < items.length - 1) {
      slide.addShape("line", {
        x: MARGIN + 0.62,
        y: y + 1.28,
        w: 6.05,
        h: 0,
        line: { color: theme.palette.pale, width: 1 },
      });
    }
  });
}

function addProcessFromBlocks(slide, blocks, theme) {
  const items = blocks.slice(0, 4);
  const startX = 0.92;
  const totalW = 11.45;
  const itemW = totalW / items.length;
  slide.addShape("line", {
    x: startX + 0.4,
    y: 3.02,
    w: totalW - 0.8,
    h: 0,
    line: { color: theme.palette.secondary, width: 3 },
  });
  items.forEach((block, index) => {
    const center = startX + index * itemW + itemW / 2;
    slide.addShape("ellipse", {
      x: center - 0.33,
      y: 2.69,
      w: 0.66,
      h: 0.66,
      fill: { color: index === items.length - 1 ? theme.palette.accent : theme.palette.primary },
      line: { color: "FFFFFF", width: 2 },
    });
    slide.addText(String(index + 1), {
      x: center - 0.25,
      y: 2.9,
      w: 0.5,
      h: 0.18,
      align: "center",
      fontFace: theme.typography.body,
      fontSize: 10.5,
      bold: true,
      color: "FFFFFF",
      margin: 0,
    });
    const top = index % 2 === 0;
    const headingY = top ? 1.64 : 3.68;
    slide.addText(block.heading, {
      x: startX + index * itemW + 0.08,
      y: headingY,
      w: itemW - 0.16,
      h: 0.44,
      align: "center",
      fontFace: theme.typography.heading,
      fontSize: 16,
      bold: true,
      color: theme.palette.foreground,
      margin: 0,
      fit: "shrink",
    });
    slide.addText(block.body, {
      x: startX + index * itemW + 0.08,
      y: headingY + 0.54,
      w: itemW - 0.16,
      h: 0.82,
      align: "center",
      fontFace: theme.typography.body,
      fontSize: 12.8,
      color: theme.palette.muted,
      margin: 0,
      valign: "top",
      fit: "shrink",
    });
  });
}

function addTable(slide, table, theme, options = {}) {
  const {
    x = 4.82,
    y = 1.72,
    w = 7.83,
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

function addChart(slide, chart, theme, options = {}) {
  const { x = 5.12, y = 1.58, w = 7.53, h = 4.92 } = options;
  const chartType = { bar: "bar", line: "line", pie: "pie" }[chart.type];
  const data = chart.series.map((series) => ({
    name: series.name,
    labels: chart.categories,
    values: series.values,
  }));
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
  slide.addChart(chartType, data, {
    x: x + 0.36,
    y: y + 0.37,
    w: w - 0.68,
    h: h - 0.72,
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

function addSideInsights(slide, content, theme) {
  contentBlocksFor(content).slice(0, 3).forEach((block, index) => {
    const y = 1.65 + index * 1.52;
    slide.addShape("roundRect", {
      x: MARGIN,
      y,
      w: 3.95,
      h: 1.27,
      rectRadius: 0.05,
      fill: { color: index === 0 ? theme.palette.dark : theme.palette.surface },
      line: { color: index === 0 ? theme.palette.dark : theme.palette.pale },
    });
    slide.addText(block.heading, {
      x: MARGIN + 0.24,
      y: y + 0.2,
      w: 3.47,
      h: 0.28,
      fontFace: theme.typography.heading,
      fontSize: 14.5,
      bold: true,
      color: index === 0 ? "FFFFFF" : theme.palette.foreground,
      margin: 0,
      fit: "shrink",
    });
    slide.addText(block.body, {
      x: MARGIN + 0.24,
      y: y + 0.56,
      w: 3.47,
      h: 0.5,
      fontFace: theme.typography.body,
      fontSize: 10.8,
      color: index === 0 ? "E9F1F0" : theme.palette.muted,
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

function addLayoutTakeaway(slide, content, theme) {
  const block = contentBlocksFor(content)[0];
  if (!block?.body) return;
  slide.addShape("roundRect", {
    x: 1.18,
    y: 5.18,
    w: 10.98,
    h: 0.88,
    rectRadius: 0.06,
    fill: { color: theme.palette.dark },
    line: { color: theme.palette.dark },
  });
  slide.addText(block.heading.toLocaleUpperCase("uz"), {
    x: 1.45,
    y: 5.38,
    w: 2.25,
    h: 0.2,
    fontFace: theme.typography.body,
    fontSize: 10,
    bold: true,
    charSpacing: 1.1,
    color: theme.palette.secondary,
    margin: 0,
  });
  slide.addText(block.body, {
    x: 3.65,
    y: 5.31,
    w: 8.15,
    h: 0.38,
    fontFace: theme.typography.body,
    fontSize: 13.5,
    color: "FFFFFF",
    margin: 0,
    fit: "shrink",
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
  await Promise.all(
    imagePaths.map(async (imagePath) => {
      await access(imagePath);
      await assertSafeImageFormat(imagePath);
    }),
  );
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
  contentBlocksFor(content).slice(0, 3).forEach((block, bulletIndex) => {
    slide.addShape("roundRect", {
      x: MARGIN,
      y: 2.55 + bulletIndex * 1.12,
      w: 7.25,
      h: 0.9,
      rectRadius: 0.05,
      fill: { color: theme.palette.primary },
      line: { color: theme.palette.secondary },
    });
    slide.addText(`0${bulletIndex + 1}  ${block.heading}`, {
      x: MARGIN + 0.25,
      y: 2.68 + bulletIndex * 1.12,
      w: 6.7,
      h: 0.24,
      fontFace: theme.typography.heading,
      fontSize: 14.5,
      bold: true,
      color: "FFFFFF",
      margin: 0,
      fit: "shrink",
    });
    slide.addText(block.body, {
      x: MARGIN + 0.65,
      y: 3.0 + bulletIndex * 1.12,
      w: 6.2,
      h: 0.3,
      fontFace: theme.typography.body,
      fontSize: 11.5,
      color: "F4E8DA",
      margin: 0,
      fit: "shrink",
    });
  });
  if (medical) {
    addVisualCluster(slide, theme, 8.55, 1.3, 0.92, true);
  } else {
    addHistoricalArchiveVisual(
      slide,
      theme,
      8.2,
      1.45,
      0.95,
      ["DALIL", "METOD", "NATIJA"],
    );
  }
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
    const editorialImages = curatedHistoricalImages(content, medical);
    const explicitRole = content.role;
    const canBeCover =
      (explicitRole === "cover" || (!explicitRole && index === 0)) &&
      !content.table &&
      !content.chart &&
      !content.layout &&
      !editorialImages.length;
    const isConclusion =
      (explicitRole === "conclusion" ||
        (!explicitRole && index === deck.slides.length - 1)) &&
      deck.slides.length > 2;

    if (canBeCover) {
      addCover(slide, content, deck, theme, index, medical);
    } else if (isConclusion) {
      addConclusion(slide, content, deck, theme, index, medical);
    } else {
      slide.background = { color: theme.palette.background };
      addHeader(slide, content.title, theme, index, sectionLabel);
      if (editorialImages.length) {
        addSideInsights(slide, content, theme);
        await addImages(slide, editorialImages, theme);
      } else if (content.chart && content.table) {
        addSideInsights(slide, content, theme);
        addChart(slide, content.chart, theme, {
          x: 5.12,
          y: 1.58,
          w: 7.53,
          h: 2.55,
        });
        addTable(slide, content.table, theme, {
          x: 5.12,
          y: 4.33,
          w: 7.53,
          h: 2.12,
          fontSize: 11.5,
          rowH: 0.32,
        });
      } else if (content.chart) {
        addSideInsights(slide, content, theme);
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
        addSideInsights(slide, content, theme);
        addTable(slide, content.table, theme);
      } else if (content.layout?.type === "swot") {
        addSwot(slide, content.layout, theme);
      } else if (content.layout) {
        addSequence(slide, content.layout, theme);
        addLayoutTakeaway(slide, content, theme);
      } else {
        const blocks = contentBlocksFor(content);
        const editorialLayout = selectEditorialLayout(content, index);
        if (["cards", "comparison"].includes(editorialLayout)) {
          addCardGrid(slide, blocks, theme);
        } else if (editorialLayout === "process") {
          addProcessFromBlocks(slide, blocks, theme);
        } else {
          addSplitLayout(slide, blocks, theme, medical, editorialLayout);
        }
      }
      addFooter(slide, index, deck.slides.length, theme);
    }

    addSourceLine(slide, content, theme, canBeCover || isConclusion);

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
