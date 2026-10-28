import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import './BillingAdmin.css'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

/**
 * 套餐管理（管理员）：套餐 CRUD / 平台订单 / 门户订单 / 订阅 / 退款。
 */
const EMPTY_PLAN = {
  name: '',
  model: '标准版',
  price_yuan: '',
  period_hours: 720,
  duration_months: 1,
  device_count: 1,
  sort: 0,
  active: true,
  server_ips: [],
  sku_id: '',
  resolution: '',
  cpu: '',
  memory: '',
  storage: '',
  android: '',
}

const SPEC_OPTIONS = {
  android: ['Android 9', 'Android 10', 'Android 11', 'Android 12', 'Android 13', 'Android 14'],
  resolution: ['720×1280', '1080×1920', '1080×2340', '1440×2560'],
  cpu: ['2核', '4核', '8核'],
  memory: ['2GB', '4GB', '6GB', '8GB', '12GB', '16GB'],
  storage: ['16GB', '32GB', '64GB', '128GB', '256GB'],
}

const STATUS_TEXT = {
  paid: '已支付',
  pending: '待支付',
  cancelled: '已取消',
  expired: '已过期',
  refunded: '已退款',
  success: '已开通',
  failed: '失败',
}

function statusClass(status) {
  if (['paid', 'success'].includes(status)) return 'ok'
  if (['failed', 'cancelled', 'expired', 'refunded'].includes(status)) return 'fail'
  return ''
}

export default function BillingAdmin() {
  const [plans, setPlans] = useState([])
  const [orders, setOrders] = useState([])
  const [portalOrders, setPortalOrders] = useState([])
  const [subs, setSubs] = useState([])
  const [refunds, setRefunds] = useState([])
  const [stats, setStats] = useState(null)
  const [serverIps, setServerIps] = useState([])
  const [form, setForm] = useState(EMPTY_PLAN)
  const [editingId, setEditingId] = useState(null)
  const [showForm, setShowForm] = useState(false)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('plans')
  const [orderSource, setOrderSource] = useState('all')

  async function load() {
    try {
      const [p, o, po, s, st, rf, ips] = await Promise.all([
        api.listPlans(),
        api.adminOrders(),
        api.adminPortalOrders(),
        api.adminSubscriptions(),
        api.billingStats(),
        api.adminRefunds(),
        api.listServerIps().catch(() => ({ data: [] })),
      ])
      setPlans(p.data || [])
      setOrders(o.data || [])
      setPortalOrders(po.data || [])
      setSubs((s.data || []).filter((x) => x.active))
      setStats(st.data || null)
      setRefunds(rf.data || [])
      setServerIps(Array.isArray(ips.data) ? ips.data : [])
    } catch (e) {
      setErr(e.response?.data?.error || '加载失败')
    }
  }

  useEffect(() => { load() }, [])

  function startEdit(p) {
    setEditingId(p.id)
    setShowForm(true)
    const sp = p.specs || {}
    setForm({
      name: p.name,
      model: p.model,
      price_yuan: p.price_yuan,
      period_hours: p.period_hours || (p.duration_months || 1) * 720,
      server_ips: p.server_ips || [],
      sku_id: p.sku_id || '',
      duration_months: p.duration_months,
      device_count: p.device_count || 1,
      sort: p.sort,
      active: p.active,
      resolution: sp.resolution || '',
      cpu: sp.cpu || '',
      memory: sp.memory || '',
      storage: sp.storage || '',
      android: sp.android || '',
    })
    setErr('')
    setMsg('')
  }

  function resetForm() {
    setEditingId(null)
    setForm(EMPTY_PLAN)
    setErr('')
    setMsg('')
  }

  function closeForm() {
    setShowForm(false)
    resetForm()
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setErr('')
    setMsg('')
    const price_yuan = Number(form.price_yuan)
    if (!form.name.trim()) { setErr('套餐名必填'); return }
    if (!(price_yuan > 0)) { setErr('价格必须大于 0（元）'); return }
    const payload = {
      name: form.name.trim(),
      model: form.model.trim() || '标准版',
      price_cents: Math.round(price_yuan * 100),
      period_hours: Math.max(1, Number(form.period_hours) || 720),
      server_ips: form.server_ips || [],
      sku_id: (form.sku_id || '').trim(),
      duration_months: Number(form.duration_months),
      device_count: Math.max(1, Number(form.device_count) || 1),
      sort: Number(form.sort) || 0,
      active: form.active !== false,
      specs: {
        resolution: (form.resolution || '').trim(),
        cpu: (form.cpu || '').trim(),
        memory: (form.memory || '').trim(),
        storage: (form.storage || '').trim(),
        android: (form.android || '').trim(),
        device_count: Math.max(1, Number(form.device_count) || 1),
      },
    }
    try {
      if (editingId) await api.updatePlan(editingId, payload)
      else await api.createPlan(payload)
      setMsg(editingId ? '套餐已更新' : '套餐已创建')
      closeForm()
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    }
  }

  async function handleToggle(p) {
    await api.togglePlan(p.id)
    load()
  }

  async function handleDelete(p) {
    if (!await uiConfirm(`确认删除套餐「${p.name}」？\n如有历史订单/订阅，会保留为「停用」状态（可重新启用）。`)) return
    try {
      const r = await api.deletePlan(p.id)
      setMsg(
        r.data?.deactivated
          ? `「${p.name}」有历史订单/订阅，已停用并保留在列表（可重新启用）`
          : r.data?.message || '已删除'
      )
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  const fmtTime = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '-')
  const serverLabel = (ips) => {
    const arr = Array.isArray(ips) ? ips.filter(Boolean) : []
    if (!arr.length) return '默认'
    if (serverIps.length > 0 && arr.length >= serverIps.length) return '全部'
    return arr.length > 3 ? `${arr.length} 台服务器` : arr.join('、')
  }

  async function refundOrder(o) {
    const ok = await uiConfirm(
      `确认给「${o.user}」的订单 ${o.no} 退单？\n\n` +
      `金额：¥${o.amount}\n套餐：${o.plan}\n\n` +
      '退单后会立即可作废对应订阅，并收回该订阅下的云手机（回到公共池）。'
    )
    if (!ok) return
    setBusy(true)
    setMsg('')
    try {
      const r = await api.adminRefundOrder(o.id)
      const d = r.data || {}
      setMsg(`已退款 ¥${d.amount_yuan ?? o.amount}，收回云手机 ${d.released ?? 0} 台`)
      await load()
    } catch (e) {
      setMsg(`退单失败：${e.response?.data?.error || e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const mergedOrders = [
    ...orders.map((o) => ({
      key: `w${o.id}`,
      source: 'web',
      id: o.id,
      no: o.out_trade_no,
      user: o.username || `#${o.user_id}`,
      enterprise: o.enterprise_name || '',
      plan: o.plan_name,
      amount: o.amount_yuan,
      period: '-',
      status: o.status,
      devices: '-',
      time: o.created_at,
    })),
    ...portalOrders.map((p) => ({
      key: `p${p.id}`,
      source: 'portal',
      no: p.order_item_id,
      user: `#${p.local_user_id || '-'}${p.username ? ` ${p.username}` : ''}`,
      plan: `${p.plan_name || p.sku_id}${p.amount_yuan != null ? ` ¥${p.amount_yuan}` : ''}`,
      amount: p.amount_yuan,
      period: p.period_hours ? `${p.period_hours} 小时` : '-',
      status: p.status,
      devices: (p.device_ids || []).join(', ') || '-',
      time: p.created_at,
    })),
  ]
    .filter((o) => orderSource === 'all' || o.source === orderSource)
    .sort((a, b) => String(b.time || '').localeCompare(String(a.time || '')))

  const specField = (key, label, placeholder, options) => (
    <div className="plan-field" key={key}>
      <span className="plan-field-label">{label}</span>
      <div style={{ display: 'flex', gap: 6 }}>
        <input
          style={{ flex: 1 }}
          placeholder={placeholder}
          value={form[key] || ''}
          onChange={(e) => setForm({ ...form, [key]: e.target.value })}
        />
        <select
          value=""
          style={{ width: 92 }}
          onChange={(e) => { if (e.target.value) setForm({ ...form, [key]: e.target.value }) }}
        >
          <option value="">常用</option>
          {options.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </div>
    </div>
  )

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>套餐管理</h2>
          <span className="perm-badge">管理员</span>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      {stats && (
        <div className="hc-stats" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
          {[
            { label: '套餐数', value: plans.length },
            { label: '总订单', value: stats.order_total },
            { label: '已支付', value: stats.paid_total },
            { label: '累计收入', value: `¥${stats.revenue_yuan}` },
            { label: '有效订阅', value: stats.active_subs },
            { label: '待支付', value: stats.pending_orders },
          ].map((s) => (
            <div key={s.label} className="hc-stat" style={{ border: '1px solid var(--border-light)', borderRadius: 10, padding: '10px 16px', minWidth: 110 }}>
              <div style={{ fontSize: 20, fontWeight: 700 }}>{s.value}</div>
              <div className="muted" style={{ fontSize: 12 }}>{s.label}</div>
            </div>
          ))}
        </div>
      )}

      <div className="tabs">
        {[['plans', '套餐'], ['orders', '订单'], ['subs', '订阅'], ['refunds', '退款单']].map(([k, label]) => (
          <button key={k} className={tab === k ? 'btn-primary' : ''} onClick={() => setTab(k)}>{label}</button>
        ))}
      </div>

      {tab === 'refunds' && (
        <div style={{ marginTop: 8 }}>
          <table>
            <thead>
              <tr><th>ID</th><th>用户</th><th>套餐</th><th>金额</th><th>来源订单</th><th>原因</th><th>状态</th><th>申请时间</th><th>操作</th></tr>
            </thead>
            <tbody>
              {(refunds || []).map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td>{r.username || `#${r.user_id}`}</td>
                  <td>{r.plan_name}</td>
                  <td>¥{r.amount_yuan}</td>
                  <td>{r.order_id || '-'}</td>
                  <td style={{ fontSize: 12, color: '#666' }}>{r.reason || '-'}</td>
                  <td>{r.status === 'pending' ? '待处理' : (r.status === 'approved' ? '已同意' : '已驳回')}</td>
                  <td style={{ fontSize: 12 }}>{fmtTime(r.created_at)}</td>
                  <td>
                    {r.status === 'pending' ? (
                      <>
                        <button onClick={async () => { await api.adminRefundAction(r.id, 'approve'); setMsg('已同意退款（订阅已作废）'); load() }}>同意</button>
                        <button className="danger" onClick={async () => { await api.adminRefundAction(r.id, 'reject'); setMsg('已驳回'); load() }}>驳回</button>
                      </>
                    ) : '-'}
                  </td>
                </tr>
              ))}
              {(!refunds || refunds.length === 0) && <tr><td colSpan={9} style={{ textAlign: 'center', color: '#888' }}>暂无退款单</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'plans' && (
        <>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
            <button
              className="btn-primary"
              onClick={() => { resetForm(); setShowForm(true); }}
            >
              + 新建套餐
            </button>
          </div>

          {showForm && (
            <div className="modal-mask" onClick={closeForm}>
              <div
                className="modal-body"
                style={{ maxWidth: 860, maxHeight: '90vh', overflowY: 'auto' }}
                onClick={(e) => e.stopPropagation()}
              >
                <div className="modal-title">
                  <span>{editingId ? '编辑套餐' : '新建套餐'}</span>
                  <button type="button" className="modal-close" onClick={closeForm}>✕</button>
                </div>

                <form onSubmit={handleSubmit} className="plan-editor plan-editor-modal">
                  <div className="plan-editor-card">
                    <div className="plan-editor-hint" style={{ marginBottom: 12 }}>
                      带 <i>*</i> 为必填项
                    </div>

                    <div className="plan-editor-group">
                      <div className="plan-group-label">基础信息</div>
                      <div className="plan-fields">
                        <label className="plan-field">
                          <span className="plan-field-label"><i>*</i>套餐名称</span>
                          <input placeholder="如：标准包季套餐" value={form.name}
                            onChange={(e) => setForm({ ...form, name: e.target.value })} />
                        </label>
                        <label className="plan-field">
                          <span className="plan-field-label">门户商品号（sku_id）</span>
                          <input placeholder="如：phone-std-90d" value={form.sku_id || ''}
                            onChange={(e) => setForm({ ...form, sku_id: e.target.value })} />
                          <span className="plan-field-tip">留空自动生成 phone-&lt;套餐ID&gt;；也可手动指定</span>
                        </label>
                        <label className="plan-field">
                          <span className="plan-field-label"><i>*</i>机型档位</span>
                          <input placeholder="如：标准版 / 旗舰版" value={form.model}
                            onChange={(e) => setForm({ ...form, model: e.target.value })} />
                        </label>
                        <label className="plan-field plan-field-sort">
                          <span className="plan-field-label">排序权重</span>
                          <input type="number" value={form.sort}
                            onChange={(e) => setForm({ ...form, sort: e.target.value })} />
                          <span className="plan-field-tip">数字越大越靠前</span>
                        </label>
                      </div>
                    </div>

                    <div className="plan-editor-group">
                      <div className="plan-group-label">计费信息</div>
                      <div className="plan-fields">
                        <label className="plan-field">
                          <span className="plan-field-label"><i>*</i>价格（元）</span>
                          <input type="number" step="0.01" min="0.01" placeholder="如：99" value={form.price_yuan}
                            onChange={(e) => setForm({ ...form, price_yuan: e.target.value })} />
                        </label>
                        <label className="plan-field">
                          <span className="plan-field-label"><i>*</i>套餐周期（小时）</span>
                          <input type="number" min="1" step="1" placeholder="如：720" value={form.period_hours}
                            onChange={(e) => setForm({ ...form, period_hours: e.target.value })} />
                          <span className="plan-field-tip">1 天 = 24 小时；720 = 30 天，2160 = 90 天</span>
                        </label>
                        <label className="plan-field">
                          <span className="plan-field-label"><i>*</i>包含台数</span>
                          <input type="number" min="1" step="1" placeholder="如：10" value={form.device_count}
                            onChange={(e) => setForm({ ...form, device_count: e.target.value })} />
                          <span className="plan-field-tip">套餐包含的云手机台数</span>
                        </label>
                      </div>
                    </div>

                    <div className="plan-editor-group">
                      <div className="plan-group-label">分配服务器</div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                        {serverIps.map((ip) => (
                          <label className="chip" style={{ display: 'flex', alignItems: 'center', gap: 6 }} key={ip}>
                            <input
                              type="checkbox"
                              checked={(form.server_ips || []).includes(ip)}
                              onChange={(e) => {
                                const set = new Set(form.server_ips || [])
                                if (e.target.checked) set.add(ip)
                                else set.delete(ip)
                                setForm({ ...form, server_ips: [...set] })
                              }}
                            />
                            {ip}
                          </label>
                        ))}
                        {serverIps.length === 0 && <span className="hint">暂无可用服务器</span>}
                      </div>
                      <div className="hint" style={{ marginTop: 6 }}>不选 = 默认池；可多选</div>
                    </div>

                    <div className="plan-editor-group">
                      <div className="plan-group-label">云手机规格</div>
                      <div className="plan-fields">
                        {specField('android', 'Android 版本', '如：Android 11', SPEC_OPTIONS.android)}
                        {specField('resolution', '屏幕分辨率', '如：720×1280', SPEC_OPTIONS.resolution)}
                        {specField('cpu', 'CPU 核数', '如：4核', SPEC_OPTIONS.cpu)}
                        {specField('memory', '运行内存', '如：4GB', SPEC_OPTIONS.memory)}
                        {specField('storage', '存储空间', '如：32GB', SPEC_OPTIONS.storage)}
                      </div>
                    </div>

                    <div className="plan-editor-footer">
                      <label className="plan-active-check">
                        <input type="checkbox" checked={form.active !== false}
                          onChange={(e) => setForm({ ...form, active: e.target.checked })} />
                        创建后立即启用（用户可在购买页看到该套餐）
                      </label>
                      <div className="plan-footer-actions">
                        <button type="button" className="btn-ghost" onClick={closeForm}>取消</button>
                        <button type="submit" className="btn-primary">{editingId ? '保存修改' : '新建套餐'}</button>
                      </div>
                    </div>
                  </div>
                </form>
              </div>
            </div>
          )}

          <div style={{ overflowX: 'auto' }}>
            <table style={{ whiteSpace: 'nowrap' }}>
              <thead>
                <tr>
                  <th>ID</th><th>套餐名</th><th>机型</th><th>规格</th><th>价格</th>
                  <th>周期</th><th>服务器</th><th>台数</th><th>排序</th><th>状态</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {plans.map((p) => (
                  <tr key={p.id} style={p.active ? undefined : { opacity: 0.5 }}>
                    <td>{p.id}</td>
                    <td>
                      <div>{p.name}</div>
                      <div style={{ fontSize: 11, color: p.sku_id ? '#0f766e' : '#94a3b8' }}>
                        {p.sku_id ? `SKU: ${p.sku_id}` : '未上架（无 SKU）'}
                      </div>
                    </td>
                    <td>{p.model}</td>
                    <td style={{ fontSize: 12, color: '#555' }}>
                      {[(p.specs || {}).android, (p.specs || {}).resolution, (p.specs || {}).cpu,
                        (p.specs || {}).memory, (p.specs || {}).storage].filter(Boolean).join(' · ') || '-'}
                    </td>
                    <td>¥{p.price_yuan}</td>
                    <td>{p.period_label || p.duration_label}</td>
                    <td
                      style={{ fontSize: 12, whiteSpace: 'nowrap', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}
                      title={(p.server_ips || []).join(', ') || '默认（全部服务器）'}
                    >
                      {serverLabel(p.server_ips)}
                    </td>
                    <td>{p.device_count || 1} 台</td>
                    <td>{p.sort}</td>
                    <td>{p.active
                      ? <span className="hc-badge ok" style={{ border: 'none' }}>启用</span>
                      : <span className="hc-badge fail" style={{ border: 'none' }}>停用</span>}</td>
                    <td>
                      <button onClick={() => startEdit(p)}>编辑</button>
                      <button onClick={() => handleToggle(p)}>{p.active ? '停用' : '启用'}</button>
                      <button className="danger" onClick={() => handleDelete(p)}>删除</button>
                    </td>
                  </tr>
                ))}
                {plans.length === 0 && <tr><td colSpan={11} className="muted" style={{ textAlign: 'center' }}>暂无套餐</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === 'orders' && (
        <>
          <div style={{ display: 'flex', gap: 8, marginBottom: 10, alignItems: 'center' }}>
            {[['all', '全部'], ['web', '云手机平台'], ['portal', '门户']].map(([k, label]) => (
              <button key={k} className={orderSource === k ? 'btn-primary' : ''} onClick={() => setOrderSource(k)}>{label}</button>
            ))}
            <span className="muted" style={{ marginLeft: 'auto' }}>共 {mergedOrders.length} 单</span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ whiteSpace: 'nowrap' }}>
              <thead>
                <tr><th>来源</th><th>订单号</th><th>用户</th><th>套餐</th><th>金额</th><th>周期</th><th>状态</th><th>设备</th><th>时间</th><th style={{ textAlign: 'right' }}>操作</th></tr>
              </thead>
              <tbody>
                {mergedOrders.map((o) => (
                  <tr key={o.key}>
                    <td><span className={`hc-badge ${o.source === 'portal' ? '' : 'ok'}`} style={{ border: 'none' }}>
                      {o.source === 'portal' ? '门户' : '云手机平台'}
                    </span></td>
                    <td style={{ fontFamily: 'monospace', fontSize: 12 }}>{o.no}</td>
                    <td>{o.user}</td>
                    <td>{o.plan}</td>
                    <td>{o.amount != null ? `¥${o.amount}` : '-'}</td>
                    <td>{o.period}</td>
                    <td><span className={`hc-badge ${statusClass(o.status)}`} style={{ border: 'none' }}>{STATUS_TEXT[o.status] || o.status}</span></td>
                    <td style={{ fontSize: 12 }}>{o.devices}</td>
                    <td style={{ fontSize: 12 }}>{fmtTime(o.time)}</td>
                    <td style={{ textAlign: 'right' }}>
                      {o.source === 'web' && o.status === 'paid' ? (
                        <button className="danger" onClick={() => refundOrder(o)} disabled={busy}>
                          退单
                        </button>
                      ) : (
                        <span className="hint">—</span>
                      )}
                    </td>
                  </tr>
                ))}
                {mergedOrders.length === 0 && <tr><td colSpan={10} className="muted" style={{ textAlign: 'center' }}>暂无订单</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === 'subs' && (
        <table>
          <thead>
            <tr><th>ID</th><th>用户</th><th>套餐</th><th>云手机<br /><span className="hint" style={{ fontWeight: 400 }}>已用 / 共</span></th><th>状态</th><th>生效</th><th>到期</th><th>剩余</th></tr>
          </thead>
          <tbody>
            {subs.map((s) => (
              <tr key={s.id}>
                <td>{s.id}</td>
                <td>{s.username || `#${s.user_id}`}</td>
                <td>{s.plan_name}（{s.model}）</td>
                <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                  {s.used_devices ?? 0} / {s.qty ?? 1} 台
                </td>
                <td>
                  {s.active
                    ? <span className="hc-badge ok" style={{ border: 'none' }}>有效</span>
                    : <span className={`hc-badge ${s.status === 'expired' ? 'fail' : ''}`} style={{ border: 'none' }}>{s.status}</span>}
                </td>
                <td style={{ fontSize: 12 }}>{fmtTime(s.started_at)}</td>
                <td style={{ fontSize: 12 }}>{fmtTime(s.expires_at)}</td>
                <td>{s.active
                  ? (s.remain_seconds != null
                    ? (s.remain_seconds >= 86400
                      ? `${Math.floor(s.remain_seconds / 86400)} 天 ${Math.floor((s.remain_seconds % 86400) / 3600)} 小时`
                      : `${Math.floor(s.remain_seconds / 3600)} 小时 ${Math.floor((s.remain_seconds % 3600) / 60)} 分`)
                    : `${s.remain_days} 天`)
                  : '-'}</td>
              </tr>
            ))}
            {subs.length === 0 && <tr><td colSpan={8} className="muted" style={{ textAlign: 'center' }}>暂无订阅</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}
