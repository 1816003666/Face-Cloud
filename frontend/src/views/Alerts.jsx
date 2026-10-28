import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { fmtMinute } from '../lib/fmt.js'

const TYPE_LABEL = {
  device_offline: '设备离线',
  resource_limit: '资源超限',
  operation_failure: '操作失败',
}
const LEVEL_LABEL = { info: '提示', warning: '警告', critical: '严重' }
// 后端返回的是 ISO（含微秒与 T），列表里只显示到分钟
function fmtTime(v) {
  return fmtMinute(v)
}

const STATUS_LABEL = { active: '未处理', acknowledged: '已确认', resolved: '已解决' }
const LEVEL_CLASS = { info: 'lv-info', warning: 'lv-warn', critical: 'lv-crit' }
const STATUS_CLASS = { active: 'st-active', acknowledged: 'st-ack', resolved: 'st-ok' }

export default function Alerts() {
  const [items, setItems] = useState([])
  const [summary, setSummary] = useState({ total: 0, active: 0, critical: 0, warning: 0, by_type: {} })
  const [q, setQ] = useState({ status: '', level: '', type: '', limit: 200 })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const [list, sum] = await Promise.all([
        api.listAlerts(q),
        api.alertSummary(),
      ])
      setItems(list.data)
      setSummary(sum.data)
    } catch (e) {
      setErr(e.response?.data?.error || '加载失败')
    }
  }
  useEffect(() => { load() }, [])

  async function ack(id) {
    setBusy(true)
    try { await api.ackAlert(id); await load() } catch (e) { setErr(e.response?.data?.error || '操作失败') } finally { setBusy(false) }
  }
  async function resolve(id) {
    setBusy(true)
    try { await api.resolveAlert(id); await load() } catch (e) { setErr(e.response?.data?.error || '操作失败') } finally { setBusy(false) }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>告警中心</h2>
          <span className="perm-badge">管理员</span>
          <button className="ghost" onClick={() => api.downloadCsv("alerts")}>导出 CSV</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}

      <div className="alert-cards">
        <div className="acard"><div className="alabel">未解决</div><div className="anum">{summary.active}</div></div>
        <div className="acard crit"><div className="alabel">严重</div><div className="anum">{summary.critical}</div></div>
        <div className="acard warn"><div className="alabel">警告</div><div className="anum">{summary.warning}</div></div>
        <div className="acard"><div className="alabel">总计</div><div className="anum">{summary.total}</div></div>
      </div>

      <h3>按类型分布</h3>
      <div className="dev-pick">
        {Object.entries(summary.by_type || {}).map(([t, c]) => (
          <span key={t} className="chip">{TYPE_LABEL[t] || t} ×{c}</span>
        ))}
        {Object.keys(summary.by_type || {}).length === 0 && <span className="muted">暂无</span>}
      </div>

      <h3>筛选</h3>
      <div className="task-form">
        <select value={q.status} onChange={(e) => setQ({ ...q, status: e.target.value })}>
          <option value="">全部状态</option>
          <option value="active">未处理</option>
          <option value="acknowledged">已确认</option>
          <option value="resolved">已解决</option>
        </select>
        <select value={q.level} onChange={(e) => setQ({ ...q, level: e.target.value })}>
          <option value="">全部级别</option>
          <option value="info">提示</option>
          <option value="warning">警告</option>
          <option value="critical">严重</option>
        </select>
        <select value={q.type} onChange={(e) => setQ({ ...q, type: e.target.value })}>
          <option value="">全部类型</option>
          <option value="device_offline">设备离线</option>
          <option value="resource_limit">资源超限</option>
          <option value="operation_failure">操作失败</option>
        </select>
        <button onClick={load} disabled={busy}>查询</button>
        <button onClick={() => { setQ({ status: '', level: '', type: '', limit: 200 }); load() }} disabled={busy}>重置</button>
      </div>

      <table>
        <thead>
          <tr><th>ID</th><th>级别</th><th>类型</th><th>设备</th><th>状态</th><th>信息</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          {items.map((a) => (
            <tr key={a.id}>
              <td>{a.id}</td>
              <td><span className={`badge ${LEVEL_CLASS[a.level]}`}>{LEVEL_LABEL[a.level]}</span></td>
              <td>{TYPE_LABEL[a.type] || a.type}</td>
              <td>{a.device_id ? `#${a.device_id}` : '-'}</td>
              <td><span className={`badge ${STATUS_CLASS[a.status]}`}>{STATUS_LABEL[a.status]}</span></td>
              <td style={{ maxWidth: 360 }}>{a.message}</td>
              <td className="mono" style={{ whiteSpace: 'nowrap' }}>{fmtTime(a.created_at)}</td>
              <td>
                {a.status !== 'acknowledged' && a.status !== 'resolved' && (
                  <button onClick={() => ack(a.id)} disabled={busy}>确认</button>
                )}
                {a.status !== 'resolved' && (
                  <button onClick={() => resolve(a.id)} disabled={busy}>解决</button>
                )}
                {a.status === 'resolved' && <span className="muted">已解决</span>}
              </td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr><td colSpan="8">暂无告警</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
