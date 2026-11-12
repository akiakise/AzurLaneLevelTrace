let nextShipTypes = ["重巡"];
let pendingTypesSave = null;
let typesDirty = false;
let selectionVersion = 0;
let manualTargets = {}, targetLimits = {};
let server = ["重巡"];
const calls = [];
function requestJson(url, options) {
  const submitted = options ? JSON.parse(options.body).next_ship_types : null;
  return new Promise(resolve => calls.push({submitted, resolve}));
}
function applyRemoteConfig(result, keepLocalTypes) {
  if (!keepLocalTypes) nextShipTypes = Array.isArray(result.next_ship_types) ? result.next_ship_types : [];
  manualTargets = result.manual_targets || {};
  targetLimits = result.target_limits || {};
}
async function loadNextTypeConfig() {
  const versionAtStart = selectionVersion;
  const hadPendingSave = Boolean(pendingTypesSave);
  const result = await requestJson('/api/config');
  const stale = versionAtStart !== selectionVersion;
  applyRemoteConfig(result, stale || typesDirty || hadPendingSave);
}
async function saveNextTypeConfig() {
  const submitted = [...nextShipTypes];
  const snapshot = JSON.stringify(submitted);
  const result = await requestJson('/api/config', {body: JSON.stringify({next_ship_types: submitted})});
  server = result.next_ship_types;
  const unchanged = JSON.stringify(nextShipTypes) === snapshot;
  applyRemoteConfig(result, !unchanged);
  if (unchanged) { typesDirty = false; selectionVersion += 1; }
}
async function syncNextTypes() {
  if (pendingTypesSave) { pendingTypesSave.queued = true; return pendingTypesSave.promise; }
  const job = {queued:false, promise:null};
  job.promise = (async () => {
    try {
      while (true) {
        job.queued = false;
        const snapshot = JSON.stringify(nextShipTypes);
        await saveNextTypeConfig();
        if (job.queued || JSON.stringify(nextShipTypes) !== snapshot) continue;
        if (!job.queued && JSON.stringify(nextShipTypes) === snapshot) break;
      }
      typesDirty = false;
      selectionVersion += 1;
    } finally { if (pendingTypesSave === job) pendingTypesSave = null; }
  })();
  pendingTypesSave = job;
  return job.promise;
}
function resolve(idx, types) {
  calls[idx].resolve({next_ship_types: types, manual_targets:{}, target_limits:{}});
}

// 场景A：GET 在 POST 之前返回
nextShipTypes = ["重巡","驱逐"]; typesDirty = true; selectionVersion += 1;
const savingA = syncNextTypes();
const loadingA = loadNextTypeConfig();
resolve(calls.findIndex(c => !c.submitted), ["重巡"]);
await loadingA;
const getIdx = calls.findIndex(c => c.submitted);
server = calls[getIdx].submitted;
resolve(getIdx, server);
await savingA;
console.log('A GET先返回 → 本地', JSON.stringify(nextShipTypes), '服务端', JSON.stringify(server));
console.log('断言A:', JSON.stringify(nextShipTypes) === JSON.stringify(["重巡","驱逐"]) && JSON.stringify(server) === JSON.stringify(["重巡","驱逐"]));

calls.length = 0;

// 场景B：GET 在 POST 之后返回
nextShipTypes = ["重巡","驱逐","轻巡"]; typesDirty = true; selectionVersion += 1;
const loadingB = loadNextTypeConfig();   // 读取旧配置，挂起
const savingB = syncNextTypes();
const postIdx = calls.findIndex(c => c.submitted);
server = calls[postIdx].submitted;
resolve(postIdx, server);
await savingB;
resolve(calls.findIndex(c => !c.submitted), ["重巡"]);  // 迟到旧 GET
await loadingB;
console.log('B GET后返回 → 本地', JSON.stringify(nextShipTypes), '服务端', JSON.stringify(server));
console.log('断言B:', JSON.stringify(nextShipTypes) === JSON.stringify(["重巡","驱逐","轻巡"]) && JSON.stringify(server) === JSON.stringify(["重巡","驱逐","轻巡"]));
