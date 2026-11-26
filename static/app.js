"use strict";

const byId = id => document.getElementById(id);
const specimen = byId("specimen");
const workspace = document.querySelector(".workspace");
const rangeNames = ["weight", "width", "tracking", "slant", "contrast", "roundness", "sharpness", "x_height"];
const inputs = Object.fromEntries(rangeNames.map(name => [name, byId(name)]));
const baseFonts = { editorial: "Lora", grotesk: "Plex", mono: '"JetBrains Mono"' };
const russian = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ";
const english = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
const characterSets = {
  cyrillic: russian + russian.toLowerCase(),
  latin: english + english.toLowerCase(),
  symbols: "0123456789.,!?-–—:;«»“”()[]&@#%+×÷=₽$€№",
};
const samples = [
  "У каждого слова\nсвой характер.",
  "Съешь же ещё этих мягких\nфранцузских булок.",
  "The quick brown fox\njumps over the lazy dog.",
  "Москва · Moscow\n2026 № 01 — 123 ₽",
];

let variants = [];
let selectedVariant;
let imported;
let active;
let sourceMode = "prompt";
let previewMode = "text";
let selectedSet = "cyrillic";
let sampleIndex = 0;
let previewJob;
let previewTimer;
let loadedFace;
let loadingImport;
let customSize = false;

function defaultMetrics() {
  return { units_per_em: 1000, ascender: 800, descender: -200 };
}

function fallbackParameters() {
  return {
    weight: 400, width: 100, slant: 0, contrast: 0.35, roundness: 0.15, sharpness: 0.5,
    serif: false, terminal_style: "flat", stroke_contrast_axis: "vertical",
    x_height: 520, cap_height: 720, overshoot: true, ink_trap: false,
    mood: "neutral", style: "modern", purpose: "print", language_support: ["latin", "cyrillic"],
  };
}

function activeParameters() {
  return active?.parameters || active?.baseParameters || fallbackParameters();
}

function setStatus(id, text = "", style = "") {
  const element = byId(id);
  element.textContent = text;
  element.hidden = !text;
  element.classList.toggle("error", style === "error");
  element.classList.toggle("success", style === "success");
}

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}));
    throw new Error(failure.error || "Сервер не ответил. Попробуйте ещё раз.");
  }
  return response;
}

function setSourceMode(mode) {
  sourceMode = mode;
  byId("prompt-mode").setAttribute("aria-pressed", mode === "prompt");
  byId("import-mode").setAttribute("aria-pressed", mode === "import");
  byId("prompt-form").hidden = mode !== "prompt";
  byId("import-panel").hidden = mode !== "import";
  setStatus("source-status");
  if (mode === "prompt" && selectedVariant && active !== selectedVariant) activate(selectedVariant);
  if (mode === "import" && imported && active !== imported) activate(imported);
}

function renderVariants() {
  const grid = byId("variant-grid");
  const showingImport = active?.id === "imported";
  const options = showingImport ? [active] : variants;
  byId("variants-title").textContent = showingImport ? "Исходный шрифт" : "Варианты шрифта";
  byId("variant-count").textContent = showingImport ? active.font.format : `${options.length} варианта`;
  grid.replaceChildren(...options.map(option => {
    const button = document.createElement("button");
    const sample = document.createElement("span");
    const title = document.createElement("strong");
    const category = document.createElement("small");
    button.type = "button";
    button.className = `variant ${option.id}`;
    button.setAttribute("aria-pressed", option === active);
    sample.className = "variant-specimen";
    sample.textContent = option.id === "imported" && !["А", "а"].every(char => option.font.sets.cyrillic.includes(char)) ? "Aa" : "Аа";
    title.textContent = option.variant_name || option.title;
    category.textContent = option.description || option.category;
    button.title = option.description || option.category;
    button.append(sample, title, category);
    button.addEventListener("click", () => {
      if (option === active) return;
      selectedVariant = option;
      activate(option);
      grid.querySelector('[aria-pressed="true"]')?.focus();
    });
    return button;
  }));
}

function renderGlyphs() {
  const available = active?.font.sets[selectedSet] ?? "";
  const chars = characterSets[selectedSet];
  const cards = Array.from(chars, char => {
    const supported = available.includes(char);
    const tile = document.createElement("div");
    const value = document.createElement("span");
    const code = document.createElement("small");
    tile.className = "glyph-tile" + (supported ? "" : " missing");
    tile.title = supported ? char : `Символ ${char} отсутствует`;
    value.textContent = supported ? char : "–";
    code.textContent = "U+" + char.codePointAt(0).toString(16).toUpperCase().padStart(4, "0");
    tile.append(value, code);
    return tile;
  });
  byId("glyph-count").textContent = active ? `${available.length} из ${chars.length}` : "";
  byId("glyph-summary").textContent = active
    ? selectedSet === "cyrillic"
      ? `Русский алфавит · блок U+0400–U+04FF: ${active.font.cyrillicBlockCount} из 256 кодовых позиций`
      : `${active.font.unicodeCount} символов Unicode в файле`
    : "Выберите гарнитуру для просмотра символов.";
  byId("glyph-grid").replaceChildren(...(active ? cards : []));
}

function updateOutputs() {
  const suffixes = { width: "%", slant: "°" };
  for (const name of rangeNames) {
    const value = Number(inputs[name].value);
    const prefix = (name === "slant" || name === "tracking") && value > 0 ? "+" : "";
    let display;
    if (["contrast", "roundness", "sharpness"].includes(name)) display = value.toFixed(2);
    else display = prefix + value + (suffixes[name] || "");
    byId(name + "-value").value = inputs[name].disabled ? "—" : display;
  }
}

function resetControls() {
  const parameters = activeParameters();
  const capHeight = Math.max(500, Math.min(780, parameters.cap_height || 720));
  for (const name of rangeNames) inputs[name].disabled = false;
  inputs.weight.min = 100;
  inputs.weight.max = 900;
  inputs.weight.step = 10;
  inputs.weight.value = parameters.weight ?? 400;
  inputs.width.min = 50;
  inputs.width.max = 200;
  inputs.width.value = parameters.width ?? 100;
  inputs.tracking.value = 0;
  inputs.slant.min = -12;
  inputs.slant.max = 12;
  inputs.slant.value = parameters.slant ?? 0;
  inputs.contrast.value = parameters.contrast ?? 0.35;
  inputs.roundness.value = parameters.roundness ?? 0.15;
  inputs.sharpness.value = parameters.sharpness ?? 0.5;
  inputs.x_height.min = 300;
  inputs.x_height.max = Math.max(300, capHeight - 20);
  inputs.x_height.value = Math.min(parameters.x_height ?? 520, capHeight - 20);
  const serif = byId("serif");
  serif.checked = !!parameters.serif;
  serif.disabled = active?.id === "imported";
  const terminal = byId("terminal_style");
  terminal.value = parameters.terminal_style || "flat";
  terminal.disabled = false;
  updateOutputs();
}

function collectParameters() {
  const base = activeParameters();
  const capHeight = Math.max(500, Math.min(780, base.cap_height || 720));
  let xHeight = Number(inputs.x_height.value);
  if (xHeight >= capHeight) xHeight = capHeight - 20;
  return {
    ...base,
    weight: Number(inputs.weight.value),
    width: Number(inputs.width.value),
    slant: Number(inputs.slant.value),
    contrast: Number(inputs.contrast.value),
    roundness: Number(inputs.roundness.value),
    sharpness: Number(inputs.sharpness.value),
    serif: byId("serif").checked,
    terminal_style: byId("terminal_style").value,
    x_height: xHeight,
    cap_height: capHeight,
  };
}

function activate(option) {
  active = option;
  byId("reset-button").disabled = false;
  byId("export-button").disabled = false;
  byId("specimen-category").textContent = (option.category || "Шрифт").toLocaleUpperCase("ru");
  workspace.style.setProperty("--active-font", baseFonts[option.id] || "Lora");
  resetControls();
  renderVariants();
  if (previewMode === "glyph") renderGlyphs();
  schedulePreview(0);
}

function compileRequest(path, name, signal) {
  const payload = {
    parameters: collectParameters(),
    metrics: active?.metrics || defaultMetrics(),
    base_font_id: active?.id === "imported" ? active.baseFontId : null,
    base_parameters: active?.id === "imported" ? active.baseParameters : null,
    family_name: name,
    tracking: Number(inputs.tracking.value),
    autohint: true,
    download: path === "/api/export",
  };
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
}

function schedulePreview(delay = 220) {
  clearTimeout(previewTimer);
  previewJob?.abort();
  setStatus("preview-status");
  previewTimer = setTimeout(renderPreview, delay);
}

async function renderPreview() {
  const job = new AbortController();
  previewJob = job;
  specimen.setAttribute("aria-busy", "true");
  try {
    const response = await compileRequest("/api/preview", "AI Font Preview", job.signal);
    const face = await new FontFace("LiteraLive", await response.arrayBuffer()).load();
    if (job.signal.aborted) return;
    if (loadedFace) document.fonts.delete(loadedFace);
    document.fonts.add(face);
    loadedFace = face;
    workspace.style.setProperty("--active-font", "LiteraLive");
  } catch (error) {
    if (!job.signal.aborted) setStatus("preview-status", error.message, "error");
  } finally {
    if (!job.signal.aborted) specimen.removeAttribute("aria-busy");
  }
}

async function loadRecommendations(prompt, initial = false) {
  const button = byId("generate-button");
  const label = button.lastElementChild;
  button.disabled = true;
  label.textContent = "Подбираем…";
  setStatus("source-status");
  try {
    const response = await request("/api/recommend", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt }),
    });
    const data = await response.json();
    variants = data.variants;
    byId("mode-label").textContent = data.mode === "llm" ? "LLM-режим" : "Эвристика";
    selectedVariant = variants[0];
    if (sourceMode === "prompt" || !active) activate(selectedVariant);
    if (!initial && sourceMode === "prompt") {
      setStatus("source-status", "Первый вариант ближе всего к описанию.", "success");
    }
  } catch (error) {
    if (sourceMode === "prompt") setStatus("source-status", error.message, "error");
  } finally {
    button.disabled = false;
    label.textContent = "Подобрать варианты";
  }
}

async function importFile(file) {
  if (!file) return;
  loadingImport?.abort();
  const job = new AbortController();
  loadingImport = job;
  setSourceMode("import");
  if (!/\.(ttf|otf)$/i.test(file.name) || file.size > 10 * 1024 * 1024 || !file.size) {
    setStatus("source-status", "Выберите непустой файл .ttf или .otf размером до 10 МБ.", "error");
    return;
  }
  setStatus("source-status", "Анализируем файл…");
  try {
    const response = await request("/api/import", { method: "POST", body: file, signal: job.signal });
    const font = await response.json();
    if (job.signal.aborted) return;
    imported = {
      id: "imported",
      title: font.family,
      category: font.style,
      font,
      parameters: font.parameters,
      metrics: font.metrics,
      baseFontId: font.base_font_id,
      baseParameters: font.base_parameters || font.parameters,
    };
    byId("import-family").textContent = font.family;
    byId("import-style").textContent = font.style;
    byId("import-format").textContent = font.format;
    byId("import-glyphs").textContent = font.glyphCount;
    byId("import-weight").textContent = font.parameters?.weight ?? "—";
    byId("import-width").textContent = font.parameters ? font.parameters.width + "%" : "—";
    byId("import-slant").textContent = font.parameters ? font.parameters.slant + "°" : "—";
    byId("import-contrast").textContent = font.parameters ? Number(font.parameters.contrast).toFixed(2) : "—";
    byId("import-xheight").textContent = font.parameters?.x_height ?? "—";
    byId("import-serif").textContent = font.parameters?.serif ? "Есть" : "Нет";
    byId("font-info").hidden = false;
    if (sourceMode === "import") {
      activate(imported);
      setStatus("source-status", font.restrictedEmbedding
        ? "Метаданные шрифта указывают на ограниченное встраивание."
        : "Шрифт загружен. Его можно настроить и экспортировать.", font.restrictedEmbedding ? "" : "success");
    }
  } catch (error) {
    if (!job.signal.aborted && sourceMode === "import") setStatus("source-status", error.message, "error");
  }
}

function setPreviewMode(mode) {
  previewMode = mode;
  byId("text-mode").setAttribute("aria-pressed", mode === "text");
  byId("glyph-mode").setAttribute("aria-pressed", mode === "glyph");
  byId("text-view").hidden = mode !== "text";
  byId("glyph-view").hidden = mode !== "glyph";
  if (mode === "glyph") renderGlyphs();
}

function updateSize() {
  const size = byId("size").value;
  specimen.style.setProperty("--specimen-size", size + "px");
  byId("size-value").value = size + " px";
}

byId("prompt-mode").addEventListener("click", () => setSourceMode("prompt"));
byId("import-mode").addEventListener("click", () => setSourceMode("import"));
byId("text-mode").addEventListener("click", () => setPreviewMode("text"));
byId("glyph-mode").addEventListener("click", () => setPreviewMode("glyph"));
byId("prompt-form").addEventListener("submit", event => {
  event.preventDefault();
  loadRecommendations(byId("prompt").value.trim());
});
for (const button of document.querySelectorAll("[data-prompt]")) {
  button.addEventListener("click", () => {
    byId("prompt").value = button.dataset.prompt;
    byId("prompt").focus();
  });
}
for (const button of document.querySelectorAll("[data-set]")) {
  button.addEventListener("click", () => {
    selectedSet = button.dataset.set;
    for (const filter of document.querySelectorAll("[data-set]")) filter.setAttribute("aria-pressed", filter === button);
    renderGlyphs();
  });
}
for (const name of rangeNames) {
  inputs[name].addEventListener("input", () => {
    updateOutputs();
    schedulePreview();
  });
}
byId("serif").addEventListener("change", () => schedulePreview(0));
byId("terminal_style").addEventListener("change", () => schedulePreview(0));
byId("reset-button").addEventListener("click", () => {
  resetControls();
  schedulePreview(0);
});
byId("size").addEventListener("input", () => {
  customSize = true;
  updateSize();
});
new ResizeObserver(entries => {
  const visibleWidth = entries[0].contentRect.width;
  if (!customSize && visibleWidth > 0) {
    byId("size").value = Math.min(88, Math.round(visibleWidth / 8.2));
    updateSize();
  }
}).observe(specimen);
byId("sample-button").addEventListener("click", () => {
  sampleIndex = (sampleIndex + 1) % samples.length;
  byId("specimen-text").textContent = samples[sampleIndex];
});
byId("font-upload").addEventListener("change", event => {
  importFile(event.target.files[0]);
  event.target.value = "";
});
const dropZone = byId("drop-zone");
dropZone.addEventListener("dragover", event => {
  event.preventDefault();
  dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
dropZone.addEventListener("drop", event => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  importFile(event.dataTransfer.files[0]);
});
byId("about-button").addEventListener("click", () => byId("about-dialog").showModal());
byId("export-button").addEventListener("click", () => {
  byId("font-name").value = active.id === "imported"
    ? "AI Custom"
    : "AI " + { editorial: "Editorial", grotesk: "Grotesk", mono: "Mono" }[active.id];
  const missing = Object.keys(characterSets).filter(key => characterSets[key].length !== active.font.sets[key].length);
  byId("export-summary").textContent = missing.length
    ? "В файл попадут символы исходной гарнитуры. Покрытие можно проверить на вкладке «Символы»."
    : "TTF с кириллицей, латиницей, цифрами и знаками.";
  setStatus("export-status");
  byId("export-dialog").showModal();
  byId("font-name").select();
});
byId("export-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = byId("confirm-export");
  const name = byId("font-name").value.trim();
  if (!name) {
    setStatus("export-status", "Введите название семейства.", "error");
    return;
  }
  button.disabled = true;
  setStatus("export-status", "Собираем файл…");
  try {
    const response = await compileRequest("/api/export", name);
    const filename = response.headers.get("Content-Disposition")?.split("filename*=UTF-8''")[1];
    const download = document.createElement("a");
    const objectUrl = URL.createObjectURL(await response.blob());
    download.href = objectUrl;
    download.download = filename ? decodeURIComponent(filename) : "AI-Font.ttf";
    download.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    byId("export-dialog").close();
  } catch (error) {
    setStatus("export-status", error.message, "error");
  } finally {
    button.disabled = false;
  }
});

const phoneLayout = matchMedia("(max-width: 525px)");
const adjustmentPanel = document.querySelector(".adjustments");
function placeAdjustments() {
  if (phoneLayout.matches) workspace.insertBefore(adjustmentPanel, document.querySelector(".variants"));
  else document.querySelector(".sidebar").append(adjustmentPanel);
}
phoneLayout.addEventListener("change", placeAdjustments);
placeAdjustments();
loadRecommendations("Редакционная антиква для журнала", true);
