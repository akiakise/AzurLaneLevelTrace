// 从 page.html 抽取真实导入交互实现，验证导入与全量同步一致的流程：
// 1) 上传后进入进度弹窗并轮询 /api/import/status；
// 2) 完成时先下载结果文件，再整页刷新；
// 3) 上传失败时不刷新；轮询失败时关闭弹窗并恢复按钮。
import fs from "node:fs";
import path from "node:path";

const htmlPath = path.join(process.cwd(), "blhx/web/page.html");
const html = fs.readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function extract(name) {
  const start = script.indexOf(`async function ${name}(`) >= 0
    ? script.indexOf(`async function ${name}(`)
    : script.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`未找到 ${name}`);
  const braceStart = script.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < script.length; i += 1) {
    if (script[i] === "{") depth += 1;
    else if (script[i] === "}") {
      depth -= 1;
      if (depth === 0) return script.slice(start, i + 1);
    }
  }
  throw new Error(`${name} 括号不匹配`);
}

const source = [
  extract("setSyncModalMode"),
  extract("showImportOverlay"),
  extract("finishImportInteraction"),
  extract("refreshImportStatus"),
  extract("downloadImportResult"),
  extract("scheduleImportReload"),
  extract("importCsv"),
  "let importDownloadDone = false;",
  "return { importCsv, refreshImportStatus, downloadImportResult, get importDownloadDone() { return importDownloadDone; } };",
].join("\n");

function createHarness({ statusPlan = [], failUpload = false, failStatus = false } = {}) {
  const events = [];
  const timers = [];
  const statuses = [...statusPlan];
  let pollIndex = 0;
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        textContent: "",
        innerHTML: "",
        hidden: false,
        disabled: false,
        style: {},
        classList: { add() {}, remove() {}, toggle() {} },
        click() {
          if (id === "syncDownload") events.push({ type: "download" });
        },
        ...(id === "syncDownload" ? { hidden: true, disabled: true } : {}),
      });
    }
    return elements.get(id);
  };
  const sandbox = {
    fetch: async () => {
      if (failUpload) {
        return { ok: false, statusText: "boom", json: async () => ({ error: "上传失败" }) };
      }
      const status = { running: true, status: "导入中", message: "正在导入", total: 2, processed: 0, skipped: 0, errors: [] };
      return { ok: true, json: async () => ({ started: true, status }) };
    },
    requestJson: async url => {
      if (failStatus) throw new Error("状态读取失败");
      if (url !== "/api/import/status") throw new Error(`意外请求 ${url}`);
      const value = statuses[Math.min(pollIndex, statuses.length - 1)];
      pollIndex += 1;
      return value;
    },
    fetchJson: async () => ({}),
    document: {
      getElementById: element,
      createElement: () => ({
        click() {
          events.push({ type: "download" });
        },
      }),
      body: { classList: { add() {}, remove() {}, toggle() {} } },
    },
    window: { location: { reload: () => events.push({ type: "reload" }) } },
    showMessage: () => {},
    showToast: () => {},
    showSyncOverlay: () => {},
    closeSyncOverlay: () => {},
    updateSyncModal: result => {
      if (result.mode === "import") {
        element("syncDownload").hidden = false;
        element("syncDownload").disabled =
          result.running || result.status !== "完成" ||
          api.importDownloadDone;
      }
    },
    escapeHtml: value => String(value),
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    FormData: class {
      append() {}
    },
    URL: { createObjectURL: () => "blob:mock", revokeObjectURL() {} },
  };
  let api = null;
  const factory = new Function(...Object.keys(sandbox), source);
  api = factory(...Object.values(sandbox));
  return { api, events, timers };
}

async function flushTimers(timers) {
  let guard = 0;
  while (timers.length) {
    if ((guard += 1) > 50) throw new Error("定时器未收敛");
    const timer = timers.sort((a, b) => a.ms - b.ms).shift();
    await timer.fn();
  }
}

let failures = 0;
function check(label, condition) {
  if (!condition) {
    failures += 1;
    console.log(`断言失败: ${label}`);
  }
}

const done = { running: false, status: "完成", message: "导入完成", total: 2, processed: 2, skipped: 0, errors: [] };

// 场景1：启动后轮询直至完成，先下载再刷新
{
  const { api, events, timers } = createHarness({
    statusPlan: [{ ...done, running: true }, done],
  });
  await api.importCsv({ files: [{ name: "a.csv" }], value: "a.csv" });
  await flushTimers(timers);
  const downloads = events.filter(e => e.type === "download").length;
  const reloads = events.filter(e => e.type === "reload").length;
  const downloadIndex = events.findIndex(e => e.type === "download");
  const reloadIndex = events.findIndex(e => e.type === "reload");
  check("导入完成下载一次", downloads === 1);
  check("导入完成刷新一次", reloads === 1);
  check("下载早于刷新", downloadIndex >= 0 && reloadIndex > downloadIndex);
}

// 场景1b：手动重复点击下载不得重复下载或重复安排刷新
{
  const { api, events, timers } = createHarness({
    statusPlan: [{ ...done, running: true }, done],
  });
  await api.importCsv({ files: [{ name: "a.csv" }], value: "a.csv" });
  await flushTimers(timers);
  api.downloadImportResult();
  api.downloadImportResult();
  await flushTimers(timers);
  check("重复下载只下载一次", events.filter(e => e.type === "download").length === 1);
  check("重复下载只刷新一次", events.filter(e => e.type === "reload").length === 1);
}

// 场景1c：导入失败时不得启用下载或刷新
{
  const failed = { running: false, status: "失败", message: "导入失败", total: 1, processed: 0, skipped: 1, errors: ["boom"] };
  const { api, events, timers } = createHarness({ statusPlan: [failed] });
  await api.importCsv({ files: [{ name: "a.csv" }], value: "a.csv" });
  await flushTimers(timers);
  check("失败不下载", events.filter(e => e.type === "download").length === 0);
  check("失败不刷新", events.filter(e => e.type === "reload").length === 0);
}

// 场景2：上传失败，不得轮询、下载或刷新
{
  const { api, events, timers } = createHarness({ failUpload: true });
  await api.importCsv({ files: [{ name: "a.csv" }], value: "a.csv" });
  await flushTimers(timers);
  check("上传失败不下载", events.filter(e => e.type === "download").length === 0);
  check("上传失败不刷新", events.filter(e => e.type === "reload").length === 0);
}

// 场景3：状态轮询失败，不得下载或刷新
{
  const { api, events, timers } = createHarness({ failStatus: true });
  await api.importCsv({ files: [{ name: "a.csv" }], value: "a.csv" });
  await flushTimers(timers);
  check("轮询失败不下载", events.filter(e => e.type === "download").length === 0);
  check("轮询失败不刷新", events.filter(e => e.type === "reload").length === 0);
}

console.log("断言:", failures === 0);
process.exit(failures === 0 ? 0 : 1);
