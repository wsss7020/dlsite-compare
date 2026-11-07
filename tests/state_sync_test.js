// 前端状态同步回归测试：Node 内置模块即可运行，不访问页面、不修改真实数据。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

// 提取纯函数到隔离 VM；只用于无 DOM 的状态函数。
function extractFunction(name) {
  const start = html.indexOf(`function ${name}(`);
  assert(start >= 0, `找不到函数 ${name}`);
  const open = html.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}') {
      depth--;
      if (depth === 0) return html.slice(start, i + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号不完整`);
}

const ctx = { console, FILTER: 'favorited', RES_ONLY: true, RESULT: null };
vm.createContext(ctx);
for (const name of ['hasResource', 'matchFilter', 'recomputeIgnoredFlags']) {
  vm.runInContext(extractFunction(name), ctx, { filename: `index.html:${name}` });
}

function mergeAndRecompute(oldItem, responseItem, lists) {
  ctx.RESULT = {
    items: [{ ...oldItem, ...responseItem }],
    ignored: lists.ignored || [],
    ignored_makers: lists.ignored_makers || [],
    favorites: lists.favorites || [],
  };
  vm.runInContext('recomputeIgnoredFlags()', ctx);
  return ctx.RESULT.items[0];
}

// 1. 本地不存在：收藏 + 只看有资源中检测后仍应显示，资源和评级保留。
{
  const oldItem = {
    rj: 'RJ01561365', status: 'missing', favorited: true,
    resources: { alist: { f: true } }, rating: 4.50, rating_manual: true,
  };
  const it = mergeAndRecompute(
    oldItem,
    { rj: oldItem.rj, status: 'missing', favorited: true, ignored: false },
    { favorites: [oldItem.rj] });
  assert.strictEqual(it.status, 'missing');
  assert.strictEqual(it.favorited, true);
  assert.strictEqual(it.resources.alist.f, true);
  assert.strictEqual(it.rating, 4.50);
  ctx.FILTER = 'favorited'; ctx.RES_ONLY = true;
  assert.strictEqual(vm.runInContext('matchFilter(RESULT.items[0])', ctx), true);
}

// 2. 检测为已下载：脱离收藏、进入已下载，资源和评级仍保留。
{
  const oldItem = {
    rj: 'RJ000001', status: 'missing', favorited: true,
    resources: { asmrone: { f: true } }, rating: 4.75, rating_manual: true,
  };
  const it = mergeAndRecompute(
    oldItem,
    { rj: oldItem.rj, status: 'owned', favorited: false, ignored: false, matched: [oldItem.rj] },
    { favorites: [] });
  assert.strictEqual(it.status, 'owned');
  assert.strictEqual(it.favorited, false);
  assert.strictEqual(it.resources.asmrone.f, true);
  assert.strictEqual(it.rating, 4.75);
  assert.strictEqual(ctx.RESULT.favorites.includes(it.rj), false);
  ctx.FILTER = 'favorited';
  assert.strictEqual(vm.runInContext('matchFilter(RESULT.items[0])', ctx), false);
  ctx.FILTER = 'owned';
  assert.strictEqual(vm.runInContext('matchFilter(RESULT.items[0])', ctx), true);
}

// 3. 收藏/忽略数组重算只改状态，不得清掉响应注入字段。
{
  const oldItem = {
    rj: 'RJ000002', status: 'missing', favorited: false,
    resources: { alist: { f: true } }, rating: 3.50,
  };
  const it = mergeAndRecompute(oldItem, {}, { favorites: [oldItem.rj], ignored: [] });
  assert.strictEqual(it.favorited, true);
  assert.strictEqual(it.resources.alist.f, true);
  assert.strictEqual(it.rating, 3.50);
}

// 4. “只看有资源”必须按任一站点 f=true 判断。
{
  ctx.RESULT = { items: [
    { rj: 'A', status: 'missing', resources: { alist: { f: false } } },
    { rj: 'B', status: 'missing', resources: { alist: { f: true } } },
  ], ignored: [], ignored_makers: [], favorites: [] };
  ctx.RES_ONLY = true; ctx.FILTER = 'missing';
  assert.strictEqual(vm.runInContext('matchFilter(RESULT.items[0])', ctx), false);
  assert.strictEqual(vm.runInContext('matchFilter(RESULT.items[1])', ctx), true);
}

// 5. 静态接线检查：防止以后又改回整项覆盖或漏掉全局清单同步。
{
  assert(!html.includes('RESULT.items[i] = r.item;'));
  assert(html.includes('RESULT.items[i] = {...RESULT.items[i], ...r.item};'));
  const start = html.indexOf('async function recheckItem(');
  const end = html.indexOf('\nfunction startRename', start);
  const block = html.slice(start, end);
  for (const field of ['r.ignored', 'r.ignored_makers', 'r.favorites']) {
    assert(block.includes(field), `检测本地未同步 ${field}`);
  }
  assert(block.includes('recomputeIgnoredFlags();'));
}

// 6. 搜索结果链接复制和 Cookie 分步提示必须保持接线完整。
{
  assert(html.includes('title="复制这条比对的 DLsite 搜索结果链接"'));
  assert(html.includes("copyText(decodeURIComponent('${ue(r.url||\'\')}'), this)"));
  assert(html.includes('id="cookieHelp"'));
  assert(html.includes('如何获取 DLsite Cookie？（点击查看分步说明）'));
  assert(html.includes('请求标头 / Request Headers'));
  assert(html.includes('Cookie: 后面的完整值'));
}

console.log('state_sync_test.js: 6 groups passed');
