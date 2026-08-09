import { z } from "zod";

const hex = z.string().regex(/^[0-9A-Fa-f]{6}$/, "must be a six-digit RGB hex value");
const nonEmpty = z.string().trim().min(1);

const sourceSchema = z.object({
  label: nonEmpty,
  url: z.string().url()
}).strict();

const tableSchema = z.object({
  headers: z.array(nonEmpty).min(1).max(8),
  rows: z.array(z.array(z.union([z.string(), z.number(), z.boolean()])).min(1).max(8)).min(1).max(12)
}).strict().superRefine((table, context) => {
  table.rows.forEach((row, index) => {
    if (row.length !== table.headers.length) {
      context.addIssue({ code: "custom", path: ["rows", index], message: "row width must match headers" });
    }
  });
});

const chartSchema = z.object({
  type: z.enum(["bar", "line", "pie"]),
  categories: z.array(nonEmpty).min(1).max(12),
  series: z.array(z.object({
    name: nonEmpty,
    values: z.array(z.number().finite()).min(1).max(12)
  }).strict()).min(1).max(6)
}).strict().superRefine((chart, context) => {
  chart.series.forEach((series, index) => {
    if (series.values.length !== chart.categories.length) {
      context.addIssue({ code: "custom", path: ["series", index, "values"], message: "values length must match categories" });
    }
  });
  if (chart.type === "pie" && chart.series.length !== 1) {
    context.addIssue({ code: "custom", path: ["series"], message: "pie charts require exactly one series" });
  }
});

const layoutItem = z.object({ label: nonEmpty.max(60), description: z.string().trim().max(180).optional() }).strict();
const contentBlockSchema = z.object({
  heading: nonEmpty.max(90),
  body: nonEmpty.max(420)
}).strict();
const layoutSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("timeline"), items: z.array(layoutItem).min(2).max(6) }).strict(),
  z.object({ type: z.literal("process"), items: z.array(layoutItem).min(2).max(6) }).strict(),
  z.object({
    type: z.literal("swot"),
    strengths: z.array(nonEmpty).min(1).max(5),
    weaknesses: z.array(nonEmpty).min(1).max(5),
    opportunities: z.array(nonEmpty).min(1).max(5),
    threats: z.array(nonEmpty).min(1).max(5)
  }).strict()
]);

const slideSchema = z.object({
  title: nonEmpty.max(180),
  bullets: z.array(nonEmpty.max(600)).max(12),
  content_blocks: z.array(contentBlockSchema).min(1).max(4).optional(),
  table: tableSchema.optional(),
  chart: chartSchema.optional(),
  layout: layoutSchema.optional(),
  speaker_notes: z.string().max(10000).optional(),
  sources: z.array(sourceSchema).max(30),
  image_paths: z.array(nonEmpty).max(4).optional()
}).strict().superRefine((slide, context) => {
  const hasImages = Boolean(slide.image_paths?.length);
  if (slide.layout && (slide.chart || hasImages)) {
    context.addIssue({ code: "custom", path: ["layout"], message: "layout cannot share a slide with chart or images" });
  }
  if (hasImages && (slide.table || slide.chart)) {
    context.addIssue({ code: "custom", path: ["image_paths"], message: "images cannot share a slide with table or chart" });
  }
  if (slide.table && slide.chart && slide.table.rows.length > 5) {
    context.addIssue({ code: "custom", path: ["table", "rows"], message: "table is limited to five data rows when sharing a slide with a chart" });
  }
  if (slide.table && slide.layout && slide.table.rows.length > 5) {
    context.addIssue({ code: "custom", path: ["table", "rows"], message: "table is limited to five data rows when sharing a slide with a layout" });
  }
});

export const presentationSchema = z.object({
  title: nonEmpty.max(140),
  language: nonEmpty.max(35),
  theme: z.object({
    palette: z.object({
      background: hex,
      foreground: hex,
      primary: hex,
      secondary: hex,
      accent: hex
    }).strict(),
    typography: z.object({ heading: nonEmpty.max(80), body: nonEmpty.max(80) }).strict()
  }).strict(),
  plan_code: z.enum(["FREE_DEMO", "MEDIUM", "PREMIUM", "PREMIUM_PLUS"]),
  slides: z.array(slideSchema).min(1).max(30)
}).strict();

export function validatePresentation(input) {
  return presentationSchema.parse(input);
}
