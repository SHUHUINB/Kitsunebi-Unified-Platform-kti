<script setup>
/**
 * CCProxy 账号管理。
 *
 * ★ 上游是适配层的 /account（CCProxy 管理协议），成功与否靠返回体里的中文标记判定。
 *   后端已经把「适配层没确认」转成 502，所以这里失败就是失败，不显示成成功。
 * ★ edit 时 autodisable / connection / bandwidth 必须带当前值（适配层无条件写），
 *   所以编辑表单每次都把当前值一起提交。
 */
import { ref, computed, onMounted } from 'vue'
import { ccpxApi } from '../api'

const rows = ref([])
const live = ref(null)
const loading = ref(false)
const error = ref('')
const okMsg = ref('')

const editing = ref(null)   // {username, ...} 或 null
const creating = ref(false)
const form = ref(blank())
const logs = ref('')
const logsOpen = ref(false)

function blank() {
  return {
    username: '', password: '', enabled: true, require_password: true,
    auto_disable: true, disable_date: '', disable_time: '',
    max_conn: -1, bandwidth: -1,
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await ccpxApi.accounts()
    rows.value = r.data || []
  } catch (e) {
    error.value = e.message
    rows.value = []
  } finally {
    loading.value = false
  }
}

async function loadLive() {
  try { live.value = (await ccpxApi.live()).data } catch { live.value = null }
}

function startCreate() {
  form.value = blank()
  creating.value = true
  editing.value = null
  okMsg.value = ''
}

function startEdit(r) {
  form.value = {
    username: r.user, password: '', enabled: r.state === 1,
    require_password: r.pwdstate === 1, auto_disable: r.autodisable === 1,
    disable_date: r.date || '', disable_time: r.time || '',
    max_conn: r.connection === '' ? -1 : Number(r.connection),
    bandwidth: r.bandwidth === '' ? -1 : Number(r.bandwidth),
  }
  editing.value = r.user
  creating.value = false
  okMsg.value = ''
}

function cancel() { creating.value = false; editing.value = null }

async function submit() {
  error.value = ''
  okMsg.value = ''
  loading.value = true
  try {
    const f = form.value
    if (creating.value) {
      await ccpxApi.add({
        username: f.username.trim(), password: f.password,
        enabled: f.enabled, require_password: f.require_password,
        auto_disable: f.auto_disable,
        disable_date: f.disable_date, disable_time: f.disable_time,
        max_conn: Number(f.max_conn), bandwidth: Number(f.bandwidth),
      })
      okMsg.value = '已创建 ' + f.username
    } else {
      const patch = {
        enabled: f.enabled, require_password: f.require_password,
        auto_disable: f.auto_disable,
        disable_date: f.disable_date, disable_time: f.disable_time,
        max_conn: Number(f.max_conn), bandwidth: Number(f.bandwidth),
      }
      // 密码留空 = 不改。传空串会被后端当成显式覆盖，所以这里干脆不传。
      if (f.password) patch.password = f.password
      await ccpxApi.update(editing.value, patch)
      okMsg.value = '已更新 ' + editing.value
    }
    cancel()
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function remove(r) {
  if (!window.confirm(`删除账号 ${r.user}？会同时从适配层删除。`)) return
  error.value = ''
  try {
    await ccpxApi.remove(r.user)
    okMsg.value = '已删除 ' + r.user
    await load()
  } catch (e) {
    error.value = e.message
  }
}

async function toggle(r) {
  error.value = ''
  try {
    await ccpxApi.update(r.user, { enabled: r.state !== 1 })
    await load()
  } catch (e) {
    error.value = e.message
  }
}

async function showLogs() {
  logsOpen.value = true
  try { logs.value = (await ccpxApi.logs(200)).data || '' } catch (e) { logs.value = e.message }
}

const liveRows = computed(() => {
  if (!live.value) return []
  const d = live.value
  const arr = Array.isArray(d) ? d : (d.accounts || d.users || [])
  return Array.isArray(arr) ? arr : []
})

onMounted(() => { load(); loadLive() })
</script>

<template>
  <div class="col">
    <div class="row">
      <button class="btn sm primary" @click="startCreate">新增账号</button>
      <button class="btn sm" :disabled="loading" @click="load">刷新</button>
      <button class="btn sm ghost" @click="showLogs">适配层日志</button>
      <button class="btn sm ghost" @click="loadLive">刷新连接数</button>
      <span class="spacer" />
      <span class="hint">共 {{ rows.length }} 个账号</span>
    </div>

    <div v-if="error" class="err-box">{{ error }}</div>
    <div v-if="okMsg" class="ok-box">{{ okMsg }}</div>

    <!-- 表单 -->
    <div v-if="creating || editing" class="panel">
      <header>
        <span>{{ creating ? '新增账号' : '编辑 ' + editing }}</span>
        <span class="spacer" />
        <button class="btn sm ghost" @click="cancel">取消</button>
      </header>
      <div class="body">
        <div class="fgrid">
          <div class="field">
            <label>用户名（≥5 位，字母数字）</label>
            <input v-model="form.username" class="mono" :disabled="!!editing" />
          </div>
          <div class="field">
            <label>{{ editing ? '密码（留空 = 不改）' : '密码' }}</label>
            <input v-model="form.password" class="mono" type="text" autocomplete="off" />
          </div>
          <div class="field">
            <label>到期日期（YYYY-MM-DD）</label>
            <input v-model="form.disable_date" class="mono" placeholder="留空 = 不过期" />
          </div>
          <div class="field">
            <label>到期时间（HH:MM:SS）</label>
            <input v-model="form.disable_time" class="mono" placeholder="00:00:00" />
          </div>
          <div class="field">
            <label>最大连接数（-1 = 不限）</label>
            <input v-model="form.max_conn" class="mono" />
          </div>
          <div class="field">
            <label>带宽 KB/s（-1 = 不限）</label>
            <input v-model="form.bandwidth" class="mono" />
          </div>
        </div>
        <div class="row">
          <label class="chk"><input v-model="form.enabled" type="checkbox" />启用</label>
          <label class="chk"><input v-model="form.require_password" type="checkbox" />需要密码</label>
          <label class="chk"><input v-model="form.auto_disable" type="checkbox" />到期自动停用</label>
          <span class="spacer" />
          <button class="btn sm primary" :disabled="loading" @click="submit">
            {{ creating ? '创建' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 列表 -->
    <div class="panel">
      <header><span>账号列表</span></header>
      <div class="body">
        <div v-if="!rows.length && !loading" class="empty">没有账号。</div>
        <table v-else class="grid">
          <thead>
            <tr>
              <th style="width:130px">用户名</th>
              <th style="width:110px">密码</th>
              <th style="width:70px">状态</th>
              <th style="width:70px">需密码</th>
              <th style="width:74px">自动停用</th>
              <th style="width:150px">到期</th>
              <th style="width:70px">连接</th>
              <th style="width:80px">带宽</th>
              <th style="width:190px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.user">
              <td class="mono">{{ r.user }}</td>
              <td class="mono">{{ r.pwd || '—' }}</td>
              <td>
                <span class="tag" :class="r.state === 1 ? 'ok' : 'err'">
                  {{ r.state === 1 ? '启用' : '停用' }}
                </span>
              </td>
              <td class="mono">{{ r.pwdstate ? '是' : '否' }}</td>
              <td class="mono">{{ r.autodisable ? '是' : '否' }}</td>
              <td class="mono">
                {{ r.expireAt || '—' }}
                <span v-if="r.expire" class="tag err">已过期</span>
              </td>
              <td class="mono">{{ r.connection || '—' }}</td>
              <td class="mono">{{ r.bandwidth || '—' }}</td>
              <td>
                <div class="row">
                  <button class="btn sm" @click="startEdit(r)">编辑</button>
                  <button class="btn sm" @click="toggle(r)">
                    {{ r.state === 1 ? '停用' : '启用' }}
                  </button>
                  <button class="btn sm danger" @click="remove(r)">删除</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 实时连接 -->
    <div class="panel">
      <header>
        <span>实时连接（/live）</span>
        <span class="spacer" />
        <span class="hint">这一栏才是「谁真的连着」；账号表里的 connection 是上限</span>
      </header>
      <div class="body">
        <div v-if="!live" class="empty">未取到。</div>
        <table v-else-if="liveRows.length" class="grid">
          <thead><tr><th>账号</th><th>当前</th><th>峰值</th><th>累计</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in liveRows" :key="i">
              <td class="mono">{{ r.user || r.username || r.account || '—' }}</td>
              <td class="mono">{{ r.current ?? r.now ?? r.cur ?? '—' }}</td>
              <td class="mono">{{ r.peak ?? r.max ?? '—' }}</td>
              <td class="mono">{{ r.total ?? r.sum ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <details v-else open>
          <summary>原始返回</summary>
          <pre class="mono boxed">{{ JSON.stringify(live, null, 2).slice(0, 6000) }}</pre>
        </details>
      </div>
    </div>

    <!-- 日志 -->
    <div v-if="logsOpen" class="panel">
      <header>
        <span>适配层日志（尾部 200 行）</span>
        <span class="spacer" />
        <button class="btn sm ghost" @click="showLogs">重新拉取</button>
        <button class="btn sm ghost" @click="logsOpen = false">关闭</button>
      </header>
      <div class="body">
        <pre class="mono boxed logs">{{ logs || '（空）' }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.fgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; margin-bottom: 9px; }
.boxed { background: var(--bg-sunken); padding: 8px; border-radius: var(--radius-sm);
         max-height: 300px; overflow: auto; margin: 6px 0; }
.logs { max-height: 420px; white-space: pre-wrap; word-break: break-all; }
details summary { cursor: pointer; font-size: 12px; color: var(--fg-muted); }
</style>
