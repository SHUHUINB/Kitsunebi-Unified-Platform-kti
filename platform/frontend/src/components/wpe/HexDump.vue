<script setup>
/**
 * 十六进制视图。
 *
 * 只做两件事：把 hex 排成 16 字节一行，以及**按 off/len 高亮**。
 * 结构面板给的偏移是相对整个载荷的，所以高亮必须落在字节上 ——
 * 偏移错了就是「指错地方」，那比不高亮更糟。
 */
import { computed } from 'vue'

const props = defineProps({
  hex: { type: String, default: '' },
  ascii: { type: String, default: '' },
  // [{off, len, name}] —— 只用于高亮，不做结构判定
  marks: { type: Array, default: () => [] },
  perLine: { type: Number, default: 16 },
  maxBytes: { type: Number, default: 4096 },
})

const bytes = computed(() => {
  const s = (props.hex || '').replace(/[^0-9a-fA-F]/g, '')
  const out = []
  const n = Math.min(Math.floor(s.length / 2), props.maxBytes)
  for (let i = 0; i < n; i++) out.push(parseInt(s.substr(i * 2, 2), 16))
  return out
})

const truncatedBytes = computed(() => {
  const total = Math.floor((props.hex || '').replace(/[^0-9a-fA-F]/g, '').length / 2)
  return Math.max(0, total - bytes.value.length)
})

const rows = computed(() => {
  const out = []
  const per = props.perLine
  for (let i = 0; i < bytes.value.length; i += per) {
    const slice = bytes.value.slice(i, i + per)
    out.push({
      off: i,
      cells: slice.map((b, k) => ({ b, i: i + k })),
    })
  }
  return out
})

function markOf(index) {
  for (const m of props.marks) {
    const off = Number(m.off) || 0
    const len = Number(m.len) || 0
    if (len > 0 && index >= off && index < off + len) return m
  }
  return null
}

const markClass = computed(() => {
  const map = new Map()
  const palette = ['m0', 'm1', 'm2', 'm3', 'm4', 'm5']
  props.marks.forEach((m, idx) => {
    const off = Number(m.off) || 0
    const len = Number(m.len) || 0
    if (len <= 0) return
    for (let i = off; i < off + len; i++) {
      if (!map.has(i)) map.set(i, palette[idx % palette.length])
    }
  })
  return map
})

function cls(index) {
  return markClass.value.get(index) || ''
}

function hex(b) { return b.toString(16).padStart(2, '0') }

function printable(b) { return b >= 0x20 && b < 0x7f ? String.fromCharCode(b) : '.' }

function offText(o) { return o.toString(16).padStart(8, '0') }
</script>

<template>
  <div class="hex">
    <div v-if="!bytes.length" class="empty">无载荷</div>
    <template v-else>
      <div v-for="r in rows" :key="r.off" class="line">
        <span class="off">{{ offText(r.off) }}</span>
        <span class="bytes">
          <span v-for="c in r.cells" :key="c.i" class="b" :class="cls(c.i)"
                :title="'0x' + offText(c.i)">{{ hex(c.b) }}</span>
        </span>
        <span class="asc">
          <span v-for="c in r.cells" :key="c.i" class="a" :class="cls(c.i)">{{ printable(c.b) }}</span>
        </span>
      </div>
      <div v-if="truncatedBytes" class="hint pad">
        仅显示前 {{ bytes.length }} 字节，还有 {{ truncatedBytes }} 字节未渲染（完整内容请用导出）。
      </div>
    </template>
  </div>
</template>

<style scoped>
.hex { font-family: var(--mono); font-size: 12px; line-height: 1.5; overflow: auto; }
.line { display: flex; gap: 10px; white-space: pre; }
.line:hover { background: var(--bg-hover); }
.off { color: var(--fg-faint); flex: 0 0 auto; }
.bytes { flex: 0 0 auto; }
.asc { color: var(--fg-muted); flex: 0 0 auto; }
.b { padding: 0 1px; border-radius: 2px; }
.a { border-radius: 2px; }
.pad { padding: 6px 2px; }

/* 结构高亮的六色轮转 —— 相邻字段颜色不同，能看清边界。 */
.m0 { background: #cfe0fb; }
.m1 { background: #cdeadb; }
.m2 { background: #fbe6c4; }
.m3 { background: #e6d6f8; }
.m4 { background: #fbdedb; }
.m5 { background: #d5eef1; }
</style>
