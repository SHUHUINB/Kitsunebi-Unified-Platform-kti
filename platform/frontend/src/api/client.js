/**
 * 统一 API 客户端。
 *
 * 三件事，一件都不省：
 *   1. 令牌走 `Authorization: Bearer`（同时后端也会认 HttpOnly Cookie）。
 *   2. 统一解包 `{ok, data}`；`ok=false` 一律抛，绝不把失败当成功往下传。
 *   3. 错误里带 HTTP 状态码和上游原文 —— 「502 上游返回 …」比「请求失败」有用一万倍。
 */

const TOKEN_KEY = 'ccpx.token'

let token = localStorage.getItem(TOKEN_KEY) || ''

export function setToken(v) {
  token = v || ''
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function getToken() {
  return token
}

export class ApiError extends Error {
  constructor(status, message, payload) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

/** 未登录时由 router 接管跳转 —— 客户端不直接依赖 router，避免循环引用。 */
let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

/**
 * 把后端 `detail` 压成一句能读的话。
 *
 * ★ 为什么必须做这件事：FastAPI 的 422 里 `detail` 是**数组**
 *   （Pydantic 逐字段报错），直接 `new Error(数组)` 会被 `String()` 变成
 *   `[object Object]` —— 「哪里错了」这条信息整个丢掉。
 *   线上真实后果：登录页把 422 显示成 `[object Object]`，用户既不知道
 *   错在哪、也不知道要做什么，只能反馈「无法登录」。
 *   这类「错误信息本身坏掉」的问题，修一次就要在**所有**接口上生效，
 *   所以放在客户端这一层，而不是在每个调用点各写一遍。
 */
function readableDetail(detail) {
  if (detail === undefined || detail === null) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      if (d === null || d === undefined) return ''
      if (typeof d !== 'object') return String(d)
      // Pydantic v2: {type, loc:['body','username'], msg:'String should have at least 1 character'}
      const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== 'body').join('.') : ''
      const msg = d.msg || d.message || d.type || '参数不合法'
      return loc ? `${loc}：${msg}` : msg
    }).filter(Boolean).join('；')
  }
  if (typeof detail === 'object') {
    return detail.error || detail.message || detail.msg || JSON.stringify(detail)
  }
  return String(detail)
}

async function request(method, path, { body, query, raw } = {}) {
  let url = path
  if (query) {
    const sp = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue
      sp.append(k, typeof v === 'boolean' ? (v ? '1' : '0') : String(v))
    }
    const qs = sp.toString()
    if (qs) url += (url.includes('?') ? '&' : '?') + qs
  }

  const headers = { Accept: 'application/json' }
  if (token) headers.Authorization = 'Bearer ' + token
  let payload
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  const resp = await fetch(url, {
    method,
    headers,
    body: payload,
    credentials: 'same-origin',   // HttpOnly Cookie 会话
  })

  if (resp.status === 401) {
    setToken('')
    if (onUnauthorized) onUnauthorized()
    throw new ApiError(401, '未登录或会话已过期')
  }

  if (raw) {
    if (!resp.ok) throw new ApiError(resp.status, `HTTP ${resp.status}`)
    return resp
  }

  const text = await resp.text()
  let obj = null
  try { obj = text ? JSON.parse(text) : null } catch { /* 非 JSON 原文 */ }

  if (!resp.ok) {
    const detail = readableDetail(obj && (obj.detail ?? obj.error ?? obj.err))
    throw new ApiError(resp.status, detail || text.slice(0, 300) || `HTTP ${resp.status}`, obj)
  }
  if (obj && obj.ok === false) {
    const detail = readableDetail(obj.err ?? obj.error)
    throw new ApiError(resp.status, detail || '上游返回 ok=false', obj)
  }
  return obj
}

export const http = {
  get: (p, opts) => request('GET', p, opts),
  post: (p, body, opts) => request('POST', p, { ...opts, body: body ?? {} }),
  put: (p, body, opts) => request('PUT', p, { ...opts, body: body ?? {} }),
  patch: (p, body, opts) => request('PATCH', p, { ...opts, body: body ?? {} }),
  del: (p, opts) => request('DELETE', p, opts),
}

/** 只取 data 字段，省得每个调用点都写 `.data`。 */
export async function data(method, path, opts) {
  const r = await request(method, path, opts)
  return r && Object.prototype.hasOwnProperty.call(r, 'data') ? r.data : r
}
