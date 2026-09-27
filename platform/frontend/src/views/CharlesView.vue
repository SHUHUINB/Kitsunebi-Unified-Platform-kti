<script setup>
/**
 * Charles 配置。
 *
 * ★ 写配置是有副作用的动作，且 Charles 退出时会把内存配置回写磁盘 ——
 *   所以「先停 → 改 → 起」这套顺序由后端串行化处理，前端**不要**自己
 *   先点「停止」再点「保存」。所有写操作都走 /changes、/raw、/backups。
 * ★ 返回里的 applied / errors 逐条展示。静默跳过一条改动，比直接报错坏得多。
 */
import { ref, onMounted } from 'vue'
import { charlesApi } from '../api'

const state = ref(null)
const xml = ref('')
const xmlDirty = ref(false)
const backups = ref([])
const logs = ref('')
const ca = ref(null)
const webif = ref('')

const busy = ref(false)
const error = ref('')
const okMsg = ref('')
const result = ref(null)

const changes = ref([])
const restart = ref(true)

function blankChange() { return { path: '', value: '', type: 'str' } }

async function loadAll() {
  error.value = ''
  await Promise.all([loadState(), loadBackups()])
}

async function loadState() {
  try { state.value = await charlesApi.state() } catch (e) { error.value = e.message }
}

async function loadConfig() {
  busy.value = true; error.value = ''
  try {
    const r = await charlesApi.config()
    xml.value = r.xml
    xmlDirty.value = false
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function loadBackups() {
  try { backups.value = await charlesApi.backups() } catch (e) { error.value = e.message }
}

async function doService(action) {
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const r = await charlesApi.service(action, restart.value)
    okMsg.value = `${action}：rc=${r.rc} ${r.output || ''}`.trim()
    await loadState()
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function doBackup() {
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const r = await charlesApi.backup('manual')
    okMsg.value = '已备份：' + r.name
    await loadBackups()
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function doRestore(name) {
  if (!window.confirm(`恢复备份 ${name}？当前配置会被覆盖（会先自动备份一次）。`)) return
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const r = await charlesApi.restore(name, restart.value)
    okMsg.value = '已恢复：' + name
    result.value = r
    await Promise.all([loadState(), loadBackups()])
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function applyChanges() {
  const list = changes.value.filter((c) => c.path.trim())
  if (!list.length) { error.value = '至少填一条 path'; return }
  busy.value = true; error.value = ''; okMsg.value = ''
  result.value = null
  try {
    result.value = await charlesApi.applyChanges(
      list.map((c) => ({ path: c.path.trim(), value: c.value, type: c.type })), restart.value)
    okMsg.value = '改动已应用'
    await Promise.all([loadState(), loadBackups()])
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function applyRaw() {
  if (!xmlDirty.value) { error.value = '内容没有改动'; return }
  if (!window.confirm('整份覆盖写入 Charles 配置？会先自动备份。')) return
  busy.value = true; error.value = ''; okMsg.value = ''
  result.value = null
  try {
    result.value = await charlesApi.applyRaw(xml.value, restart.value)
    okMsg.value = '整份写入完成'
    xmlDirty.value = false
    await Promise.all([loadState(), loadBackups()])
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function loadLogs() {
  busy.value = true
  try { logs.value = await charlesApi.logs(200) } catch (e) { logs.value = e.message } finally { busy.value = false }
}

async function loadCa() {
  try { ca.value = await charlesApi.ca() } catch (e) { error.value = e.message }
}

async function loadWebif() {
  busy.value = true
  try { webif.value = await charlesApi.webif('/') } catch (e) { webif.value = e.message } finally { busy.value = false }
}

/**
 * 官方帮助页（charles.jar 里自带的那份）。
 *
 * 名单是后端从 jar 里**扫出来**的，不是前端硬编码 —— 升级 Charles 后
 * 帮助页会增减，写死一份名单等于给自己留一堆点了 404 的按钮。
 */
const helpNames = ref([])
const helpName = ref('')
const helpHtml = ref('')

async function loadHelpNames() {
  try { helpNames.value = await charlesApi.helpNames() } catch (e) { error.value = e.message }
}

async function loadHelp() {
  if (!helpName.value) return
  busy.value = true
  try {
    const r = await charlesApi.help(helpName.value)
    helpHtml.value = await r.text()
  } catch (e) {
    // 取不到就明说取不到 —— 留一片空白会让人以为「这页本来就是空的」。
    helpHtml.value = `<p style="font:13px system-ui;padding:12px">取帮助页失败：${e.message}</p>`
  } finally { busy.value = false }
}

function addChange() { changes.value.push(blankChange()) }
function removeChange(i) { changes.value.splice(i, 1) }

function fmtTime(t) {
  if (!t) return '—'
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t)
  return isNaN(d) ? String(t) : d.toLocaleString('zh-CN', { hour12: false })
}

function sizeText(b) {
  if (!b) return '—'
  if (b < 1024) return b + ' B'
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB'
  return (b / 1024 / 1024).toFixed(2) + ' MB'
}

onMounted(() => { loadAll(); loadCa(); loadHelpNames() })
</script>

<template>
  <div class="col">
    <div class="row">
      <span v-if="state" class="tag" :class="state.service ? 'ok' : 'err'">
        {{ state.service ? '运行中' : '已停止' }}
      </span>
      <span v-if="state" class="hint mono">PID {{ state.pid || '—' }} · 配置 {{ sizeText(state.configSize) }}</span>
      <span class="spacer" />
      <label class="chk"><input v-model="restart" type="checkbox" />改动后重启</label>
      <button class="btn sm" :disabled="busy" @click="doService('restart')">重启</button>
      <button class="btn sm" :disabled="busy" @click="doService('stop')">停止</button>
      <button class="btn sm" :disabled="busy" @click="doService('start')">启动</button>
      <button class="btn sm" :disabled="busy" @click="loadAll">刷新</button>
    </div>

    <div v-if="error" class="err-box">{{ error }}</div>
    <div v-if="okMsg" class="ok-box">{{ okMsg }}</div>
    <div v-if="state" class="hint mono">配置文件：{{ state.configPath }}</div>

    <!-- 增量改动 -->
    <div class="panel">
      <header>
        <span>增量改动</span>
        <span class="spacer" />
        <button class="btn sm" @click="addChange">加一条</button>
        <button class="btn sm primary" :disabled="busy" @click="applyChanges">应用</button>
      </header>
      <div class="body">
        <div class="hint">
          path 支持三种写法：<code>a/b</code>（按名字层层下钻）、
          <code>entry[3]</code>（按子节点序号）、<code>entry[@断点]</code>（按属性值定位）。
          type 选 bool / int 时会做严格转换，转不动就报错，不猜。
        </div>
        <div v-if="!changes.length" class="empty">还没有改动。点「加一条」。</div>
        <table v-else class="grid">
          <thead><tr><th style="width:34%">path</th><th>value</th><th style="width:96px">type</th><th style="width:64px"></th></tr></thead>
          <tbody>
            <tr v-for="(c, i) in changes" :key="i">
              <td><input v-model="c.path" class="mono" placeholder="sslLocations/entry[0]" /></td>
              <td><input v-model="c.value" class="mono" placeholder="值" /></td>
              <td>
                <select v-model="c.type">
                  <option value="str">str</option>
                  <option value="bool">bool</option>
                  <option value="int">int</option>
                </select>
              </td>
              <td><button class="btn sm danger" @click="removeChange(i)">删</button></td>
            </tr>
          </tbody>
        </table>
        <div v-if="result" class="res">
          <div class="row kv">
            <span v-if="result.backup">备份 {{ result.backup }}</span>
            <span v-if="result.restarted !== undefined">已重启 {{ result.restarted ? '是' : '否' }}</span>
          </div>
          <details open>
            <summary>应用结果（逐条）</summary>
            <pre class="mono boxed">{{ JSON.stringify(result, null, 2).slice(0, 8000) }}</pre>
          </details>
        </div>
      </div>
    </div>

    <!-- 整份 XML -->
    <div class="panel">
      <header>
        <span>整份 XML</span>
        <span class="spacer" />
        <button class="btn sm" :disabled="busy" @click="loadConfig">读取当前配置</button>
        <button class="btn sm primary" :disabled="busy || !xmlDirty" @click="applyRaw">整份写入</button>
      </header>
      <div class="body">
        <div class="hint">
          会先校验 XML 结构、自动补齐 charles 处理指令。整份覆盖是危险动作 —— 建议先「备份」。
        </div>
        <textarea v-model="xml" rows="14" spellcheck="false" class="mono"
                  @input="xmlDirty = true" />
      </div>
    </div>

    <!-- 备份 -->
    <div class="panel">
      <header>
        <span>备份</span>
        <span class="spacer" />
        <span class="hint">最多保留 200 份，超出自动清理最旧的</span>
        <button class="btn sm" :disabled="busy" @click="doBackup">立即备份</button>
      </header>
      <div class="body">
        <div v-if="!backups.length" class="empty">还没有备份。</div>
        <table v-else class="grid">
          <thead><tr><th>名称</th><th style="width:88px">大小</th><th style="width:170px">时间</th><th style="width:80px"></th></tr></thead>
          <tbody>
            <tr v-for="(b, i) in backups" :key="i">
              <td class="mono ell">{{ b.name || b }}</td>
              <td class="mono">{{ sizeText(b.size) }}</td>
              <td class="mono small">{{ fmtTime(b.mtime || b.time) }}</td>
              <td><button class="btn sm" :disabled="busy" @click="doRestore(b.name || b)">恢复</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- CA / 日志 / Web 接口 -->
    <div class="panel">
      <header>
        <span>证书与诊断</span>
        <span class="spacer" />
        <button class="btn sm ghost" @click="loadCa">CA 信息</button>
        <button class="btn sm ghost" :disabled="busy" @click="loadLogs">服务日志</button>
        <button class="btn sm ghost" :disabled="busy" @click="loadWebif">Web 接口</button>
      </header>
      <div class="body col">
        <div v-if="ca">
          <div class="hint">根证书现场（CA 每台各自生成；设备装完还要再开「证书信任设置」）</div>
          <pre class="mono boxed">{{ JSON.stringify(ca, null, 2).slice(0, 4000) }}</pre>
        </div>
        <div v-if="logs">
          <div class="hint">服务日志</div>
          <pre class="mono boxed logs">{{ logs }}</pre>
        </div>
        <div v-if="webif">
          <div class="hint">Web Interface</div>
          <pre class="mono boxed">{{ webif.slice(0, 4000) }}</pre>
        </div>
      </div>
    </div>
    <!-- 官方帮助 -->
    <div class="panel">
      <header>
        <span>官方帮助</span>
        <span class="spacer" />
        <select v-model="helpName" class="mono" style="min-width:220px">
          <option value="">— 选择帮助页 —</option>
          <option v-for="n in helpNames" :key="n" :value="n">{{ n }}</option>
        </select>
        <button class="btn sm" :disabled="busy || !helpName" @click="loadHelp">打开</button>
      </header>
      <div class="body">
        <div class="hint">
          以下为 <code>charles.jar</code> 内自带的官方帮助原文（英文/原版排版），
          按字段名一一对应，用来查某个配置项到底是什么意思。
        </div>
        <div v-if="!helpNames.length" class="empty">
          没读到帮助页 —— 服务器上找不到 charles.jar（或它不可读）。
        </div>
        <iframe v-if="helpHtml" class="help-frame" :srcdoc="helpHtml" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.kv { font-size: 12px; color: var(--fg-muted); gap: 14px; }
.small { font-size: 11px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px; }
.boxed { background: var(--bg-sunken); padding: 8px; border-radius: var(--radius-sm);
         max-height: 320px; overflow: auto; margin: 6px 0; }
.logs { max-height: 420px; white-space: pre-wrap; word-break: break-all; }
details summary { cursor: pointer; font-size: 12px; color: var(--fg-muted); }
.res { margin-top: 9px; }
/* iframe 必须给白底：charles.jar 里的帮助页是为浅色 Swing 界面写的，
   继承深色主题会变成深底深字，等于看不见。 */
.help-frame { width: 100%; height: 460px; border: 1px solid var(--line);
              border-radius: var(--radius-sm); background: #fff; }
</style>
