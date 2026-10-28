import axios from 'axios'
import {
  cacheKey, get as cacheGet, put as cachePut, putPending as cachePutPending,
  markRevalidating as cacheMarkRevalidating,
  drop as cacheDrop, clear as cacheClear, DEFAULT_TTL_MS, REVALIDATE_MS,
} from '../lib/apiCache.js'

// 所有请求走 /api，本地由 vite 代理转发到后端 8000
const http = axios.create({ baseURL: '/api', timeout: 60000 })

// 平台账号可以「以某企业视角」查看：企业 ID 存在 localStorage，随请求头发给后端
http.interceptors.request.use((cfg) => {
  try {
    const ent = localStorage.getItem('view_enterprise_id')
    if (ent) cfg.headers['X-Enterprise-Id'] = ent
  } catch { /* 忽略 */ }
  return cfg
})

/**
 * GET 响应缓存（与 PC 客户端同一套策略）：
 * 命中缓存直接返回上次响应，不发请求；陈旧则在后台静默刷新（SWR）。
 * 非 GET 请求放行成功后清空缓存，避免读到脏数据。
 */
const NO_CACHE_PATTERN = /(screenshot|\/download|\/fs\/file|\/fs$)/i

function resolveAdapter() {
  try {
    if (typeof axios.getAdapter === 'function') return axios.getAdapter(http.defaults.adapter)
  } catch { /* ignore */ }
  return typeof http.defaults.adapter === 'function' ? http.defaults.adapter : null
}

const rawAdapter = resolveAdapter()
if (rawAdapter) {
  http.defaults.adapter = async (config) => {
    const method = String(config.method || 'get').toLowerCase()
    if (method !== 'get') {
      const res = await rawAdapter(config)
      cacheClear()
      return res
    }
    const url = String(config.url || '')
    if (config.fresh === true || config.cacheTtl === 0 || NO_CACHE_PATTERN.test(url)) {
      return rawAdapter(config)
    }
    const key = cacheKey({
      baseURL: config.baseURL,
      url,
      params: config.params,
      responseType: config.responseType,
      token: typeof localStorage !== 'undefined' ? localStorage.getItem('token') : '',
    })
    const explicitTtl = Number.isFinite(config.cacheTtl) ? Number(config.cacheTtl) : null
    const ttl = explicitTtl != null && explicitTtl > 0 ? explicitTtl : DEFAULT_TTL_MS
    const cacheable = (res) =>
      !!res && res.status >= 200 && res.status < 300 &&
      !(typeof res.data === 'string' && res.data.trimStart().startsWith('<'))

    const entry = cacheGet(key)
    if (entry && entry.pending) return entry.pending
    if (entry && explicitTtl !== 0 && !config.fresh) {
      const age = Date.now() - entry.at
      if (explicitTtl != null) {
        if (age <= explicitTtl) return entry.value
      } else {
        if (age > REVALIDATE_MS && !entry.revalidating) {
          cacheMarkRevalidating(key, true)
          rawAdapter(config)
            .then((res) => { if (cacheable(res)) cachePut(key, res, ttl); else cacheDrop(key) })
            .catch(() => { cacheMarkRevalidating(key, false) })
        }
        return entry.value
      }
    }
    const p = rawAdapter(config)
      .then((res) => { if (cacheable(res)) cachePut(key, res, ttl); return res })
      .catch((err) => { cacheDrop(key); throw err })
    cachePutPending(key, p)
    return p
  }
}

// 注入 JWT
http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

// 401 自动登出 + 跳登录页
let handling401 = false
http.interceptors.response.use(
  (r) => r,
  (err) => {
    const status = err.response?.status
    const url = err.config?.url || ''
    if (status === 401 && !url.includes('/auth/login') && !handling401) {
      handling401 = true
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      // 同步清 store（若存在），避免路由守卫误判已登录
      window.dispatchEvent(new Event('auth:logout'))
      setTimeout(() => (handling401 = false), 1000)
      if (!location.pathname.startsWith('/login')) {
        location.href = '/login'
      }
    }
    return Promise.reject(err)
  }
)

export const api = {
  login: (username, password, extra = {}) => http.post('/auth/login', { username, password, ...extra }),
  // 企业版登录：按企业编码查企业名（公开接口）
  enterpriseLookup: (code) => http.get(`/auth/enterprise/${encodeURIComponent(code)}`),
  me: () => http.get('/auth/me'),

  listDevices: (params) => http.get('/devices', { params }),
  // 设备库里出现过的全部服务器 IP（不受当前筛选影响，供服务器过滤区渲染）
  listServerIps: () => http.get('/devices/server-ips'),
  getDevice: (id) => http.get(`/devices/${id}`),
  deviceSpecs: (id) => http.get(`/devices/${id}/specs`, { timeout: 30000 }),
  createDevice: (body) => http.post('/devices', body),
  renameDevice: (id, name) => http.patch(`/devices/${id}`, { name }),
  batchCreate: (body) => http.post('/devices/batch', body),
  deleteDevice: (id) => http.delete(`/devices/${id}`),
  batchDelete: (deviceIds) => http.post('/devices/batch-delete', { device_ids: deviceIds }),
  batchPower: (deviceIds, action) => http.post('/devices/batch-power', { device_ids: deviceIds, action }),
  batchSetGroup: (deviceIds, groupId) => http.post('/devices/batch-set-group', { device_ids: deviceIds, group_id: groupId }),
  installApk: (deviceIds, file) => {
    const fd = new FormData()
    fd.append('device_ids', deviceIds.join(','))
    fd.append('file', file)
    return http.post('/devices/install-apk', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  controlDevice: (id, action, body) => http.post(`/devices/${id}/control/${action}`, body),
  powerDevice: (id, action) => http.post(`/devices/${id}/power/${action}`),
  deviceScreenshot: (id) => http.get(`/devices/${id}/screenshot`, { responseType: 'blob' }),
  discoverDevices: () => http.get('/devices/discover'),
  importDevice: (body) => http.post('/devices/import', body),
  syncDevices: () => http.post('/devices/sync'),

  // —— 轮次运行模式 ——
  getRotation: () => http.get('/rotation'),
  updateRotation: (body) => http.put('/rotation', body),

  listTasks: () => http.get('/tasks'),
  createTask: (body) => http.post('/tasks', body),
  runTask: (id) => http.post(`/tasks/${id}/run`),
  updateTask: (id, body) => http.put(`/tasks/${id}`, body),
  toggleTask: (id, enabled) => http.patch(`/tasks/${id}/enabled`, { enabled }),
  deleteTask: (id) => http.delete(`/tasks/${id}`),
  taskExecutions: (id) => http.get(`/tasks/${id}/executions`),
  retryTaskFailed: (tid, eid) => http.post(`/tasks/${tid}/executions/${eid}/retry`),

  listUsers: () => http.get('/users'),
  createUser: (body) => http.post('/users', body),
  updateUser: (id, body) => http.patch(`/users/${id}`, body),
  resetUserPassword: (id) => http.post(`/users/${id}/reset-password`, {}),
  getUserPermissions: (id) => http.get(`/users/${id}/permissions`),
  setUserPermissions: (id, body) => http.put(`/users/${id}/permissions`, body),
  listPermPresets: () => http.get('/perm-presets'),
  createPermPreset: (body) => http.post('/perm-presets', body),
  updatePermPreset: (id, body) => http.patch(`/perm-presets/${id}`, body),
  deletePermPreset: (id) => http.delete(`/perm-presets/${id}`),
  deleteUser: (id) => http.delete(`/users/${id}`),

  // —— 注册（任务书 #13）——
  register: (username, password) => http.post('/auth/register', { username, password }),

  // —— 找回密码 ——
  forgotPassword: (username) => http.post('/auth/forgot-password', { username }),
  resetPassword: (username, code, new_password) => http.post('/auth/reset-password', { username, code, new_password }),

  // —— 文件管理（任务书 #8）——
  uploadFile: (file, onProgress) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post('/files/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: onProgress,
    })
  },
  listFiles: (params) => http.get('/files', { params }),
  fileStats: () => http.get('/files/stats'),
  downloadFile: (id, filename) =>
    http.get(`/files/${id}/download`, { responseType: 'blob' }).then((r) => {
      const url = window.URL.createObjectURL(new Blob([r.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = filename || `file_${id}`
      a.click()
      window.URL.revokeObjectURL(url)
    }),
  deleteFile: (id) => http.delete(`/files/${id}`),
  pushFile: (id, deviceIds) => http.post(`/files/${id}/push`, { device_ids: deviceIds }),
  installFile: (id, deviceIds) => http.post(`/files/${id}/install`, { device_ids: deviceIds }),
  getFileBlob: (id) => http.get(`/files/${id}/download`, { responseType: 'blob' }),
  deviceFs: (id, path) => http.get(`/files/devices/${id}/fs`, { params: { path } }),
  deviceFsRead: (id, path) => http.get(`/files/devices/${id}/fs/read`, { params: { path } }),
  deviceFsFile: (id, path) => http.get(`/files/devices/${id}/fs/file`, { params: { path }, responseType: 'blob' }),

  // —— 脚本管理（任务书 #9）——
  listScripts: () => http.get('/scripts'),
  scriptTemplates: () => http.get('/scripts/templates'),
  createScript: (body) => http.post('/scripts', body),
  updateScript: (id, body) => http.patch(`/scripts/${id}`, body),
  deleteScript: (id) => http.delete(`/scripts/${id}`),
  executeScript: (id, deviceIds, variables) => http.post(`/scripts/${id}/execute`, { device_ids: deviceIds, variables: variables || {} }),
  duplicateScript: (id) => http.post(`/scripts/${id}/duplicate`),
  exportScript: (id) => http.get(`/scripts/${id}/export`),
  importScript: (body) => http.post('/scripts/import', body),
  scriptExecutions: (scriptId) => http.get('/scripts/executions', { params: { script_id: scriptId } }),

  // —— 分组管理（任务书 #10）——
  listGroups: () => http.get('/groups'),
  createGroup: (body) => http.post('/groups', body),
  updateGroup: (id, body) => http.patch(`/groups/${id}`, body),
  deleteGroup: (id) => http.delete(`/groups/${id}`),
  groupBatchAction: (id, body) => http.post(`/groups/${id}/batch-action`, body),

  // —— 审计日志（任务书 #11）——
  listAudit: (params) => http.get('/audit', { params }),
  auditStats: () => http.get('/audit/stats'),

  // —— 告警系统（任务书 #12）——
  listAlerts: (params) => http.get('/alerts', { params }),
  alertSummary: () => http.get('/alerts/summary'),
  ackAlert: (id) => http.post(`/alerts/${id}/ack`),
  resolveAlert: (id) => http.post(`/alerts/${id}/resolve`),

  overview: () => http.get('/metrics/overview'),

  // —— 我的套餐 / 订阅（普通用户）——
  mySubscription: () => http.get('/subscription'),
  mySubscriptions: () => http.get('/subscriptions'),
  // 「我的云手机」统计：只统计已购买套餐分配到的设备
  myDeviceStats: () => http.get('/my/device-stats'),
  deleteSubscription: (id) => http.delete(`/subscriptions/${id}`),
  renewSubscription: (id, units = 1) => http.post(`/subscriptions/${id}/renew`, { units }),


  downloadCsv: async (kind) => {
    const res = await http.get(`/export/${kind}`, { responseType: 'blob' })
    const url = URL.createObjectURL(new Blob([res.data]))
    const a = document.createElement('a')
    a.href = url
    a.download = `${kind}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
  getSettings: () => http.get('/settings'),
  updateSettings: (data) => http.put('/settings', data),
  getLanInfo: () => http.get('/settings/lan'),

  // —— 服务器管理（管理员 SSH 自助接入 / 发现云手机）——
  listServers: () => http.get('/servers'),
  testServer: (body) => http.post('/servers/test', body, { timeout: 30000 }),
  discoverServer: (body) => http.post('/servers/discover', body, { timeout: 180000 }),
  rediscoverServer: (id, body) => http.post(`/servers/${id}/rediscover`, body, { timeout: 180000 }),
  createServer: (body) => http.post('/servers', body, { timeout: 180000 }),
  importServerDevices: (id, devices) => http.post(`/servers/${id}/import`, { devices }, { timeout: 60000 }),
  updateServer: (id, body) => http.patch(`/servers/${id}`, body),
  deleteServer: (id, removeDevices = false) =>
    http.delete(`/servers/${id}`, { params: { remove_devices: removeDevices ? 1 : 0 } }),

  exportUrl: (kind) => `/api/export/${kind}`,
  serverMetrics: () => http.get('/metrics/server'),
  metricsTrend: (hours) => http.get('/metrics/trend', { params: { hours } }),
  nodesMetrics: () => http.get('/metrics/nodes'),
  // —— 套餐销售（数据看板，仅管理员）——
  salesMetrics: () => http.get('/metrics/sales'),

  // —— VPC 出口线路（管理员 SOCKS5）——
listVpcLines: () => http.get('/vpc-lines'),
deployVpcDevProxy: () => http.post('/vpc-lines/dev-proxy'),
createVpcLine: (body) => http.post('/vpc-lines', body),
deleteVpcLine: (id) => http.delete(`/vpc-lines/${id}`),
testVpcLine: (id) => http.post(`/vpc-lines/${id}/test`),
startVpcLine: (id) => http.post(`/vpc-lines/${id}/start`),
stopVpcLine: (id) => http.post(`/vpc-lines/${id}/stop`),
adminVpcs: () => http.get('/admin/vpcs'),
setVpcLine: (vpcId, vpcLineId) => http.patch(`/admin/vpcs/${vpcId}/line`, { vpc_line_id: vpcLineId }),

  // —— 计费管理（套餐 / 订单 / 订阅，admin）——
  listPlans: () => http.get('/plans'),
  createPlan: (data) => http.post('/plans', data),
  updatePlan: (id, data) => http.put(`/plans/${id}`, data),
  deletePlan: (id) => http.delete(`/plans/${id}`),
  togglePlan: (id) => http.post(`/plans/${id}/toggle`),
  createOrder: (planId, channel = 'mock', extra = {}) => http.post('/orders', { plan_id: planId, channel, ...extra }),
  // ---- 诊断修复 ----
  systemDiag: () => http.get('/diag/system'),
  deviceDiag: (id) => http.get(`/diag/device/${id}`),
  fixDevice: (id) => http.post(`/diag/device/${id}/fix`, {}),
  batchFix: (ids) => http.post('/diag/batch-fix', { device_ids: ids || [] }),
  diagHistory: (params) => http.get('/diag/history', { params }),

  // 下单前报价：台数 × 时长
  orderQuote: (params) => http.get('/orders/quote', { params }),
  payOrder: (orderId) => http.post(`/orders/${orderId}/pay`, {}),
  myOrders: (params) => http.get('/orders', { params }),
  deleteOrder: (id) => http.delete(`/orders/${id}`),
  batchDeleteOrders: (ids) => http.post('/orders/batch-delete', { ids }),
  adminOrders: (params) => http.get('/admin/orders', { params }),
  adminPortalOrders: () => http.get('/admin/portal-orders'),
  adminSubscriptions: () => http.get('/admin/subscriptions'),
  billingStats: () => http.get('/admin/billing-stats'),

  // 退款单（阶段2）
  requestRefund: (subId, reason = '') => http.post(`/subscription/${subId}/refund`, { reason }),
  refundHistory: (subId) => http.get(`/subscription/${subId}/refunds`),
  adminRefunds: () => http.get('/admin/refunds'),
  adminRefundAction: (id, action) => http.post(`/admin/refunds/${id}/${action}`, {}),
  adminRefundOrder: (id) => http.post(`/admin/orders/${id}/refund`, {}),

  // 企业管理（平台级）
  listEnterprises: () => http.get('/enterprises'),
  createEnterprise: (body) => http.post('/enterprises', body),
  updateEnterprise: (id, body) => http.patch(`/enterprises/${id}`, body),
  deleteEnterprise: (id) => http.delete(`/enterprises/${id}`),

  // 部门与角色
  orgOverview: () => http.get('/org/overview'),
  orgMembers: (params) => http.get('/org/members', { params }),
  orgCreateMember: (body) => http.post('/org/members', body),
  orgUpdateMember: (id, body) => http.patch(`/org/members/${id}`, body),
  orgDeleteMember: (id) => http.delete(`/org/members/${id}`),
  orgResetMemberPassword: (id) => http.post(`/org/members/${id}/reset-password`, {}),
  orgMemberDevices: (id) => http.get(`/org/members/${id}/devices`),
  orgAssignDevices: (id, ids) => http.post(`/org/members/${id}/devices`, { device_ids: ids }),
  orgCreateDept: (body) => http.post('/org/departments', body),
  orgUpdateDept: (id, body) => http.patch(`/org/departments/${id}`, body),
  orgDeleteDept: (id) => http.delete(`/org/departments/${id}`),
  orgCreateRole: (body) => http.post('/org/roles', body),
  orgUpdateRole: (id, body) => http.patch(`/org/roles/${id}`, body),
  orgDeleteRole: (id) => http.delete(`/org/roles/${id}`),
}

export default http
