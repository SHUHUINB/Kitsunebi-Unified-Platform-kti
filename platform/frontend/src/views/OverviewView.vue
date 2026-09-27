<script setup>
/**
 * 概览：把三套系统的状态聚合成一屏。
 *
 * ★ 单个下游挂掉不影响整体 —— 后端会标 degraded 并列出 down 的项，
 *   这里如实显示，而不是整页报错。
 */
import { ref, onMounted } from 'vue'
import { overviewApi } from '../api'

const data = ref(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await overviewApi.get()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <div v-if="data" class="tag" :class="data.degraded ? 'warn' : 'ok'">
        {{ data.degraded ? '部分不可用：' + (data.down || []).join(', ') : '全部正常' }}
      </div>
      <span class="spacer" />
      <button class="btn sm" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </div>

    <div v-if="error" class="err-box">{{ error }}</div>

    <div v-if="data" class="cards">
      <!-- Charles -->
      <section class="panel">
        <header>
          <span>Charles</span>
          <span class="spacer" />
          <span class="tag" :class="data.data.charles.status === 'up' ? 'ok' : 'err'">
            {{ data.data.charles.status === 'up' ? '在线' : '不可用' }}
          </span>
        </header>
        <div class="body">
          <div v-if="data.data.charles.error" class="err-box">{{ data.data.charles.error }}</div>
          <template v-else>
            <div class="kv"><span>服务</span><b>{{ data.data.charles.state?.active ? '运行中' : '已停止' }}</b></div>
            <div class="kv"><span>PID</span><span class="mono">{{ data.data.charles.state?.pid || '—' }}</span></div>
            <div class="kv"><span>配置文件</span><span class="mono ell">{{ data.data.charles.state?.config || '—' }}</span></div>
            <div class="kv"><span>备份数</span><b>{{ data.data.charles.backups }}</b></div>
          </template>
        </div>
      </section>

      <!-- 适配层 -->
      <section class="panel">
        <header>
          <span>CCProxy 适配层</span>
          <span class="spacer" />
          <span class="tag" :class="data.data.ccpx.status === 'up' ? 'ok' : 'err'">
            {{ data.data.ccpx.status === 'up' ? '在线' : '不可用' }}
          </span>
        </header>
        <div class="body">
          <div v-if="data.data.ccpx.error" class="err-box">{{ data.data.ccpx.error }}</div>
          <template v-else>
            <details>
              <summary>原始状态</summary>
              <pre class="mono boxed">{{ JSON.stringify(data.data.ccpx.status_raw, null, 2).slice(0, 4000) }}</pre>
            </details>
            <details>
              <summary>实时统计（/live）</summary>
              <pre class="mono boxed">{{ data.data.ccpx.live ? JSON.stringify(data.data.ccpx.live, null, 2).slice(0, 4000) : '未取到' }}</pre>
            </details>
            <div v-if="data.data.ccpx_live_error" class="warn-box">
              live 未取到：{{ data.data.ccpx_live_error }}
            </div>
          </template>
        </div>
      </section>

      <!-- 平台自身 -->
      <section class="panel">
        <header>
          <span>平台数据</span>
          <span class="spacer" />
          <span class="tag ok">本地库</span>
        </header>
        <div class="body">
          <div class="kv"><span>代理账号</span><b>{{ data.data.platform.accounts }}</b></div>
          <div class="kv"><span>卡密</span><b>{{ data.data.platform.kami }}</b></div>
          <div class="kv"><span>应用</span><b>{{ data.data.platform.kamiApps }}</b></div>
          <div class="kv"><span>服务器</span><b>{{ data.data.platform.kamiServers }}</b></div>
          <div class="kv"><span>用户</span><b>{{ data.data.platform.kamiUsers }}</b></div>
        </div>
      </section>
    </div>

    <div v-else-if="!loading" class="empty">暂无数据。</div>
  </div>
</template>

<style scoped>
.spacer { flex: 1; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; }
.kv { display: flex; gap: 10px; padding: 3px 0; font-size: 12px; }
.kv > span:first-child { color: var(--fg-muted); flex: 0 0 74px; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.boxed { background: var(--bg-sunken); padding: 8px; border-radius: var(--radius-sm);
         max-height: 240px; overflow: auto; margin: 6px 0; }
details summary { cursor: pointer; font-size: 12px; color: var(--fg-muted); }
</style>
