// 执行真实页面脚本，仅替换浏览器边界；请求和定时器由测试逐步推进。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const html = fs.readFileSync(path.join(process.cwd(), 'blhx/web/page.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/initialize\(\)\.catch\(error => showMessage\(error\.message, true\)\);/, '');

export const done = { running: false, status: '完成', message: '处理完成', total: 2, processed: 2, skipped: 0, errors: [] };
export const running = { ...done, running: true, status: '进行中', processed: 0 };
export const config = { next_ship_types: ['重巡'], target_limits: {}, manual_targets: {} };
export const settle = () => new Promise(resolve => setImmediate(resolve));

export function createHarness() {
  const elements = new Map();
  const requests = [];
  const events = [];
  const timers = new Map();
  const documentListeners = new Map();
  let timerId = 0;
  let document;

  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const value = {
      id, hidden: false, disabled: false, inert: false, isConnected: true,
      style: {}, dataset: {}, value: '', innerHTML: '', children: [],
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name),
        toggle(name, force = !classes.has(name)) {
          if (force) classes.add(name); else classes.delete(name);
          return force;
        },
      },
      get textContent() { return this.text || ''; },
      set textContent(text) {
        this.text = String(text);
        this.innerHTML = this.text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
      setAttribute: (name, text) => attributes.set(name, String(text)),
      getAttribute: name => attributes.get(name) ?? null,
      removeAttribute: name => attributes.delete(name),
      addEventListener: (name, callback) => listeners.set(name, callback),
      dispatch: (name, event) => listeners.get(name)?.(event),
      focus() { document.activeElement = this; },
      contains(target) { return target === this || this.children.some(child => child.contains(target)); },
      closest(selector) { return selector === '[inert]' && this.inert ? this : null; },
      appendChild(child) { this.children.push(child); },
      remove() { this.isConnected = false; },
      click() { events.push({ type: 'download', href: this.href }); },
    };
    elements.set(id, value);
    return value;
  }

  const overlays = [...html.matchAll(/id="([^"]+)" class="modal-overlay"/g)].map(match => element(match[1]));
  document = {
    body: element('body'),
    activeElement: element('originalFocus'),
    getElementById: element,
    querySelector: selector => element(selector),    querySelectorAll: selector => {
      assert.equal(selector, '.container, .modal-overlay');
      return [element('.container'), ...overlays];
    },
    createElement: tag => element(`${tag}-${elements.size}`),
    addEventListener: (name, callback) => documentListeners.set(name, callback),
    dispatch: (name, event) => documentListeners.get(name)?.(event),
  };
  const windowListeners = new Map();
  const scrolls = [];
  // 极简地址栏：replaceState 写入 pathname/hash，并同步暴露给被测脚本。
  const location = { pathname: '/', hash: '' };
  const sandbox = {
    document,
    window: {
      location: Object.assign(location, { reload: () => events.push({ type: 'reload' }) }),
      history: {
        replaceState: (state, title, url) => {
          const text = String(url || '');
          const index = text.indexOf('#');
          location.pathname = index >= 0 ? (text.slice(0, index) || '/') : text;
          location.hash = index >= 0 ? text.slice(index) : '';
        },
      },
      scrollTo: (x, y) => scrolls.push({ x, y }),
      addEventListener: (name, callback) => windowListeners.set(name, callback),
      dispatch: (name, event) => windowListeners.get(name)?.(event),
    },
    URLSearchParams,
    FormData: class { append() {} },
    fetch: (url, options = {}) => new Promise((resolve, reject) => {
      requests.push({ url, options, resolve, reject, settled: false });
    }),
    setTimeout: (fn, ms) => {
      timers.set(++timerId, { fn, ms });
      return timerId;
    },
    clearTimeout: id => timers.delete(id),
  };
  const source = `${script}\nreturn {
    requestJson, renderShips, recommendationItem, completeShipStatus, clearShipStatus,
    togglePickerShip, changeTargetLimit, removeManualTarget, clearShipPickerTarget,
    deleteShip, confirmClearShips, toggleNextType, startSync, importCsv,
    refreshSyncStatus, refreshImportStatus, downloadImportResult,
    switchTab, tabFromHash,
    seed() {
      nextTypeConfig = ['重巡'];
      pickerState = { shipType: '重巡', field: 'is_idling', keyword: '', timer: null };
    },
    renderTestShip(ship) { shipData = [ship]; renderShips(); },
  };`;
  const api = new Function(...Object.keys(sandbox), source)(...Object.values(sandbox));
  api.seed();

  const pending = () => requests.filter(request => !request.settled);
  async function reply(url, payload, { error = null, status = 200 } = {}) {
    const request = pending().find(item => typeof url === 'function' ? url(item) : item.url.startsWith(url));
    assert.ok(request, `没有等待中的请求：${url}`);
    request.settled = true;
    if (error) request.reject(new Error(error));
    else request.resolve({ ok: status < 400, statusText: '请求失败', json: async () => payload });
    await settle();
  }
  async function runTimer(ms) {
    const timer = [...timers.entries()].find(([, value]) => value.ms === ms);
    assert.ok(timer, `没有 ${ms}ms 定时器`);
    timers.delete(timer[0]);
    timer[1].fn();
    await settle();
  }
  async function finishReads(check = () => {}) {
    let count = 0;
    while (pending().length) {
      assert.ok(++count < 30, '请求未收敛');
      const request = pending()[0];
      assert.equal(request.options.method || 'GET', 'GET', `意外写请求：${request.url}`);
      check(request);
      const payload = request.url.startsWith('/api/ships?') ? { ships: [] }
        : request.url === '/api/config' ? config
          : request.url === '/api/next-options' ? { ship_types: [{ ship_type: '重巡', idle_count: 1, train_count: 1 }] }
            : request.url.startsWith('/api/next-group?') ? { rows: [] }
              : assert.fail(`未定义响应：${request.url}`);
      await reply(request.url, payload);
    }
  }
  function assertBusy(expected, generic = expected) {
    assert.equal(document.body.classList.contains('loading'), expected, '全页 loading 状态');
    assert.equal(element('.container').inert, expected, '主页面交互锁');
    overlays.forEach(overlay => assert.equal(overlay.inert, expected, `${overlay.id} 交互锁`));
    assert.equal(element('loadingOverlay').classList.contains('active'), generic, '普通写操作遮罩');
    if (expected) assert.equal(document.body.getAttribute('aria-busy'), 'true');
  }
  return { api, document, window: sandbox.window, location, scrolls, element, requests, pending, events, timers, reply, runTimer, finishReads, assertBusy };
}
