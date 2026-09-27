<script setup>
/**
 * WPE 封包编辑器（网页端）。
 *
 * 抓包点在适配层的转发路径上，不注入任何目标进程。
 *
 * ★ 列表默认走「新→旧」，翻旧页靠 before 游标（适配层返回 oldest_id / has_more）。
 *   没有这个入口的话，缓冲 4000 条时后 2000 条永远看不到 —— 旧栈就是这个毛病。
 * ★ 轮询用 since=newest_id 增量取，不整表重拉 —— 4000 条的列表每 3 秒重拉一次
 *   是自找的卡顿，而且会把用户翻到的旧页冲掉。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { wpeApi } from '../api'
import PacketTable from '../components/wpe/PacketTable.vue'
import PacketDetail from '../components/wpe/PacketDetail.vue'
import RewritePanel from '../components/wpe/RewritePanel.vue'
import HoldPanel from '../components/wpe/HoldPanel.vue'
import SendPanel from '../components/wpe/SendPanel.vue'
import ExportPanel from '../components/wpe/ExportPanel.vue'

const stat = ref(null)
const packets = ref([])
const meta = ref({})
const hasMore = ref(false)
const loading = ref(false)
const error = ref('')

const selectedId = ref(null)
const detail = ref(null)
const struct = ref(null)
const detailBusy = ref(false)

const tab = ref('rewrite')
const rewriteState = ref(null)
const holdState = ref(null)
// 断点队列里的封包。适配层不在封包上打 hold 标记，唯一可信来源是
// only_hold 过滤 —— 用列表里的 p.hold 猜会永远猜成「没有」。
const heldPackets = ref([])
const heldIds = computed(() => heldPackets.value.map((p) => p.id))

const autoRefresh = ref(true)
const filter = ref({
  dir: '', proto: '', app: '', target: '', hex: '', label: '',
  min_len: '', max_len: '', only_hold: false, limit: 300,
})

let timer = null

const selectedBrief = computed(() => packets.value.find((p) => p.id === selectedId.value) || null)

function query(extra = {}) {
  const f = filter.value
  const q = { limit: f.limit || 300, ...extra }
  for (const k of ['dir', 'proto', 'app', 'target', 'hex', 'label']) {
    if (f[k]) q[k] = f[k]
  }
  if (f.min_len !== '') q.min_len = Number(f.min_len)
  if (f.max_len !== '') q.max_len = Number(f.max_len)
  if (f.only_hold) q.only_hold = true
  return q
}

async function loadStat() {
  try {
    const r = await wpeApi.stat()
    stat.value = r.stat || r.data?.stat || null
  } catch (e) { /* 状态条失败不打断主流程，但也不假装成功 */ }
}

async function loadRewrite() {
  try {
    const r = await wpeApi.rewrite()
    rewriteState.value = r.data || r
  } catch (e) { error.value = e.message }
}

async function loadHold() {
  try {
    const r = await wpeApi.hold()
    holdState.value = r.data || r
  } catch (e) { error.value = e.message }
  // 队列内容单独取一次（only_hold），和 hold 状态分开 —— 一个挂了不代表另一个也挂。
  try {
    const r = await wpeApi.packets({ only_hold: true, limit: 200 })
    heldPackets.value = r.packets || []
  } catch (e) { heldPackets.value = [] }
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const r = await wpeApi.packets(query())
    packets.value = r.packets || []
    meta.value = r
    hasMore.value = !!r.has_more
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function poll() {
  if (!autoRefresh.value || !packets.value.length) return
  const newest = packets.value[0].id
  try {
    const r = await wpeApi.packets(query({ since: newest }))
    const fresh = r.packets || []
    if (fresh.length) {
      packets.value = fresh.concat(packets.value)
      // 只更新增量相关的元信息，别把已翻页的状态冲掉
      meta.value = { ...meta.value, buffered: r.buffered, newest_id: r.newest_id }
    }
  } catch (e) { /* 轮询失败静默重试，不刷屏报错 */ }
}

async function more() {
  const oldest = packets.value.length ? packets.value[packets.value.length - 1].id : 0
  if (!oldest) return
  loading.value = true
  try {
    const r = await wpeApi.packets(query({ before: oldest }))
    packets.value = packets.value.concat(r.packets || [])
    hasMore.value = !!r.has_more
    meta.value = { ...meta.value, scanned: r.scanned, has_more: r.has_more }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function select(id) {
  selectedId.value = id
  detailBusy.value = true
  error.value = ''
  try {
    const r = await wpeApi.packet(id)
    detail.value = r.packet || null
    struct.value = r.struct || null
    if (!r.ok && r.err) error.value = r.err
  } catch (e) {
    error.value = e.message
    detail.value = null
    struct.value = null
  } finally {
    detailBusy.value = false
  }
}

async function release({ id, action, hex }) {
  detailBusy.value = true
  error.value = ''
  try {
    await wpeApi.release(id, action, hex)
    await Promise.all([loadHold(), refresh()])
    if (selectedId.value === id) await select(id)
  } catch (e) {
    error.value = e.message
  } finally {
    detailBusy.value = false
  }
}

async function replay(payload) {
  detailBusy.value = true
  error.value = ''
  try {
    const { id, ...rest } = payload
    const r = await wpeApi.replay(id, rest)
    const d = r.data || r
    error.value = d.ok ? '' : (d.err || '重放失败')
    if (d.ok) setTimeout(() => { loadStat(); poll() }, 400)
  } catch (e) {
    error.value = e.message
  } finally {
    detailBusy.value = false
  }
}

function resetFilter() {
  filter.value = { dir: '', proto: '', app: '', target: '', hex: '', label: '',
                   min_len: '', max_len: '', only_hold: false, limit: 300 }
  refresh()
}

function onSaved() {
  loadRewrite(); loadHold(); loadStat()
}

onMounted(async () => {
  await Promise.all([loadStat(), loadRewrite(), loadHold()])
  await refresh()
  timer = setInterval(() => { loadStat(); poll() }, 3000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

const counters = computed(() => {
  const s = stat.value || {}
  return [
    { k: '捕获', v: s.captured },
    { k: '缓冲', v: s.buffered !== undefined ? `${s.buffered}/${s.capacity}` : undefined },
    { k: '断点中', v: s.holding },
    { k: '改写命中', v: s.rewritten },
    { k: '重放', v: s.replayed },
    { k: '发送', v: s.sent },
    { k: '丢弃', v: s.dropped },
    { k: '截断', v: s.truncated },
  ].filter((x) => x.v !== undefined)
})

const udp = computed(() => (stat.value && stat.value.udp) || null)
</script>

<template>
  <div class="wpe">
    <!-- 状态条 -->
    <div class="statbar panel">
      <div class="counters">
        <div v-for="c in counters" :key="c.k" class="ctr">
          <div class="ctr-k">{{ c.k }}</div>
          <div class="ctr-v mono">{{ c.v }}</div>
        </div>
      </div>
      <div v-if="udp" class="udp" :title="'UDP 中继账本：orphan - adopted 就是确实没转出去的包数'">
        <span class="udp-t">UDP 中继</span>
        <span class="tag">in {{ udp.in_pkts }}</span>
        <span class="tag">out {{ udp.out_pkts }}</span>
        <span class="tag" :class="udp.orphan > udp.adopted ? 'warn' : ''">orphan {{ udp.orphan }}</span>
        <span class="tag" :class="udp.orphan > udp.adopted ? 'warn' : 'ok'">adopted {{ udp.adopted }}</span>
        <span class="tag">wild {{ udp.wild }}</span>
        <span class="tag">routes {{ udp.routes }}</span>
        <span class="tag">pending {{ udp.pending }}</span>
      </div>
      <span class="spacer" />
      <label class="chk"><input v-model="autoRefresh" type="checkbox" />自动刷新</label>
      <button class="btn sm" :disabled="loading" @click="refresh">刷新</button>
    </div>

    <div v-if="error" class="err-box">{{ error }}</div>

    <!-- 过滤 -->
    <div class="panel filters">
      <div class="body">
        <div class="fgrid">
          <div class="field"><label>方向</label>
            <select v-model="filter.dir" @change="refresh">
              <option value="">不限</option>
              <option value="c2s">c2s 发</option>
              <option value="s2c">s2c 收</option>
            </select>
          </div>
          <div class="field"><label>协议</label>
            <input v-model="filter.proto" class="mono" placeholder="tcp/udp/http"
                   @keyup.enter="refresh" /></div>
          <div class="field"><label>应用层</label>
            <input v-model="filter.app" class="mono" placeholder="HTTP/TLS/DNS"
                   @keyup.enter="refresh" /></div>
          <div class="field"><label>目标包含</label>
            <input v-model="filter.target" class="mono" placeholder="m.baidu.com"
                   @keyup.enter="refresh" /></div>
          <div class="field"><label>载荷 hex 包含</label>
            <input v-model="filter.hex" class="mono" placeholder="474554"
                   @keyup.enter="refresh" /></div>
          <div class="field"><label>标签包含</label>
            <input v-model="filter.label" class="mono" placeholder="wpe-send"
                   @keyup.enter="refresh" /></div>
          <div class="field pair"><label>长度区间</label>
            <div class="row">
              <input v-model="filter.min_len" class="mono" placeholder="min" @keyup.enter="refresh" />
              <input v-model="filter.max_len" class="mono" placeholder="max" @keyup.enter="refresh" />
            </div>
          </div>
          <div class="field"><label>单次条数</label>
            <input v-model="filter.limit" class="mono" placeholder="300（上限 2000）"
                   @keyup.enter="refresh" /></div>
        </div>
        <div class="row">
          <label class="chk"><input v-model="filter.only_hold" type="checkbox" @change="refresh" />只看断点队列</label>
          <span class="spacer" />
          <button class="btn sm ghost" @click="resetFilter">重置</button>
          <button class="btn sm primary" :disabled="loading" @click="refresh">应用筛选</button>
        </div>
      </div>
    </div>

    <!-- 列表 + 详情 -->
    <div class="split">
      <section class="panel list-panel">
        <header>
          <span>封包列表</span>
          <span class="spacer" />
          <span class="hint">新 → 旧</span>
        </header>
        <PacketTable :packets="packets" :selected-id="selectedId" :has-more="hasMore"
                     :held-ids="heldIds" :loading="loading" :meta="meta"
                     @select="select" @more="more" />
      </section>

      <section class="panel detail-panel">
        <header>
          <span>详情</span>
          <span class="spacer" />
          <span v-if="detailBusy" class="hint">加载中…</span>
          <span v-else-if="detail" class="hint">#{{ detail.id }}</span>
        </header>
        <div class="body">
          <PacketDetail :packet="detail" :struct="struct"
                        :held="selectedId !== null && heldIds.indexOf(selectedId) !== -1"
                        :busy="detailBusy"
                        @replay="replay" @release="release" />
        </div>
      </section>
    </div>

    <!-- 工具面板 -->
    <div class="panel tools">
      <header>
        <div class="tabs">
          <button v-for="t in [
              { k: 'rewrite', l: '自动改写' },
              { k: 'hold', l: '断点' },
              { k: 'send', l: '发送 / 重放' },
              { k: 'export', l: '导出 / 导入' },
            ]" :key="t.k" class="tab" :class="{ on: tab === t.k }" @click="tab = t.k">
            {{ t.l }}
          </button>
        </div>
        <span class="spacer" />
        <span v-if="tab === 'rewrite' && rewriteState" class="tag" :class="rewriteState.on ? 'ok' : ''">
          改写 {{ rewriteState.on ? '已开启' : '关闭' }}
        </span>
        <span v-if="tab === 'hold' && holdState" class="tag" :class="holdState.on ? 'hold' : ''">
          断点 {{ holdState.on ? '已开启' : '关闭' }}
        </span>
      </header>
      <div class="body">
        <RewritePanel v-if="tab === 'rewrite'" :state="rewriteState" @saved="onSaved" />
        <HoldPanel v-else-if="tab === 'hold'" :state="holdState"
                   :held="heldPackets" :busy="detailBusy"
                   @saved="onSaved" @refresh="loadHold" @release="release" />
        <SendPanel v-else-if="tab === 'send'" :selected="detail" @done="refresh" />
        <ExportPanel v-else :selected="selectedBrief" @done="refresh" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.wpe { display: flex; flex-direction: column; gap: 10px; }
.spacer { flex: 1; }

.statbar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; flex-wrap: wrap; }
.counters { display: flex; gap: 14px; flex-wrap: wrap; }
.ctr-k { font-size: 11px; color: var(--fg-faint); }
.ctr-v { font-weight: 600; }
.udp { display: flex; align-items: center; gap: 4px; padding-left: 12px;
       border-left: 1px solid var(--line); flex-wrap: wrap; }
.udp-t { font-size: 11px; color: var(--fg-faint); margin-right: 3px; }

.filters .fgrid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(136px, 1fr));
  gap: 8px; margin-bottom: 8px;
}
.filters .pair .row { gap: 6px; }

.split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
.list-panel { display: flex; flex-direction: column; min-height: 340px; max-height: 620px; }
.list-panel :deep(.tbl-wrap) { flex: 1; min-height: 0; }
.detail-panel { min-height: 340px; max-height: 620px; overflow: auto; }

.tools header { padding: 0 10px; }
.tabs { display: flex; gap: 2px; }
.tab {
  padding: 9px 12px; border: none; background: transparent; cursor: pointer;
  border-bottom: 2px solid transparent; color: var(--fg-muted);
}
.tab:hover { color: var(--fg); }
.tab.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

@media (max-width: 1200px) { .split { grid-template-columns: 1fr; } }
</style>
