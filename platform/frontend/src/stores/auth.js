/** 会话状态。手写 reactive store —— 只有一处状态，不值得为它引一整个状态库。 */
import { reactive, computed } from 'vue'
import { authApi } from '../api'
import { getToken, setToken } from '../api/client'

const state = reactive({
  user: null,
  ready: false,
  loading: false,
  error: '',
})

export const auth = {
  state,
  user: computed(() => state.user),
  isLoggedIn: computed(() => !!state.user),
  display: computed(() => (state.user && (state.user.display || state.user.username)) || ''),

  /** 启动时用 Cookie / 本地令牌探一次会话，失败就当未登录。 */
  async bootstrap() {
    try {
      const d = await authApi.me()
      state.user = d.user
    } catch {
      state.user = null
      setToken('')
    } finally {
      state.ready = true
    }
  },

  async login(username, password) {
    state.loading = true
    state.error = ''
    try {
      const r = await authApi.login(username, password)
      setToken(r.data.token)
      state.user = r.data.user
      return true
    } catch (e) {
      // 后端对「账号不存在 / 停用 / 口令错」回同一句话，这里原样透出，不加戏。
      state.error = e.message || '登录失败'
      return false
    } finally {
      state.loading = false
    }
  },

  async logout() {
    try { await authApi.logout() } catch { /* 令牌本来就无效也无所谓 */ }
    setToken('')
    state.user = null
  },

  clearError() { state.error = '' },
  /**
   * 由调用方直接设错 —— 本地就能判定的问题（空账号/空口令）不必打一趟网络。
   * 打网络那趟只会拿到 422，而 422 的 detail 是 Pydantic 的错误数组，
   * 对用户没有任何信息量。
   */
  setError(msg) { state.error = msg || '' },
  hasToken: () => !!getToken(),
}
