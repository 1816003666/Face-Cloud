// 时间显示统一入口。
//
// 背景：后端很多字段是「naive UTC」的 ISO 串（形如 2026-10-27T08:47:32.342898，没有 Z）。
// 以前表格里直接字符串切片显示，等于把 UTC 当本地时间展示，东八区会整整差 8 小时。
// 这里统一：
//   1) 没有时区标记的字符串按 UTC 解析（补 Z）；
//   2) 输出前转成本地时区。

const HAS_TZ = /(Z|[+-]\d{2}:?\d{2})$/i

/** 把后端时间值解析成 Date（无时区标记时按 UTC）。解析失败返回 null。 */
export function toDate(v) {
  if (v === null || v === undefined || v === '') return null
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v
  let s = String(v).trim()
  if (!s) return null
  // "2026-10-27 08:47:32" 这种空格分隔也支持
  s = s.replace(' ', 'T')
  if (!HAS_TZ.test(s)) s += 'Z'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

const pad = (n) => String(n).padStart(2, '0')

/** 表格里显示到分钟：2026-10-27 16:47（本地时区） */
export function fmtMinute(v) {
  const d = toDate(v)
  if (!d) return v ? String(v).replace('T', ' ').slice(0, 16) : '—'
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 只显示日期：2026-10-27（本地时区） */
export function fmtDay(v) {
  const d = toDate(v)
  if (!d) return v ? String(v).replace('T', ' ').slice(0, 10) : '—'
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 显示到秒：2026-10-27 16:47:32（本地时区） */
export function fmtSecond(v) {
  const d = toDate(v)
  if (!d) return v ? String(v).replace('T', ' ').slice(0, 19) : '—'
  return `${fmtMinute(v)}:${pad(d.getSeconds())}`
}

export default fmtMinute
