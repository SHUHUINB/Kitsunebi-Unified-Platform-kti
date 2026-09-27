<script setup>
/**
 * 封包列表。
 *
 * ★ 列表里的 hex 是**截断预览**（适配层按 512 字符截），所以这一列必须
 *   标出 hex_trunc —— 不标的话，看的人会以为这就是全部内容。
 * ★ has_more 为真时必须给「加载更早」的入口。没有它，缓冲 4000 条时
 *   后 2000 条永远看不到（旧栈就是这个毛病）。
 */
const props = defineProps({
  packets: { type: Array, default: () => [] },
  selectedId: { type: [Number, null], default: null },
  // 断点队列里的封包 id。适配层**不会**在封包上打 hold 标记，
  // 唯一可信来源是 /wpe/hold 的 holding 数 + only_hold 过滤出来的列表。
  heldIds: { type: Array, default: () => [] },
  hasMore: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  meta: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['select', 'more'])

function isHeld(p) {
  return props.heldIds.indexOf(p.id) !== -1
}

function flags(p) {
  const out = []
  if (p.wpe_action === 'modify') out.push({ k: 'mod', t: '人工改写' })
  if (p.wpe_action === 'rewrite') out.push({ k: 'mod', t: '规则改写' })
  if (p.mod_hex && !p.wpe_action) out.push({ k: 'mod', t: '已改写' })
  if (isHeld(p)) out.push({ k: 'hold', t: '断点' })
  if (p.imported) out.push({ k: '', t: '导入' })
  if (p.trunc) out.push({ k: 'warn', t: '截断' })
  return out
}
</script>

<template>
  <div class="tbl-wrap">
    <div class="tbl-scroll">
    <table class="grid">
      <thead>
        <tr>
          <th style="width:64px">#</th>
          <th style="width:86px">时间</th>
          <th style="width:46px">方向</th>
          <th style="width:52px">协议</th>
          <th style="width:60px">应用</th>
          <th style="width:150px">目标</th>
          <th style="width:64px">长度</th>
          <th>摘要</th>
          <th style="width:120px">标记</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="p in packets" :key="p.id"
            :class="{ sel: p.id === selectedId }"
            @click="emit('select', p.id)">
          <td class="mono">{{ p.id }}</td>
          <td class="mono dim">{{ p.t }}</td>
          <td><span class="tag" :class="p.dir">{{ p.dir === 'c2s' ? '发' : '收' }}</span></td>
          <td class="mono">{{ p.proto }}</td>
          <td><span class="tag">{{ p.app || '—' }}</span></td>
          <td class="mono ell" :title="p.target">{{ p.target || '—' }}</td>
          <td class="mono">
            {{ p.len }}
            <span v-if="p.hex_trunc" class="warn-mark" title="列表里是截断预览，完整 hex 看详情">*</span>
          </td>
          <td class="ell" :title="p.summary">{{ p.summary || p.note || '—' }}</td>
          <td>
            <span v-for="(f, i) in flags(p)" :key="i" class="tag" :class="f.k">{{ f.t }}</span>
          </td>
        </tr>
        <tr v-if="!packets.length && !loading">
          <td colspan="9" class="empty">
            没有匹配的封包。
            <template v-if="meta.scanned !== undefined">
              （已扫描 {{ meta.scanned }} / 缓冲 {{ meta.buffered }} 条）
            </template>
          </td>
        </tr>
        <tr v-if="loading">
          <td colspan="9" class="empty">加载中…</td>
        </tr>
      </tbody>
    </table>
    </div>

    <div class="foot">
      <div class="hint">
        <template v-if="meta.matched !== undefined">
          命中 {{ meta.matched }} 条 · 已扫描 {{ meta.scanned }} · 缓冲 {{ meta.buffered }}
          <template v-if="meta.truncated">
            · <b>未扫到最旧</b>（这次搜索没有覆盖全部缓冲，0 命中不等于不存在）
          </template>
        </template>
      </div>
      <button v-if="hasMore" class="btn sm" :disabled="loading" @click="emit('more')">
        加载更早
      </button>
    </div>
  </div>
</template>

<style scoped>
.tbl-wrap { display: flex; flex-direction: column; min-height: 0; }
.tbl-scroll { flex: 1; min-height: 0; overflow: auto; }
table.grid { table-layout: fixed; }
td { font-size: 12px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dim { color: var(--fg-faint); }
.warn-mark { color: var(--warn); font-weight: 700; }
.foot {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px; border-top: 1px solid var(--line); background: var(--bg-sunken);
}
.foot .hint { flex: 1; }
tbody tr { cursor: pointer; }
</style>
