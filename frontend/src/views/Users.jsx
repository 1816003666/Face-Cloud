import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { useAuth } from '../store/auth.jsx'
import { uiAlert, uiConfirm } from '../lib/dialog.js'
import { copyText } from '../lib/clipboard.js'
import { fmtSecond } from '../lib/fmt.js'

const ROLES = ['viewer', 'operator', 'admin', 'superadmin']
const ROLE_LABEL = { viewer: '用户', operator: '运营', admin: '管理员', superadmin: '超级管理员' }
const ROLE_DESC = {
  viewer: '普通用户：只能查看/控制分配给自己的云手机，不能建号、不能进管理后台',
  operator: '可运维设备：开关机、远程控制、上传推送文件、执行任务',
  admin: '可做管理：建成员/部门、管设备与计费、审核退款（范围受所在企业限制）',
  superadmin: '平台最高权限：跨企业管理、分配管理员角色',
}

/**
 * 用户管理（系统管理后台 / 平台级）。
 *
 * 这里管的是"平台账号"：登录角色 + 归属哪家企业。
 * 企业内部的部门、成员档案、业务角色在「企业管理 → 部门与角色」里维护。
 */
export default function Users() {
  const { user } = useAuth()
  const isSuper = user?.role === 'superadmin'
  const [list, setList] = useState([])
  const [ents, setEnts] = useState([])
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [creating, setCreating] = useState(null)  // {username, password, role, enterprise_id}
  const [editing, setEditing] = useState(null)    // {u, enterprise_id}
  const [pwd, setPwd] = useState(null)
  const [copied, setCopied] = useState(false)            // {username, password}
  const [permModal, setPermModal] = useState(null) // {u, data, selected, preset}
  const [roleModal, setRoleModal] = useState(null) // {u, role}
  const [presetEdit, setPresetEdit] = useState(null) // {preset, name, desc, applyPerms}
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const [u, e] = await Promise.all([api.listUsers(), api.listEnterprises()])
      setList(Array.isArray(u.data) ? u.data : [])
      setEnts(Array.isArray(e.data) ? e.data : [])
      setErr('')
    } catch (e2) {
      setErr(e2.response?.data?.error || '加载失败')
    }
  }
  useEffect(() => { load() }, [])

  const entName = (id) => (ents.find((x) => x.id === id) || {}).name || ''
  const roleOptions = ROLES.filter((r) => isSuper || r === 'viewer' || r === 'operator')

  function openCreate() {
    setErr(''); setMsg('')
    setCreating({ username: '', password: '', role: 'viewer', enterprise_id: '' })
  }

  async function saveCreate() {
    const f = creating
    if (!f) return
    if (!f.username.trim()) { setErr('请填写用户名'); return }
    if (!f.password || f.password.length < 6) { setErr('密码至少 6 位，且需含字母和数字'); return }
    setBusy(true); setErr(''); setMsg('')
    try {
      const r = await api.createUser({
        username: f.username.trim(), password: f.password, role: f.role,
      })
      const created = r.data || {}
      if (created.id) {
        if (f.enterprise_id === 'personal') {
          await api.updateUser(created.id, { enterprise_id: null, account_kind: 'personal' })
        } else if (f.enterprise_id) {
          await api.updateUser(created.id, { enterprise_id: Number(f.enterprise_id) })
        }
      }
      setCreating(null)
      setMsg(`账号「${f.username.trim()}」已创建`)
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '创建失败')
    } finally {
      setBusy(false)
    }
  }

  function changeRole(u) {
    setErr('')
    setRoleModal({ u, role: u.role })
  }

  async function saveRole() {
    const m = roleModal
    if (!m) return
    if (m.role === m.u.role) { setRoleModal(null); return }
    try {
      await api.updateUser(m.u.id, { role: m.role })
      setRoleModal(null)
      setMsg(`「${m.u.username}」的登录角色已改为 ${ROLE_LABEL[m.role] || m.role}`)
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '修改失败')
    }
  }

  async function saveEnterprise() {
    if (!editing) return
    try {
      await api.updateUser(editing.u.id, {
        enterprise_id: editing.enterprise_id ? Number(editing.enterprise_id) : null,
        account_kind: editing.enterprise_id ? '' : (editing.kind || 'platform'),
      })
      setEditing(null)
      setMsg('账号所属已更新')
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '修改失败')
    }
  }

  async function copyPassword() {
    const ok = await copyText(pwd?.password || '')
    setCopied(ok)
    setTimeout(() => setCopied(false), 1500)
    if (!ok) {
      // 浏览器拒绝自动复制时，把密码选中，用户直接 Ctrl+C 就行
      const el = document.querySelector('.org-pwd')
      if (el) {
        const range = document.createRange()
        range.selectNodeContents(el)
        const sel = window.getSelection()
        sel.removeAllRanges()
        sel.addRange(range)
      }
      uiAlert('浏览器拒绝了自动复制，密码已选中，按 Ctrl+C 复制即可')
    }
  }

  async function resetPwd(u) {
    if (!await uiConfirm(`确认重置「${u.username}」的密码？\n\n系统会随机生成一个新密码，只显示这一次。`)) return
    try {
      const r = await api.resetUserPassword(u.id)
      setPwd({ username: r.data.username || u.username, password: r.data.password })
    } catch (e2) {
      setErr(e2.response?.data?.error || '重置失败')
    }
  }

  async function openPerms(u) {
    setErr('')
    try {
      const r = await api.getUserPermissions(u.id)
      setPermModal({
        u, data: r.data,
        selected: new Set(r.data.account_perms || r.data.effective || []),
        preset: r.data.preset || 'custom',
      })
    } catch (e2) {
      setErr(e2.response?.data?.error || '读取功能权限失败')
    }
  }

  async function savePerms() {
    const m = permModal
    if (!m) return
    try {
      const body = m.preset && m.preset !== 'custom'
        ? { preset: m.preset }
        : { perms: [...m.selected] }
      const r = await api.setUserPermissions(m.u.id, body)
      setPermModal(null)
      setMsg(`已更新「${m.u.username}」的功能权限（${(r.data.perms || []).length} 项）`)
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '保存失败')
    }
  }

  async function refreshPermModal() {
    if (!permModal) return
    const r = await api.getUserPermissions(permModal.u.id)
    setPermModal((m) => (m ? { ...m, data: r.data } : m))
  }

  async function saveAsPreset() {
    const m = permModal
    if (!m) return
    const name = window.prompt('给这套权限起个名字（例如：客服专员）')
    if (!name || !name.trim()) return
    try {
      await api.createPermPreset({ name: name.trim(), perms: [...m.selected] })
      await refreshPermModal()
      setMsg(`已新增权限档位「${name.trim()}」（${m.selected.size} 项）`)
    } catch (e2) {
      setErr(e2.response?.data?.error || '保存模板失败')
    }
  }

  function openPresetEdit(p) {
    setPresetEdit({ preset: p, name: p.name, desc: p.desc || '', applyPerms: false })
  }

  async function savePresetEdit() {
    const e = presetEdit
    if (!e) return
    if (!e.name.trim()) { setErr('档位名称不能为空'); return }
    try {
      const body = { name: e.name.trim(), desc: e.desc.trim() }
      if (e.applyPerms) body.perms = [...permModal.selected]
      await api.updatePermPreset(e.preset.id, body)
      setPresetEdit(null)
      await refreshPermModal()
      setMsg(`档位「${e.name.trim()}」已更新`)
    } catch (e2) {
      setErr(e2.response?.data?.error || '保存失败')
    }
  }

  async function removePreset(p) {
    if (!await uiConfirm(`确定删除档位「${p.name}」？已按它授权的账号不受影响。`)) return
    try {
      await api.deletePermPreset(p.id)
      await refreshPermModal()
      setMsg('档位已删除')
    } catch (e2) {
      setErr(e2.response?.data?.error || '删除失败')
    }
  }

  function applyPreset(code) {
    const p = (permModal?.data.presets || []).find((x) => x.code === code)
    if (!p) return
    setPermModal({ ...permModal, preset: code, selected: new Set(p.perms || []) })
  }

  async function toggleActive(u) {
    try {
      await api.updateUser(u.id, { is_active: !u.is_active })
      setMsg(u.is_active ? '已停用' : '已启用')
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '操作失败')
    }
  }

  async function handleDelete(u) {
    if (!await uiConfirm(`确定删除账号「${u.username}」？名下设备会归还公共池。`)) return
    try {
      await api.deleteUser(u.id)
      setMsg('账号已删除')
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '删除失败')
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>用户管理</h2>
          <span className="perm-badge">平台管理员</span>
        </div>
        <button onClick={openCreate}>＋ 新建账号</button>
      </div>

      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      <div className="hint" style={{ marginBottom: 12 }}>
        平台账号管理：登录角色决定能调哪些接口，账号所属决定能看哪家企业的数据。
        企业内部的部门 / 成员档案 / 业务角色请到「企业管理 → 部门与角色」。
      </div>

      <table>
        <thead>
          <tr>
            <th>ID</th><th>账号</th><th>登录角色</th><th>账号所属</th>
            <th>状态</th><th>创建时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {list.map((u) => (
            <tr key={u.id}>
              <td>{u.id}</td>
              <td style={{ fontWeight: 600 }}>{u.username}</td>
              <td><span className="chip">{ROLE_LABEL[u.role] || u.role}</span></td>
              <td>
                {u.enterprise_id
                  ? <span className="chip">{entName(u.enterprise_id) || `#${u.enterprise_id}`}</span>
                  : <span className="chip">{['admin', 'superadmin'].includes(u.role) ? '平台级' : '个人用户'}</span>}
              </td>
              <td>
                <span className={`badge ${u.is_active ? 'st-running' : 'st-error'}`}>
                  {u.is_active ? '启用' : '停用'}
                </span>
              </td>
              <td>{fmtSecond(u.created_at)}</td>
              <td>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  <button className="ghost" style={{ padding: '3px 8px', fontSize: 12 }}
                    onClick={() => changeRole(u)}>改角色</button>
                  <button className="ghost" style={{ padding: '3px 8px', fontSize: 12 }}
                    onClick={() => setEditing({
                      u,
                      enterprise_id: u.enterprise_id || '',
                      kind: u.enterprise_id ? ''
                        : (['admin', 'superadmin'].includes(u.role) ? 'platform' : 'personal'),
                    })}>账号所属</button>
                  {u.enterprise_id ? (
                    <button className="ghost" style={{ padding: '3px 8px', fontSize: 12 }}
                      onClick={() => openPerms(u)}>功能权限</button>
                  ) : null}
                  <button className="ghost" style={{ padding: '3px 8px', fontSize: 12 }}
                    onClick={() => resetPwd(u)}>重置密码</button>
                  <button className="ghost" style={{ padding: '3px 8px', fontSize: 12 }}
                    onClick={() => toggleActive(u)}>{u.is_active ? '停用' : '启用'}</button>
                  <button className="ghost" style={{ padding: '3px 8px', fontSize: 12, color: 'var(--danger)' }}
                    onClick={() => handleDelete(u)}>删除</button>
                </div>
              </td>
            </tr>
          ))}
          {list.length === 0 && (
            <tr><td colSpan={7} className="table-empty">还没有账号</td></tr>
          )}
        </tbody>
      </table>

      {/* 新建账号 */}
      {creating && (
        <div className="modal-mask" onClick={() => setCreating(null)}>
          <div className="modal-body" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>新建账号</span>
              <button className="modal-close" onClick={() => setCreating(null)}>✕</button>
            </div>

            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">用户名</span>
              <input value={creating.username} autoFocus
                onChange={(e) => setCreating({ ...creating, username: e.target.value })}
                placeholder="登录用的账号，如 zhangsan" />
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">初始密码</span>
              <input type="text" value={creating.password}
                onChange={(e) => setCreating({ ...creating, password: e.target.value })}
                placeholder="至少 6 位，需含字母和数字" />
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">登录角色</span>
              <select value={creating.role}
                onChange={(e) => setCreating({ ...creating, role: e.target.value })}>
                {roleOptions.map((r) => (
                  <option key={r} value={r}>{ROLE_LABEL[r]}（{r}）</option>
                ))}
              </select>
              <span className="plan-field-tip">只有超级管理员才能分配「管理员 / 超级管理员」。</span>
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">账号所属</span>
              <select value={creating.enterprise_id}
                onChange={(e) => setCreating({ ...creating, enterprise_id: e.target.value })}>
                <option value="">平台账号（可跨企业）</option>
                <option value="personal">个人用户（不归属企业）</option>
                {ents.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select>
              <span className="plan-field-tip">
                选企业 = 企业账号，只能看该企业数据；个人用户不属于任何企业；平台账号建议只给运维。
              </span>
            </div>

            <div className="modal-actions">
              <button className="ghost" onClick={() => setCreating(null)}>取消</button>
              <button onClick={saveCreate} disabled={busy}>{busy ? '创建中…' : '创建'}</button>
            </div>
          </div>
        </div>
      )}

      {/* 改登录角色 */}
      {roleModal && (
        <div className="modal-mask" onClick={() => setRoleModal(null)}>
          <div className="modal-body" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>修改登录角色 · {roleModal.u.username}</span>
              <button className="modal-close" onClick={() => setRoleModal(null)}>✕</button>
            </div>

            <div className="hint" style={{ marginBottom: 12 }}>
              登录角色决定这个人能调用哪些接口；能看哪家企业的数据由「账号所属」决定。
            </div>

            {roleOptions.map((r) => {
              const on = roleModal.role === r
              return (
                <div
                  key={r}
                  onClick={() => setRoleModal({ ...roleModal, role: r })}
                  style={{
                    display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer',
                    padding: '10px 12px', marginBottom: 8, borderRadius: 9,
                    border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                    background: on ? 'var(--accent-soft, #f0fdfa)' : '#fff',
                  }}
                >
                  <input type="radio" checked={on} readOnly style={{ marginTop: 3 }} />
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>
                      {ROLE_LABEL[r]}（{r}）
                      {roleModal.u.role === r && (
                        <span className="chip" style={{ marginLeft: 8, fontSize: 10 }}>当前</span>
                      )}
                    </div>
                    <div className="hint" style={{ marginTop: 2 }}>{ROLE_DESC[r]}</div>
                  </div>
                </div>
              )
            })}

            {!isSuper && (
              <div className="hint">
                只有超级管理员可以分配「管理员 / 超级管理员」。
              </div>
            )}

            <div className="modal-actions">
              <button className="ghost" onClick={() => setRoleModal(null)}>取消</button>
              <button onClick={saveRole}>保存</button>
            </div>
          </div>
        </div>
      )}

      {/* 功能权限（平台管理员给企业管理员分配） */}
      {permModal && (
        <div className="modal-mask" onClick={() => setPermModal(null)}>
          <div className="modal-body" style={{ maxWidth: 980, width: '94vw' }}
            onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>功能权限 · {permModal.u.username}</span>
              <button className="modal-close" onClick={() => setPermModal(null)}>✕</button>
            </div>

            <div className="hint" style={{ marginBottom: 10 }}>
              企业管理员本质是用户，只是多一个「部门与角色」。这里决定他能用到哪一步；
              平台侧的计费管控、退款审核、告警、审计、服务器与系统设置不在此列。
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <span className="hint">当前勾选 {permModal.selected.size} 项</span>
              <button className="ghost" style={{ padding: '3px 10px', fontSize: 12 }}
                onClick={saveAsPreset}>
                存为模板
              </button>
              <span className="hint">把当前勾选存成新档位，以后一键套用</span>
            </div>

            {/* 档位 × 模块 一览：一眼看出每个档位覆盖了哪些模块、各多少项 */}
            <div style={{ overflowX: 'auto', marginBottom: 14 }}>
              <table style={{ fontSize: 11.5, width: '100%', tableLayout: 'fixed' }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: 'left', width: 160, whiteSpace: 'nowrap' }}>档位</th>
                    {(permModal.data.modules || []).map((m) => (
                      <th key={m.name}
                        style={{ textAlign: 'center', fontSize: 11, padding: '4px 2px', lineHeight: 1.3 }}
                        title={m.name}>
                        {m.name}
                      </th>
                    ))}
                    <th style={{ textAlign: 'center', width: 56, whiteSpace: 'nowrap' }}>合计</th>
                  </tr>
                </thead>
                <tbody>
                  {(permModal.data.presets || []).map((p) => {
                    const active = permModal.preset === p.code
                    return (
                      <tr key={p.code} onClick={() => applyPreset(p.code)}
                        style={{ cursor: 'pointer',
                          background: active ? 'var(--accent-soft, #f0fdfa)' : undefined }}>
                        <td style={{ whiteSpace: 'nowrap', fontWeight: active ? 700 : 400 }}>
                          <span style={{
                            display: 'inline-block', maxWidth: 90, overflow: 'hidden',
                            textOverflow: 'ellipsis', verticalAlign: 'bottom',
                          }} title={p.name}>{p.name}</span>
                          {active && <span className="chip" style={{ marginLeft: 6, fontSize: 10 }}>已选</span>}
                          <button
                            className="ghost"
                            title={p.builtin ? '编辑内置档位（可改名/改内容，不可删除）' : '编辑档位'}
                            style={{ marginLeft: 4, padding: '0 4px', fontSize: 11 }}
                            onClick={(e) => { e.stopPropagation(); openPresetEdit(p) }}
                          >✎</button>
                          {!p.builtin && (
                            <button
                              className="ghost"
                              title="删除该档位"
                              style={{ marginLeft: 2, padding: '0 4px', fontSize: 11, color: 'var(--danger)' }}
                              onClick={(e) => { e.stopPropagation(); removePreset(p) }}
                            >✕</button>
                          )}
                        </td>
                        {(permModal.data.modules || []).map((m) => {
                          const codes = m.items.map((i) => i[0])
                          const on = codes.filter((c) => (p.perms || []).includes(c)).length
                          return (
                            <td key={m.name} style={{ textAlign: 'center',
                              color: on ? 'var(--text)' : 'var(--text-muted)' }}>
                              {on === 0 ? '—' : `${on}/${codes.length}`}
                            </td>
                          )
                        })}
                        <td style={{ textAlign: 'center', fontWeight: 600 }}>{p.count}</td>
                      </tr>
                    )
                  })}
                  <tr onClick={() => setPermModal({ ...permModal, preset: 'custom' })}
                    style={{ cursor: 'pointer',
                      background: permModal.preset === 'custom' ? 'var(--accent-soft, #f0fdfa)' : undefined }}>
                    <td style={{ fontWeight: permModal.preset === 'custom' ? 700 : 400 }}>
                      自定义
                      {permModal.preset === 'custom'
                        && <span className="chip" style={{ marginLeft: 6, fontSize: 10 }}>已选</span>}
                    </td>
                    {(permModal.data.modules || []).map((m) => {
                      const codes = m.items.map((i) => i[0])
                      const on = codes.filter((c) => permModal.selected.has(c)).length
                      return (
                        <td key={m.name} style={{ textAlign: 'center',
                          color: on ? 'var(--text)' : 'var(--text-muted)' }}>
                          {on === 0 ? '—' : `${on}/${codes.length}`}
                        </td>
                      )
                    })}
                    <td style={{ textAlign: 'center', fontWeight: 600 }}>{permModal.selected.size}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            {(permModal.data.modules || []).map((mod) => (
              <div key={mod.name} style={{ marginBottom: 10 }}>
                <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 6 }}>{mod.name}</div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {mod.items.map(([code, label]) => {
                    const on = permModal.selected.has(code)
                    return (
                      <label key={code}
                        className={`chip ${on ? 'chip-active' : ''}`}
                        style={{ cursor: 'pointer', padding: '4px 10px', fontSize: 12 }}
                      >
                        <input type="checkbox" checked={on} style={{ marginRight: 4 }}
                          onChange={() => {
                            const next = new Set(permModal.selected)
                            if (on) next.delete(code); else next.add(code)
                            setPermModal({ ...permModal, selected: next, preset: 'custom' })
                          }}
                        />
                        {label}
                      </label>
                    )
                  })}
                </div>
              </div>
            ))}

            <div className="modal-actions">
              <button className="ghost" onClick={() => setPermModal(null)}>取消</button>
              <button onClick={savePerms}>保存</button>
            </div>
          </div>
        </div>
      )}

      {/* 编辑权限档位 */}
      {presetEdit && (
        <div className="modal-mask" onClick={() => setPresetEdit(null)}>
          <div className="modal-body" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>编辑档位 · {presetEdit.preset.name}</span>
              <button className="modal-close" onClick={() => setPresetEdit(null)}>✕</button>
            </div>

            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">档位名称</span>
              <input value={presetEdit.name} autoFocus
                onChange={(e) => setPresetEdit({ ...presetEdit, name: e.target.value })}
                placeholder="如：客服专员" />
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">说明</span>
              <input value={presetEdit.desc}
                onChange={(e) => setPresetEdit({ ...presetEdit, desc: e.target.value })}
                placeholder="这个档位给谁用、能做什么" />
            </div>

            <label className="hint" style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
              <input type="checkbox" checked={presetEdit.applyPerms}
                onChange={(e) => setPresetEdit({ ...presetEdit, applyPerms: e.target.checked })}
                style={{ marginTop: 3 }} />
              <span>
                同时把当前勾选的 <b>{permModal?.selected.size || 0}</b> 项写入这个档位
                <br />
                <span style={{ color: 'var(--text-muted)' }}>
                  不勾就只改名称/说明；已经按这个档位授权过的账号不会被改动。
                </span>
              </span>
            </label>

            {presetEdit.preset.builtin && (
              <div className="hint" style={{ marginTop: 10 }}>
                内置档位不能删除（重启后会按代码重建），但内容可以随便改。
              </div>
            )}

            <div className="modal-actions">
              <button className="ghost" onClick={() => setPresetEdit(null)}>取消</button>
              <button onClick={savePresetEdit}>保存</button>
            </div>
          </div>
        </div>
      )}

      {/* 调整账号所属 */}
      {editing && (
        <div className="modal-mask" onClick={() => setEditing(null)}>
          <div className="modal-body" style={{ maxWidth: 500 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>调整账号所属 · {editing.u.username}</span>
              <button className="modal-close" onClick={() => setEditing(null)}>✕</button>
            </div>
            <div className="plan-field" style={{ marginBottom: 12 }}>
              <span className="plan-field-label">账号所属</span>
              <select
                value={editing.enterprise_id ? String(editing.enterprise_id) : (editing.kind || 'platform')}
                onChange={(e) => {
                  const v = e.target.value
                  if (v === 'platform' || v === 'personal') {
                    setEditing({ ...editing, enterprise_id: '', kind: v })
                  } else {
                    setEditing({ ...editing, enterprise_id: v, kind: '' })
                  }
                }}>
                <option value="platform">平台账号（可跨企业）</option>
                <option value="personal">个人用户（不归属企业）</option>
                {ents.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select>
              <span className="plan-field-tip">
                平台账号需管理员角色；个人用户不归属任何企业；选企业则只看该企业数据。
              </span>
            </div>
            <div className="modal-actions">
              <button className="ghost" onClick={() => setEditing(null)}>取消</button>
              <button onClick={saveEnterprise}>保存</button>
            </div>
          </div>
        </div>
      )}

      {/* 密码结果 */}
      {pwd && (
        <div className="modal-mask" onClick={() => setPwd(null)}>
          <div className="modal-body" style={{ maxWidth: 380 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>密码已重置</span>
              <button className="modal-close" onClick={() => setPwd(null)}>✕</button>
            </div>
            <div className="hint" style={{ marginBottom: 8 }}>{pwd.username} 的新密码：</div>
            <div className="org-pwd-row">
              <div className="org-pwd">{pwd.password}</div>
              <button
                className={`ghost org-pwd-copy${copied ? ' done' : ''}`}
                onClick={copyPassword}
                title="复制新密码"
              >
                {copied ? '已复制' : '复制'}
              </button>
            </div>
            <div className="modal-actions" style={{ marginTop: 14 }}>
              <button onClick={() => setPwd(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
