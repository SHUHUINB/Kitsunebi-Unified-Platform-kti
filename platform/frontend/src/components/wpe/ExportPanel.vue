<script setup>
/**
 * 导出 / 导入 / 清空。
 *
 * ★ 两条路要分清：
 *   - `/wpe/export`（分页 JSON）有响应上限，没导完会给 next_since，要接着导；
 *   - `/wpe/export/save`（落盘）不受上限约束 —— 这是拿**完整**数据的正路。
 *   界面上把「没导完」明确标出来，绝不让人以为一次就导全了。
 * ★ 导入的返回带 imported / skipped 与逐条跳过原因，原样展示 —— 跳过绝不静默。
 */
import { ref } from 'vue'
import { wpeApi } from '../../api'

const props = defineProps({ selected: { type: Object, default: null } })
const emit = defineEmits(['done', 'select'])

const busy = ref(false)
const error = ref('')
const okMsg = ref('')

const exportRes = ref(null)
const since = ref('')
const limit = ref('')
const maxHex = ref('')
const nextSince = ref('')

const files = ref([])
const kind = ref('json')
const importText = ref('')
const importRes = ref('')

function fail(e) { error.value = e.message; okMsg.value = '' }
function good(m) { okMsg.value = m; error.value = '' }

async function doExport() {
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const r = await wpeApi.exportPackets({
      since: since.value === '' ? undefined : Number(since.value),
      limit: limit.value === '' ? undefined : Number(limit.value),
      max_hex: maxHex.value === '' ? undefined : Number(maxHex.value),
    })
    exportRes.value = r
    nextSince.value = r.next_since !== undefined && r.next_since !== null ? String(r.next_since) : ''
    good('导出完成')
  } catch (e) { fail(e) } finally { busy.value = false }
}

function continueExport() {
  if (!nextSince.value) return
  since.value = nextSince.value
  doExport()
}

async function doSave() {
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const r = await wpeApi.exportSave({
      since: since.value === '' ? undefined : Number(since.value),
      kind: kind.value,
    })
    good('已落盘：' + (r.data && (r.data.name || r.data.path) ? (r.data.name || r.data.path) : JSON.stringify(r.data)))
    await loadFiles()
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function loadFiles() {
  busy.value = true; error.value = ''
  try {
    const r = await wpeApi.exports()
    files.value = (r.files || (r.data && r.data.files) || []).slice()
  } catch (e) { fail(e) } finally { busy.value = false }
}

function fmtSize(n) {
  if (n === undefined || n === null) return '—'
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}

function fmtTime(t) {
  if (!t) return '—'
  const d = new Date(t * 1000)
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function doImport() {
  busy.value = true; error.value = ''; okMsg.value = ''; importRes.value = null
  try {
    let arr
    try {
      const parsed = JSON.parse(importText.value)
      arr = Array.isArray(parsed) ? parsed : parsed.packets
    } catch (e) {
      throw new Error('JSON 解析失败：' + e.message)
    }
    if (!Array.isArray(arr) || !arr.length) throw new Error('packets 必须是非空数组')
    const r = await wpeApi.importPackets(arr)
    importRes.value = r.data || r
    good('导入完成')
    emit('done')
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function doClear() {
  if (!window.confirm('清空封包环形缓冲？只清缓冲，不动改写规则和断点设置。')) return
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    await wpeApi.clear()
    good('缓冲已清空')
    emit('done')
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function loadFile(name) {
  // 走 raw fetch，把文件原文读回来给用户看（超大时由浏览器/后端各自兜底）。
  const url = wpeApi.exportDownloadUrl(name)
  const resp = await fetch(url, { credentials: 'same-origin' })
  if (!resp.ok) { error.value = 'HTTP ' + resp.status; return }
  const text = await resp.text()
  importText.value = text.length > 400000 ? text.slice(0, 400000) : text
  good(`已把 ${name} 读进下方文本框（${text.length} 字符）`)
}

loadFiles()
</script>

<template>
  <div class="col">
    <div v-if="error" class="err-box">{{ error }}</div>
    <div v-if="okMsg" class="ok-box">{{ okMsg }}</div>

    <!-- 分页导出 -->
    <div class="panel">
      <header><span>导出（分页 JSON）</span></header>
      <div class="body col">
        <div class="grid3">
          <div class="field"><label>since（从哪个 id 之后）</label>
            <input v-model="since" class="mono" placeholder="0" /></div>
          <div class="field"><label>limit（本次最多几条）</label>
            <input v-model="limit" class="mono" placeholder="默认" /></div>
          <div class="field"><label>max_hex（hex 字节预算）</label>
            <input v-model="maxHex" class="mono" placeholder="默认" /></div>
        </div>
        <div class="row">
          <button class="btn sm primary" :disabled="busy" @click="doExport">导出</button>
          <button v-if="nextSince" class="btn sm" :disabled="busy" @click="continueExport">
            接着导（since={{ nextSince }}）
          </button>
          <span class="spacer" />
          <span class="hint">这条有响应体积上限，没导完要接着导。</span>
        </div>

        <div v-if="exportRes" class="res">
          <div class="row kv">
            <span>导出 {{ exportRes.exported ?? (exportRes.packets ? exportRes.packets.length : '—') }} 条</span>
            <span v-if="exportRes.buffered !== undefined">缓冲 {{ exportRes.buffered }}</span>
            <span v-if="exportRes.next_since">next_since {{ exportRes.next_since }}</span>
            <span class="tag" :class="exportRes.complete ? 'ok' : 'warn'">
              {{ exportRes.complete ? '已导完' : '未导完' }}
            </span>
          </div>
          <details class="raw">
            <summary>原始返回</summary>
            <pre class="mono">{{ JSON.stringify(exportRes, null, 2).slice(0, 20000) }}</pre>
          </details>
        </div>
      </div>
    </div>

    <!-- 落盘导出 -->
    <div class="panel">
      <header>
        <span>落盘导出（完整数据正路）</span>
        <span class="spacer" />
        <button class="btn sm ghost" :disabled="busy" @click="loadFiles">刷新列表</button>
      </header>
      <div class="body col">
        <div class="row">
          <label class="fmt">
            格式
            <select v-model="kind" class="fmt-sel">
              <option value="json">json</option>
              <option value="txt">txt</option>
            </select>
          </label>
          <button class="btn sm primary" :disabled="busy" @click="doSave">落盘导出</button>
          <span class="hint">不受响应体积限制，能拿到整个缓冲。</span>
        </div>

        <div v-if="!files.length" class="empty">还没有导出文件。</div>
        <table v-else class="grid">
          <thead>
            <tr><th>文件名</th><th style="width:88px">大小</th><th style="width:160px">时间</th><th style="width:150px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="f in files" :key="f.name">
              <td class="mono ell" :title="f.name">{{ f.name }}</td>
              <td class="mono">{{ fmtSize(f.size) }}</td>
              <td class="mono small">{{ fmtTime(f.mtime) }}</td>
              <td>
                <div class="row">
                  <a class="btn sm" :href="wpeApi.exportDownloadUrl(f.name)" download>下载</a>
                  <button class="btn sm" @click="loadFile(f.name)">读进文本框</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 导入 -->
    <div class="panel">
      <header><span>导入</span></header>
      <div class="body col">
        <textarea v-model="importText" rows="5" spellcheck="false"
                  placeholder='[{"hex":"474554...","len":N,"dir":"c2s","proto":"tcp","target":"1.2.3.4:80"}]' />
        <div class="row">
          <button class="btn sm primary" :disabled="busy || !importText.trim()" @click="doImport">
            导入到缓冲
          </button>
          <button class="btn sm" @click="importText = ''">清空文本框</button>
          <span class="spacer" />
          <button class="btn sm danger" :disabled="busy" @click="doClear">清空封包缓冲</button>
        </div>
        <div v-if="importRes" class="res">
          <div class="row kv">
            <span>导入 {{ importRes.imported ?? '—' }}</span>
            <span>跳过 {{ importRes.skipped ?? '—' }}</span>
          </div>
          <div v-if="importRes.skipped && importRes.reasons" class="warn-box">
            <div v-for="(r, i) in importRes.reasons" :key="i">{{ r }}</div>
          </div>
          <details class="raw">
            <summary>原始返回</summary>
            <pre class="mono">{{ JSON.stringify(importRes, null, 2).slice(0, 8000) }}</pre>
          </details>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; }
.kv { font-size: 12px; color: var(--fg-muted); gap: 14px; }
.small { font-size: 11px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 340px; }
.fmt { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--fg-muted); }
.fmt-sel { width: 84px; }
.raw { margin-top: 8px; }
.raw pre { max-height: 280px; overflow: auto; background: var(--bg-sunken);
           padding: 8px; border-radius: var(--radius-sm); margin: 5px 0 0; }
</style>
