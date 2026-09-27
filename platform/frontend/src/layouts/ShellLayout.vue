<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { auth } from '../stores/auth'
import { BRAND, BRAND_SUB } from '../brand'

const route = useRoute()
const router = useRouter()
const navOpen = ref(false)

const items = [
  { name: 'overview', label: '概览', hint: '三套系统健康状态' },
  { name: 'wpe', label: 'WPE 封包编辑器', hint: '抓包 / 断点 / 改写 / 重放' },
  { name: 'ccpx', label: 'CCProxy 账号', hint: '代理账号增删改' },
  { name: 'charles', label: 'Charles 配置', hint: 'Map Remote / SSL / 备份' },
  { name: 'kami', label: '卡密', hint: '应用 / 服务器 / 卡密' },
]

const current = computed(() => route.meta.title || '')

async function doLogout() {
  await auth.logout()
  router.replace({ name: 'login' })
}
</script>

<template>
  <div class="shell">
    <aside class="side" :class="{ open: navOpen }">
      <div class="brand">
        <span class="dot" />
        <div>
          <div class="brand-t">{{ BRAND }}</div>
          <div class="brand-s">{{ BRAND_SUB }}</div>
        </div>
      </div>
      <nav>
        <RouterLink v-for="it in items" :key="it.name" :to="{ name: it.name }"
                    class="nav-item" @click="navOpen = false">
          <span class="nav-l">{{ it.label }}</span>
          <span class="nav-h">{{ it.hint }}</span>
        </RouterLink>
      </nav>
      <div class="side-foot">
        <div class="who">
          <div class="who-n">{{ auth.display.value || '—' }}</div>
          <div class="who-r">已登录</div>
        </div>
        <button class="btn sm" @click="doLogout">退出</button>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <button class="btn sm ghost burger" @click="navOpen = !navOpen">☰</button>
        <h1>{{ current }}</h1>
      </header>
      <div class="content">
        <slot />
      </div>
    </main>
  </div>
</template>

<style scoped>
.shell { display: flex; height: 100%; }

.side {
  width: 232px; flex: 0 0 232px; display: flex; flex-direction: column;
  background: var(--bg-panel); border-right: 1px solid var(--line);
}
.brand {
  display: flex; gap: 9px; align-items: center;
  padding: 14px 14px 12px; border-bottom: 1px solid var(--line);
}
.dot {
  width: 9px; height: 9px; border-radius: 50%; background: var(--ok);
  box-shadow: 0 0 0 3px var(--ok-weak);
}
.brand-t { font-weight: 600; }
.brand-s { font-size: 11px; color: var(--fg-faint); }

nav { flex: 1; padding: 8px; display: flex; flex-direction: column; gap: 2px; overflow: auto; }
.nav-item {
  display: flex; flex-direction: column; padding: 7px 10px;
  border-radius: var(--radius-sm); color: var(--fg); text-decoration: none;
}
.nav-item:hover { background: var(--bg-hover); text-decoration: none; }
.nav-item.router-link-active { background: var(--accent-weak); color: var(--accent); }
.nav-l { font-weight: 500; }
.nav-h { font-size: 11px; color: var(--fg-faint); }
.nav-item.router-link-active .nav-h { color: var(--accent); opacity: .75; }

.side-foot {
  padding: 10px 12px; border-top: 1px solid var(--line);
  display: flex; align-items: center; gap: 8px;
}
.who { flex: 1; min-width: 0; }
.who-n { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.who-r { font-size: 11px; color: var(--fg-faint); }

.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar {
  display: flex; align-items: center; gap: 10px;
  padding: 11px 16px; background: var(--bg-panel); border-bottom: 1px solid var(--line);
}
.topbar h1 { font-size: 15px; margin: 0; font-weight: 600; }
.burger { display: none; }
.content { flex: 1; overflow: auto; padding: 14px 16px 28px; }

@media (max-width: 860px) {
  .side { position: fixed; inset: 0 auto 0 0; z-index: 30; transform: translateX(-100%);
          transition: transform .18s; }
  .side.open { transform: none; box-shadow: var(--shadow); }
  .burger { display: inline-flex; }
}
</style>
