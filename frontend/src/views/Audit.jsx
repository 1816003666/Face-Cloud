import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { fmtMinute } from '../lib/fmt.js'

export default function Audit() {
  const [items, setItems] = useState([])
  const [stats, setStats] = useState([])
  const [q, setQ] = useState({ action: '', actor_id: '', limit: 200 })
  const [err, setErr] = useState('')

  async function load() {
    try {
      const [list, st] = await Promise.all([
        api.listAudit(q),
        api.auditStats(),
      ])
      setItems(list.data)
      setStats(st.data)
    } catch (e) {
      setErr(e.response?.data?.error || '加载失败')
    }
  }
  useEffect(() => { load() }, [])

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>审计日志</h2>
          <span className="perm-badge">管理员</span>
          <button className="ghost" onClick={() => api.downloadCsv("audit")}>导出 CSV</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}

      <h3>高频操作 Top</h3>
      <div className="dev-pick">
        {stats.map((s) => (
          <span key={s.action} className="chip" onClick={() => { setQ({ ...q, action: s.action }); load() }}
                style={{ cursor: 'pointer' }}>
            {s.action} ×{s.count}
          </span>
        ))}
      </div>

      <h3>过滤</h3>
      <div className="task-form">
        <input placeholder="操作类型 action" value={q.action} onChange={(e) => setQ({ ...q, action: e.target.value })} />
        <input placeholder="操作者 ID" value={q.actor_id} onChange={(e) => setQ({ ...q, actor_id: e.target.value })} />
        <button onClick={load}>查询</button>
        <button onClick={() => { setQ({ action: '', actor_id: '', limit: 200 }); load() }}>重置</button>
      </div>

      <table>
        <thead>
          <tr><th>ID</th><th>操作者</th><th>动作</th><th>对象</th><th>详情</th><th>时间</th></tr>
        </thead>
        <tbody>
          {items.map((l) => (
            <tr key={l.id}>
              <td>{l.id}</td>
              <td>{l.actor_id}</td>
              <td>{l.action}</td>
              <td>{l.target_type}{l.target_id ? `#${l.target_id}` : ''}</td>
              <td style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis' }}>{l.detail}</td>
              <td className="mono" style={{ whiteSpace: 'nowrap' }}>{fmtMinute(l.created_at)}</td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr><td colSpan="6">暂无审计记录</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
