<script setup>
/**
 * 构造发送 / 重放。
 *
 * ★ 结果原样展示（sent / total / bytes / 每条 results）。只回一句「成功」
 *   等于把「3 次里成功 1 次」藏起来。
 */
import { ref, computed } from 'vue'
import { wpeApi } from '../../api'

const props = defineProps({ selected: { type: Object, default: null } })
const emit = defineEmits(['done'])

const proto = ref('tcp')
const target = ref('')
const data = ref('')
const times = ref(1)
const busy = ref(false)
const error = ref('')
const result = ref(null)

const hexClean = computed(() => (data.value || '').replace(/[^0-9a-fA-F]/g, ''))
const hexValid = computed(() => hexClean.value.length > 0 && hexClean.value.length % 2 === 0)
const hexBytes = computed(() => Math.floor(hexClean.value.length / 2))

function useSelected() {
  if (!props.selected) return
  proto.value = props.selected.proto || 'tcp'
  target.value = props.selected.target || ''
  data.value = props.selected.mod_hex || props.selected.hex || ''
}

async function send() {
  busy.value = true
  error.value = ''
  result.value = null
  try {
    result.value = (await wpeApi.send({
      proto: proto.value, target: target.value.trim(), data: hexClean.value,
    })).data
    emit('done')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function replay() {
  if (!props.selected) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const r = await wpeApi.replay(props.selected.id, {
      data: hexClean.value || undefined,
      target: target.value.trim() || undefined,
      proto: proto.value || undefined,
      times: Number(times.value) || 1,
    })
    result.value = r
    emit('done')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

function fmt(hex) { return (hex || '').replace(/(..)/g, '$1 ').trim() }
</script>

<template>
  <div class="col">
    <div class="grid3">
      <div class="field">
        <label>协议</label>
        <select v-model="proto">
          <option value="tcp">tcp</option>
          <option value="udp">udp</option>
        </select>
      </div>
      <div class="field wide">
        <label>目标（host:port）</label>
        <input v-model="target" class="mono" placeholder="1.2.3.4:80" />
      </div>
    </div>

    <div class="field">
      <label>
        载荷 hex
        <span v-if="hexClean" :class="hexValid ? 'ok-t' : 'err-t'">
          （{{ hexBytes }} 字节{{ hexValid ? '' : ' · 长度为奇数，非法' }}）
        </span>
      </label>
      <textarea v-model="data" rows="4" spellcheck="false"
                placeholder="474554202f20485454502f312e310d0a0d0a" />
    </div>

    <div class="row">
      <button class="btn sm" :disabled="busy" @click="useSelected" :title="selected ? '' : '先在列表里选一个包'"
              :class="{ ghost: !selected }">
        用选中包填充
      </button>
      <span class="spacer" />
      <label class="times">
        重放次数
        <input v-model="times" class="mono tiny" />
      </label>
      <button class="btn sm" :disabled="busy || !selected" @click="replay">重放选中包</button>
      <button class="btn sm primary" :disabled="busy || !hexValid || !target.trim()" @click="send">
        {{ busy ? '发送中…' : '构造发送' }}
      </button>
    </div>

    <div v-if="error" class="err-box">{{ error }}</div>

    <div v-if="result" class="panel">
      <header>
        <span>结果</span>
        <span class="spacer" />
        <span class="tag" :class="result.ok ? 'ok' : 'err'">{{ result.ok ? '成功' : '失败' }}</span>
      </header>
      <div class="body">
        <div v-if="result.err" class="err-box">{{ result.err }}</div>
        <div class="row kv">
          <span v-if="result.sent !== undefined">发出 {{ result.sent }} / {{ result.total }}</span>
          <span v-if="result.bytes !== undefined">载荷 {{ result.bytes }} 字节</span>
          <span v-if="result.proto">协议 {{ result.proto }}</span>
          <span v-if="result.target" class="mono">{{ result.target }}</span>
        </div>
        <table v-if="result.results && result.results.length" class="grid">
          <thead><tr><th style="width:48px">#</th><th style="width:64px">状态</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in result.results" :key="i">
              <td class="mono">{{ i + 1 }}</td>
              <td><span class="tag" :class="r.ok ? 'ok' : 'err'">{{ r.ok ? 'ok' : 'fail' }}</span></td>
              <td class="mono small">{{ r.err || r.note || r.detail || '已发出' }}</td>
            </tr>
          </tbody>
        </table>
        <details class="raw">
          <summary>原始返回</summary>
          <pre class="mono">{{ JSON.stringify(result, null, 2) }}</pre>
        </details>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.grid3 { display: grid; grid-template-columns: 120px 1fr; gap: 8px; }
.times { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--fg-muted); }
.tiny { width: 62px; }
.ok-t { color: var(--ok); }
.err-t { color: var(--err); }
.kv { font-size: 12px; color: var(--fg-muted); gap: 14px; }
.small { font-size: 11px; }
.raw { margin-top: 8px; }
.raw pre { max-height: 260px; overflow: auto; background: var(--bg-sunken);
           padding: 8px; border-radius: var(--radius-sm); margin: 5px 0 0; }
</style>
