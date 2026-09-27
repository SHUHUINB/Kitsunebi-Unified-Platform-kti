<script setup>
/**
 * 断点（Hold）面板。
 *
 * ★ 规则为空对象 `{}` 是**合法**的 —— 断点的语义就是「不填条件 = 全拦」。
 *   这一点和自动改写规则相反（那边无条件直接拒收），界面上必须说清楚，
 *   否则很容易照着改写的习惯把空规则当成「填错了」。
 * ★ GET /wpe/hold 是只读的（适配层专门修过这个 bug），所以这个面板
 *   不做任何「刷新时顺手关掉」的动作 —— 读就是读。
 */
import { ref, computed, watch } from 'vue'
import { wpeApi } from '../../api'

const props = defineProps({
  state: { type: Object, default: null },
  held: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['saved', 'release', 'refresh'])

const on = ref(false)
const dir = ref('')
const proto = ref('')
const app = ref('')
const target = ref('')
const hex = ref('')
const minLen = ref('')
const maxLen = ref('')
const error = ref('')

watch(() => props.state, (s) => {
  if (!s) return
  on.value = !!s.on
  const r = s.rule || {}
  dir.value = r.dir || ''
  proto.value = r.proto || ''
  app.value = r.app || ''
  target.value = r.target || ''
  hex.value = r.hex || ''
  minLen.value = r.min_len ?? ''
  maxLen.value = r.max_len ?? ''
}, { immediate: true })

const rule = computed(() => {
  const o = {}
  if (dir.value) o.dir = dir.value
  if (proto.value) o.proto = proto.value
  if (app.value) o.app = app.value
  if (target.value) o.target = target.value
  if (hex.value) o.hex = hex.value
  if (minLen.value !== '') o.min_len = Number(minLen.value)
  if (maxLen.value !== '') o.max_len = Number(maxLen.value)
  return o
})

const isAll = computed(() => Object.keys(rule.value).length === 0)

async function save() {
  error.value = ''
  try {
    await wpeApi.setHold(on.value, rule.value)
    emit('saved')
  } catch (e) {
    error.value = e.message
  }
}

function clearRule() {
  dir.value = ''; proto.value = ''; app.value = ''
  target.value = ''; hex.value = ''; minLen.value = ''; maxLen.value = ''
}
</script>

<template>
  <div class="col">
    <div class="row">
      <label class="chk">
        <input v-model="on" type="checkbox" />
        开启断点
      </label>
      <span class="tag" :class="on ? 'hold' : ''">
        队列 {{ state ? state.holding : 0 }} 个
      </span>
      <span v-if="state" class="tag">超时 {{ state.timeout }}s</span>
      <span class="spacer" />
      <button class="btn sm" @click="clearRule">清空条件</button>
      <button class="btn sm primary" :disabled="busy" @click="save">应用</button>
    </div>

    <div class="hint">
      条件全空 = <b>拦下全部封包</b>（这是断点的语义，不是填错）。
      命中的包会被挂住，直到放行 / 丢弃 / 改写，或等到超时。
    </div>
    <div v-if="isAll" class="warn-box">
      当前条件为空 —— 一旦开启，<b>每一个包</b>都会被挂住，流量会停。
    </div>
    <div v-if="error" class="err-box">{{ error }}</div>

    <div class="grid3">
      <div class="field">
        <label>方向 dir</label>
        <select v-model="dir">
          <option value="">（不限）</option>
          <option value="c2s">c2s</option>
          <option value="s2c">s2c</option>
        </select>
      </div>
      <div class="field">
        <label>协议 proto</label>
        <input v-model="proto" class="mono" placeholder="tcp / http" />
      </div>
      <div class="field">
        <label>应用层 app</label>
        <input v-model="app" class="mono" placeholder="HTTP" />
      </div>
      <div class="field">
        <label>目标 target</label>
        <input v-model="target" class="mono" placeholder="m.baidu.com" />
      </div>
      <div class="field">
        <label>载荷 hex</label>
        <input v-model="hex" class="mono" placeholder="474554" />
      </div>
      <div class="field pair">
        <label>长度区间</label>
        <div class="row">
          <input v-model="minLen" class="mono" placeholder="min" />
          <input v-model="maxLen" class="mono" placeholder="max" />
        </div>
      </div>
    </div>

    <div class="panel">
      <header>
        <span>断点队列</span>
        <span class="spacer" />
        <button class="btn sm ghost" @click="emit('refresh')">刷新</button>
      </header>
      <div class="body">
        <div v-if="!held.length" class="empty">队列为空。</div>
        <table v-else class="grid">
          <thead>
            <tr>
              <th style="width:56px">#</th>
              <th style="width:46px">方向</th>
              <th style="width:56px">协议</th>
              <th>目标</th>
              <th style="width:64px">长度</th>
              <th style="width:196px">处置</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in held" :key="p.id">
              <td class="mono">{{ p.id }}</td>
              <td><span class="tag" :class="p.dir">{{ p.dir === 'c2s' ? '发' : '收' }}</span></td>
              <td class="mono">{{ p.proto }}</td>
              <td class="mono ell" :title="p.target">{{ p.target }}</td>
              <td class="mono">{{ p.len }}</td>
              <td>
                <div class="row">
                  <button class="btn sm" :disabled="busy"
                          @click="emit('release', { id: p.id, action: 'forward', hex: '' })">放行</button>
                  <button class="btn sm danger" :disabled="busy"
                          @click="emit('release', { id: p.id, action: 'drop', hex: '' })">丢弃</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.pair .row { gap: 6px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
