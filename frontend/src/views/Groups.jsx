import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { useAuth } from '../store/auth.jsx'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

export default function Groups() {
  const { user } = useAuth()
  const isAdmin = ['admin', 'superadmin'].includes(user?.role)
  const [items, setItems] = useState([])
  const [form, setForm] = useState({ name: '', description: '' })
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  // 组内设备管理弹窗
  const [grp, setGrp] = useState(null)          // 当前查看的分组
  const [grpDevs, setGrpDevs] = useState([])
  const [sel, setSel] = useState(new Set())

  async function load() {
    const r = await api.listGroups()
    setItems(r.data)
  }
  useEffect(() => { load() }, [])

  async function handleCreate(e) {
    e.preventDefault()
    setErr(''); setMsg('')
    try {
      await api.createGroup(form)
      setForm({ name: '', description: '' })
      setMsg('分组创建成功')
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '创建失败')
    }
  }

  async function handleDelete(id) {
    if (!await uiConfirm('确认删除该分组？组内设备会变为未分组')) return
    await api.deleteGroup(id)
    load()
  }

  async function handleBatch(id, action) {
    const label = action === 'start' ? '开机' : action === 'stop' ? '关机' : '删除'
    const tip = action === 'delete'
      ? '确认删除分组内的全部云手机？删除后云手机归还公共池，设备内数据会被清除（服务器容器不受影响）。'
      : `确认对分组执行「${label}」？`
    if (!await uiConfirm(tip)) return
    try {
      const r = await api.groupBatchAction(id, { action })
      setMsg(`已对 ${r.data.ok} 台设备执行 ${label}`)
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '操作失败')
    }
  }

  /* ---------- 组内设备管理 ---------- */
  async function openGroup(g) {
    setGrp(g)
    setSel(new Set())
    const r = await api.listDevices({ group_id: g.id, page_size: 300 })
    const arr = Array.isArray(r.data) ? r.data : r.data.items || []
    setGrpDevs(arr)
  }
  function toggleDev(id) {
    const n = new Set(sel)
    n.has(id) ? n.delete(id) : n.add(id)
    setSel(n)
  }
  const selIds = [...sel]
  async function grpPower(action) {
    if (!selIds.length) return setErr('请先勾选设备')
    setBusy(true); setErr('')
    try {
      const r = await api.batchPower(selIds, action)
      setMsg(`已对 ${r.data.ok} 台设备${action === 'restart' ? '重启' : action === 'start' ? '开机' : '关机'}`)
      openGroup(grp)
    } catch (e) {
      setErr(e.response?.data?.error || '操作失败')
    } finally {
      setBusy(false)
    }
  }
  async function grpDelete() {
    if (!selIds.length) return setErr('请先勾选设备')
    if (!await uiConfirm(`确认删除勾选的 ${selIds.length} 台设备？（服务器容器一并删除）`)) return
    setBusy(true); setErr('')
    try {
      const r = await api.batchDelete(selIds)
      setMsg(`已删除 ${r.data.ok} 台设备`)
      await load()
      openGroup(grp)
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    } finally {
      setBusy(false)
    }
  }
  async function grpMove(targetGroupId) {
    if (!selIds.length) return setErr('请先勾选设备')
    setBusy(true); setErr('')
    try {
      const r = await api.batchSetGroup(selIds, targetGroupId)
      setMsg(`已移动 ${r.data.ok} 台设备`)
      await load()
      openGroup(grp)
    } catch (e) {
      setErr(e.response?.data?.error || '移动失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>分组管理</h2>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      {isAdmin && (
        <form onSubmit={handleCreate} className="task-form">
          <input
            list="existing-groups"
            placeholder="分组名（可下拉选择已有分组）"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <datalist id="existing-groups">
            {items.map((g) => (
              <option key={g.id} value={g.name}>{g.description || ''}</option>
            ))}
          </datalist>
          <input placeholder="描述" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <button type="submit">新建分组</button>
        </form>
      )}

      <div className="device-table-wrap files-table-wrap">
        <table className="device-table">
          <thead>
            <tr><th>ID</th><th>名称</th><th>设备数</th><th>描述</th><th>操作</th></tr>
          </thead>
          <tbody>
            {items.map((g) => (
              <tr key={g.id}>
                <td>{g.id}</td>
                <td className="file-name"><span className="file-name-text" title={g.name}>{g.name}</span></td>
                <td>{g.device_count}</td>
                <td className="file-name"><span className="file-name-text" title={g.description}>{g.description}</span></td>
                <td className="file-ops">
                  <button className="accent" onClick={() => openGroup(g)}>管理</button>
                  <button className="ghost" onClick={() => handleBatch(g.id, 'start')}>开机</button>
                  <button className="ghost" onClick={() => handleBatch(g.id, 'stop')}>关机</button>
                  <button className="ghost" onClick={() => handleBatch(g.id, 'delete')}>删除</button>
                  {isAdmin && <button className="danger" onClick={() => handleDelete(g.id)}>删除分组</button>}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr><td colSpan="4" className="empty-cell">暂无分组</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 组内设备管理弹窗 */}
      {grp && (
        <div className="modal-mask" onClick={() => setGrp(null)}>
          <div className="modal-body modal-lg" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>分组「{grp.name}」· 组内设备 {grpDevs.length} 台</span>
              <button className="modal-close" onClick={() => setGrp(null)}>✕</button>
            </div>

            <div className="push-sec-title">批量操作（已选 {selIds.length} 台）</div>
            <div className="file-ops" style={{ marginBottom: 10 }}>
              <button className="ghost" onClick={() => grpPower('start')} disabled={busy || !selIds.length}>开机</button>
              <button className="ghost" onClick={() => grpPower('stop')} disabled={busy || !selIds.length}>关机</button>
              <button className="ghost" onClick={() => grpPower('restart')} disabled={busy || !selIds.length}>重启</button>
              <button className="ghost" onClick={() => grpMove(grp.id)} disabled={busy || !selIds.length}>移出分组</button>
              <button className="danger" onClick={grpDelete} disabled={busy || !selIds.length}>删除</button>
            </div>

            <div className="device-table-wrap files-table-wrap">
              <table className="device-table">
                <thead>
                  <tr><th></th><th>名称</th><th>状态</th><th>机型</th></tr>
                </thead>
                <tbody>
                  {grpDevs.map((d) => (
                    <tr key={d.id}>
                      <td><input type="checkbox" checked={sel.has(d.id)} onChange={() => toggleDev(d.id)} /></td>
                      <td className="file-name"><span className="file-name-text">{d.name}</span></td>
                      <td><span className={`badge st-${d.status}`}>{d.status === 'running' ? '运行中' : d.status}</span></td>
                      <td>{d.model || '-'}</td>
                    </tr>
                  ))}
                  {grpDevs.length === 0 && <tr><td colSpan="5" className="empty-cell">该分组暂无设备，可在设备管理页勾选设备后「加入分组」</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="modal-actions">
              <button onClick={() => setGrp(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
