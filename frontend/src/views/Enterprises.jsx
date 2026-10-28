import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

const EMPTY = { name: '', code: '', contact: '', remark: '' }

const VIEW_KEY = 'view_enterprise_id'

function currentViewId() {
  try {
    return localStorage.getItem(VIEW_KEY) || ''
  } catch {
    return ''
  }
}

export default function Enterprises() {
  const [rows, setRows] = useState([])
  const [viewId, setViewId] = useState(currentViewId())
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [editing, setEditing] = useState(null)   // null | {mode, form}
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const r = await api.listEnterprises()
      setRows(Array.isArray(r.data) ? r.data : [])
      setErr('')
    } catch (e) {
      setErr(e.response?.data?.error || '加载失败')
    }
  }
  useEffect(() => { load() }, [])

  function switchView(id) {
    try {
      if (id) localStorage.setItem(VIEW_KEY, String(id))
      else localStorage.removeItem(VIEW_KEY)
    } catch { /* 忽略 */ }
    setViewId(id ? String(id) : '')
    window.location.reload()
  }

  function openCreate() {
    setEditing({ mode: 'create', form: { ...EMPTY } })
  }
  function openEdit(row) {
    setEditing({
      mode: 'edit', id: row.id,
      form: { name: row.name, code: row.code, contact: row.contact || '', remark: row.remark || '' },
    })
  }

  async function save() {
    const f = editing?.form
    if (!f) return
    if (!f.name.trim() || !f.code.trim()) {
      setErr('企业名称和编码必填')
      return
    }
    setBusy(true)
    setErr(''); setMsg('')
    try {
      if (editing.mode === 'create') {
        await api.createEnterprise({
          name: f.name.trim(), code: f.code.trim().toUpperCase(),
          contact: f.contact.trim(), remark: f.remark.trim(),
        })
        setMsg('企业已创建，系统已自动生成它的根部门与默认角色')
      } else {
        await api.updateEnterprise(editing.id, {
          name: f.name.trim(), contact: f.contact.trim(), remark: f.remark.trim(),
        })
        setMsg('企业信息已保存')
      }
      setEditing(null)
      await load()
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    } finally {
      setBusy(false)
    }
  }

  async function toggleStatus(row) {
    const next = row.status === 'active' ? 'disabled' : 'active'
    if (next === 'disabled' && !await uiConfirm(
      `确定停用「${row.name}」？\n\n停用后该企业所有账号都无法登录，数据保留。`)) return
    try {
      await api.updateEnterprise(row.id, { status: next })
      setMsg(next === 'disabled' ? '企业已停用' : '企业已启用')
      await load()
    } catch (e) {
      setErr(e.response?.data?.error || '操作失败')
    }
  }

  async function remove(row) {
    if (!await uiConfirm(`确定删除企业「${row.name}」？\n\n只有成员、设备、订阅都清空的企业才能删除。`)) return
    try {
      await api.deleteEnterprise(row.id)
      setMsg('企业已删除')
      await load()
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>企业管理</h2>
          <span className="perm-badge">平台管理员</span>
        </div>
        <button onClick={openCreate}>＋ 新建企业</button>
      </div>

      {viewId && (
        <div className="ok" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span>
            正在以「{(rows.find((r) => String(r.id) === viewId) || {}).name || viewId}」的视角查看数据
          </span>
          <button className="ghost" style={{ padding: '3px 10px', fontSize: 12 }}
            onClick={() => switchView('')}>退出视角</button>
        </div>
      )}
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      <div className="hint" style={{ marginBottom: 12 }}>
        每家企业独立运营：各自的部门、成员、套餐、订单、设备互不可见。
        停用企业会立即阻止该企业所有账号登录；平台管理员可跨企业查看。
      </div>

      <table>
        <thead>
          <tr>
            <th>企业</th><th>编码</th><th>状态</th><th>企业管理员</th>
            <th>成员</th><th>部门</th><th>设备</th><th>生效订阅</th><th>待审退款</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>
                <div style={{ fontWeight: 600 }}>{r.name}</div>
                {r.contact && <div className="hint">{r.contact}</div>}
              </td>
              <td style={{ fontFamily: 'var(--font-mono, monospace)' }}>{r.code}</td>
              <td>
                <span className={`badge ${r.status === 'active' ? 'st-running' : 'st-error'}`}>
                  {r.status === 'active' ? '正常' : '已停用'}
                </span>
              </td>
              <td>
                {(r.admins || []).length === 0
                  ? <span className="hint">未设置</span>
                  : (r.admins || []).map((a) => (
                    <span key={a.id} className="chip" style={{ marginRight: 4 }}>
                      {a.username}
                      {!a.is_active && <span style={{ color: 'var(--danger)', marginLeft: 4 }}>停用</span>}
                    </span>
                  ))}
              </td>
              <td>{r.users}</td>
              <td>{r.departments}</td>
              <td>{r.devices}</td>
              <td>{r.active_subs}</td>
              <td>{r.pending_refunds}</td>
              <td>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <button className="ghost" style={{ padding: '4px 8px', fontSize: 12 }}
                    onClick={() => openEdit(r)}>编辑</button>
                  <button className="ghost" style={{ padding: '4px 8px', fontSize: 12 }}
                    onClick={() => switchView(r.id)}>
                    {String(r.id) === viewId ? '当前视角' : '以该视角查看'}
                  </button>
                  <button className="ghost" style={{ padding: '4px 8px', fontSize: 12 }}
                    onClick={() => toggleStatus(r)}>
                    {r.status === 'active' ? '停用' : '启用'}
                  </button>
                  <button className="ghost" style={{ padding: '4px 8px', fontSize: 12, color: 'var(--danger)' }}
                    onClick={() => remove(r)}>删除</button>
                </div>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={10} className="table-empty">还没有企业，点右上角「新建企业」</td></tr>
          )}
        </tbody>
      </table>

      {editing && (
        <div className="modal-mask" onClick={() => setEditing(null)}>
          <div className="modal-body" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>{editing.mode === 'create' ? '新建企业' : '编辑企业'}</span>
              <button className="modal-close" onClick={() => setEditing(null)}>✕</button>
            </div>

            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">企业名称</span>
              <input value={editing.form.name}
                onChange={(e) => setEditing({ ...editing, form: { ...editing.form, name: e.target.value } })}
                placeholder="如：某某科技有限公司" />
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">企业编码</span>
              <input value={editing.form.code} disabled={editing.mode === 'edit'}
                onChange={(e) => setEditing({ ...editing, form: { ...editing.form, code: e.target.value } })}
                placeholder="如：SY-002（唯一，创建后不可改）" />
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">联系人</span>
              <input value={editing.form.contact}
                onChange={(e) => setEditing({ ...editing, form: { ...editing.form, contact: e.target.value } })}
                placeholder="对接人 / 电话" />
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">备注</span>
              <input value={editing.form.remark}
                onChange={(e) => setEditing({ ...editing, form: { ...editing.form, remark: e.target.value } })}
                placeholder="可选" />
            </div>

            <div className="modal-actions">
              <button className="ghost" onClick={() => setEditing(null)}>取消</button>
              <button onClick={save} disabled={busy}>{busy ? '保存中…' : '保存'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
