import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { auth } from './stores/auth'
import './styles/tokens.css'

// 先尝试用 HttpOnly Cookie 恢复会话，再挂载 —— 否则刷新页面会闪一下登录页。
auth.bootstrap().finally(() => {
  createApp(App).use(router).mount('#app')
})
