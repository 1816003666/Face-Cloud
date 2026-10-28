/**
 * 轻量 GET 响应缓存（进程内、按请求维度）。
 *
 * 解决「客户端每次切换功能页都要重新调接口」：
 *  - 同一请求在 TTL 内直接复用上一次的响应（不发网络请求，切页瞬时完成）；
 *  - 并发发出的相同请求合并为一次（多个组件同时挂载时只打一个接口）；
 *  - 任何写操作（POST/PUT/PATCH/DELETE）之后整体失效，保证读到新数据；
 *  - 调用方可传 { fresh: true } 或 { cacheTtl: 0 } 强制绕过/禁用缓存。
 *
 * 注意：截图、文件下载、设备文件浏览等二进制/实时接口不走缓存（见 client.js 的 NO_CACHE）。
 */

// 语义说明（v2）：
//  - 不传 cacheTtl（默认）：SWR —— 有缓存就立刻返回（不阻塞切页），
//    超过 REVALIDATE_MS 才在后台刷新一次，下次读到的就是新数据；
//  - 传 cacheTtl: N：明确"最多接受 N 毫秒的陈旧" —— 超过就同步取新数据（轮询用这种）；
//  - cacheTtl: 0 或 fresh: true：绕过缓存（fresh 仍会写回缓存）。
export const DEFAULT_TTL_MS = 5 * 60 * 1000
export const REVALIDATE_MS = 30 * 1000
const MAX_AGE_MS = 30 * 60 * 1000

/** key -> { at, expires, value, revalidating } 或 { pending: Promise } */
const store = new Map()

/** 取记录：不看 TTL，只按最大保留期淘汰（供 SWR 用） */
export function get(key) {
  const entry = store.get(key)
  if (!entry) return null
  if (entry.pending) return entry
  if (entry.at && Date.now() - entry.at > MAX_AGE_MS) {
    store.delete(key)
    return null
  }
  return entry
}

/** 标记"正在后台刷新"，避免同一个 key 频繁重复刷新 */
export function markRevalidating(key, on) {
  const entry = store.get(key)
  if (entry && !entry.pending) entry.revalidating = !!on
}

export function cacheKey({ baseURL, url, params, token, responseType }) {
  let q = ''
  try {
    q = JSON.stringify(params === undefined ? null : params)
  } catch {
    q = String(params)
  }
  // token 参与 key：换账号/换服务端后不会命中旧数据
  return [baseURL || '', url || '', q, responseType || '', token || ''].join('|')
}

export function peek(key) {
  const entry = store.get(key)
  if (!entry) return null
  if (entry.pending) return entry
  if (entry.expires <= Date.now()) {
    store.delete(key)
    return null
  }
  return entry
}

export function put(key, value, ttlMs) {
  const now = Date.now()
  store.set(key, { at: now, expires: now + Math.max(0, ttlMs), value })
}

export function putPending(key, promise) {
  store.set(key, { pending: promise })
}

export function drop(key) {
  store.delete(key)
}

export function clear() {
  store.clear()
}

export function size() {
  return store.size
}

/** 仅供调试/自检使用 */
export const __store = store
