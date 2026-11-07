// 运行真实页面脚本，验证 Tab 切换、hash 记忆与键盘导航。
import assert from 'node:assert/strict';
import { createHarness } from './page_harness.mjs';

const tabs = [
  ['recommend', 'tabRecommend', 'panelRecommend'],
  ['training', 'tabTraining', 'panelTraining'],
  ['equipment', 'tabEquipment', 'panelEquipment'],
];

function assertActive(h, expected) {
  for (const [name, button, panel] of tabs) {
    const active = name === expected;
    assert.equal(h.element(button).getAttribute('aria-selected'), String(active), `${name} aria-selected`);
    assert.equal(h.element(button).classList.contains('selected'), active, `${name} 选中样式`);
    assert.equal(h.element(button).tabIndex, active ? 0 : -1, `${name} tabindex`);
    assert.equal(h.element(panel).hidden, !active, `${name} 面板可见性`);
  }
  assert.equal(h.api.tabFromHash(), expected, '地址栏 hash 与当前 Tab 一致');
}

// 默认进入推荐页面，其余两个面板隐藏。
{
  const h = createHarness();
  h.api.switchTab('recommend');
  assertActive(h, 'recommend');
  assert.equal(h.location.hash, '', '推荐页面不写 hash');
}

// 点击 Tab 切换展示并写入 hash；回到推荐页时清空 hash。
{
  const h = createHarness();
  h.api.switchTab('training');
  assertActive(h, 'training');
  assert.equal(h.location.hash, '#training');
  h.api.switchTab('equipment');
  assertActive(h, 'equipment');
  assert.equal(h.location.hash, '#equipment');
  h.api.switchTab('recommend');
  assertActive(h, 'recommend');
  assert.equal(h.location.hash, '');
}

// 未知 hash 回退到推荐页面，避免刷新后出现空白页。
{
  const h = createHarness();
  h.document.defaultView = undefined;
  h.api.switchTab('training');
  h.location.hash = '#nope';
  assert.equal(h.api.tabFromHash(), 'recommend');
}

// 键盘左右键在 Tab 间循环移动，Home/End 跳到首尾，并把焦点交给新 Tab。
{
  const h = createHarness();
  const dispatch = h.element('.tabs').dispatch;
  const key = (name, event = {}) => dispatch('keydown', { key: name, preventDefault() {}, ...event });
  h.api.switchTab('recommend', false);
  key('ArrowLeft');
  assertActive(h, 'equipment');
  assert.equal(h.document.activeElement, h.element('tabEquipment'), '焦点跟随选中 Tab');
  key('ArrowRight');
  assertActive(h, 'recommend');
  key('End');
  assertActive(h, 'equipment');
  key('Home');
  assertActive(h, 'recommend');
  // 非导航键不改变 Tab。
  key('a');
  assertActive(h, 'recommend');
  // 点击 Tab 同样生效，并且不强制移动焦点。
  const trainingButton = h.element('tabTraining');
  trainingButton.dataset.tab = 'training';
  const focusBeforeClick = h.document.activeElement;
  h.element('.tabs').dispatch('click', { target: { closest: () => trainingButton } });
  assertActive(h, 'training');
  assert.equal(h.document.activeElement, focusBeforeClick, '点击切换不抢焦点');
}

// hashchange 时同步切换，支持浏览器前进/后退。
{
  const h = createHarness();
  h.api.switchTab('recommend');
  h.location.hash = '#equipment';
  h.window.dispatch('hashchange', {});
  assertActive(h, 'equipment');
}

console.log('断言: true');
