import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { useAuth } from '../store/auth.jsx'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

function AdminDashboard() {
  const [nodes, setNodes] = useState(null)
  const [trend, setTrend] = useState([])
  const [trendHours, setTrendHours] = useState(24)
  const [offendersOpen, setOffendersOpen] = useState(true)
  const [loadErr, setLoadErr] = useState(false)
  const [sales, setSales] = useState(null)
  const [salesHidden, setSalesHidden] = useState(false)

  // —— 节点聚合数据：5s 轮询（后端缓存状态5s/指标15s，不会重复SSH）——
  useEffect(() => {
    let alive = true
    const load = () => api.nodesMetrics().then((r) => alive && setNodes(r.data)).catch(() => alive && setLoadErr(true))
    load()
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  // —— 趋势图：60s 轮询 ——
  useEffect(() => {
    let alive = true
    const load = () => api.metricsTrend(trendHours).then((r) => alive && setTrend(r.data)).catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => { alive = false; clearInterval(t) }
  }, [trendHours])

  // —— 套餐销售（仅管理员）：60s 轮询，403 则隐藏 ——
  useEffect(() => {
    let alive = true
    let ok = false
    try {
      const u = JSON.parse(localStorage.getItem('user') || '{}')
      ok = !!u && (u.role === 'admin' || u.role === 'superadmin')
    } catch (e) { ok = false }
    if (!ok) { setSalesHidden(true); return () => { alive = false } }
    const load = () => api.salesMetrics()
      .then((r) => alive && setSales(r.data))
      .catch((err) => { if (alive && err && err.response && err.response.status === 403) setSalesHidden(true) })
    load()
    const t = setInterval(load, 60000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const kpis = nodes?.kpis ?? null
  const lastTrend = trend.length > 0 ? trend[trend.length - 1] : null
  const top_offenders = nodes?.top_offenders ?? []
  const syncing = !nodes && !loadErr

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>数据看板</h2>
          {syncing && <span className="hint">同步中…</span>}
        </div>
        {loadErr && (
          <button
            className="ghost"
            onClick={() => {
              setLoadErr(false)
              api.nodesMetrics().then((r) => setNodes(r.data)).catch(() => setLoadErr(true))
            }}
          >重试</button>
        )}
      </div>

      {/* —— 顶部 KPI 聚合条 —— */}
      <div className="kpi-row">
        <Kpi
          label="服务器在线"
          value={kpis ? `${kpis.servers_online}/${kpis.servers_total}` : '—'}
          icon="M5 12h14M12 5l7 7-7 7"
          tone={kpis ? (kpis.servers_online === kpis.servers_total ? 'green' : 'amber') : 'amber'}
        />
        <Kpi
          label="云手机总数"
          value={kpis ? kpis.devices_total : '—'}
          sub={kpis ? `运行中 ${kpis.devices_running}（库内状态）` : ''}
          title="设备表里的全部云手机；「运行中」按设备状态字段统计"
          icon="M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zm4 4v10M12 7v10M16 7v10"
          tone="teal"
        />
        <Kpi
          label="在线设备"
          title="最近一次巡检中 adb 实测能连上的台数，可能多于「运行中」（状态未及时回写）"
          value={lastTrend ? lastTrend.devices_online : '—'}
          sub={lastTrend && lastTrend.devices_total ? `在线率 ${Math.round((100 * lastTrend.devices_online) / lastTrend.devices_total)}%` : ''}
          icon="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"
          tone="blue"
        />
        <Kpi
          label="活跃告警"
          value={kpis ? kpis.alerts_active : '—'}
          icon="M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"
          tone={kpis ? (kpis.alerts_active > 0 ? 'red' : 'green') : 'green'}
        />
        {!salesHidden && (
          <Kpi
            label="今日套餐销量"
            value={sales ? String(sales.today.orders) : '—'}
            sub={sales ? `昨日 ${sales.yesterday.orders} 单${sales.today.pending > 0 ? ` · 待支付 ${sales.today.pending}` : ''}` : ''}
            icon="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"
            tone="violet"
          />
        )}
        {!salesHidden && (
          <Kpi
            label="今日销售金额"
            value={sales ? `¥${fmtYuan(sales.today.amount_yuan)}` : '—'}
            sub={sales ? `昨日 ¥${fmtYuan(sales.yesterday.amount_yuan)}` : ''}
            icon="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"
            tone="amber"
          />
        )}
      </div>

      {!salesHidden && <SalesPanel data={sales} />}

      {/* —— 异常/高负载区（可折叠） —— */}
      <Panel title={`异常 / 高负载节点${top_offenders.length > 0 ? `（${top_offenders.length}）` : ''}`}>
        <button className="collapse-btn" onClick={() => setOffendersOpen((v) => !v)}>
          {offendersOpen ? '收起 ▲' : '展开 ▼'}
        </button>
        {offendersOpen && (
          !nodes ? (
            <div className="all-ok">数据加载中…</div>
          ) : top_offenders.length === 0 ? (
            <div className="all-ok">全部正常</div>
          ) : (
            <div className="offender-list">
              {top_offenders.map((o) => (
                <div key={o.ip} className="offender-item">
                  <span className="offender-ip">{o.ip}</span>
                  <span className="offender-reason">{o.reason}</span>
                  {o.cpu != null && <span className="offender-metric">CPU {o.cpu.toFixed(1)}%</span>}
                  {o.mem != null && <span className="offender-metric">内存 {o.mem.toFixed(1)}%</span>}
                </div>
              ))}
            </div>
          )
        )}
      </Panel>

      {/* —— 趋势图（整体聚合平均，仅 CPU + 内存 两组曲线） —— */}
      <TrendPanel data={trend} hours={trendHours} onHours={setTrendHours} />
    </div>
  )
}

export default function Dashboard() {
  const { user } = useAuth()
  // 企业管理员本质是用户，只是多了部门与角色——看板也按"用户"给他（我的套餐），
  // 平台运营视角（节点/容器/销售趋势）只属于平台账号。
  const isPlatform = (user?.role === 'admin' || user?.role === 'superadmin') && !user?.enterprise
  return isPlatform ? <AdminDashboard /> : <UserDashboard />
}

/* ---------- 普通用户看板：只看自己的套餐 ---------- */
/** 时长选项：小时套餐给 1/2/4/8/12/24 小时；长周期按"份"叠加 */
function durationOptions(plan) {
  const base = Number(plan?.min_hours || plan?.period_hours || 720)
  if (base <= 24) {
    // 小时套餐：起购时长就是标称小时数
    const hours = [...new Set([base, 2, 4, 8, 12, 24].filter((h) => h >= base))].sort((a, b) => a - b)
    return hours.map((h) => ({ hours: h, label: `${h} 小时` }))
  }
  const multipliers = [1, 2, 3, 6, 12]
  return multipliers.map((m) => {
    const hours = base * m
    const days = Math.round(hours / 24)
    return { hours, label: `${m} 份（${days} 天）` }
  })
}

/** 剩余时长：精确到分钟（X 天 Y 小时 Z 分） */
function fmtRemain(seconds, days) {
  const sec = Number(seconds)
  if (Number.isFinite(sec)) {
    if (sec <= 0) return '已到期'
    const d = Math.floor(sec / 86400)
    const h = Math.floor((sec % 86400) / 3600)
    const m = Math.floor((sec % 3600) / 60)
    if (d > 0) return `${d} 天 ${h} 小时 ${m} 分`
    if (h > 0) return `${h} 小时 ${m} 分`
    return `${Math.max(m, 1)} 分`
  }
  if (days != null) return `${days} 天`
  return '—'
}

function fmtDuration(hours) {
  const h = Number(hours || 0)
  if (h <= 0) return '—'
  if (h < 24) return `${h} 小时`
  const days = Math.round(h / 24)
  return `${days} 天`
}


function fmtDate(s) {
  if (!s) return '—'
  try { return new Date(s).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) } catch { return s }
}

const ORDER_BADGE = {
  pending: { label: '待支付', cls: 'st-warning' },
  paid: { label: '已支付', cls: 'st-running' },
  cancelled: { label: '已取消', cls: 'st-error' },
  expired: { label: '已过期', cls: 'st-error' },
}

const REFUND_LABEL = { pending: '审核中', approved: '已退款', rejected: '已驳回' }
const REFUND_TONE = {
  pending: { color: '#b45309', bg: '#fef3c7' },
  approved: { color: '#15803d', bg: '#dcfce7' },
  rejected: { color: '#b91c1c', bg: '#fee2e2' },
}

function RefundBadge({ refund, status, onClick }) {
  const st = status || refund?.status
  if (!st) return '—'
  const tone = REFUND_TONE[st] || { color: '#475569', bg: '#f1f5f9' }
  const tip = [
    refund?.reason ? `原因：${refund.reason}` : '',
    refund?.admin_note ? `处理意见：${refund.admin_note}` : '',
    onClick ? '点击查看全部退款申请记录' : '',
  ].filter(Boolean).join('\n')
  const style = {
    display: 'inline-block', padding: '2px 8px', borderRadius: 999,
    fontSize: 12, fontWeight: 600, color: tone.color, background: tone.bg,
    cursor: onClick ? 'pointer' : (tip ? 'help' : 'default'),
    border: 0, fontFamily: 'inherit', lineHeight: 1.6,
  }
  if (onClick) {
    return (
      <button type="button" onClick={onClick} title={tip || undefined} style={style}>
        {REFUND_LABEL[st] || st}
      </button>
    )
  }
  return (
    <span title={tip || undefined} style={style}>
      {REFUND_LABEL[st] || st}
    </span>
  )
}

function UserDashboard() {
  const [status, setStatus] = useState(null)
  const [subs, setSubs] = useState([])
  const [recentOrders, setRecentOrders] = useState([])
  const [myOrderList, setMyOrderList] = useState([])   // 订单列表（全部历史）
  const [paying, setPaying] = useState(0)
  const [devices, setDevices] = useState([])
  // 「我的云手机」统计：只算已购买套餐分配到的设备（不再把平台设备池算进来）
  const [myDev, setMyDev] = useState({ total: 0, online: 0 })
  const [plans, setPlans] = useState([])
  const [loading, setLoading] = useState(true)
  const [buyTarget, setBuyTarget] = useState(null)
  const [buyUnits, setBuyUnits] = useState(1)
  const [buyQuote, setBuyQuote] = useState(null)
  const [buying, setBuying] = useState(0)
  const [buyMsg, setBuyMsg] = useState('')
  const [refundTarget, setRefundTarget] = useState(null)
  const [deletingOrder, setDeletingOrder] = useState(0)
  const [orderSel, setOrderSel] = useState([])
  const [orderMsg, setOrderMsg] = useState('')
  const [batchBusy, setBatchBusy] = useState(false)
  const [renewTarget, setRenewTarget] = useState(null)
  const [renewUnits, setRenewUnits] = useState(1)
  const [renewBusy, setRenewBusy] = useState(false)
  const [renewMsg, setRenewMsg] = useState('')
  const [refundReason, setRefundReason] = useState('')
  const [refunding, setRefunding] = useState(false)
  const [refundMsg, setRefundMsg] = useState('')
  const [historySub, setHistorySub] = useState(null)
  const [history, setHistory] = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let alive = true
    Promise.all([
      api.mySubscription().then((r) => alive && setStatus(r.data)).catch(() => {}),
      api.mySubscriptions().then((r) => alive && setSubs(Array.isArray(r.data) ? r.data : [])).catch(() => {}),
      api.myOrders({ minutes: 15 }).then((r) => alive && setRecentOrders(Array.isArray(r.data) ? r.data : [])).catch(() => {}),
      api.myOrders().then((r) => alive && setMyOrderList(Array.isArray(r.data) ? r.data : [])).catch(() => {}),
      api.listDevices().then((r) => alive && setDevices(Array.isArray(r.data) ? r.data : [])).catch(() => {}),
      api.myDeviceStats().then((r) => alive && setMyDev(r.data || { total: 0, online: 0 })).catch(() => {}),
      api.listPlans().then((r) => alive && setPlans(Array.isArray(r.data) ? r.data : [])).catch(() => {}),
    ]).finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [reloadKey])

  // 台数/时长变化时向后端要报价（口径与真正下单一致）
  useEffect(() => {
    let alive = true
    if (!buyTarget) { setBuyQuote(null); return undefined }
    api.orderQuote({ plan_id: buyTarget.id, units: buyUnits })
      .then((r) => { if (alive) setBuyQuote(r.data) })
      .catch(() => { if (alive) setBuyQuote(null) })
    return () => { alive = false }
  }, [buyTarget, buyUnits])

  function toggleOrderSel(id) {
    setOrderSel((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  async function batchDeleteOrders() {
    if (orderSel.length === 0 || batchBusy) return
    if (!(await uiConfirm(`确认删除选中的 ${orderSel.length} 条订单？\n\n只从你的订单列表移除，平台对账记录仍保留。`))) return
    setBatchBusy(true)
    try {
      const r = await api.batchDeleteOrders(orderSel)
      const d = r.data || {}
      const blocked = (d.blocked || []).length
      setOrderMsg(`已删除 ${d.ok || 0} 条订单` + (blocked ? `，${blocked} 条因套餐仍在生效未删除` : ''))
      setOrderSel([])
      setReloadKey((v) => v + 1)
    } catch (e) {
      await uiAlert(e.response?.data?.error || '批量删除失败')
    } finally {
      setBatchBusy(false)
    }
  }

  async function deleteOrder(o) {
    const tip = o.status === 'paid'
      ? `确认从订单列表删除 ${o.out_trade_no || '#' + o.id}？\n\n只从你的列表里移除，平台对账记录仍保留。`
      : `确认删除订单 ${o.out_trade_no || '#' + o.id}？`
    if (!(await uiConfirm(tip))) return
    setDeletingOrder(o.id)
    try {
      await api.deleteOrder(o.id)
      setReloadKey((v) => v + 1)
    } catch (e) {
      await uiAlert(e.response?.data?.error || '删除失败')
    } finally {
      setDeletingOrder(0)
    }
  }

  async function doRenew() {
    if (!renewTarget) return
    setRenewBusy(true); setRenewMsg('')
    try {
      const r = await api.renewSubscription(renewTarget.id, Number(renewUnits) || 1)
      const d = r.data || {}
      setRenewMsg(`续费成功：支付 ¥${d.amount_yuan}，到期时间延至 ${fmtDate(d.expires_at)}`)
      // 这个组件的数据刷新走 reloadKey（没有 load() 函数）
      setReloadKey((v) => v + 1)
      setTimeout(() => setRenewTarget(null), 1600)
    } catch (e) {
      // 把真实原因打到控制台，别只显示一句续费失败
      console.error('[renew] 失败', e?.response?.data || e?.message || e)
      setRenewMsg(e.response?.data?.error || e?.message || '续费失败')
    } finally {
      setRenewBusy(false)
    }
  }

  async function purchase() {
    const plan = buyTarget
    if (!plan || buying) return
    setBuying(plan.id)
    setBuyMsg('')
    try {
      const order = await api.createOrder(plan.id, 'mock', { units: buyUnits })
      await api.payOrder(order.data.id)
      setBuyMsg(`购买成功：${plan.name}，云手机已分配`)
      setBuyTarget(null)
      setReloadKey((v) => v + 1)
    } catch (e) {
      setBuyMsg(`购买失败：${e.response?.data?.error || e.message}`)
    } finally {
      setBuying(0)
    }
  }

  const online = devices.filter((d) => d.status === 'running').length

  // 订阅记录只保留"订阅成功"的记录：已取消的不再展示，
  // 但带退款记录的除外——否则用户退款后就看不到自己的退款进度了。
  const visibleSubs = subs.filter((s) => s.status !== 'cancelled' || s.refund)
  // 15 分钟内创建、还没付钱的订单，先在这里露出来，用户可以接着付。
  const pendingOrders = recentOrders.filter((o) => o.status === 'pending')

  async function payPending(order) {
    if (paying) return
    setPaying(order.id)
    setRefundMsg('')
    setBuyMsg('')
    try {
      await api.payOrder(order.id)
      setBuyMsg(`支付成功：${order.plan_name || ''}，云手机已分配`)
      setReloadKey((v) => v + 1)
    } catch (e) {
      setBuyMsg(`购买失败：${e.response?.data?.error || e.message}`)
    } finally {
      setPaying(0)
    }
  }

  function openRefund(sub) {
    setRefundTarget(sub)
    setRefundReason('')
    setRefundMsg('')
  }

  async function deleteSub(sub) {
    if (!await uiConfirm('确认从订阅记录中删除这条记录？删除后不再显示，不影响已完成的退款。')) return
    try {
      await api.deleteSubscription(sub.id)
      setRefundMsg('')
      setReloadKey((v) => v + 1)
    } catch (e) {
      setRefundMsg(`删除失败：${e.response?.data?.error || e.message}`)
    }
  }

  async function openRefundHistory(sub) {
    setHistorySub(sub)
    setHistory([])
    setHistoryLoading(true)
    try {
      const r = await api.refundHistory(sub.id)
      setHistory(Array.isArray(r.data) ? r.data : [])
    } catch {
      setHistory([])
    } finally {
      setHistoryLoading(false)
    }
  }

  async function submitRefund() {
    const sub = refundTarget
    if (!sub || refunding) return
    setRefunding(true)
    setRefundMsg('')
    try {
      await api.requestRefund(sub.id, refundReason.trim())
      setRefundTarget(null)
      setBuyMsg('')
      setRefundMsg(`退款申请已提交（订阅 #${sub.id} · ${sub.plan_name || sub.model || ''}），等待管理员审核`)
      setReloadKey((v) => v + 1)
    } catch (e) {
      setRefundMsg(`提交失败：${e.response?.data?.error || e.message}`)
    } finally {
      setRefunding(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>我的套餐</h2>
        </div>
      </div>
      {buyMsg && (
        <div className={buyMsg.startsWith('购买失败') ? 'err' : 'ok'} style={{ marginBottom: 12 }}>
          {buyMsg}
        </div>
      )}
      {refundMsg && (
        <div className={refundMsg.startsWith('提交失败') ? 'err' : 'ok'} style={{ marginBottom: 12 }}>
          {refundMsg}
        </div>
      )}
      {loading ? (
        <div className="hint">加载中…</div>
      ) : (
        <>
          <div className="kpi-row">
            <Kpi
              label="剩余时长"
              value={status?.valid ? fmtRemain(status.remain_seconds, status.remain_days) : '未开通'}
              sub={status?.plan_name || ''}
              icon="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 5v5l4 2"
              tone={status?.valid ? 'teal' : 'amber'}
            />
            <Kpi
              label="设备配额"
              value={status?.device_quota ?? '—'}
              sub={status?.device_used != null ? `已用 ${status.device_used}` : ''}
              icon="M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zm4 4v10M12 7v10M16 7v10"
              tone="blue"
            />
            <Kpi
              label="我的云手机"
              value={myDev.total}
              sub={`在线 ${myDev.online}`}
              icon="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"
              tone="violet"
            />
            <Kpi
              label="到期时间"
              value={status?.expires_at ? fmtDate(status.expires_at) : '—'}
              icon="M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"
              tone="amber"
            />
          </div>

          <Panel title="订阅记录">
            {visibleSubs.length === 0 && pendingOrders.length === 0 ? (
              <div className="hint">暂无订阅记录。套餐请在统一门户购买，购买成功后会自动同步到这里。</div>
            ) : (
              <table className="device-table">
                <thead>
                  <tr><th>ID</th><th>套餐</th><th>台数</th><th>状态</th><th>生效时间</th><th>到期时间</th><th>剩余</th><th>退款</th><th>操作</th></tr>
                </thead>
                <tbody>
                  {visibleSubs.map((s) => (
                    <tr key={s.id}>
                      <td className="mono">{s.id}</td>
                      <td>{s.plan_name || s.model || '—'}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        {s.qty || 1} 台
                        <span className="hint" style={{ marginLeft: 4 }}>
                          （已分配 {s.used_devices ?? 0} 台）
                        </span>
                      </td>
                      <td>{s.active ? '有效' : ({ expired: '已过期', cancelled: '已取消' }[s.status] || s.status || '—')}</td>
                      <td>{fmtDate(s.started_at)}</td>
                      <td>{fmtDate(s.expires_at)}</td>
                      <td>{s.remain_seconds != null
                        ? fmtRemain(s.remain_seconds, s.remain_days)
                        : (s.remain_days != null ? `${s.remain_days} 天` : '—')}</td>
                      <td>
                        {s.refund
                          ? <RefundBadge refund={s.refund} onClick={() => openRefundHistory(s)} />
                          : '—'}
                      </td>
                      <td>
                        {s.refund?.status === 'pending' ? (
                          <button
                            type="button"
                            className="ghost"
                            disabled
                            style={{ padding: '3px 10px', fontSize: 12, opacity: 0.55, cursor: 'not-allowed' }}
                            title="退款申请审核中，请等待管理员处理"
                          >
                            审核中
                          </button>
                        ) : s.active ? (
                          <span style={{ display: 'inline-flex', gap: 6 }}>
                            <button
                              type="button"
                              className="ghost"
                              style={{ padding: '3px 10px', fontSize: 12 }}
                              onClick={() => { setRenewMsg(''); setRenewUnits(1); setRenewTarget(s) }}
                            >
                              续费
                            </button>
                            <button
                              type="button"
                              className="ghost"
                              style={{ padding: '3px 10px', fontSize: 12 }}
                              onClick={() => openRefund(s)}
                            >
                              {s.refund?.status === 'rejected' ? '重新申请' : '申请退款'}
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            className="ghost"
                            style={{ padding: '3px 10px', fontSize: 12, color: '#b91c1c' }}
                            onClick={() => deleteSub(s)}
                          >
                            删除
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {pendingOrders.map((o) => (
                    <tr key={`order-${o.id}`} style={{ background: 'rgba(251,191,36,0.07)' }}>
                      <td className="mono">#{o.id}</td>
                      <td>{o.plan_name || '—'}</td>
                      <td style={{ color: '#b45309', fontWeight: 600 }}>待支付</td>
                      <td>{fmtDate(o.created_at)}</td>
                      <td>—</td>
                      <td>—</td>
                      <td>—</td>
                      <td>
                        <button
                          type="button"
                          className="btn-primary"
                          style={{ padding: '3px 12px', fontSize: 12 }}
                          disabled={paying === o.id}
                          onClick={() => payPending(o)}
                          title="15 分钟内创建的订单可以在线支付，超时会自动取消"
                        >
                          {paying === o.id ? '支付中…' : `去支付 ¥${Number(o.amount_yuan || 0).toFixed(2)}`}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>

          <Panel title="套餐列表">
            {plans.length === 0 ? (
              <div className="hint">暂无可购买套餐。</div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 12 }}>
                {plans.map((p) => (
                  <div key={p.id} style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
                    <div style={{ fontWeight: 600 }}>{p.name}</div>
                    <div style={{ fontSize: 22, fontWeight: 700, margin: '6px 0' }}>
                      ¥{Number(p.price_yuan ?? (p.price_cents / 100)).toFixed(2)}
                    </div>
                    <div className="hint">{p.period_label || p.duration_label || (p.duration_months ? p.duration_months + ' 个月' : '')} · {p.device_count} 台设备</div>
                    {p.model && <div className="hint" style={{ marginTop: 4 }}>{p.model}</div>}
                    {p.stock != null && (
                      <div className="hint" style={{ marginTop: 4, color: p.stock === 0 ? '#dc2626' : 'inherit' }}>
                        {p.stock === 0 ? '已售罄' : `剩余 ${p.stock} 台`}
                      </div>
                    )}
                    <button
                      className="btn-primary"
                      style={{ marginTop: 10, width: '100%' }}
                      disabled={buying === p.id || p.stock === 0 || p.stock < (p.min_qty || 1)}
                      onClick={() => {
                        setBuyTarget(p)
                        setBuyUnits(1)
                        setBuyQuote(null)
                      }}
                    >
                      {buying === p.id
                        ? '开通中…'
                        : p.stock === 0
                          ? '已售罄'
                          : p.stock < (p.min_qty || 1)
                            ? `库存不足（需 ≥${p.min_qty} 台）`
                            : '立即购买'}
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="hint" style={{ marginTop: 8 }}>支持在线购买；支付成功后自动分配云手机，并同步到订阅记录。</div>
          </Panel>

          <Panel title="订单列表">
            {myOrderList.length === 0 ? (
              <div className="hint">还没有订单记录，购买套餐后会出现在这里。</div>
            ) : (
              <>
              <div className="order-bulk">
                <span className="hint">已选 {orderSel.length} 条</span>
                {orderMsg && <span className="ok" style={{ fontSize: 12.5, padding: '2px 8px' }}>{orderMsg}</span>}
                <button
                  type="button"
                  className="ghost"
                  disabled={orderSel.length === 0 || batchBusy}
                  onClick={batchDeleteOrders}
                >
                  {batchBusy ? '删除中…' : '批量删除'}
                </button>
                <button
                  type="button"
                  className="ghost"
                  disabled={orderSel.length === 0}
                  onClick={() => setOrderSel([])}
                >清空选择</button>
              </div>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 34 }}>
                      <input
                        type="checkbox"
                        title="全选"
                        checked={myOrderList.length > 0 && orderSel.length === myOrderList.length}
                        onChange={(e) => setOrderSel(e.target.checked ? myOrderList.map((o) => o.id) : [])}
                      />
                    </th>
                    <th>订单号</th><th>套餐</th><th>数量</th><th>时长</th>
                    <th>金额</th><th>状态</th><th>下单时间</th><th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {myOrderList.map((o) => (
                    <tr key={o.id}>
                      <td>
                        <input
                          type="checkbox"
                          checked={orderSel.includes(o.id)}
                          onChange={() => toggleOrderSel(o.id)}
                        />
                      </td>
                      <td style={{ fontSize: 12 }}>{o.out_trade_no || `#${o.id}`}</td>
                      <td>{o.plan_name || '—'}</td>
                      <td>{o.qty ? `${o.qty} 台` : '—'}</td>
                      <td>{o.hours ? fmtDuration(o.hours) : '—'}</td>
                      <td style={{ fontWeight: 600 }}>¥{Number(o.amount_yuan ?? (o.amount_cents / 100)).toFixed(2)}</td>
                      <td>
                        <span className={`badge ${ORDER_BADGE[o.status]?.cls || ''}`}>
                          {ORDER_BADGE[o.status]?.label || o.status}
                        </span>
                      </td>
                      <td style={{ fontSize: 12 }}>{fmtDate(o.created_at)}</td>
                      <td>
                        <span style={{ display: 'inline-flex', gap: 6 }}>
                          {o.status === 'pending' && (
                            <button className="ghost" style={{ padding: '3px 8px', fontSize: 12 }}
                              disabled={paying === o.id}
                              onClick={() => payPending(o)}>
                              {paying === o.id ? '支付中…' : '去支付'}
                            </button>
                          )}
                          <button className="ghost" style={{ padding: '3px 8px', fontSize: 12, color: '#b91c1c' }}
                            disabled={deletingOrder === o.id}
                            onClick={() => deleteOrder(o)}>
                            {deletingOrder === o.id ? '删除中…' : '删除'}
                          </button>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </>
            )}
            <div className="hint" style={{ marginTop: 8 }}>
              订单 15 分钟内未支付会自动取消；已支付订单可在「订阅记录」里查看使用情况。
            </div>
          </Panel>



        </>
      )}

      {buyTarget && (
        <div className="modal-mask" onClick={() => { if (!buying) setBuyTarget(null) }}>
          <div className="modal-body" style={{ maxWidth: 400 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>确认购买</span>
              <button className="modal-close" onClick={() => setBuyTarget(null)} disabled={!!buying}>关闭</button>
            </div>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>{buyTarget.name}</div>
            <div className="hint" style={{ marginBottom: 12 }}>
              1 份 = {fmtDuration(buyTarget.period_hours || 720)} · {buyTarget.device_count || 1} 台 ·
              价格 ¥{Number(buyTarget.price_yuan ?? buyTarget.price_cents / 100).toFixed(2)} / 份
            </div>

            <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
              <label className="plan-field" style={{ flex: 1 }}>
                <span className="plan-field-label">购买份数</span>
                <input
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  value={buyUnits}
                  onChange={(e) => {
                    const v = Number(e.target.value) || 1
                    setBuyUnits(Math.max(1, Math.min(100, v)))
                  }}
                />
              </label>
            </div>

            <div style={{
              display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
              padding: '10px 12px', borderRadius: 10, background: 'var(--bg-subtle)', marginBottom: 12,
            }}>
              <span className="hint" style={{ margin: 0 }}>
                {buyUnits} 份 = {buyUnits * (buyTarget.device_count || 1)} 台 × {fmtDuration(buyTarget.period_hours || 720)}
                {buyTarget.stock ? ` · 库存 ${buyTarget.stock} 台` : ''}
              </span>
              <span style={{ fontSize: 22, fontWeight: 700, color: 'var(--accent)' }}>
                ¥{Number(buyQuote?.amount_cents ?? 0) / 100 > 0
                  ? (Number(buyQuote.amount_cents) / 100).toFixed(2)
                  : '—'}
              </span>
            </div>

            <div className="hint" style={{ marginBottom: 14 }}>模拟支付，确认后立即开通并分配云手机。</div>
            {buyMsg && (
              <div className={buyMsg.startsWith('购买失败') ? 'err' : 'ok'} style={{ marginBottom: 10 }}>
                {buyMsg}
              </div>
            )}
            <div className="modal-actions">
              <button className="ghost" onClick={() => setBuyTarget(null)} disabled={!!buying}>取消</button>
              <button className="btn-primary" onClick={purchase} disabled={!!buying}>
                {buying ? '开通中…' : '确认购买'}
              </button>
            </div>
          </div>
        </div>
      )}

      {renewTarget && (
        <div className="modal-mask" onClick={() => { if (!renewBusy) setRenewTarget(null) }}>
          <div className="modal-body" style={{ maxWidth: 420 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>续费</span>
              <button className="modal-close" onClick={() => setRenewTarget(null)} disabled={renewBusy}>关闭</button>
            </div>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>
              {renewTarget.plan_name || renewTarget.model || '—'}
            </div>
            <div className="hint" style={{ marginBottom: 12 }}>
              订阅 #{renewTarget.id} · {renewTarget.qty || 1} 台
              {renewTarget.expires_at ? ` · 当前 ${fmtDate(renewTarget.expires_at)} 到期` : ''}
            </div>

            <label className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">续费份数</span>
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={renewUnits}
                onChange={(e) => setRenewUnits(Math.max(1, Math.min(100, Number(e.target.value) || 1)))}
              />
            </label>

            <div style={{
              display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
              padding: '10px 12px', borderRadius: 10, background: 'var(--bg-subtle)', marginBottom: 12,
            }}>
              <span className="hint" style={{ margin: 0 }}>
                {renewUnits} 份 × {(renewTarget.plan_period_hours || 0) >= 24
                  ? `${Math.round((renewTarget.plan_period_hours || 0) / 24)} 天`
                  : `${renewTarget.plan_period_hours || 0} 小时`}
                {' · '}台数不变（{renewTarget.qty || 1} 台）
              </span>
              <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--accent)' }}>
                ¥{(((renewTarget.plan_price_cents || 0) * renewUnits) / 100).toFixed(2)}
              </span>
            </div>

            <div className="hint" style={{ marginBottom: 12 }}>
              续费只延长到期时间，不新增云手机；未过期的在原到期时间上叠加，已过期的从现在起算。
            </div>

            {renewMsg && (
              <div className={renewMsg.includes('失败') ? 'err' : 'ok'} style={{ marginBottom: 10 }}>
                {renewMsg}
              </div>
            )}

            <div className="modal-actions">
              <button className="ghost" onClick={() => setRenewTarget(null)} disabled={renewBusy}>取消</button>
              <button className="btn-primary" onClick={doRenew} disabled={renewBusy}>
                {renewBusy ? '支付中…' : '确认续费'}
              </button>
            </div>
          </div>
        </div>
      )}

      {refundTarget && (
        <div className="modal-mask" onClick={() => { if (!refunding) setRefundTarget(null) }}>
          <div className="modal-body" style={{ maxWidth: 440 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>申请退款</span>
              <button className="modal-close" onClick={() => setRefundTarget(null)} disabled={refunding}>关闭</button>
            </div>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>
              {refundTarget.plan_name || refundTarget.model || '—'}
            </div>
            <div className="hint" style={{ marginBottom: 10 }}>
              订阅 #{refundTarget.id}
              {refundTarget.expires_at ? ` · ${fmtDate(refundTarget.expires_at)} 到期` : ''}
            </div>
            <div className="hint" style={{ marginBottom: 12 }}>
              提交后由管理员审核，通过后该订阅作废并退款。付款后 5 分钟内可自助申请；
              超过 5 分钟且已创建/使用过云手机的，请联系客服处理。
            </div>
            <textarea
              value={refundReason}
              onChange={(e) => setRefundReason(e.target.value)}
              maxLength={200}
              placeholder="退款原因（选填，200 字以内）"
              style={{
                width: '100%', boxSizing: 'border-box', minHeight: 72, resize: 'vertical',
                padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 8,
                fontSize: 13, fontFamily: 'inherit', lineHeight: 1.5,
                background: 'var(--bg-elevated)', color: 'var(--text)',
              }}
            />
            {refundMsg && (
              <div className="err" style={{ marginTop: 10 }}>{refundMsg}</div>
            )}
            <div className="modal-actions">
              <button className="ghost" onClick={() => setRefundTarget(null)} disabled={refunding}>取消</button>
              <button className="btn-primary" onClick={submitRefund} disabled={refunding}>
                {refunding ? '提交中…' : '提交申请'}
              </button>
            </div>
          </div>
        </div>
      )}

      {historySub && (
        <div className="modal-mask" onClick={() => setHistorySub(null)}>
          <div className="modal-body" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>退款申请记录</span>
              <button className="modal-close" onClick={() => setHistorySub(null)}>关闭</button>
            </div>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 4 }}>
              {historySub.plan_name || historySub.model || '—'}
            </div>
            <div className="hint" style={{ marginBottom: 16 }}>订阅 #{historySub.id}</div>

            {historyLoading ? (
              <div className="hint">加载中…</div>
            ) : history.length === 0 ? (
              <div className="hint">这条订阅还没有退款申请记录。</div>
            ) : (
              <div>
                {history.map((r, i) => {
                  const tone = REFUND_TONE[r.status] || { color: '#475569', bg: '#f1f5f9' }
                  const last = i === history.length - 1
                  return (
                    <div key={r.id} style={{ display: 'flex', gap: 12 }}>
                      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: 10, flex: 'none' }}>
                        <span style={{ width: 9, height: 9, borderRadius: '50%', background: tone.color, marginTop: 7, flex: 'none' }} />
                        {!last && <span style={{ flex: 1, width: 2, background: 'var(--border)', marginTop: 4 }} />}
                      </div>
                      <div style={{ flex: 1, minWidth: 0, paddingBottom: last ? 0 : 16 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                          <RefundBadge status={r.status} refund={r} />
                          <span className="hint">申请 {fmtDate(r.created_at)}</span>
                          <span className="hint">¥{Number(r.amount_yuan || 0).toFixed(2)}</span>
                        </div>
                        {r.reason && (
                          <div style={{ fontSize: 12.5, marginTop: 5, lineHeight: 1.5 }}>原因：{r.reason}</div>
                        )}
                        {r.handled_at && (
                          <div className="hint" style={{ marginTop: 4, lineHeight: 1.5 }}>
                            {r.status === 'approved' ? '通过' : r.status === 'rejected' ? '驳回' : '处理'}于 {fmtDate(r.handled_at)}
                            {r.admin_note ? ` · 意见：${r.admin_note}` : ''}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
            <div className="modal-actions" style={{ marginTop: 16 }}>
              <button className="btn-primary" onClick={() => setHistorySub(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/* ---------- KPI 卡片 ---------- */
const KPI_TONES = {
  teal: { bg: '#ccfbf1', fg: '#0f766e' },
  green: { bg: '#dcfce7', fg: '#15803d' },
  red: { bg: '#fee2e2', fg: '#b91c1c' },
  blue: { bg: 'var(--surface-sunken)', fg: 'var(--ink-2)' },
  amber: { bg: '#fef3c7', fg: '#b45309' },
  violet: { bg: 'var(--surface-sunken)', fg: 'var(--ink-3)' },
}

function Kpi({ label, value, sub, icon, tone = 'teal', title }) {
  // 状态只用一个 6px 圆点表达，不用图标色块、不用整块底色
  // 只在"需要引起注意"时给一个状态点（红/橙），正常状态不加颜色
  const toneColor = { red: 'var(--negative)', amber: 'var(--warn)' }[tone]
  return (
    <div className="kpi" title={title || undefined}>
      <div className="kpi-label">
        {toneColor && <span className="kpi-dot" style={{ background: toneColor }} aria-hidden="true" />}
        {label}
      </div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

/* ---------- 套餐销售面板（r5 销售卡片补丁） ---------- */
function fmtYuan(n) {
  return (n == null ? 0 : n).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 2 })
}

function SalesPanel({ data }) {
  if (!data) {
    return (
      <Panel title="套餐销售 · 最近 7 天">
        <div className="hint">数据加载中…</div>
      </Panel>
    )
  }
  const days = data.days || []
  const mx = Math.max(1, ...days.map((d) => d.amount_yuan || 0))
  const today = data.today ? data.today.date : ''
  const plans = data.today_plans || []
  const totO = days.reduce((a, d) => a + (d.orders || 0), 0)
  const totA = days.reduce((a, d) => a + (d.amount_yuan || 0), 0)
  return (
    <Panel title="套餐销售 · 最近 7 天（金额 / 单数）">
      <div>
        <div
          className="sales-bars"
          style={{ display: 'flex', alignItems: 'flex-end', gap: 8, height: 128, padding: '2px 2px 0', boxSizing: 'border-box' }}
        >
          {days.map((d) => {
            const hpx = Math.max(3, Math.round(((d.amount_yuan || 0) / mx) * 84))
            return (
              <div
                key={d.date}
                className="sales-col"
                style={{ flex: '1 1 0', minWidth: 0, height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'flex-end', gap: 3 }}
              >
                <div className="sales-amt" style={{ fontSize: 11, color: '#64748b', whiteSpace: 'nowrap' }}>
                  {d.amount_yuan > 0 ? `¥${fmtYuan(d.amount_yuan)}` : '—'}
                </div>
                <div
                  className="sales-bar"
                  title={`${d.date}：${d.orders} 单 / ¥${fmtYuan(d.amount_yuan)}`}
                  style={{ width: '56%', minWidth: 14, borderRadius: '2px 2px 0 0', background: d.date === today ? 'var(--accent)' : '#cfd9d7', height: `${hpx}px` }}
                />
                <div className="sales-day" style={{ fontSize: 11, color: '#64748b' }}>{d.date.slice(5)}</div>
                <div className="sales-orders" style={{ fontSize: 10, color: '#94a3b8' }}>{d.orders} 单</div>
              </div>
            )
          })}
        </div>
        <div className="hint sales-total" style={{ marginTop: 8 }}>
          近 7 天合计 {totO} 单 · ¥{fmtYuan(totA)}　本月合计 {data.month ? data.month.orders : 0} 单 · ¥{fmtYuan(data.month ? data.month.amount_yuan : 0)}
        </div>
        {plans.length > 0 && (
          <div className="sales-plans" style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
            <span className="hint">今日分套餐：</span>
            {plans.map((p) => (
              <span key={p.plan_id} className="chip">{p.plan_name} {p.orders} 单 · ¥{fmtYuan(p.amount_yuan)}</span>
            ))}
          </div>
        )}
      </div>
    </Panel>
  )
}

/* ---------- 面板容器 ---------- */
function Panel({ title, children }) {
  return (
    <div className="panel">
      <h3>{title}</h3>
      {children}
    </div>
  )
}

/* ---------- 趋势图（纯 SVG，设备在线数） ---------- */
function TrendPanel({ data, hours, onHours }) {
  const chart = () => {
    const pts = data.map((d) => d.devices_online).filter((v) => v != null)
    const W = 560, H = 140, pad = 8
    const maxv = Math.max(1, ...pts.map((v) => Math.max(0, v))) * 1.15
    const x = (i) => pad + (data.length <= 1 ? 0 : (i / (data.length - 1)) * (W - pad * 2))
    const y = (v) => H - pad - (Math.max(0, Math.min(maxv, v)) / maxv) * (H - pad * 2)
    const line = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(d.devices_online ?? 0).toFixed(1)}`).join(' ')
    const last = pts[pts.length - 1]
    return (
      <div className="trend-chart">
        <div className="trend-head">
          <span className="trend-title">在线设备数</span>
          <span className="trend-last" style={{ color: '#0ea5e9' }}>{last != null ? `${Math.round(last)} 台` : '-'}</span>
        </div>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="trend-svg">
          <line x1={pad} y1={H - pad} x2={W - pad} y2={H - pad} stroke="var(--rule)" strokeWidth="1" />
          <line x1={pad} y1={pad} x2={pad} y2={H - pad} stroke="var(--rule)" strokeWidth="1" />
          <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
    )
  }

  return (
    <Panel title={`历史趋势 · 最近 ${hours} 小时（设备在线数）`}>
      {data.length === 0 ? (
        <div className="hint">数据采集中…（每 60 秒一个采样点，稍后刷新）</div>
      ) : (
        <>
          <div className="trend-ops">
            {[24, 72, 168].map((h) => (
              <button key={h} className={`chip ${hours === h ? 'active' : ''}`} onClick={() => onHours(h)}>
                {h === 24 ? '24 小时' : h === 72 ? '3 天' : '7 天'}
              </button>
            ))}
          </div>
          <div className="trend-grid">{chart()}</div>
        </>
      )}
    </Panel>
  )
}
