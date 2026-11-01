// 从 page.html 抽取真实 importExcel / scheduleImportReload 实现，验证：
// 1) 下载点击发生在整页刷新之前；2) 成功导入恰好刷新一次；
// 3) 局部回读（loadShips / refreshNextOptions）失败时仍会刷新；
// 4) 导入失败时不刷新。
import fs from "node:fs";
import path from "node:path";

const htmlPath = path.join(process.cwd(), "blhx/page.html");
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

const source = `${extract("importExcel")}\n${extract("scheduleImportReload")}\nreturn { importExcel, scheduleImportReload };`;

function createHarness({ failReadback = false, failImport = false } = {}) {
  const events = [];
  const timers = [];
  const sandbox = {
    fetch: async () => {
      if (failImport) return { ok: false, statusText: "boom", json: async () => ({ error: "失败" }) };
      return { ok: true, blob: async () => new Blob(["x"]) };
    },
    URL: {
      createObjectURL: () => "blob:mock",
      revokeObjectURL: () => events.push({ type: "revoke", at: timers.length }),
    },
    document: {
      createElement: () => ({ click() { events.push({ type: "download" }); } }),
    },
    window: {
      location: { reload: () => events.push({ type: "reload" }) },
    },
    showMessage: () => {},
    showToast: () => {},
    loadShips: async () => {
      if (failReadback) throw new Error("readback failed");
    },
    refreshNextOptions: async () => {
      if (failReadback) throw new Error("readback failed");
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); },
  };
  const factory = new Function(...Object.keys(sandbox), source);
  const api = factory(...Object.values(sandbox));
  return { api, events, timers };
}

function flushTimers(timers) {
  // 按延迟顺序执行，模拟真实定时器语义。
  [...timers].sort((a, b) => a.ms - b.ms).forEach(timer => timer.fn());
}

let failures = 0;
function check(label, condition) {
  if (!condition) {
    failures += 1;
    console.log(`断言失败: ${label}`);
  }
}

// 场景1：正常成功
{
  const { api, events, timers } = createHarness();
  await api.importExcel({ files: [{ name: "a.xlsx" }], value: "a.xlsx" });
  flushTimers(timers);
  const downloads = events.filter(e => e.type === "download").length;
  const reloads = events.filter(e => e.type === "reload").length;
  const downloadIndex = events.findIndex(e => e.type === "download");
  const reloadIndex = events.findIndex(e => e.type === "reload");
  check("成功导入下载一次", downloads === 1);
  check("成功导入刷新一次", reloads === 1);
  check("下载早于刷新", downloadIndex >= 0 && reloadIndex > downloadIndex);
  check("对象 URL 被回收一次", events.filter(e => e.type === "revoke").length === 1);
}

// 场景2：局部回读失败，仍必须刷新
{
  const { api, events, timers } = createHarness({ failReadback: true });
  await api.importExcel({ files: [{ name: "a.xlsx" }], value: "a.xlsx" });
  flushTimers(timers);
  check("回读失败仍刷新一次", events.filter(e => e.type === "reload").length === 1);
}

// 场景3：导入失败，不得刷新
{
  const { api, events, timers } = createHarness({ failImport: true });
  await api.importExcel({ files: [{ name: "a.xlsx" }], value: "a.xlsx" });
  flushTimers(timers);
  check("导入失败不刷新", events.filter(e => e.type === "reload").length === 0);
  check("导入失败不下载", events.filter(e => e.type === "download").length === 0);
}

console.log("断言:", failures === 0);
process.exit(failures === 0 ? 0 : 1);
