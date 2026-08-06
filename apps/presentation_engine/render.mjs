#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import path from "node:path";

import { renderPresentation } from "./src/render.mjs";

const [, , inputArg, outputArg] = process.argv;
if (!inputArg || !outputArg) {
  console.error("Usage: npm run render -- <input.json> <output.pptx>");
  process.exitCode = 2;
} else {
  try {
    const inputPath = path.resolve(inputArg);
    const outputPath = path.resolve(outputArg);
    const payload = JSON.parse(await readFile(inputPath, "utf8"));
    await renderPresentation(payload, outputPath);
    console.log(outputPath);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
