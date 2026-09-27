<script setup>
/**
 * 封包详情：元数据 + 完整 hex + 分层结构。
 *
 * ★ 「实际转发的是什么」必须一眼可见。包被改写过（mod_hex 存在）时，
 *   默认显示 mod_hex，并把原始 hex 收在一边 —— 只显示原始包等于骗人。
 * ★ 结构表的 off/len 直接喂给 HexDump 高亮，两边用的是同一份数据。
 */
import { computed, ref } from 'vue'
import HexDump from './HexDump.vue'

const props = defineProps({
  packet: { type: Object, default: null },
  struct: { type: Object, default: null },
  // 是否在断点队列里。适配层不在封包上打 hold 标记，所以由父组件按
  // /wpe/hold 的实际队列传进来 —— 猜出来的「是否被断住」比不显示更糟。
  held: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['replay', 'release'])

const showRaw = ref(false)
const showAscii = ref(true)

const isModified = computed(() => !!(props.packet && props.packet.mod_hex))
const activeHex = computed(() => {
  if (!props.packet) return ''
  if (isModified.value && !showRaw.value) return props.packet.mod_hex
  return props.packet.hex || ''
})

const marks = computed(() => {
  const f = (props.struct && props.struct.fields) || []
  return f.map((x) => ({ off: x.off, len: x.len, name: x.name }))
})

const fields = computed(() => (props.struct && props.struct.fields) || [])
const layers = computed(() => (props.struct && props.struct.layers) || [])

function hexToAscii(hex) {
  const s = (hex || '').replace(/[^0-9a-fA-F]/g, '')
  let out = ''
  for (let i = 0; i + 1 < s.length; i += 2) {
    const b = parseInt(s.substr(i * 2, 2), 16)
    out += b >= 0x20 && b < 0x7f ? String.fromCharCode(b) : '.'
  }
  return out
}
</script>

<template>
  <div v-if="!packet" class="empty">从左侧选一个封包查看详情。</div>
  <div v-else class="detail">
    <!-- 元数据 -->
    <div class="meta">
      <div class="mrow"><span class="k">封包号</span><span class="mono">#{{ packet.id }}</span></div>
      <div class="mrow"><span class="k">时间</span><span class="mono">{{ packet.t }}</span></div>
      <div class="mrow">
        <span class="k">方向</span>
        <span><span class="tag" :class="packet.dir">{{ packet.dir }}</span></span>
      </div>
      <div class="mrow"><span class="k">协议</span><span class="mono">{{ packet.proto }}</span></div>
      <div class="mrow"><span class="k">应用层</span><span class="tag">{{ packet.app || '—' }}</span></div>
      <div class="mrow"><span class="k">客户端</span><span class="mono ell">{{ packet.client || '—' }}</span></div>
      <div class="mrow"><span class="k">目标</span><span class="mono ell">{{ packet.target || '—' }}</span></div>
      <div class="mrow"><span class="k">长度</span>
        <span class="mono">
          {{ packet.len }}
          <template v-if="packet.mod_len !== undefined"> → {{ packet.mod_len }}（改写后）</template>
        </span>
      </div>
      <div class="mrow"><span class="k">标签</span><span class="mono">{{ packet.label || '—' }}</span></div>
    </div>

    <div v-if="packet.note" class="note">{{ packet.note }}</div>

    <div v-if="packet.trunc" class="warn-box">
      这个包在**捕获时**就被截断了（超过引擎单包上限），hex 不完整。
    </div>

    <!-- 动作 -->
    <div class="row actions">
      <button class="btn sm" :disabled="busy"
              @click="emit('replay', { id: packet.id, data: activeHex, target: packet.target, proto: packet.proto, times: 1 })">
        重放
      </button>
      <button v-if="held" class="btn sm primary" :disabled="busy"
              @click="emit('release', { id: packet.id, action: 'forward', hex: '' })">
        放行
      </button>
      <button v-if="held" class="btn sm danger" :disabled="busy"
              @click="emit('release', { id: packet.id, action: 'drop', hex: '' })">
        丢弃
      </button>
      <span v-if="held" class="tag hold">在断点队列中</span>
      <span class="spacer" />
      <label v-if="isModified" class="chk">
        <input v-model="showRaw" type="checkbox" />
        显示原始包（未改写）
      </label>
      <label class="chk">
        <input v-model="showAscii" type="checkbox" />
        显示 ASCII
      </label>
    </div>

    <div v-if="isModified" class="mod-box">
      实际转发的是**改写后**的载荷（{{ packet.mod_len }} 字节）。当前显示：
      <b>{{ showRaw ? '原始包' : '改写后' }}</b>。
    </div>

    <!-- 结构 -->
    <div class="cols">
      <section class="panel">
        <header>
          <span>分层结构</span>
          <span class="spacer" />
          <span class="hint">
            {{ struct && struct.app ? struct.app : '未识别' }}
            <template v-if="struct && struct.of"> · 基于 {{ struct.of }}</template>
          </span>
        </header>
        <div class="body fields">
          <div v-if="!fields.length" class="hint">
            没有可拆的字段 —— 认不出的载荷只按原始字节显示，不猜结构。
          </div>
          <table v-else class="grid">
            <thead>
              <tr>
                <th style="width:74px">偏移</th>
                <th style="width:56px">长度</th>
                <th style="width:132px">字段</th>
                <th>值</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(f, i) in fields" :key="i">
                <td class="mono">{{ f.off }}</td>
                <td class="mono">{{ f.len }}</td>
                <td>{{ f.name }}</td>
                <td class="mono ell" :title="String(f.value)">{{ f.value }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="layers.length" class="layers">
            <span v-for="(l, i) in layers" :key="i" class="tag">
              {{ l.name }} @{{ l.off }}+{{ l.len }}
            </span>
          </div>
        </div>
      </section>

      <section class="panel">
        <header>
          <span>载荷（{{ activeHex.length / 2 }} 字节）</span>
          <span class="spacer" />
          <span class="hint">高亮来自左侧结构表的 off/len</span>
        </header>
        <div class="body">
          <HexDump :hex="activeHex" :marks="marks" />
          <details v-if="showAscii" class="ascii">
            <summary>ASCII</summary>
            <pre class="mono">{{ hexToAscii(activeHex) }}</pre>
          </details>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.detail { display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.meta {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
  gap: 3px 14px; background: var(--bg-sunken); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 9px 11px;
}
.mrow { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.mrow .k { color: var(--fg-muted); font-size: 12px; flex: 0 0 60px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.note {
  font-size: 12px; color: var(--warn); background: var(--warn-weak);
  border: 1px solid #ecd7ac; border-radius: var(--radius-sm); padding: 6px 9px;
}
.mod-box {
  font-size: 12px; background: #f2e9fd; color: var(--mod);
  border: 1px solid #ddc9f5; border-radius: var(--radius-sm); padding: 6px 9px;
}
.actions { gap: 6px; }
.spacer { flex: 1; }
.cols { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 10px; }
.fields { max-height: 380px; overflow: auto; }
.layers { display: flex; flex-wrap: wrap; gap: 4px; padding-top: 7px; }
.ascii { margin-top: 9px; }
.ascii pre { margin: 5px 0 0; white-space: pre-wrap; word-break: break-all; color: var(--fg-muted); }
@media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }
</style>
