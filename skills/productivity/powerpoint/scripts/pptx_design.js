#!/usr/bin/env node
/**
 * Build an art-directed .pptx deck using PptxGenJS.
 *
 * Run via Hermes `terminal`:
 *   node scripts/pptx_design.js spec.json out.pptx
 *
 * Design-spec format (all image paths are local, resolved from the spec):
 * {
 *   "language": "ar" | "en",                 // default: en
 *   "theme": {"accent":"B4482E", "font":"Cairo"},
 *   "title": "Deck metadata",
 *   "slides": [
 *     {"type":"cover", "kicker":"2026", "title":"...", "subtitle":"...",
 *      "image":"hero.jpg", "notes":"..."},
 *     {"type":"cards", "kicker":"01", "title":"...", "summary":"...",
 *      "cards":[{"title":"...", "body":"...", "label":"01"}]},
 *     {"type":"data", "kicker":"02", "title":"...", "summary":"...",
 *      "chart":{"labels":["A","B"], "values":[45,72], "series":"Score"},
 *      "callout":{"title":"...", "body":"..."}},
 *     {"type":"split", "kicker":"03", "title":"...", "summary":"...",
 *      "rtl":{"title":"...", "body":"..."},
 *      "ltr":{"title":"...", "body":"..."}}
 *   ]
 * }
 *
 * The script deliberately has a small slide grammar. It produces a coherent
 * visual system and refuses the text-dump pattern that makes generated decks
 * unreadable. For a supplied company deck, use pptx_from_template.py instead.
 */
'use strict';

const fs = require('fs');
const path = require('path');

let pptxgen;
try {
  pptxgen = require('pptxgenjs');
} catch (error) {
  console.error(JSON.stringify({
    ok: false,
    error: 'PptxGenJS is required for the design path.',
    setup: 'Install it locally in the task workspace: npm install pptxgenjs',
    detail: error.message,
  }));
  process.exit(2);
}

const W = 13.333;
const H = 7.5;
const DEFAULTS = {
  ink: '1B1A18', paper: 'F6F3EE', muted: '6E685F', accent: 'B4482E',
  gold: 'C9922B', blue: '27566B', line: 'D1C4AF', arabicFont: 'Cairo',
  latinFont: 'Aptos',
};

function fail(message) {
  console.error(JSON.stringify({ ok: false, error: message }));
  process.exit(1);
}

function loadSpec(specPath) {
  let spec;
  try {
    spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
  } catch (error) {
    fail(`Cannot read valid JSON spec: ${error.message}`);
  }
  if (!Array.isArray(spec.slides) || spec.slides.length === 0) {
    fail('Spec requires a non-empty slides array.');
  }
  return spec;
}

function makeTextHelpers(isArabic, C) {
  const rtl = {
    fontFace: C.arabicFont, color: C.ink, align: 'right', rtlMode: true,
    margin: 0, fit: 'shrink', valign: 'mid', breakLine: false,
  };
  const ltr = {
    fontFace: C.latinFont, color: C.ink, align: 'left', rtlMode: false,
    margin: 0, fit: 'shrink', valign: 'mid', breakLine: false,
  };
  return {
    rtl,
    ltr,
    addMain(slide, text, opts = {}) { slide.addText(String(text || ''), { ...(isArabic ? rtl : ltr), ...opts }); },
    addArabic(slide, text, opts = {}) { slide.addText(String(text || ''), { ...rtl, ...opts }); },
    addLatin(slide, text, opts = {}) { slide.addText(String(text || ''), { ...ltr, ...opts }); },
  };
}

function resolveAsset(baseDir, asset) {
  if (!asset) return null;
  const resolved = path.resolve(baseDir, asset);
  if (!fs.existsSync(resolved)) fail(`Image asset does not exist: ${asset}`);
  return resolved;
}

function addMaster(pptx, C, T, isArabic) {
  pptx.defineSlideMaster({
    title: 'HERMES_DESIGN_MASTER',
    background: { color: C.paper },
    objects: [
      { line: { x: 0.55, y: 0.38, w: 12.23, h: 0, line: { color: C.line, width: 0.75 } } },
      { text: { text: 'HERMES PRESENTATION QUALITY', options: { ...T.ltr, x: 0.55, y: 7.05, w: 4.5, h: 0.2, fontSize: 6.5, color: C.muted, charSpacing: 1.1 } } },
      { text: { text: isArabic ? 'تجربة تصميم وعرض' : 'Design and presentation test', options: { ...(isArabic ? T.rtl : T.ltr), x: 8.4, y: 7.01, w: 4.35, h: 0.25, fontSize: 7.5, color: C.muted } } },
    ],
    slideNumber: { x: 12.28, y: 0.52, color: C.muted, fontFace: C.latinFont, fontSize: 7 },
  });
}

function addKicker(slide, T, text, isArabic, C) {
  T.addMain(slide, text || '', { x: isArabic ? 7.0 : 0.58, y: 0.72, w: 5.75, h: 0.28, fontSize: 10, color: C.accent, bold: true, charSpacing: 1.0 });
}

function addTitle(slide, T, text, isArabic) {
  T.addMain(slide, text || '', { x: isArabic ? 4.72 : 0.58, y: 1.08, w: 7.98, h: 0.82, fontSize: 30, bold: true, valign: 'mid' });
}

function addSummary(slide, T, text, isArabic, C) {
  if (!text) return;
  T.addMain(slide, text, { x: isArabic ? 5.12 : 0.58, y: 2.04, w: 7.58, h: 0.46, fontSize: 15, color: C.muted, valign: 'top' });
}

function addSource(slide, T, text, isArabic, C) {
  if (!text) return;
  T.addMain(slide, text, { x: isArabic ? 6.3 : 0.58, y: 6.53, w: 6.45, h: 0.23, fontSize: 8, color: C.muted, valign: 'mid' });
}

function addNotes(slide, notes) {
  if (notes) slide.addNotes(String(notes));
}

function buildCover({ slide, item, assetsDir, T, isArabic, C, pptx }) {
  slide.background = { color: C.ink };
  const image = resolveAsset(assetsDir, item.image);
  if (image) {
    slide.addImage({ path: image, x: 0, y: 0, w: W, h: H, sizing: { type: 'cover', x: 0, y: 0, w: W, h: H } });
    slide.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: W, h: H, fill: { color: C.ink, transparency: 53 }, line: { color: C.ink, transparency: 100 } });
  }
  slide.addShape(pptx.ShapeType.arc, { x: -1.2, y: -1.7, w: 7.4, h: 7.4, adjustPoint: 0.25, rotate: 34, line: { color: C.gold, transparency: 25, width: 2 }, fill: { color: C.ink, transparency: 100 } });
  T.addLatin(slide, item.kicker || 'QUALITY PRESENTATION', { x: 0.62, y: 0.65, w: 3.6, h: 0.25, fontSize: 8, color: 'F6F3EE', charSpacing: 1.5, bold: true });
  T.addMain(slide, item.title || '', { x: isArabic ? 3.1 : 0.62, y: 2.1, w: isArabic ? 9.65 : 8.85, h: 0.8, fontSize: 36, color: 'FFFFFF', bold: true, valign: 'mid' });
  T.addMain(slide, item.subtitle || '', { x: isArabic ? 3.1 : 0.62, y: 3.1, w: isArabic ? 9.65 : 8.85, h: 0.46, fontSize: 17, color: 'F4E9DD', valign: 'top' });
  slide.addShape(pptx.ShapeType.line, { x: isArabic ? 9.95 : 0.62, y: 4.05, w: 2.8, h: 0, line: { color: C.gold, width: 2.25 } });
  if (item.caption) T.addMain(slide, item.caption, { x: isArabic ? 7.3 : 0.62, y: 6.42, w: 5.45, h: 0.28, fontSize: 10, color: 'F6F3EE' });
}

function buildCards({ slide, item, T, isArabic, C, pptx }) {
  addKicker(slide, T, item.kicker, isArabic, C);
  addTitle(slide, T, item.title, isArabic);
  addSummary(slide, T, item.summary, isArabic, C);
  const cards = (item.cards || []).slice(0, 3);
  if (!cards.length) fail('A cards slide requires at least one card.');
  const cardW = cards.length === 1 ? 5.0 : cards.length === 2 ? 4.0 : 2.45;
  const gap = cards.length === 1 ? 0 : cards.length === 2 ? 0.32 : 0.33;
  const rowW = cards.length * cardW + (cards.length - 1) * gap;
  const start = (W - rowW) / 2;
  cards.forEach((card, index) => {
    const visualIndex = isArabic ? cards.length - index - 1 : index;
    const x = start + visualIndex * (cardW + gap);
    slide.addShape(pptx.ShapeType.roundRect, { x, y: 3.15, w: cardW, h: 2.32, rectRadius: 0.07, fill: { color: 'FFFFFF' }, line: { color: C.line, width: 0.8 }, shadow: { type: 'outer', color: 'B7AFA5', opacity: 0.12, blur: 1, angle: 45, distance: 1 } });
    slide.addShape(pptx.ShapeType.ellipse, { x: x + cardW - 0.72, y: 3.4, w: 0.42, h: 0.42, fill: { color: index === 1 ? C.gold : C.accent }, line: { color: 'FFFFFF', transparency: 100 } });
    T.addLatin(slide, card.label || String(index + 1).padStart(2, '0'), { x: x + 0.22, y: 3.4, w: 0.9, h: 0.25, fontSize: 9, color: C.muted, bold: true });
    T.addMain(slide, card.title || '', { x: x + 0.22, y: 4.0, w: cardW - 0.45, h: 0.32, fontSize: 16, bold: true, valign: 'mid' });
    T.addMain(slide, card.body || '', { x: x + 0.22, y: 4.5, w: cardW - 0.45, h: 0.61, fontSize: 10, color: C.muted, valign: 'top' });
  });
  addSource(slide, T, item.source, isArabic, C);
}

function buildData({ slide, item, T, isArabic, C, pptx }) {
  addKicker(slide, T, item.kicker, isArabic, C);
  addTitle(slide, T, item.title, isArabic);
  addSummary(slide, T, item.summary, isArabic, C);
  const chart = item.chart || {};
  if (!Array.isArray(chart.labels) || !Array.isArray(chart.values) || chart.labels.length !== chart.values.length) {
    fail('A data slide requires chart.labels and chart.values arrays of the same length.');
  }
  const chartX = isArabic ? 6.25 : 0.58;
  slide.addChart(pptx.ChartType.bar, [{ name: chart.series || 'Series', labels: chart.labels, values: chart.values }], {
    x: chartX, y: 2.72, w: 6.5, h: 3.25,
    catAxisLabelFontFace: isArabic ? C.arabicFont : C.latinFont, catAxisLabelFontSize: 11, catAxisLabelColor: C.ink,
    valAxisLabelFontFace: C.latinFont, valAxisLabelFontSize: 9, valAxisMinVal: chart.min || 0, valAxisMaxVal: chart.max || Math.max(...chart.values),
    valAxisMajorUnit: chart.majorUnit || Math.max(1, Math.ceil(Math.max(...chart.values) / 5)),
    showLegend: false, showTitle: false, showValue: true, dataLabelPosition: 'outEnd', dataLabelColor: C.ink, dataLabelFormatCode: '0',
    chartColors: [C.accent], valGridLine: { color: 'DCD6CD', width: 0.5 }, showBorder: false,
  });
  const callout = item.callout || {};
  const boxX = isArabic ? 0.55 : 7.92;
  slide.addShape(pptx.ShapeType.roundRect, { x: boxX, y: 2.72, w: 4.85, h: 3.25, rectRadius: 0.06, fill: { color: C.blue }, line: { color: C.blue } });
  T.addMain(slide, callout.title || '', { x: boxX + 0.4, y: 3.22, w: 4.05, h: 0.32, fontSize: 15, bold: true, color: 'FFFFFF' });
  T.addMain(slide, callout.body || '', { x: boxX + 0.4, y: 3.86, w: 4.05, h: 1.05, fontSize: 14, color: 'EAE4DA', valign: 'top' });
  addSource(slide, T, item.source, isArabic, C);
}

function buildSplit({ slide, item, T, isArabic, C, pptx }) {
  addKicker(slide, T, item.kicker, isArabic, C);
  addTitle(slide, T, item.title, isArabic);
  addSummary(slide, T, item.summary, isArabic, C);
  const rtlBlock = item.rtl || {};
  const ltrBlock = item.ltr || {};
  slide.addShape(pptx.ShapeType.roundRect, { x: 6.85, y: 2.9, w: 5.9, h: 2.45, rectRadius: 0.06, fill: { color: 'FFFFFF' }, line: { color: C.line, width: 0.8 } });
  T.addArabic(slide, rtlBlock.title || '', { x: 7.22, y: 3.27, w: 5.15, h: 0.45, fontSize: 17, bold: true });
  T.addArabic(slide, rtlBlock.body || '', { x: 7.22, y: 4.14, w: 5.15, h: 0.42, fontSize: 13, color: C.muted, valign: 'top' });
  slide.addShape(pptx.ShapeType.roundRect, { x: 0.55, y: 2.9, w: 5.55, h: 2.45, rectRadius: 0.06, fill: { color: C.ink }, line: { color: C.ink } });
  T.addLatin(slide, ltrBlock.title || '', { x: 0.95, y: 3.27, w: 4.75, h: 0.45, fontSize: 20, bold: true, color: 'FFFFFF' });
  T.addLatin(slide, ltrBlock.body || '', { x: 0.95, y: 4.14, w: 4.75, h: 0.42, fontSize: 13, color: 'EAE4DA', valign: 'top' });
  slide.addShape(pptx.ShapeType.line, { x: 0.55, y: 6.25, w: 12.2, h: 0, line: { color: C.line, width: 0.75 } });
  addSource(slide, T, item.source, isArabic, C);
}

function main(argv) {
  if (argv.length !== 2) {
    console.error('Usage: node pptx_design.js <spec.json> <out.pptx>');
    return 2;
  }
  const [specPath, outputPath] = argv;
  const spec = loadSpec(specPath);
  const isArabic = (spec.language || 'en').toLowerCase().startsWith('ar');
  const C = { ...DEFAULTS, ...(spec.theme || {}) };
  const pptx = new pptxgen();
  pptx.defineLayout({ name: 'HERMES_WIDE', width: W, height: H });
  pptx.layout = 'HERMES_WIDE';
  pptx.author = 'Hermes Agent';
  pptx.title = spec.title || 'Presentation';
  pptx.subject = spec.subject || 'Presentation';
  pptx.lang = isArabic ? 'ar-SA' : 'en-US';
  pptx.rtlMode = isArabic;
  pptx.theme = { headFontFace: isArabic ? C.arabicFont : C.latinFont, bodyFontFace: isArabic ? C.arabicFont : C.latinFont, lang: pptx.lang };
  const T = makeTextHelpers(isArabic, C);
  addMaster(pptx, C, T, isArabic);
  const assetsDir = path.dirname(path.resolve(specPath));
  const builders = { cover: buildCover, cards: buildCards, data: buildData, split: buildSplit };
  spec.slides.forEach((item, index) => {
    const builder = builders[item.type];
    if (!builder) fail(`Slide ${index + 1} has unsupported type: ${item.type}. Use cover, cards, data, or split.`);
    const slide = pptx.addSlide('HERMES_DESIGN_MASTER');
    builder({ slide, item, assetsDir, T, isArabic, C, pptx });
    addNotes(slide, item.notes);
  });
  return pptx.writeFile({ fileName: outputPath }).then(() => {
    console.log(JSON.stringify({ ok: true, output: outputPath, slides: spec.slides.length, language: isArabic ? 'ar' : 'en', engine: 'PptxGenJS' }));
  });
}

main(process.argv.slice(2)).catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message, stack: error.stack }));
  process.exit(1);
});
