// 运行真实页面脚本，验证上传、轮询、下载及整页刷新期间的交互。
import assert from 'node:assert/strict';
import { createHarness, done, running } from './page_harness.mjs';

// 后台运行与首次状态即完成均需先下载结果，再整页刷新；刷新前保持锁定。
for (const pollWhileRunning of [true, false]) {
  const h = createHarness();
  const input = { files: [{ name: 'a.csv' }], value: 'a.csv' };
  const upload = h.api.importCsv(input);
  h.assertBusy(true, false);
  assert.ok(h.element('syncOverlay').classList.contains('active'));
  assert.equal(h.element('syncClose').disabled, true);
  await h.reply('/api/import', { started: true, status: pollWhileRunning ? running : done });
  await upload;
  assert.equal(input.value, '');
  h.assertBusy(true, false);
  if (pollWhileRunning) {
    await h.reply('/api/import/status', running);
    h.assertBusy(true, false);
    await h.runTimer(400);
  }
  await h.reply('/api/import/status', done);
  assert.deepEqual(h.events.map(event => event.type), ['download']);
  assert.equal(h.events[0].href, '/api/import/result');
  h.assertBusy(true, false);
  assert.equal(h.element('syncClose').disabled, true);
  assert.equal(h.element('importButton').disabled, true);
  h.api.downloadImportResult();
  h.api.downloadImportResult();
  assert.equal([...h.timers.values()].filter(timer => timer.ms === 1200).length, 1);
  await h.runTimer(1200);
  assert.deepEqual(h.events.map(event => event.type), ['download', 'reload']);
}

// 后台失败、上传失败、状态轮询失败均不得下载或刷新，并恢复页面交互。
for (const failure of ['background', 'upload', 'poll']) {
  const h = createHarness();
  const upload = h.api.importCsv({ files: [{ name: 'a.csv' }], value: 'a.csv' });
  if (failure === 'upload') {
    await h.reply('/api/import', { error: '上传失败' }, { status: 400 });
  } else {
    await h.reply('/api/import', { started: true, status: running });
    if (failure === 'poll') {
      await h.reply('/api/import/status', null, { error: '状态读取失败' });
    } else {
      await h.reply('/api/import/status', { ...done, status: '失败', errors: ['导入失败'] });
    }
  }
  await upload;
  h.assertBusy(false);
  assert.equal(h.element('syncClose').disabled, false);
  assert.equal(h.element('syncDownload').disabled, true);
  h.api.downloadImportResult();
  assert.deepEqual(h.events, []);
  assert.equal([...h.timers.values()].filter(timer => [400, 1200].includes(timer.ms)).length, 0);
  if (failure !== 'background') assert.equal(h.element('syncOverlay').classList.contains('active'), false);
}

console.log('断言: true');
