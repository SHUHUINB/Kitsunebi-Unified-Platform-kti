<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { auth } from '../stores/auth'
import { BRAND, BRAND_SUB } from '../brand'

const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const userBox = ref(null)
const pwdBox = ref(null)

/**
 * 以 DOM 里的实际值为准 —— 不信 v-model 的 ref。
 *
 * ★ 为什么：浏览器口令管理器自动填充时**可能不派发 `input` 事件**，
 *   于是 v-model 的 ref 还是空的，提交出去就是
 *   `{"username":"","password":""}`。线上日志里就是这个形态 ——
 *   同一个 IP 一会儿 422（填充没被 ref 拿到）一会儿 401（填充的是旧口令）。
 *   DOM 是唯一事实源，ref 只是它的镜像；提交这一刻必须回去问 DOM。
 */
function readForm() {
  return {
    u: (userBox.value?.value ?? username.value).trim(),
    p: pwdBox.value?.value ?? password.value,
  }
}

/**
 * 登录成功后去哪。
 *
 * ★ `next` 是用户可控的 query，必须当**不可信输入**处理，两个理由：
 *   1. `?next=//evil.com` 是协议相对 URL，原样丢给 router 等于开重定向口子；
 *   2. `?next=a&next=b` 会让 `route.query.next` 变成**数组**，
 *      直接喂给 `router.replace()` 会抛异常 —— 而异常发生在登录**成功之后**，
 *      用户看到的就是「点了登录没反应」，连一句错误都没有。
 *      这条症状跟本文件里那个 422 的 bug 长得一模一样，很容易误判。
 */
function safeNext() {
  const n = route.query.next
  if (typeof n !== 'string') return '/overview'
  if (!n.startsWith('/') || n.startsWith('//')) return '/overview'
  return n
}

async function submit() {
  if (auth.state.loading) return
  const { u, p } = readForm()
  username.value = u
  password.value = p
  // 本地就能判定的事不打网络 —— 打过去只会换回一个 Pydantic 错误数组，
  // 那东西对用户没有任何信息量。
  if (!u || !p) {
    auth.setError(!u && !p ? '请输入账号和口令' : (u ? '请输入口令' : '请输入账号'))
    ;(u ? pwdBox : userBox).value?.focus()
    return
  }
  const ok = await auth.login(u, p)
  if (ok) {
    // 会话已经建立，导航再出岔子也不能把用户困在登录页。
    try {
      await router.replace(safeNext())
    } catch {
      await router.replace('/overview')
    }
    return
  }
  // 失败就选中口令框，直接重打 —— 省一次三击。
  pwdBox.value?.focus()
  pwdBox.value?.select()
}

onMounted(() => pwdBox.value?.focus())
</script>

<template>
  <div class="wrap">
    <form class="card" @submit.prevent="submit">
      <div class="head">
        <span class="dot" />
        <div>
          <h1>{{ BRAND }}</h1>
          <p>{{ BRAND_SUB }}</p>
        </div>
      </div>

      <div class="field">
        <label for="u">账号</label>
        <input id="u" ref="userBox" v-model="username" type="text" autocomplete="username"
               placeholder="admin" @input="auth.clearError()" />
      </div>

      <div class="field">
        <label for="p">口令</label>
        <input id="p" ref="pwdBox" v-model="password" type="password"
               autocomplete="current-password" @input="auth.clearError()" />
      </div>

      <div v-if="auth.state.error" class="err-box">{{ auth.state.error }}</div>

      <button class="btn primary wide" type="submit" :disabled="auth.state.loading">
        {{ auth.state.loading ? '登录中…' : '登录' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.wrap {
  height: 100%; display: grid; place-items: center; padding: 24px;
  background: linear-gradient(180deg, #f7f8fa, #eef1f6);
}
.card {
  width: 100%; max-width: 372px; display: flex; flex-direction: column; gap: 13px;
  background: var(--bg-panel); border: 1px solid var(--line);
  border-radius: 10px; box-shadow: var(--shadow); padding: 22px;
}
.head { display: flex; gap: 11px; align-items: flex-start; margin-bottom: 3px; }
.dot {
  width: 10px; height: 10px; border-radius: 50%; background: var(--ok);
  box-shadow: 0 0 0 3px var(--ok-weak); margin-top: 5px; flex: 0 0 auto;
}
h1 { font-size: 16px; margin: 0 0 3px; }
p { margin: 0; font-size: 12px; color: var(--fg-faint); }
.wide { width: 100%; justify-content: center; padding: 7px; }
</style>
