<script setup>
/**
 * 自动改写（过滤器改写）规则编辑器。
 *
 * ★ 条件字段是**硬要求**：没有条件的规则会命中每一个包，适配层直接拒收。
 *   所以每条规则都必须至少填一个条件 —— 界面上也照这个规则拦一次，
 *   省得提交完只看到一条 rejected。
 * ★ rejected 必须原样展示。静默丢规则是最坏的做法：界面看着保存成功，
 *   实际少了一条，流量照原样转发，用的人会以为「改写不生效」查半天。
 */
import { ref, computed, watch } from 'vue'
import { wpeApi } from '../../api'

const props = defineProps({ state: { type: Object, default: null } })
const emit = defineEmits(['saved'])

const COND_KEYS = ['dir', 'proto', 'target', 'hex', 'app', 'min_len', 'max_len']

const rules = ref([])
const on = ref(false)
const busy = ref(false)
const error = ref('')
const rejected = ref([])
const note = ref('')
const dirty = ref(false)

function blankRule() {
  return { act: 'find_replace', find: '', replace: '', set_hex: '',
           dir: '', proto: '', target: '', hex: '', app: '',
           min_len: '', max_len: '', count: '' }
}

function load() {
  const s = props.state
  if (!s) return
  on.value = !!s.on
  rules.value = (s.rules || []).map((r) => Object.assign(blankRule(), r))
  rejected.value = s.rejected || []
  note.value = s.note || ''
  dirty.value = false
}

function add() {
  rules.value.push(blankRule())
  dirty.value = true
}

function remove(i) {
  rules.value.splice(i, 1)
  dirty.value = true
}

function hasCond(r) {
  return COND_KEYS.some((k) => r[k] !== '' && r[k] !== null && r[k] !== undefined)
}

const localIssues = computed(() =>
  rules.value.map((r, i) => {
    if (!hasCond(r)) return { i, reason: '没有任何条件 —— 会被适配层拒收' }
    if (r.act === 'set_hex' && !String(r.set_hex || '').replace(/\s/g, '')) {
      return { i, reason: '整包替换规则没有 set_hex' }
    }
    if (r.act !== 'set_hex' && !String(r.find || '').replace(/\s/g, '')) {
      return { i, reason: '字节替换规则没有 find' }
    }
    return null
  }).filter(Boolean)
)

function payload() {
  const num = (v) => (v === '' || v === null || v === undefined ? undefined : Number(v))
  return rules.value.map((r) => {
    const o = { act: r.act }
    if (r.act === 'set_hex') o.set_hex = String(r.set_hex || '').replace(/\s/g, '')
    else {
      o.find = String(r.find || '').replace(/\s/g, '')
      o.replace = String(r.replace || '').replace(/\s/g, '')
      if (r.count !== '' && r.count !== undefined && r.count !== null) o.count = Number(r.count)
    }
    for (const k of COND_KEYS) {
      if (k === 'min_len' || k === 'max_len') {
        const n = num(r[k])
        if (n !== undefined) o[k] = n
      } else if (r[k]) o[k] = r[k]
    }
    return o
  })
}

async function save() {
  busy.value = true
  error.value = ''
  rejected.value = []
  try {
    const r = await wpeApi.setRewrite(on.value, payload())
    const d = r.data || {}
    rejected.value = d.rejected || []
    dirty.value = false
    emit('saved')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

function hitsOf(i) {
  const h = (props.state && props.state.hits) || {}
  return h[i] !== undefined ? h[i] : (h[String(i)] || 0)
}

defineExpose({ load })
// 面板可能先于 /wpe/rewrite 的返回挂载（默认就停在「自动改写」页签）。
// 不监听的话，state 后到就永远读不到规则 —— 看起来像「规则丢了」。
watch(() => props.state, load)
load()
</script>

<template>
  <div class="col">
    <div class="row">
      <label class="chk">
        <input v-model="on" type="checkbox" @change="dirty = true" />
        开启自动改写
      </label>
      <span class="spacer" />
      <button class="btn sm" :disabled="busy" @click="add">加一条</button>
      <button class="btn sm primary" :disabled="busy || !dirty" @click="save">
        {{ busy ? '保存中…' : '保存（整表覆盖）' }}
      </button>
    </div>

    <div class="hint">
      保存是**整表覆盖**，不是追加。每条规则至少要有一个条件，否则适配层拒收。
    </div>

    <div v-if="note" class="hint">{{ note }}</div>
    <div v-if="error" class="err-box">{{ error }}</div>

    <div v-if="localIssues.length" class="warn-box">
      本地检查：<span v-for="(x, i) in localIssues" :key="i">#{{ x.i }} {{ x.reason }}；</span>
    </div>

    <div v-if="rejected.length" class="err-box">
      适配层拒收的规则：
      <div v-for="(r, i) in rejected" :key="i">#{{ r.i }}：{{ r.reason }}</div>
    </div>

    <div v-if="!rules.length" class="empty">还没有规则。点「加一条」开始。</div>

    <div v-for="(r, i) in rules" :key="i" class="rule panel">
      <header>
        <span class="mono">规则 #{{ i }}</span>
        <span class="tag">命中 {{ hitsOf(i) }}</span>
        <span class="spacer" />
        <button class="btn sm danger" @click="remove(i)">删除</button>
      </header>
      <div class="body">
        <div class="grid2">
          <div class="field">
            <label>动作</label>
            <select v-model="r.act" @change="dirty = true">
              <option value="find_replace">字节替换 find_replace</option>
              <option value="set_hex">整包替换 set_hex</option>
            </select>
          </div>
          <template v-if="r.act === 'set_hex'">
            <div class="field wide">
              <label>set_hex（整包替换成这个 hex）</label>
              <input v-model="r.set_hex" class="mono" placeholder="48454c4c4f"
                     @input="dirty = true" />
            </div>
          </template>
          <template v-else>
            <div class="field">
              <label>find（要替换的 hex）</label>
              <input v-model="r.find" class="mono" placeholder="474554" @input="dirty = true" />
            </div>
            <div class="field">
              <label>replace（留空 = 删除）</label>
              <input v-model="r.replace" class="mono" placeholder="48454c4c" @input="dirty = true" />
            </div>
            <div class="field">
              <label>count（最多替换几次，留空 = 全部）</label>
              <input v-model="r.count" class="mono" placeholder="1" @input="dirty = true" />
            </div>
          </template>
        </div>

        <div class="cond-title">命中条件（至少填一个）</div>
        <div class="grid3">
          <div class="field">
            <label>方向 dir</label>
            <select v-model="r.dir" @change="dirty = true">
              <option value="">（不限）</option>
              <option value="c2s">c2s 客户端→目标</option>
              <option value="s2c">s2c 目标→客户端</option>
            </select>
          </div>
          <div class="field">
            <label>协议 proto</label>
            <input v-model="r.proto" class="mono" placeholder="tcp / udp / http" @input="dirty = true" />
          </div>
          <div class="field">
            <label>应用层 app</label>
            <input v-model="r.app" class="mono" placeholder="HTTP / TLS / DNS" @input="dirty = true" />
          </div>
          <div class="field">
            <label>目标 target（子串）</label>
            <input v-model="r.target" class="mono" placeholder="m.baidu.com" @input="dirty = true" />
          </div>
          <div class="field">
            <label>载荷 hex（子串）</label>
            <input v-model="r.hex" class="mono" placeholder="474554" @input="dirty = true" />
          </div>
          <div class="field pair">
            <label>长度区间</label>
            <div class="row">
              <input v-model="r.min_len" class="mono" placeholder="min" @input="dirty = true" />
              <input v-model="r.max_len" class="mono" placeholder="max" @input="dirty = true" />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.rule header { font-size: 12px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 8px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.wide { grid-column: 1 / -1; }
.cond-title { margin: 10px 0 6px; font-size: 12px; color: var(--fg-muted); }
.pair .row { gap: 6px; }
</style>
