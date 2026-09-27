/** 各域端点。组件只调这里，不拼路径 —— 路径只在本文件出现一次。 */
import { data, http } from './client'

const API = '/api/v1'

/* ---------------- 认证 ---------------- */
export const authApi = {
  login: (username, password) => http.post(`${API}/auth/login`, { username, password }),
  logout: () => http.post(`${API}/auth/logout`),
  me: () => data('GET', `${API}/auth/me`),
  password: (oldPwd, newPwd) => http.post(`${API}/auth/password`, { old: oldPwd, new: newPwd }),
}

/* ---------------- 概览 ---------------- */
export const overviewApi = {
  get: () => http.get(`${API}/overview`),
}

/* ---------------- Charles ---------------- */
export const charlesApi = {
  state: () => data('GET', `${API}/charles/state`),
  config: () => data('GET', `${API}/charles/config`),
  applyChanges: (changes, restart = true) =>
    http.post(`${API}/charles/changes`, { changes, restart }),
  applyRaw: (xml, restart = true) => http.post(`${API}/charles/raw`, { xml, restart }),
  backups: () => data('GET', `${API}/charles/backups`),
  backup: (tag) => http.post(`${API}/charles/backups`, { tag }),
  restore: (name, restart = true) =>
    http.post(`${API}/charles/backups/${encodeURIComponent(name)}/restore`, { restart }),
  logs: (lines = 200) => data('GET', `${API}/charles/logs`, { query: { lines } }),
  service: (action, restart = true) => http.post(`${API}/charles/service`, { action, restart }),
  ca: () => data('GET', `${API}/charles/ca`),
  webif: (path = '/') => data('GET', `${API}/charles/webif`, { query: { path } }),
  // 官方帮助页名单（从 charles.jar 扫出来，不是硬编码）。
  helpNames: () => data('GET', `${API}/charles/help`),
  // 帮助页返回的是 HTML 不是 JSON，走 raw 拿原始 Response 再 .text()。
  help: (name) => http.get(`${API}/charles/help`, { query: { name }, raw: true }),
}

/* ---------------- 适配层 ---------------- */
export const ccpxApi = {
  status: () => data('GET', `${API}/ccpx/status`),
  live: () => data('GET', `${API}/ccpx/live`),
  accounts: () => data('GET', `${API}/ccpx/accounts`),
  add: (payload) => http.post(`${API}/ccpx/accounts`, payload),
  update: (username, payload) =>
    http.patch(`${API}/ccpx/accounts/${encodeURIComponent(username)}`, payload),
  remove: (username) => http.del(`${API}/ccpx/accounts/${encodeURIComponent(username)}`),
  logs: (lines = 200) => data('GET', `${API}/ccpx/logs`, { query: { lines } }),
}

/* ---------------- WPE ---------------- */
export const wpeApi = {
  stat: () => http.get(`${API}/wpe/stat`),
  packets: (filter = {}) => http.get(`${API}/wpe/packets`, { query: filter }),
  packet: (id) => http.get(`${API}/wpe/packets/${id}`),
  replay: (id, payload = {}) => http.post(`${API}/wpe/packets/${id}/replay`, payload),
  send: (payload) => http.post(`${API}/wpe/send`, payload),
  rewrite: () => http.get(`${API}/wpe/rewrite`),
  setRewrite: (on, rules) => http.put(`${API}/wpe/rewrite`, { on, rules }),
  hold: () => http.get(`${API}/wpe/hold`),
  setHold: (on, rule) => http.put(`${API}/wpe/hold`, { on, rule }),
  release: (id, action = 'forward', hex = '') =>
    http.post(`${API}/wpe/hold/${id}/release`, { action, data: hex }),
  clear: () => http.del(`${API}/wpe/packets`),
  exportPackets: (query = {}) => http.get(`${API}/wpe/export`, { query }),
  exports: () => http.get(`${API}/wpe/exports`),
  exportSave: (payload = {}) => http.post(`${API}/wpe/exports`, payload),
  exportDownloadUrl: (name) => `${API}/wpe/exports/${encodeURIComponent(name)}/download`,
  importPackets: (packets) => http.post(`${API}/wpe/import`, { packets }),
}

/* ---------------- 卡密 ---------------- */
export const kamiApi = {
  apps: () => http.get(`${API}/kami/apps`),
  addApp: (p) => http.post(`${API}/kami/apps`, p),
  updateApp: (id, p) => http.put(`${API}/kami/apps/${id}`, p),
  removeApp: (id) => http.del(`${API}/kami/apps/${id}`),

  servers: () => http.get(`${API}/kami/servers`),
  addServer: (p) => http.post(`${API}/kami/servers`, p),
  updateServer: (id, p) => http.put(`${API}/kami/servers/${id}`, p),
  removeServer: (id) => http.del(`${API}/kami/servers/${id}`),

  users: () => http.get(`${API}/kami/users`),
  addUser: (p) => http.post(`${API}/kami/users`, p),
  updateUser: (uid, p) => http.put(`${API}/kami/users/${uid}`, p),
  removeUser: (uid) => http.del(`${API}/kami/users/${uid}`),

  list: (query = {}) => http.get(`${API}/kami/list`, { query }),
  create: (p) => http.post(`${API}/kami`, p),
  batch: (p) => http.post(`${API}/kami/batch`, p),
  // 删卡密有两个入口：列表行里有 id 用 removeKami，批量勾选时只有 code 用 removeMany。
  // 对应一花 `ajax.php?act=delkami`（它只有按 code 批量这一种）。
  removeKami: (id) => http.del(`${API}/kami/${id}`),
  removeMany: (codes) => http.post(`${API}/kami/delete`, { codes }),
  stats: () => http.get(`${API}/kami/stats`),
  logs: (lines = 200) => http.get(`${API}/kami/logs`, { query: { lines } }),
}
