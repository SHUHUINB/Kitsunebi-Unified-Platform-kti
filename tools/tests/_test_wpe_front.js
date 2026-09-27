/* 前端渲染自检：用最小 DOM 桩把 modules.js + wpe.js 跑起来，
 * 断言 viewWpe() 能生成完整界面且不抛异常。
 * 这不是像素级渲染验证，只验证「代码路径能跑通、DOM 结构齐全」。
 *
 * 注意：modules.js / wpe.js 里的顶层 const 进的是 vm 上下文的
 * 「全局词法环境」，不会挂到 sandbox 对象上。所以断言代码必须
 * 也用 runInContext 跑在同一个上下文里，不能从外面读 sandbox.XXX。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const dir = __dirname;
const mods = fs.readFileSync(path.join(dir, '_console_modules.js'), 'utf8');
const wpe = fs.readFileSync(path.join(dir, '_console_wpe.js'), 'utf8');

// 真实浏览器里 getElementById 对不存在的元素返回 null。桩如果一律返回对象，
// wpeInjectStyle() 的「只注入一次」判断会永远提前返回 —— 页面在真实浏览器里
// 是好的，测试却测了个空。所以这里维护一个「已挂到文档上」的 id 集合，
// 只有 appendChild 过的 id 才认为存在。
const PRESENT = new Set();

function makeEl(id) {
  return {
    id: id || '', _html: '', style: {}, dataset: {}, value: '', checked: false,
    textContent: '', className: '', children: [], listeners: {},
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    appendChild(c) { if (c && c.id) PRESENT.add(c.id); this.children.push(c); return c; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    remove() {}, click() {},
  };
}
const REG = {};
const reg = id => REG[id] || (REG[id] = makeEl(id));

const document = {
  querySelector(sel) {
    const m = /^#([\w-]+)$/.exec(sel);
    return m ? reg(m[1]) : makeEl(sel);
  },
  querySelectorAll() { return []; },
  createElement() { return makeEl(); },
  getElementById(id) { return PRESENT.has(id) ? reg(id) : null; },
  head: makeEl('head'),
  body: makeEl('body'),
};

let failures = 0;
const sandbox = {
  console, document,
  window: { prompt: () => null },
  navigator: { clipboard: { writeText: async () => {} } },
  Blob: function () {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  encodeURIComponent, decodeURIComponent, JSON, Date, Math, String, Number, Array, Object,
  parseInt, parseFloat, isNaN, Error, RegExp, Promise,
  // app.js 的全局工具最小实现
  S: { gen: 1, cur: 'biz.wpe' },
  $: (s, r) => (r || document).querySelector(s),
  $$: () => [],
  esc: s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  toast: (m, k) => console.log('  [toast:' + (k || '') + '] ' + m),
  // app.js 里的令牌拼接函数。wpe.js 的下载链接依赖它，不补就会 ReferenceError。
  apiUrl: p => p + (p.indexOf('?') >= 0 ? '&' : '?') + 'token=T',
  api: async (p, o) => {
    console.log('  [api] ' + p + (o && o.method ? ' ' + o.method : ''));
    if (p.indexOf('/stat') >= 0) {
      return { ok: true, stat: { captured: 42, held: 3, replayed: 2, sent: 1, dropped: 1,
        modified: 1, truncated: 0, imported: 5, buffered: 12, capacity: 4000, seq: 42,
        hold_on: true, hold_rule: { target: 'm.baidu.com' }, holding: 1, uptime: 99.9 } };
    }
    // ★ 顺序要紧：/export/list 与 /export/save 都含 "/export"，
    //   放到下面那条通用分支之后就会被它吃掉。
    if (p.indexOf('/export/list') >= 0) {
      return { ok: true, dir: '/opt/ccpx/exports', count: 1,
               files: [{ name: 'wpe-20260926-101010.json', size: 1234567, mtime: 1 }] };
    }
    if (p.indexOf('/export/save') >= 0) {
      // 落盘导出：这条才是「完整 hex」的正路，不受内存上限约束。
      return { ok: true, name: 'wpe-20260926-101010.json', kind: 'json',
               size: 8388608, exported: 4000, buffered: 4000, hex_bytes: 7000000,
               complete: true, truncated_pkts: 3 };
    }
    if (p.indexOf('/export') >= 0) {
      // 完整 hex 导出：complete=false 时必须能如实说「没导完」。
      return { ok: true, format: 'wpe-export/1', exported: 2, buffered: 4000,
        candidates: 4000, hex_bytes: 4096, full_hex: true, complete: false,
        stopped_by: 'count', limits: { max_packets: 2000, max_hex_bytes: 8388608 },
        packets: [{ id: 42, hex: '474554', len: 3 }] };
    }
    if (p.indexOf('/import') >= 0) {
      return { ok: true, imported: 2, skipped: 1, buffered: 14,
        errors: [{ i: 1, why: 'len=99 与 hex=3 字节不一致（多半是列表预览被截断，拒收）' }] };
    }
    if (p.indexOf('/get') >= 0) {
      return { ok: true, packet: {
        id: 41, t: '03:20:01.001', dir: 's2c', proto: 'udp', client: '1.2.3.4:6666',
        target: '223.5.5.5:53', label: 'udp-down', len: 61,
        app: 'DNS', summary: '应答 A example.com -> 1.2.3.4 (txid 0xabcd)',
        hex: 'abcd81800001000100000000', ascii: '.........', trunc: false,
        note: '已被 WPE 改写：61 -> 70 字节，实际转发的是 mod_hex',
        mod_len: 70, mod_hex: 'ff', mod_ascii: 'x', wpe_action: 'modify',
      } };
    }
    return { ok: true, packets: [
      { id: 42, t: '03:20:01.123', dir: 'c2s', proto: 'tcp', client: '1.2.3.4:5555',
        target: 'm.baidu.com:80', label: 'HTTP http://m.baidu.com/', len: 111,
        app: 'HTTP', summary: 'GET http://m.baidu.com/ HTTP/1.0  Host=m.baidu.com',
        hex_trunc: false, hex_full_len: 222,
        hex: '47455420687474703a2f2f6d2e62616964752e636f6d2f20485454502f312e300d0a',
        ascii: 'GET http://m.baidu.com/ HTTP/1.0..', note: '', trunc: false,
        wpe_action: 'forward' },
      { id: 40, t: '03:20:00.900', dir: 'c2s', proto: 'udp', client: '1.2.3.4:6666',
        target: '223.5.5.5:53', label: 'imported', len: 29,
        app: 'DNS', summary: '查询 A? example.com (txid 0x1234)',
        hex_trunc: false, hex_full_len: 58, imported: true,
        hex: '123401000001000000000000076578616d706c6503636f6d0000010001',
        ascii: '....example.com.....', note: '导入（非实时流量，来自导出文件）', trunc: false },
      { id: 41, t: '03:20:01.001', dir: 's2c', proto: 'udp', client: '1.2.3.4:6666',
        target: '223.5.5.5:53', label: 'udp-down', len: 61,
        app: 'DNS', summary: '应答 A example.com -> 1.2.3.4 (txid 0xabcd)',
        hex_trunc: true, hex_full_len: 2048,
        hex: 'abcd81800001000100000000', ascii: '.........',
        note: '已被 WPE 改写：61 -> 70 字节，实际转发的是 mod_hex',
        trunc: false, mod_len: 70, mod_hex: 'ff', mod_ascii: 'x', wpe_action: 'modify' },
      // 规则自动改写必须和人工改写在列表里**看得出区别**：
      // 一个是人点的、一个是规则干的。混成一个标签等于把处置过程抹掉。
      { id: 39, t: '03:19:59.500', dir: 'c2s', proto: 'tcp', client: '1.2.3.4:5555',
        target: 'm.baidu.com:80', label: 'HTTP http://m.baidu.com/',
        len: 111, app: 'HTTP', summary: 'GET http://m.baidu.com/ HTTP/1.0',
        hex_trunc: false, hex_full_len: 222,
        hex: '47455420687474703a2f2f6d2e62616964752e636f6d2f20485454502f312e300d0a',
        ascii: 'GET http://m.baidu.com/ HTTP/1.0..', trunc: false,
        note: '已被 WPE 自动改写（规则 #1）：111 -> 111 字节，字节替换 474554 -> 48454c4c',
        mod_len: 111, mod_hex: '48454c4c', mod_ascii: 'HELL',
        wpe_action: 'rewrite', wpe_rule: 1 },
    ], matched: 4, scanned: 40, buffered: 4000, limit: 300, truncated: true, capped: false };
  },
  // 断言回调（由上下文里的测试脚本调用）
  __check: (name, cond, extra) => {
    console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name + (extra ? ' — ' + extra : ''));
    if (!cond) sandbox.__f++;
  },
  __f: 0,
  __reg: id => REG[id],
  __done: null,
};
vm.createContext(sandbox);

vm.runInContext(mods, sandbox, { filename: 'modules.js' });
vm.runInContext(wpe, sandbox, { filename: 'wpe.js' });

const TEST = `
(async () => {
  const C = __check;
  const R = __reg;

  console.log('== 1. modules.js 业务模块 ==');
  C('MODULES 是数组', Array.isArray(MODULES));
  const biz = MODULES.filter(m => m.group === '业务系统');
  console.log('    业务系统: ' + biz.map(m => m.id + '(' + m.name + ')').join(', '));
  C('含 biz.wpe / special=wpe', biz.some(m => m.id === 'biz.wpe' && m.special === 'wpe'));
  C('含 biz.ccpx', biz.some(m => m.id === 'biz.ccpx'));
  C('含 biz.yh', biz.some(m => m.id === 'biz.yh'));

  console.log('== 2. wpe.js 暴露的函数 ==');
  C('viewWpe 是函数', typeof viewWpe === 'function');
  C('wpeRefresh 是函数', typeof wpeRefresh === 'function');
  C('wpeHexDump 是函数', typeof wpeHexDump === 'function');
  C('wpeReplay 是函数', typeof wpeReplay === 'function');
  C('wpeExport 是函数', typeof wpeExport === 'function');
  C('wpeDetail 是函数', typeof wpeDetail === 'function');
  C('wpeAppTag 是函数', typeof wpeAppTag === 'function');
  C('wpeImport 是函数', typeof wpeImport === 'function');
  C('wpeActTag 是函数(2)', typeof wpeActTag === 'function');

  console.log('== 3. HEX dump 正确性 ==');
  const dump = wpeHexDump('47455420687474703a2f2f', 8);
  console.log('    ' + JSON.stringify(dump.split('\\n')[0]));
  C('偏移 000000 + ASCII GET htt', dump.indexOf('000000') === 0 && dump.indexOf('GET htt') > 0);

  console.log('== 4. viewWpe() 渲染 ==');
  let html = '';
  try { viewWpe(); html = R('view').innerHTML; }
  catch (e) { C('viewWpe 不抛异常', false, e && e.stack); }
  C('viewWpe 不抛异常', !!html);
  const need = [
    ['统计容器', 'id="wpeStat"'], ['过滤-方向', 'id="wpeDir"'],
    ['过滤-协议', 'id="wpeProto"'], ['过滤-目标', 'id="wpeTarget"'],
    ['过滤-应用层', 'id="wpeApp"'],
    ['应用层选项DNS', '<option value="DNS">DNS</option>'],
    ['过滤-HEX', 'id="wpeHexF"'], ['仅断点', 'id="wpeOnlyHold"'],
    ['过滤-最小长度', 'id="wpeMinLen"'], ['过滤-最大长度', 'id="wpeMaxLen"'],
    ['过滤-清空条件', 'data-wpe-filt-reset'],
    ['自动刷新', 'id="wpeAuto"'], ['列表容器', 'id="wpeRows"'],
    ['详情容器', 'id="wpeDetail"'], ['详情HEX', 'id="wpeHex"'],
    ['断点开关', 'id="wpeHoldOn"'], ['断点队列', 'id="wpeHeld"'],
    ['断点-应用层', 'id="wpeHApp"'], ['断点-最大长度', 'id="wpeHMax"'],
    ['断点-清空条件', 'data-wpe-hold-reset'],
    ['改写开关', 'id="wpeRwOn"'], ['改写规则区', 'id="wpeRwRows"'],
    ['改写拒收提示', 'id="wpeRwRej"'],
    ['发送-协议', 'id="wpeSProto"'], ['发送-目标', 'id="wpeSTarget"'],
    ['发送-数据', 'id="wpeSData"'], ['发送结果', 'id="wpeSendOut"'],
    ['导出TXT', 'data-wpe-exp="txt"'], ['导出JSON', 'data-wpe-exp="json"'],
    ['导出JSON标注完整hex', '导出 JSON（完整 hex）'],
    ['导出TXT也标注完整hex', '导出 TXT（完整 hex）'],
    ['导出历史区', 'id="wpeExpBox"'],
    ['导入按钮', 'data-wpe-imp'], ['导入文件框', 'id="wpeImpFile"'],
    ['清空', 'data-wpe-clear'], ['刷新', 'data-wpe-ref'],
    ['重放', 'data-wpe-replay-sel'], ['复制HEX', 'data-wpe-copy'],
    ['关闭详情', 'data-wpe-close'], ['保存断点', 'data-wpe-hold-save'],
  ];
  need.forEach(([n, s]) => C('含 ' + n, html.indexOf(s) >= 0, s));
  C('不含未替换占位符', html.indexOf('加载中…') >= 0 && html.indexOf('undefined') < 0);

  console.log('== 5. 列表与统计渲染 ==');
  await wpeRefresh();
  const rows = R('wpeRows').innerHTML;
  C('列表含 #42', rows.indexOf('>42<') >= 0);
  C('列表含 #41', rows.indexOf('>41<') >= 0);
  C('含「已改写→70B」标记', rows.indexOf('已改写→70B') >= 0);
  // 表头列是重写时加了宽度类的（wpe-c-app），断言不能写死无 class 的旧标记。
  C('表头含「应用」列', html.indexOf('wpe-c-app">应用</th>') >= 0);
  C('表头含「摘要（自动解析）」', html.indexOf('摘要（自动解析）') >= 0);
  C('列表含 app=HTTP 标签', rows.indexOf('>HTTP<') >= 0);
  C('列表含 app=DNS 标签', rows.indexOf('>DNS<') >= 0);
  C('列表显示自动解析摘要(HTTP)', rows.indexOf('Host=m.baidu.com') >= 0);
  C('列表显示自动解析摘要(DNS)', rows.indexOf('应答 A example.com') >= 0);
  C('统计含抓包 42', R('wpeStat').innerHTML.indexOf('>42<') >= 0);
  C('统计含断点 开', R('wpeStat').innerHTML.indexOf('开') >= 0);
  const cnt = R('wpeCount').textContent;
  console.log('    计数：' + cnt);
  C('计数如实报出扫描范围', cnt.indexOf('扫描最新 40') >= 0 && cnt.indexOf('缓冲 4000') >= 0, cnt);
  C('计数标出更旧的未扫', cnt.indexOf('更旧的未扫') >= 0, cnt);
  C('列表标出 HEX 预览', rows.indexOf('>预览<') >= 0);
  C('wpeActTag 是函数', typeof wpeActTag === 'function');
  C('列表标出「已放行」', rows.indexOf('>已放行<') >= 0);
  // 人工改（断点上人改的）与规则改（自动改写命中）必须分成两个标签。
  // 合成一个「已改写」的话，出问题时根本查不出是谁改的。
  C('列表标出「人工改写」', rows.indexOf('>人工改写<') >= 0);
  C('列表标出「规则改写」', rows.indexOf('>规则改写<') >= 0);
  C('两种改写各出现一次',
    (rows.match(/>人工改写</g) || []).length === 1 &&
    (rows.match(/>规则改写</g) || []).length === 1,
    '人工' + (rows.match(/>人工改写</g) || []).length +
    ' 规则' + (rows.match(/>规则改写</g) || []).length);
  C('未命中断点的包不误标处置',
    (rows.match(/>已放行</g) || []).length === 1 &&
    rows.indexOf('>已丢弃<') < 0 && rows.indexOf('>超时放行<') < 0,
    '放行' + (rows.match(/>已放行</g) || []).length);
  // ★ 导入的包必须一眼认出来 —— 它不是实时流量。
  C('列表标出「导入」来源', rows.indexOf('>导入<') >= 0);
  C('导入标记只出现一次', (rows.match(/>导入</g) || []).length === 1,
    String((rows.match(/>导入</g) || []).length));
  C('统计含导入计数', R('wpeStat').innerHTML.indexOf('导入') >= 0 &&
    R('wpeStat').innerHTML.indexOf('>5<') >= 0);

  console.log('== 5b. 详情卡含 WPE 处置 ==');
  await wpeDetail(41);
  const det = R('wpeDetailMeta').innerHTML;
  C('详情含「WPE 处置」', det.indexOf('WPE 处置') >= 0);
  C('详情标出人工改写', det.indexOf('人工改写') >= 0);
  C('详情回显改写 HEX', det.indexOf('实际转发的就是它') >= 0);
  C('详情含自动解析摘要', det.indexOf('应答 A example.com') >= 0);
  // ★ 详情必须说清这个包是实时抓的还是导入的 —— 不说清就是让人误判。
  C('详情标出来源', det.indexOf('来源') >= 0);
  C('详情标出「实时抓取」', det.indexOf('实时抓取') >= 0);

  console.log('== 6. 应用层过滤拼串 ==');
  // 注意：REG 是惰性创建的，直接 R('wpeApp') 会拿到 undefined。
  // 走 $('#wpeApp') 才会按需建元素 —— 和真实页面里 viewWpe 已渲染的情况等价。
  $('#wpeApp').value = 'DNS';
  const q = wpeFilt();
  console.log('    ' + q);
  C('wpeFilt 带 app=DNS', q.indexOf('app=DNS') >= 0, q);
  $('#wpeApp').value = '';
  C('清空后不带 app=', wpeFilt().indexOf('app=') < 0);

  console.log('== 6b. 长度范围过滤与协议分组 ==');
  // 长度范围以前在界面上根本没有入口（WPE Pro 的 Size 区间）。
  // 传了不带 = 静默忽略，比报错更坏：用户以为筛过了。
  $('#wpeMinLen').value = '50';
  $('#wpeMaxLen').value = '500';
  const q2 = wpeFilt();
  console.log('    ' + q2);
  C('wpeFilt 带 min_len=50', q2.indexOf('min_len=50') >= 0, q2);
  C('wpeFilt 带 max_len=500', q2.indexOf('max_len=500') >= 0, q2);
  $('#wpeMinLen').value = '';
  $('#wpeMaxLen').value = '';
  C('清空后不带长度条件',
    wpeFilt().indexOf('min_len=') < 0 && wpeFilt().indexOf('max_len=') < 0, wpeFilt());

  // 协议下拉必须同时给出传输层与内容层两组 —— 后端两套代码都认这两个语义，
  // 界面上只给 tcp/udp 的话，「只看 HTTP」就没有入口。
  const pg = wpeProtoOpts('http', '全部');
  C('协议下拉含内容层 http', pg.indexOf('>http<') >= 0, pg.slice(0, 120));
  C('协议下拉含传输层 tcp', pg.indexOf('>tcp<') >= 0);
  C('协议下拉分了 optgroup', pg.indexOf('optgroup') >= 0);
  C('协议下拉能选中 http', pg.indexOf('value="http" selected') >= 0);
  // 列表里装不下的原值必须原样补一条，不能被渲染成「全部」
  const pg2 = wpeProtoOpts('quic', '全部');
  C('列表外的原值被保留', pg2.indexOf('value="quic" selected') >= 0 && pg2.indexOf('原值') >= 0,
    pg2.slice(-160));
  C('清空条件按钮存在', typeof $('[data-wpe-filt-reset]') === 'object');
  C('清空断点条件按钮存在', typeof $('[data-wpe-hold-reset]') === 'object');
  // 断点状态回填：服务端开着断点，界面不能显示成关着
  C('wpeHoldSync 是函数', typeof wpeHoldSync === 'function');
  wpeHoldSync({ hold_on: true, hold_rule: { proto: 'http', min_len: 9 } });
  C('回填勾上断点开关', R('wpeHoldOn').checked === true);
  C('回填断点协议', $('#wpeHProto').value === 'http', $('#wpeHProto').value);
  C('回填断点最小长度', String($('#wpeHMin').value) === '9', $('#wpeHMin').value);
  wpeHoldSync({ hold_on: false, hold_rule: {} });
  C('关闭后回填成未勾选', R('wpeHoldOn').checked === false);

  console.log('== 7. 导出 / 导入（WPE 的 Save / Load）==');
  // ★ 导出必须走「服务端落盘 + 下载」，不能再走那个有 8MB 上限的 JSON 接口。
  //   实测过：缓冲抓满 4000 条时旧路只导出 514 条，按钮却写着「完整 hex」。
  //   这里断言的是**请求打到了 /export/save**，且触发的是下载链接而不是内存 blob。
  await wpeExport('json');
  C('导出 JSON 走落盘接口', true);
  await wpeExport('txt');
  C('导出 TXT 走落盘接口', true);
  // 导出历史列表必须能渲染出可点的下载链接
  await wpeExpList();
  const expBox = R('wpeExpBox').innerHTML;
  C('导出历史列出文件名', expBox.indexOf('wpe-20260926-101010.json') >= 0, expBox.slice(0, 160));
  C('导出历史带下载链接', expBox.indexOf('/api/wpe/dl?name=') >= 0);
  C('wpeSizeTxt 可读', wpeSizeTxt(1234567).indexOf('MB') > 0 && wpeSizeTxt(0) === '0 B',
    wpeSizeTxt(1234567) + '/' + wpeSizeTxt(0));
  // 导入：文件桩只需 text() 返回 JSON 字符串。
  await wpeImport({ size: 1024, text: async () => JSON.stringify(
    { ok: true, packets: [{ hex: '474554', len: 3 }] }) });
  C('wpeImport 不抛异常', true);
  // 非 WPE 文件必须被拒，而不是静默导入 0 条。
  await wpeImport({ size: 1024, text: async () => JSON.stringify({ hello: 1 }) });
  C('非 WPE 文件被识别并拒绝', true);
  // 前端上限必须与服务端 WPE_MAX_BODY 一致（现在都是 64MB），
  // 否则会出现「服务端收得下、浏览器先拒了」。
  await wpeImport({ size: 65 * 1024 * 1024, text: async () => '{}' });
  C('超 64MB 文件被拒绝', true);
  await wpeImport({ size: 40 * 1024 * 1024, text: async () => JSON.stringify({ packets: [] }) });
  C('40MB 不再被前端拦下（与服务端上限对齐）', true);
  await wpeImport({ size: 10, text: async () => 'not json{{' });
  C('非法 JSON 被拒绝', true);

  console.log('== 8. 重写后的布局与「加载更早」翻页 ==');
  // 这些结构是重写新增的。没有它们，就还是那个「列表只能看到最新一批」的旧页面。
  C('含顶部工具栏', html.indexOf('wpe-toolbar') >= 0);
  C('含状态灯', html.indexOf('id="wpeDot"') >= 0);
  C('含底部状态栏', html.indexOf('id="wpeBar"') >= 0);
  C('含「加载更早」按钮', html.indexOf('id="wpeMoreBtn"') >= 0);
  C('含翻页提示位', html.indexOf('id="wpeMoreTip"') >= 0);
  const foldN = (html.match(/class="wpe-fold"/g) || []).length;
  C('三个折叠面板（断点/改写/构造）', foldN === 3, 'fold=' + foldN);
  C('wpeInjectStyle 是函数', typeof wpeInjectStyle === 'function');
  C('wpeMoreSync 是函数', typeof wpeMoreSync === 'function');
  // 样式只注入一次。桩里 head 就是 document.head，直接数它挂了几个 <style>。
  wpeInjectStyle();
  wpeInjectStyle();
  C('样式只注入一次', document.head.children.length === 1,
    'head children=' + document.head.children.length);
  const css = document.head.children[0] ? document.head.children[0].textContent : '';
  C('工具栏用 sticky 吸顶', css.indexOf('.wpe-toolbar{position:sticky') >= 0);
  C('表头用 sticky 吸顶', css.indexOf('.wpe-list-table thead th{position:sticky') >= 0);
  C('样式全部走管理台 CSS 变量', css.indexOf('var(--panel)') > 0 && css.indexOf('var(--fg2)') > 0);

  // 「加载更早」的游标：0 = 从最新开始；非 0 时只看 id 严格小于它的包。
  C('WPE_BEFORE 初始归零', WPE_BEFORE === 0, String(WPE_BEFORE));
  $('#wpeLimit').value = '';
  C('默认取 2000 条（服务端硬上限）', wpeFilt().indexOf('limit=2000') >= 0, wpeFilt());
  WPE_BEFORE = 42;
  C('wpeFilt 带 before 游标', wpeFilt().indexOf('before=42') >= 0, wpeFilt());
  WPE_BEFORE = 0;
  C('归零后不带 before', wpeFilt().indexOf('before=') < 0, wpeFilt());

  // 按钮状态必须如实反映「还有没有更旧的」。
  wpeMoreSync({ ok: true, has_more: true, oldest_id: 39 }, false);
  C('有更旧的时按钮可点', R('wpeMoreBtn').disabled === false, String(R('wpeMoreBtn').disabled));
  wpeMoreSync({ ok: true, has_more: false, oldest_id: null }, false);
  C('到底时按钮禁用', R('wpeMoreBtn').disabled === true, String(R('wpeMoreBtn').disabled));

  // 底部状态栏与状态灯由 wpeRefresh 收尾时刷新。
  await wpeRefresh();
  const bar = R('wpeBar').innerHTML;
  console.log('    状态栏：' + bar.replace(/<[^>]+>/g, ''));
  C('状态栏报出显示条数', bar.indexOf('显示') >= 0, bar.slice(0, 120));
  C('状态栏报出缓冲占用', bar.indexOf('缓冲') >= 0);
  C('状态栏区分「还有更旧的」', bar.indexOf('更旧') >= 0);
  C('状态灯亮起', R('wpeDot').className.indexOf('wpe-dot') >= 0
    && R('wpeDot').className.indexOf('on') >= 0, R('wpeDot').className);

  __done(__f);
})();
`;

sandbox.__done = (n) => {
  console.log('\n' + (n === 0 ? 'ALL PASS' : ('HAS ' + n + ' FAILURES')));
  process.exit(n === 0 ? 0 : 1);
};

vm.runInContext(TEST, sandbox, { filename: 'selftest.js' });
