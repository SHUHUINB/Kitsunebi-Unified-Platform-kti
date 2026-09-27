import { createRouter, createWebHistory } from 'vue-router'
import { auth } from '../stores/auth'
import { setUnauthorizedHandler } from '../api/client'
import { BRAND } from '../brand'

// 懒加载：WPE 页面很重（hex/结构面板），不该拖慢首屏。
const routes = [
  { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'),
    meta: { public: true, title: '登录' } },
  { path: '/', redirect: '/overview' },
  { path: '/overview', name: 'overview', component: () => import('../views/OverviewView.vue'),
    meta: { title: '概览' } },
  { path: '/wpe', name: 'wpe', component: () => import('../views/WpeView.vue'),
    meta: { title: 'WPE 封包编辑器' } },
  { path: '/charles', name: 'charles', component: () => import('../views/CharlesView.vue'),
    meta: { title: 'Charles 配置' } },
  { path: '/ccpx', name: 'ccpx', component: () => import('../views/CcpxView.vue'),
    meta: { title: 'CCProxy 账号' } },
  { path: '/kami', name: 'kami', component: () => import('../views/KamiView.vue'),
    meta: { title: '卡密' } },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

setUnauthorizedHandler(() => {
  if (router.currentRoute.value.name !== 'login') {
    router.replace({ name: 'login', query: { next: router.currentRoute.value.fullPath } })
  }
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (auth.isLoggedIn.value) return true
  // 还没 bootstrap 完就放行 —— 由 main.js 保证 bootstrap 先于 mount，
  // 这里只是兜底，避免「刷新页面被踢到登录页」。
  if (!auth.state.ready) return true
  return { name: 'login', query: { next: to.fullPath } }
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · ${BRAND}` : BRAND
})

export default router
