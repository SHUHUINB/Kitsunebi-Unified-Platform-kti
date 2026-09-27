<script setup>
/**
 * 卡密（取代一花 ccproxy_kami）。
 *
 * ★ state 是**算出来的**（disabled / unused / expired / active），SQL 里没有这一列，
 *   所以按 state 过滤是在内存里做的 —— total 会偏大，后端已把 total 一起回传，
 *   这里如实显示「命中 N / 库内共 M」，不假装精确。
 */
import { computed, ref, onMounted } from 'vue'
import { kamiApi } from '../api'

const tab = ref('kami')
const stats = ref(null)
const error = ref('')
const okMsg = ref('')
const busy = ref(false)

const apps = ref([])
const servers = ref([])
const users = ref([])
const kami = ref([])
const total = ref(0)
const page = ref(1)
const size = ref(50)
const q = ref({ state: '', keyword: '', app_id: '', server_id: '' })

const newKami = ref({ code: '', app_id: '', server_id: '', comment: '',
                      duration_hours: 24, max_conn: 1, bandwidth: -1 })
const batchCount = ref(10)

// code = 一花的 application.appcode，客户端查询页会把它原样回传；
// 留空则后端按 name 解析（后台只填名字也能跑）。
const newApp = ref({ name: '', code: '', server_id: '', remark: '', enabled: true })
const newServer = ref({ host: '', port: 8081, username: '', password: '', remark: '', enabled: true, weight: 1 })
const newUser = ref({ username: '', password: '', app_id: '', enabled: true, remark: '' })

// 批量删除勾选 —— 存的是**卡密码**不是 id。
// 后端 `/kami/delete` 按 code 删（一花 `delkami` 的语义），
// 列表行里虽然有 id，但勾选跨页之后 id 会混着来，统一用 code 更稳。
const picked = ref([])

function fail(e) { error.value = e.message; okMsg.value = '' }
function good(m) { okMsg.value = m; error.value = '' }

async function loadStats() {
  try { stats.value = (await kamiApi.stats()).data } catch (e) { fail(e) }
}

async function loadApps() {
  try { apps.value = (await kamiApi.apps()).data || [] } catch (e) { fail(e) }
}
async function loadServers() {
  try { servers.value = (await kamiApi.servers()).data || [] } catch (e) { fail(e) }
}
async function loadUsers() {
  try { users.value = (await kamiApi.users()).data || [] } catch (e) { fail(e) }
}

async function loadKami() {
  busy.value = true; error.value = ''
  try {
    const query = { page: page.value, size: size.value }
    if (q.value.state) query.state = q.value.state
    if (q.value.keyword) query.keyword = q.value.keyword
    if (q.value.app_id !== '') query.app_id = Number(q.value.app_id)
    if (q.value.server_id !== '') query.server_id = Number(q.value.server_id)
    const r = await kamiApi.list(query)
    kami.value = r.data || []
    total.value = r.total || 0
    // 换页/换筛选后旧勾选已经不在表里了，留着会让「删除选中」删掉看不见的行。
    picked.value = []
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function createKami() {
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const p = { ...newKami.value }
    p.app_id = p.app_id === '' ? null : Number(p.app_id)
    p.server_id = p.server_id === '' ? null : Number(p.server_id)
    p.duration_hours = Number(p.duration_hours)
    p.max_conn = Number(p.max_conn)
    p.bandwidth = Number(p.bandwidth)
    const r = await kamiApi.create(p)
    good('已生成：' + r.code)
    newKami.value.code = ''
    await Promise.all([loadKami(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function batchKami() {
  if (!window.confirm(`批量生成 ${batchCount.value} 条？单事务提交，要么全成要么全不成。`)) return
  busy.value = true; error.value = ''; okMsg.value = ''
  try {
    const p = { count: Number(batchCount.value), comment: newKami.value.comment,
                duration_hours: Number(newKami.value.duration_hours),
                max_conn: Number(newKami.value.max_conn),
                bandwidth: Number(newKami.value.bandwidth) }
    if (newKami.value.app_id !== '') p.app_id = Number(newKami.value.app_id)
    if (newKami.value.server_id !== '') p.server_id = Number(newKami.value.server_id)
    const r = await kamiApi.batch(p)
    good(`已批量生成 ${r.count} 条`)
    await Promise.all([loadKami(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function addApp() {
  busy.value = true; error.value = ''
  try {
    const p = { ...newApp.value }
    p.server_id = p.server_id === '' ? null : Number(p.server_id)
    await kamiApi.addApp(p)
    good('应用已创建')
    newApp.value = { name: '', code: '', server_id: '', remark: '', enabled: true }
    await Promise.all([loadApps(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function delApp(id) {
  if (!window.confirm('删除该应用？若下面还挂着卡密会被拒绝（409）。')) return
  try {
    await kamiApi.removeApp(id); good('已删除'); await Promise.all([loadApps(), loadStats()])
  } catch (e) { fail(e) }
}

async function addServer() {
  busy.value = true; error.value = ''
  try {
    const p = { ...newServer.value, port: Number(newServer.value.port), weight: Number(newServer.value.weight) }
    await kamiApi.addServer(p)
    good('服务器已创建')
    newServer.value = { host: '', port: 8081, username: '', password: '', remark: '', enabled: true, weight: 1 }
    await Promise.all([loadServers(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function delServer(id) {
  if (!window.confirm('删除该服务器？若下面还挂着卡密会被拒绝（409）。')) return
  try {
    await kamiApi.removeServer(id); good('已删除'); await Promise.all([loadServers(), loadStats()])
  } catch (e) { fail(e) }
}

async function addUser() {
  busy.value = true; error.value = ''
  try {
    const p = { ...newUser.value }
    p.app_id = p.app_id === '' ? null : Number(p.app_id)
    await kamiApi.addUser(p)
    good('用户已创建')
    newUser.value = { username: '', password: '', app_id: '', enabled: true, remark: '' }
    await Promise.all([loadUsers(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function delUser(uid) {
  if (!window.confirm('删除该用户？')) return
  try {
    await kamiApi.removeUser(uid); good('已删除'); await Promise.all([loadUsers(), loadStats()])
  } catch (e) { fail(e) }
}

function fmtTime(t) {
  if (!t) return '—'
  const d = new Date(t)
  return isNaN(d) ? String(t) : d.toLocaleString('zh-CN', { hour12: false })
}

function stateTag(s) {
  return { active: 'ok', unused: '', expired: 'warn', disabled: 'err' }[s] || ''
}

function stateText(s) {
  return { active: '使用中', unused: '未使用', expired: '已过期', disabled: '已停用' }[s] || s
}

function copyCodes() {
  const text = kami.value.map((k) => k.code).join('\n')
  navigator.clipboard?.writeText(text).then(
    () => good(`已复制当前页 ${kami.value.length} 条卡密`),
    () => fail(new Error('剪贴板不可用')))
}

// 一花 kami.php 有单条「删除」和批量「删除选中」两个入口，这里保持同构。
async function delKami(k) {
  // 已激活的卡密要单独提醒 —— 删了它，用户在适配层里的账号还在，
  // 只是以后查不到这张卡的来源。不提醒的话，管理员会以为「用户也没了」。
  const extra = k.state === 'active'
    ? '\n\n注意：这张卡已经激活（账号 ' + (k.usedBy || '未知') + '）。删除后该账号在代理端仍然可用，不会被回收。'
    : ''
  if (!window.confirm(`删除卡密 ${k.code}？${extra}`)) return
  busy.value = true
  try {
    await kamiApi.removeKami(k.id)
    good('已删除')
    await Promise.all([loadKami(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

async function delPicked() {
  const codes = picked.value.slice()
  if (!codes.length) return
  // 勾选里有多少张是已激活的 —— 数量对不上时用户会以为「只是删了几张没用的」。
  const usedCount = kami.value.filter((k) => codes.includes(k.code) && k.state === 'active').length
  const extra = usedCount
    ? `\n\n其中 ${usedCount} 张已激活，对应账号在代理端不会被回收。`
    : ''
  if (!window.confirm(`删除选中的 ${codes.length} 张卡密？${extra}`)) return
  busy.value = true; error.value = ''
  try {
    const r = await kamiApi.removeMany(codes)
    const d = r.data || {}
    if (d.missing && d.missing.length) {
      good(`删除 ${d.deleted} 条，未找到 ${d.missing.length} 条`)
    } else {
      good(`已删除 ${d.deleted || codes.length} 条`)
    }
    await Promise.all([loadKami(), loadStats()])
  } catch (e) { fail(e) } finally { busy.value = false }
}

const allPicked = computed(() =>
  kami.value.length > 0 && picked.value.length === kami.value.length)

function toggleAll() {
  picked.value = allPicked.value ? [] : kami.value.map((k) => k.code)
}

onMounted(() => {
  loadStats(); loadApps(); loadServers(); loadUsers(); loadKami()
})
</script>

<template>
  <div class="col">
    <div class="row">
      <div v-if="stats" class="stats">
        <span class="tag">共 {{ stats.total }}</span>
        <span class="tag ok">使用中 {{ stats.active }}</span>
        <span class="tag">未使用 {{ stats.unused }}</span>
        <span class="tag warn">已过期 {{ stats.expired }}</span>
        <span class="tag err">已停用 {{ stats.disabled }}</span>
        <span class="tag">应用 {{ stats.apps }}</span>
        <span class="tag">服务器 {{ stats.servers }}</span>
        <span class="tag">用户 {{ stats.users }}</span>
      </div>
      <span class="spacer" />
      <button class="btn sm" :disabled="busy" @click="loadStats">刷新统计</button>
    </div>

    <div v-if="error" class="err-box">{{ error }}</div>
    <div v-if="okMsg" class="ok-box">{{ okMsg }}</div>

    <div class="tabs">
      <button v-for="t in [{k:'kami',l:'卡密'},{k:'apps',l:'应用'},{k:'servers',l:'服务器'},{k:'users',l:'用户'}]"
              :key="t.k" class="tab" :class="{ on: tab === t.k }" @click="tab = t.k">{{ t.l }}</button>
    </div>

    <!-- 卡密 -->
    <template v-if="tab === 'kami'">
      <div class="panel">
        <header><span>生成卡密</span></header>
        <div class="body">
          <div class="fgrid">
            <div class="field"><label>卡密（留空自动生成）</label>
              <input v-model="newKami.code" class="mono" placeholder="自动" /></div>
            <div class="field"><label>应用</label>
              <select v-model="newKami.app_id">
                <option value="">（不限）</option>
                <option v-for="a in apps" :key="a.id" :value="a.id">{{ a.name }}</option>
              </select></div>
            <div class="field"><label>服务器</label>
              <select v-model="newKami.server_id">
                <option value="">（不限）</option>
                <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.host }}:{{ s.port }}</option>
              </select></div>
            <div class="field"><label>时长（小时）</label>
              <input v-model="newKami.duration_hours" class="mono" /></div>
            <div class="field"><label>最大连接（-1 不限）</label>
              <input v-model="newKami.max_conn" class="mono" /></div>
            <div class="field"><label>带宽（-1 不限）</label>
              <input v-model="newKami.bandwidth" class="mono" /></div>
            <div class="field wide"><label>备注</label>
              <input v-model="newKami.comment" class="mono" /></div>
          </div>
          <div class="row">
            <button class="btn sm primary" :disabled="busy" @click="createKami">生成一条</button>
            <span class="sep" />
            <label class="cnt">批量条数 <input v-model="batchCount" class="mono tiny" /></label>
            <button class="btn sm" :disabled="busy" @click="batchKami">批量生成</button>
            <span class="spacer" />
            <span class="hint">批量是单事务：要么全成，要么全不成</span>
          </div>
        </div>
      </div>

      <div class="panel">
        <header>
          <span>卡密列表</span>
          <span class="spacer" />
          <span v-if="picked.length" class="hint">已选 {{ picked.length }} 条</span>
          <button class="btn sm danger" :disabled="busy || !picked.length"
                  @click="delPicked">删除选中</button>
          <button class="btn sm ghost" @click="copyCodes">复制本页卡密</button>
        </header>
        <div class="body">
          <div class="fgrid">
            <div class="field"><label>状态</label>
              <select v-model="q.state" @change="page = 1; loadKami()">
                <option value="">全部</option>
                <option value="unused">未使用</option>
                <option value="active">使用中</option>
                <option value="expired">已过期</option>
                <option value="disabled">已停用</option>
              </select></div>
            <div class="field"><label>关键字（卡密/备注）</label>
              <input v-model="q.keyword" class="mono" @keyup.enter="page = 1; loadKami()" /></div>
            <div class="field"><label>应用</label>
              <select v-model="q.app_id" @change="page = 1; loadKami()">
                <option value="">全部</option>
                <option v-for="a in apps" :key="a.id" :value="a.id">{{ a.name }}</option>
              </select></div>
            <div class="field"><label>服务器</label>
              <select v-model="q.server_id" @change="page = 1; loadKami()">
                <option value="">全部</option>
                <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.host }}:{{ s.port }}</option>
              </select></div>
          </div>
          <div class="row">
            <button class="btn sm" :disabled="busy" @click="page = 1; loadKami()">查询</button>
            <span class="spacer" />
            <span class="hint">本页 {{ kami.length }} 条 · 库内共 {{ total }} 条</span>
          </div>

          <div v-if="!kami.length && !busy" class="empty">没有卡密。</div>
          <table v-else class="grid">
            <thead>
              <tr>
                <th style="width:34px">
                  <input type="checkbox" :checked="allPicked" @change="toggleAll" />
                </th>
                <th style="width:190px">卡密</th>
                <th style="width:80px">状态</th>
                <th style="width:110px">应用</th>
                <th style="width:140px">服务器</th>
                <th style="width:100px">归属</th>
                <th style="width:70px">时长</th>
                <th style="width:150px">激活时间</th>
                <th style="width:150px">到期时间</th>
                <th style="width:110px">使用账号</th>
                <th>备注</th>
                <th style="width:70px"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="k in kami" :key="k.id">
                <td><input v-model="picked" type="checkbox" :value="k.code" /></td>
                <td class="mono">{{ k.code }}</td>
                <td><span class="tag" :class="stateTag(k.state)">{{ stateText(k.state) }}</span></td>
                <td>{{ k.app || '—' }}</td>
                <td class="mono">{{ k.server || '—' }}</td>
                <td>{{ k.owner || '—' }}</td>
                <td class="mono">{{ k.durationHours }}h</td>
                <td class="mono small">{{ fmtTime(k.activatedAt) }}</td>
                <td class="mono small">{{ fmtTime(k.expireAt) }}</td>
                <!-- 一花把「这张卡给了谁」写在 kami.username 上。没有这一列，
                     排查「卡用了但账号不对」只能去翻审计日志，而日志是可清的。 -->
                <td class="mono">{{ k.usedBy || '—' }}</td>
                <td class="ell" :title="k.comment">{{ k.comment || '—' }}</td>
                <td><button class="btn sm danger" :disabled="busy" @click="delKami(k)">删除</button></td>
              </tr>
            </tbody>
          </table>
          <div class="row pager">
            <button class="btn sm" :disabled="page <= 1" @click="page--; loadKami()">上一页</button>
            <span class="hint">第 {{ page }} 页</span>
            <button class="btn sm" :disabled="kami.length < size" @click="page++; loadKami()">下一页</button>
          </div>
        </div>
      </div>
    </template>

    <!-- 应用 -->
    <template v-else-if="tab === 'apps'">
      <div class="panel">
        <header><span>新增应用</span></header>
        <div class="body">
          <div class="fgrid">
            <div class="field"><label>名称</label><input v-model="newApp.name" class="mono" /></div>
            <div class="field">
              <label>appcode</label>
              <input v-model="newApp.code" class="mono" placeholder="客户端查询用；留空按名称解析" />
            </div>
            <div class="field">
              <label>默认出口</label>
              <select v-model="newApp.server_id">
                <option value="">（不指定）</option>
                <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.host }}:{{ s.port }}</option>
              </select>
            </div>
            <div class="field"><label>备注</label><input v-model="newApp.remark" class="mono" /></div>
          </div>
          <div class="row">
            <label class="chk"><input v-model="newApp.enabled" type="checkbox" />启用</label>
            <span class="spacer" />
            <button class="btn sm primary" :disabled="busy || !newApp.name" @click="addApp">创建</button>
          </div>
        </div>
      </div>
      <div class="panel">
        <header><span>应用列表</span></header>
        <div class="body">
          <div v-if="!apps.length" class="empty">还没有应用。</div>
          <table v-else class="grid">
            <thead><tr><th style="width:60px">ID</th><th>名称</th><th style="width:130px">appcode</th><th style="width:150px">默认出口</th><th>备注</th><th style="width:70px">启用</th><th style="width:70px"></th></tr></thead>
            <tbody>
              <tr v-for="a in apps" :key="a.id">
                <td class="mono">{{ a.id }}</td>
                <td>{{ a.name }}</td>
                <td class="mono small">{{ a.code || '—' }}</td>
                <td class="mono small">{{ a.server || '—' }}</td>
                <td class="ell">{{ a.remark || '—' }}</td>
                <td><span class="tag" :class="a.enabled ? 'ok' : 'err'">{{ a.enabled ? '是' : '否' }}</span></td>
                <td><button class="btn sm danger" @click="delApp(a.id)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- 服务器 -->
    <template v-else-if="tab === 'servers'">
      <div class="panel">
        <header><span>新增服务器</span></header>
        <div class="body">
          <div class="fgrid">
            <div class="field"><label>主机</label><input v-model="newServer.host" class="mono" placeholder="127.0.0.1" /></div>
            <div class="field"><label>端口</label><input v-model="newServer.port" class="mono" /></div>
            <div class="field"><label>管理账号</label><input v-model="newServer.username" class="mono" /></div>
            <div class="field"><label>管理密码</label><input v-model="newServer.password" class="mono" /></div>
            <div class="field"><label>权重</label><input v-model="newServer.weight" class="mono" /></div>
            <div class="field"><label>备注</label><input v-model="newServer.remark" class="mono" /></div>
          </div>
          <div class="row">
            <label class="chk"><input v-model="newServer.enabled" type="checkbox" />启用</label>
            <span class="spacer" />
            <button class="btn sm primary" :disabled="busy || !newServer.host" @click="addServer">创建</button>
          </div>
        </div>
      </div>
      <div class="panel">
        <header><span>服务器列表</span></header>
        <div class="body">
          <div v-if="!servers.length" class="empty">还没有服务器。</div>
          <table v-else class="grid">
            <thead><tr><th style="width:60px">ID</th><th style="width:180px">主机</th><th style="width:70px">端口</th><th>账号</th><th style="width:70px">权重</th><th style="width:70px">启用</th><th style="width:70px"></th></tr></thead>
            <tbody>
              <tr v-for="s in servers" :key="s.id">
                <td class="mono">{{ s.id }}</td>
                <td class="mono">{{ s.host }}</td>
                <td class="mono">{{ s.port }}</td>
                <td class="mono">{{ s.username || '—' }}</td>
                <td class="mono">{{ s.weight }}</td>
                <td><span class="tag" :class="s.enabled ? 'ok' : 'err'">{{ s.enabled ? '是' : '否' }}</span></td>
                <td><button class="btn sm danger" @click="delServer(s.id)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- 用户 -->
    <template v-else>
      <div class="panel">
        <header><span>新增用户</span></header>
        <div class="body">
          <div class="fgrid">
            <div class="field"><label>用户名</label><input v-model="newUser.username" class="mono" /></div>
            <div class="field"><label>密码</label><input v-model="newUser.password" class="mono" /></div>
            <div class="field"><label>所属应用</label>
              <select v-model="newUser.app_id">
                <option value="">（不限）</option>
                <option v-for="a in apps" :key="a.id" :value="a.id">{{ a.name }}</option>
              </select></div>
            <div class="field"><label>备注</label><input v-model="newUser.remark" class="mono" /></div>
          </div>
          <div class="row">
            <label class="chk"><input v-model="newUser.enabled" type="checkbox" />启用</label>
            <span class="spacer" />
            <button class="btn sm primary" :disabled="busy || !newUser.username" @click="addUser">创建</button>
          </div>
        </div>
      </div>
      <div class="panel">
        <header><span>用户列表</span></header>
        <div class="body">
          <div v-if="!users.length" class="empty">还没有用户。</div>
          <table v-else class="grid">
            <thead><tr><th style="width:60px">ID</th><th>用户名</th><th style="width:120px">应用</th><th style="width:70px">启用</th><th>备注</th><th style="width:70px"></th></tr></thead>
            <tbody>
              <tr v-for="u in users" :key="u.id">
                <td class="mono">{{ u.id }}</td>
                <td class="mono">{{ u.username }}</td>
                <td>{{ u.app || '—' }}</td>
                <td><span class="tag" :class="u.enabled ? 'ok' : 'err'">{{ u.enabled ? '是' : '否' }}</span></td>
                <td class="ell">{{ u.remark || '—' }}</td>
                <td><button class="btn sm danger" @click="delUser(u.id)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.stats { display: flex; gap: 5px; flex-wrap: wrap; }
.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); }
.tab {
  padding: 8px 14px; border: none; background: transparent; cursor: pointer;
  border-bottom: 2px solid transparent; color: var(--fg-muted);
}
.tab.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.fgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 8px; margin-bottom: 9px; }
.wide { grid-column: 1 / -1; }
.small { font-size: 11px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
.sep { width: 1px; height: 20px; background: var(--line); }
.cnt { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--fg-muted); }
.tiny { width: 62px; }
.pager { margin-top: 9px; }
</style>
