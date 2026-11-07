import assert from 'node:assert/strict';
import { config, createHarness, done, running, settle } from './page_harness.mjs';

// 每一个页面写入口都必须从发送请求前锁定页面，并保持到关联数据刷新结束。
const writes = [
  ['挂机完成', '/api/ship/status', api => api.completeShipStatus(1, 'is_idling')],
  ['练级完成', '/api/ship/status', api => api.completeShipStatus(1, 'is_trained')],
  ['清除状态', '/api/ship/clear-status', api => api.clearShipStatus(1, '古鹰')],
  ['删除舰娘', '/api/ship/delete', api => api.deleteShip(1, '古鹰')],
  ['清空数据', '/api/ships/clear', api => api.confirmClearShips()],
  ['录入舰娘', '/api/manual-target', api => api.togglePickerShip(1, '古鹰')],
  ['调整上限', '/api/target-limit', api => api.changeTargetLimit('重巡', 'is_idling', 1)],
  ['取消录入', '/api/manual-target', api => api.removeManualTarget('重巡', 'is_idling', 1)],
  ['清空录入', '/api/manual-target', api => api.clearShipPickerTarget()],
  ['推荐舰种', '/api/config', api => api.toggleNextType('驱逐')],
];
for (const [label, url, action] of writes) {
  const h = createHarness();
  const focusBefore = h.document.activeElement;
  const task = action(h.api);
  h.assertBusy(true);
  assert.equal(h.document.activeElement, h.element('loadingOverlay'), `${label} 焦点移入遮罩`);
  const request = h.pending()[0];
  assert.equal(request.url, url, label);
  const submitted = JSON.parse(request.options.body);
  await h.reply(url, { ...config, next_ship_types: submitted.next_ship_types || config.next_ship_types, count: 0 });
  await h.finishReads(() => h.assertBusy(true));
  await task;
  h.assertBusy(false);
  assert.equal(h.document.activeElement, focusBefore, `${label} 完成后恢复焦点`);
}

// 保存失败及后续刷新失败都必须释放遮罩。
for (const failDuringRefresh of [false, true]) {
  const h = createHarness();
  const task = h.api.changeTargetLimit('重巡', 'is_idling', 1);
  if (failDuringRefresh) {
    await h.reply('/api/target-limit', config);
    h.assertBusy(true);
    await h.reply('/api/next-group?', null, { error: '刷新失败' });
  } else {
    await h.reply('/api/target-limit', { error: '保存失败' }, { status: 400 });
  }
  await task;
  h.assertBusy(false);
  assert.ok(h.element('toastStack').children.some(item => item.textContent.includes('失败')));
}

// 重叠写操作中，先完成的一项不能解除另一项的 loading。
{
  const h = createHarness();
  const first = h.api.changeTargetLimit('重巡', 'is_idling', 1);
  const second = h.api.changeTargetLimit('重巡', 'is_trained', 1);
  await h.reply('/api/target-limit', config);
  await h.reply('/api/next-group?', { rows: [] });
  await first;
  h.assertBusy(true);
  await h.reply('/api/target-limit', config);
  await h.reply('/api/next-group?', { rows: [] });
  await second;
  h.assertBusy(false);
}

// 普通读取不触发写操作遮罩；未包裹的写请求仍有统一兜底。
{
  const h = createHarness();
  const read = h.api.requestJson('/api/config');
  h.assertBusy(false);
  await h.reply('/api/config', config);
  await read;
  const write = h.api.requestJson('/api/config', { method: 'POST', body: '{}' });
  h.assertBusy(true);
  for (const key of ['Tab', 'Enter', ' ', 'Escape']) {
    const event = { key, preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
    h.document.dispatch('keydown', event);
    assert.equal(event.prevented, true, `${key} 不得穿透 loading`);
    assert.equal(event.stopped, true);
  }
  await h.reply('/api/config', config);
  await write;
  h.assertBusy(false);
  const event = { key: 'Enter', preventDefault() { this.prevented = true; }, stopImmediatePropagation() {} };
  h.document.dispatch('keydown', event);
  assert.equal(event.prevented, undefined, '解锁后恢复键盘操作');
}

// 全量与少量同步在启动、后台运行、完成后刷新三个阶段连续锁定。
for (const names of [null, ['古鹰']]) {
  const h = createHarness();
  const task = h.api.startSync(names);
  h.assertBusy(true, false);
  assert.ok(h.element('syncOverlay').classList.contains('active'));
  assert.equal(h.element('syncClose').disabled, true);
  await h.reply('/api/sync', { started: true });
  await task;
  await h.reply('/api/sync/status', running);
  h.assertBusy(true, false);
  await h.runTimer(1000);
  await h.reply('/api/sync/status', done);
  await h.finishReads(() => h.assertBusy(true, false));
  h.assertBusy(false);
  assert.equal(h.element('syncClose').disabled, false);
}

// 进度读取暂时失败仍需锁定并重试；恢复后才能释放页面。
{
  const h = createHarness();
  const task = h.api.startSync();
  await h.reply('/api/sync', { started: true });
  await task;
  await h.reply('/api/sync/status', null, { error: '临时网络错误' });
  h.assertBusy(true, false);
  assert.ok(h.element('syncMessage').textContent.includes('重试'));
  await h.runTimer(2000);
  await h.reply('/api/sync/status', done);
  await h.finishReads(() => h.assertBusy(true, false));
  h.assertBusy(false);

  // 上一次成功的自动关闭回调不得关闭新一轮任务的进度弹窗。
  const nextTask = h.api.startSync(['古鹰']);
  await h.runTimer(2200);
  h.assertBusy(true, false);
  assert.ok(h.element('syncOverlay').classList.contains('active'));
  await h.reply('/api/sync', { error: '启动失败' }, { status: 400 });
  await nextTask;
  h.assertBusy(false);
}

// 已知同步结束后，页面刷新失败也应解除锁定，不再按后台运行重试。
{
  const h = createHarness();
  const task = h.api.startSync();
  await h.reply('/api/sync', { started: true });
  await task;
  await h.reply('/api/sync/status', done);
  await h.reply('/api/ships?', { ships: [] });
  await h.reply('/api/armaments?', { armaments: [], status_counts: {} });
  // 舰船与兵装列表都刷新完成后才轮到推荐选项，推荐刷新失败要能解除锁定。
  await h.reply('/api/next-options', null, { error: '推荐刷新失败' });
  h.assertBusy(false);
  assert.equal(h.element('syncClose').disabled, false);
  assert.ok(h.element('syncMessage').textContent.includes('推荐刷新失败'));
  assert.equal([...h.timers.values()].filter(timer => timer.ms === 2000).length, 0);
}

// 同步启动请求失败时恢复页面；旧的状态读取不能覆盖后来启动的导入。
{
  const h = createHarness();
  const task = h.api.startSync();
  await h.reply('/api/sync', { error: '启动失败' }, { status: 400 });
  await task;
  h.assertBusy(false);
  assert.equal(h.element('syncOverlay').classList.contains('active'), false);
}
{
  const h = createHarness();
  const staleRead = h.api.refreshSyncStatus();
  const importing = h.api.importCsv({ files: [{ name: 'a.csv' }], value: 'a.csv' });
  await h.reply('/api/sync/status', done);
  await staleRead;
  h.assertBusy(true, false);
  assert.equal(h.element('syncTitle').textContent, '导入进度');
  assert.equal(h.pending().filter(item => item.url !== '/api/import').length, 0);
  await h.reply('/api/import', { error: '上传失败' }, { status: 400 });
  await importing;
  h.assertBusy(false);
}

// 文案针对真实表格与推荐输出验证，保留操作顺序。
{
  const h = createHarness();
  const ship = { id: 1, name: '古鹰', ship_type: '重巡', rarity: '精锐', tech_points: [], is_idling: false, is_trained: false };
  h.api.renderTestShip(ship);
  const table = h.element('shipTable').innerHTML;
  const labels = ['挂机完成', '练级完成', '清除数据', '删除'];
  const positions = labels.map(label => table.indexOf(`>${label}</button>`));
  assert.ok(positions.every((position, index) => position >= 0 && (!index || position > positions[index - 1])));
  for (const [field, label] of [['is_idling', '挂机完成'], ['is_trained', '练级完成']]) {
    const recommendation = h.api.recommendationItem({ ship_type: '重巡' }, 'idle', { ship }, field, 1);
    assert.ok(recommendation.includes(`>${label}</button>`));
  }
}

await settle();
console.log('断言: true');
