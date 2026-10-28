import { api } from '../api/client.js'

// 路由模块统一登记：懒加载与预取共用同一份 import 函数。
export const routeModules = {
  '/dashboard': () => import('../views/Dashboard.jsx'),
  '/devices': () => import('../views/Devices.jsx'),
  '/tasks': () => import('../views/Tasks.jsx'),
  '/files': () => import('../views/Files.jsx'),
  '/scripts': () => import('../views/Scripts.jsx'),
  '/groups': () => import('../views/Groups.jsx'),
  '/billing': () => import('../views/BillingAdmin.jsx'),
  '/org': () => import('../views/Org.jsx'),
  '/enterprises': () => import('../views/Enterprises.jsx'),
  '/users': () => import('../views/Users.jsx'),
  '/vpc-lines': () => import('../views/VpcLines.jsx'),
  '/audit': () => import('../views/Audit.jsx'),
  '/alerts': () => import('../views/Alerts.jsx'),
  '/settings': () => import('../views/Settings.jsx'),
  '/diagnostics': () => import('../views/Diagnostics.jsx'),
}

const loaded = new Map()
const pending = new Map()

function readUser() {
  try {
    return JSON.parse(localStorage.getItem('user') || 'null')
  } catch {
    return null
  }
}

function isAdmin() {
  return ['admin', 'superadmin'].includes(readUser()?.role)
}

/**
 * 预取并记住已经解析完成的 JS 模块。
 *
 * React.lazy 即使对应的 import 已经完成，第一次渲染仍会挂起一次。记住模块后，
 * App 中的 RouteView 可以直接同步渲染，避免首访页面时内容区闪一下加载态。
 */
export function prefetchRoute(path) {
  if (!path) return Promise.resolve(null)
  if (loaded.has(path)) return Promise.resolve(loaded.get(path))
  if (pending.has(path)) return pending.get(path)

  const load = routeModules[path]
  if (!load) return Promise.resolve(null)

  const request = Promise.resolve()
    .then(load)
    .then((module) => {
      loaded.set(path, module)
      pending.delete(path)
      return module
    })
    .catch((error) => {
      pending.delete(path)
      throw error
    })

  pending.set(path, request)
  return request
}

export function getLoadedRouteModule(path) {
  return loaded.get(path)
}

/**
 * 空闲预热路由 chunk。
 *
 * 以前是把 14 个路由全部拉一遍——个人用户首屏会顺带下载「用户管理/企业列表/
 * 计费/系统设置」这些他根本进不去的页面（约 450KB 未压缩）。
 * 现在只预热调用方给的白名单（= 侧边栏里当前账号可见的菜单），并且每批 2 个，
 * 避免和首屏接口抢带宽。
 */
export function prefetchAllRoutes(paths) {
  if (typeof navigator !== 'undefined' && navigator.connection?.saveData) return

  const target = (Array.isArray(paths) && paths.length ? paths : Object.keys(routeModules))
    .filter((p) => routeModules[p])

  const run = () => {
    const queue = target.slice()
    const step = () => {
      queue.splice(0, 2).forEach((path) => { prefetchRoute(path).catch(() => {}) })
      if (queue.length) {
        if (typeof window !== 'undefined' && window.requestIdleCallback) {
          window.requestIdleCallback(step, { timeout: 1500 })
        } else {
          setTimeout(step, 250)
        }
      }
    }
    step()
  }

  if (typeof window !== 'undefined' && window.requestIdleCallback) {
    window.requestIdleCallback(run, { timeout: 500 })
  } else {
    setTimeout(run, 120)
  }
}

// 导航悬停/聚焦时预热页面首屏数据。延迟只用于过滤“鼠标扫过”，不影响点击后的真正请求。
const routeDataPrefetch = {
  '/dashboard': () => (
    isAdmin()
      ? [api.nodesMetrics(), api.metricsTrend(24), api.salesMetrics()]
      : [api.mySubscription(), api.mySubscriptions(), api.listDevices(), api.listPlans()]
  ),
  '/devices': () => [
    api.listDevices(),
    api.listGroups(),
    api.getRotation(),
    api.getSettings(),
    api.listServerIps(),
    ...(isAdmin() ? [api.listServers()] : []),
  ],
  '/tasks': () => [api.listTasks()],
  '/files': () => [
    api.listFiles({ page: 1, page_size: 200 }),
    api.fileStats(),
    api.listDevices({ page: 1, page_size: 300 }),
    api.listGroups(),
  ],
  '/scripts': () => [
    api.listScripts(),
    api.scriptTemplates(),
    api.listDevices({ page: 1, page_size: 300 }),
    api.listGroups(),
  ],
  '/groups': () => [api.listGroups()],
  '/billing': () => [
    api.listPlans(),
    api.adminOrders(),
    api.adminPortalOrders(),
    api.adminSubscriptions(),
    api.billingStats(),
    api.adminRefunds(),
    api.listServerIps(),
  ],
  '/vpc-lines': () => [api.listVpcLines(), api.adminVpcs()],
  '/org': () => [api.orgOverview(), api.orgMembers()],
  '/enterprises': () => [api.listEnterprises()],
  '/users': () => [api.listUsers(), api.listEnterprises()],
  '/audit': () => [
    api.listAudit({ action: '', actor_id: '', limit: 200 }),
    api.auditStats(),
  ],
  '/alerts': () => [
    api.listAlerts({ status: '', level: '', type: '', limit: 200 }),
    api.alertSummary(),
  ],
  '/settings': () => [api.getSettings(), api.getLanInfo()],
}

let dataPrefetchTimer = null

export function prefetchRouteData(path, { delay = 0 } = {}) {
  if (!path || !routeDataPrefetch[path]) return
  if (typeof navigator !== 'undefined' && navigator.connection?.saveData) return

  const adminOnly = new Set(['/billing', '/vpc-lines', '/audit', '/alerts', '/settings', '/org', '/enterprises', '/users'])
  if (adminOnly.has(path) && !isAdmin()) return

  const run = () => {
    dataPrefetchTimer = null
    try {
      routeDataPrefetch[path]().forEach((request) => {
        Promise.resolve(request).catch(() => {})
      })
    } catch {
      // 预热失败不影响正常导航；页面挂载后仍会按原有逻辑加载。
    }
  }

  if (dataPrefetchTimer) clearTimeout(dataPrefetchTimer)
  dataPrefetchTimer = null
  if (delay > 0) dataPrefetchTimer = setTimeout(run, delay)
  else run()
}
