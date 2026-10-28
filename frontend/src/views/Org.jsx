import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client.js'
import './Org.css'
import { uiAlert, uiConfirm } from '../lib/dialog.js'
import { copyText } from '../lib/clipboard.js'

const AVATAR_COLORS = [
  'linear-gradient(135deg,#4F93D8,#2F6FB5)',
  'linear-gradient(135deg,#39B37A,#2E9E6B)',
  'linear-gradient(135deg,#E0A03A,#B87914)',
  'linear-gradient(135deg,#D2645A,#C94A3B)',
  'linear-gradient(135deg,#7C6BD1,#5B4BB7)',
  'linear-gradient(135deg,#38A3B8,#25818F)',
]
const avatarColor = (id) => AVATAR_COLORS[Math.abs(Number(id) || 0) % AVATAR_COLORS.length]

const PLATFORM_ROLE_LABELS = {
  viewer: '用户', operator: '运营', admin: '管理员', superadmin: '超级管理员',
}

function currentUser() {
  try {
    return JSON.parse(localStorage.getItem('user') || 'null')
  } catch {
    return null
  }
}

function Modal({ title, children, footer, onClose, width = 520 }) {
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal-body" style={{ maxWidth: width }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          <span>{title}</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        {children}
        {footer && <div className="modal-actions" style={{ marginTop: 16 }}>{footer}</div>}
      </div>
    </div>
  )
}

export default function Org() {
  const me = currentUser()
  const isSuper = me?.role === 'superadmin'
  const platformRoleOptions = isSuper
    ? ['viewer', 'operator', 'admin', 'superadmin']
    : ['viewer', 'operator']

  const [page, setPage] = useState('members')
  const [overview, setOverview] = useState(null)
  const [members, setMembers] = useState([])
  const [deptId, setDeptId] = useState(null)
  const [open, setOpen] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [err, setErr] = useState('')
  const [toast, setToast] = useState('')

  // 弹窗
  const [memberModal, setMemberModal] = useState(null)   // {mode, form}
  const [scopeModal, setScopeModal] = useState(null)
  const [pwdModal, setPwdModal] = useState(null)
  const [copied, setCopied] = useState(false)
  const [assignModal, setAssignModal] = useState(null)
  const [deptModal, setDeptModal] = useState(null)

  const [roleId, setRoleId] = useState(null)
  const [roleDraft, setRoleDraft] = useState(null)

  const flash = (msg) => {
    setToast(msg)
    window.setTimeout(() => setToast(''), 2200)
  }

  async function loadOverview() {
    const r = await api.orgOverview()
    setOverview(r.data)
    return r.data
  }
  async function loadMembers() {
    const r = await api.orgMembers()
    setMembers(Array.isArray(r.data) ? r.data : [])
  }
  async function reload() {
    setErr('')
    try {
      const ov = await loadOverview()
      await loadMembers()
      if (roleId == null && ov.roles?.length) setRoleId(ov.roles[0].id)
      // 默认展开根部门，能一眼看到组织架构
      const root = (ov.departments || []).find((d) => d.parent_id == null)
      if (root) setOpen((prev) => (prev.size ? prev : new Set([root.id])))
    } catch (e) {
      setErr(e.response?.data?.error || e.message || '加载失败')
    }
  }

  useEffect(() => { reload() }, [])

  const depts = overview?.departments || []
  const roles = overview?.roles || []
  const modules = overview?.modules || []
  const scopes = overview?.scopes || []

  const deptMap = useMemo(() => {
    const m = new Map()
    depts.forEach((d) => m.set(d.id, d))
    return m
  }, [depts])

  const childrenMap = useMemo(() => {
    const m = new Map()
    depts.forEach((d) => {
      const key = d.parent_id == null ? '__root__' : d.parent_id
      if (!m.has(key)) m.set(key, [])
      m.get(key).push(d)
    })
    return m
  }, [depts])

  const rootDept = depts.find((d) => d.parent_id == null) || null
  const roleMap = useMemo(() => new Map(roles.map((r) => [r.id, r])), [roles])

  const deptPath = (id) => {
    const parts = []
    let cur = deptMap.get(id)
    let guard = 0
    while (cur && guard < 20) {
      parts.unshift(cur.name)
      cur = cur.parent_id == null ? null : deptMap.get(cur.parent_id)
      guard += 1
    }
    return parts.join(' / ')
  }

  const visibleMembers = useMemo(() => {
    let list = members
    if (deptId != null) list = list.filter((m) => m.department_id === deptId)
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter((m) => (m.name || '').toLowerCase().includes(q)
        || (m.account || '').toLowerCase().includes(q))
    }
    return list
  }, [members, deptId, query])

  // ---------------- 部门树 ----------------
  function toggleOpen(id) {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function nodeHtml(dept, depth) {
    const kids = childrenMap.get(dept.id) || []
    const isOpen = open.has(dept.id)
    const active = deptId === dept.id
    return (
      <div key={dept.id}>
        <div
          className={`org-node ${active ? 'active' : ''} ${isOpen ? 'open' : ''}`}
          style={{ paddingLeft: 8 + depth * 10 }}
          onClick={() => { setDeptId(dept.id); if (kids.length) setOpen((p) => new Set(p).add(dept.id)) }}
        >
          <span className="org-caret" onClick={(e) => { e.stopPropagation(); if (kids.length) toggleOpen(dept.id) }}>
            {kids.length ? '▶' : ''}
          </span>
          <span className="org-name">{dept.name}</span>
          <button
            className="org-edit"
            title="编辑部门"
            onClick={(e) => { e.stopPropagation(); openDeptModal(dept) }}
          >✎</button>
          <span className="org-count">{dept.member_count || 0}</span>
        </div>
        {kids.length > 0 && isOpen && (
          <div className="org-children">
            {kids.map((k) => nodeHtml(k, depth + 1))}
          </div>
        )}
      </div>
    )
  }

  function openDeptModal(dept) {
    if (dept) {
      setDeptModal({ mode: 'edit', dept })
    } else {
      setDeptModal({
        mode: 'create',
        form: { name: '', parent_id: deptId ?? rootDept?.id ?? null },
      })
    }
  }

  async function saveDept() {
    const m = deptModal
    if (!m) return
    try {
      if (m.mode === 'create') {
        if (!m.form.name.trim()) { setErr('请填写部门名称'); return }
        await api.orgCreateDept({ name: m.form.name.trim(), parent_id: m.form.parent_id })
        flash('部门已创建')
      } else {
        const name = (m.name ?? m.dept.name).trim()
        if (!name) { setErr('请填写部门名称'); return }
        const body = { name }
        if (m.dept.parent_id != null && m.parent_id) body.parent_id = m.parent_id
        await api.orgUpdateDept(m.dept.id, body)
        flash('部门已保存')
      }
      setDeptModal(null)
      setErr('')
      await reload()
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    }
  }

  async function deleteDept() {
    const m = deptModal
    if (!m || m.mode !== 'edit') return
    const dept = m.dept
    const kids = depts.filter((d) => d.parent_id === dept.id).length
    if (kids) { setErr('该部门下还有下级部门，请先删除或移走'); return }
    const cnt = dept.member_count || 0
    const msg = cnt
      ? `「${dept.name}」下还有 ${cnt} 名成员，删除后他们会转入上级部门。确定删除？`
      : `确定删除部门「${dept.name}」？`
    if (!await uiConfirm(msg)) return
    try {
      const r = await api.orgDeleteDept(dept.id)
      flash(r.data?.moved_members ? `已删除，${r.data.moved_members} 名成员已转入上级部门` : '部门已删除')
      setDeptModal(null)
      if (deptId === dept.id) setDeptId(null)
      await reload()
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  // ---------------- 成员 ----------------
  function openMemberModal(member) {
    if (member) {
      setMemberModal({
        mode: 'edit',
        id: member.id,
        name: member.name,
        account: member.account,
        department_id: member.department_id ?? rootDept?.id ?? null,
        role_ids: [...(member.role_ids || [])],
        scope: member.scope || 'self',
        active: member.active,
        platform_role: member.platform_role || 'viewer',
      })
    } else {
      setMemberModal({
        mode: 'create',
        name: '',
        account: '',
        password: '',
        department_id: deptId ?? rootDept?.id ?? null,
        role_ids: roles.length ? [roles[roles.length - 1].id] : [],
        scope: 'self',
        active: true,
        platform_role: 'viewer',
      })
    }
  }

  async function saveMember() {
    const m = memberModal
    if (!m) return
    try {
      if (!m.name.trim()) { setErr('请填写姓名'); return }
      if (m.mode === 'create') {
        if (!m.account.trim()) { setErr('请填写登录账号'); return }
        const body = {
          name: m.name.trim(), account: m.account.trim(),
          department_id: m.department_id, role_ids: m.role_ids,
          scope: m.scope, active: m.active, platform_role: m.platform_role,
        }
        if (m.password && m.password.trim()) body.password = m.password.trim()
        const r = await api.orgCreateMember(body)
        setMemberModal(null)
        setErr('')
        await reload()
        if (r.data?.password) setPwdModal({ name: r.data.name, password: r.data.password })
        else flash('成员已创建')
      } else {
        await api.orgUpdateMember(m.id, {
          name: m.name.trim(), department_id: m.department_id,
          role_ids: m.role_ids, scope: m.scope, active: m.active,
          ...(m.platform_role !== me?.role ? { platform_role: m.platform_role } : {}),
        })
        setMemberModal(null)
        setErr('')
        await reload()
        flash('成员信息已保存')
      }
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    }
  }

  async function toggleMember(m) {
    try {
      await api.orgUpdateMember(m.id, { active: !m.active })
      await reload()
      flash(m.active ? '已停用' : '已启用')
    } catch (e) {
      setErr(e.response?.data?.error || '操作失败')
    }
  }

  async function deleteMember(m) {
    const tip = `确定删除成员「${m.name}」（${m.account}）？\n\n账号会被删除，该账号将无法再登录；名下 ${m.devices || 0} 台云手机会留在公共池。`
    if (!await uiConfirm(tip)) return
    try {
      await api.orgDeleteMember(m.id)
      setMemberModal(null)
      await reload()
      flash('成员已删除')
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  async function resetPassword(m) {
    try {
      const r = await api.orgResetMemberPassword(m.id)
      setPwdModal({ name: m.name, password: r.data.password })
    } catch (e) {
      setErr(e.response?.data?.error || '重置失败')
    }
  }

  async function openAssign(m) {
    try {
      const r = await api.orgMemberDevices(m.id)
      setAssignModal({
        member: m,
        owned: r.data.owned || [],
        free: r.data.free || [],
        picked: new Set((r.data.owned || []).map((d) => d.id)),
      })
    } catch (e) {
      setErr(e.response?.data?.error || '加载资源失败')
    }
  }

  async function saveAssign() {
    const m = assignModal
    if (!m) return
    try {
      await api.orgAssignDevices(m.member.id, [...m.picked])
      setAssignModal(null)
      await reload()
      flash('资源分派已保存')
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    }
  }

  // ---------------- 角色 ----------------
  const currentRole = roleId != null ? roleMap.get(roleId) : null

  useEffect(() => {
    if (currentRole && (!roleDraft || roleDraft.id !== currentRole.id)) {
      setRoleDraft({ id: currentRole.id, name: currentRole.name, code: currentRole.code,
        description: currentRole.description, perms: [...(currentRole.perms || [])] })
    }
  }, [currentRole?.id])

  function togglePerm(code) {
    setRoleDraft((d) => {
      if (!d) return d
      const on = d.perms.includes(code)
      return { ...d, perms: on ? d.perms.filter((x) => x !== code) : [...d.perms, code] }
    })
  }

  function toggleGroup(mod) {
    const codes = mod.items.map((i) => i[0])
    const allOn = codes.every((c) => roleDraft?.perms.includes(c))
    setRoleDraft((d) => {
      if (!d) return d
      const rest = d.perms.filter((c) => !codes.includes(c))
      return { ...d, perms: allOn ? rest : [...rest, ...codes] }
    })
  }

  async function saveRole() {
    if (!roleDraft) return
    try {
      if (!roleDraft.name.trim()) { setErr('角色名称不能为空'); return }
      await api.orgUpdateRole(roleDraft.id, {
        name: roleDraft.name.trim(), code: roleDraft.code.trim(),
        description: roleDraft.description.trim(), perms: roleDraft.perms,
      })
      await reload()
      flash('角色已保存')
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    }
  }

  async function createRole() {
    const name = window.prompt('新角色名称', '新角色')
    if (name == null) return
    const trimmed = name.trim()
    if (!trimmed) return
    try {
      const r = await api.orgCreateRole({ name: trimmed, perms: [], description: '' })
      await reload()
      setRoleId(r.data.id)
      flash('角色已创建')
    } catch (e) {
      setErr(e.response?.data?.error || '创建失败')
    }
  }

  async function deleteRole() {
    if (!currentRole) return
    if (!await uiConfirm(`确定删除角色「${currentRole.name}」？成员身上的该角色会一并移除。`)) return
    try {
      await api.orgDeleteRole(currentRole.id)
      setRoleId(null)
      setRoleDraft(null)
      await reload()
      flash('角色已删除')
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  // ---------------- 渲染 ----------------
  if (!overview) {
    return <div className="hint">{err || '加载中…'}</div>
  }

  return (
    <div className="org-page">
      <div className="page-header" style={{ marginBottom: 12 }}>
        <div className="page-title">
          <h2 style={{ margin: 0 }}>部门与角色</h2>
          <span className="perm-badge">管理员</span>
        </div>
        <div className="org-tabs">
          <button className={page === 'members' ? 'on' : ''} onClick={() => setPage('members')}>部门成员管理</button>
          <button className={page === 'roles' ? 'on' : ''} onClick={() => setPage('roles')}>角色管理</button>
        </div>
      </div>

      {err && <div className="err" style={{ marginBottom: 10 }}>{err}</div>}

      {page === 'members' ? (
        <div className="org-split">
          <aside className="org-panel">
            <div className="org-p-head">
              组织架构
              <span className="org-p-hint">{depts.length} 个部门</span>
            </div>
            <div className="org-p-body">
              <div className={`org-node org-all ${deptId == null ? 'active' : ''}`} onClick={() => setDeptId(null)}>
                <span className="org-caret" />
                <span className="org-name">全部成员</span>
                <span className="org-count">{members.length}</span>
              </div>
              {rootDept && nodeHtml(rootDept, 0)}
              <button className="ghost org-link" style={{ marginTop: 8 }} onClick={() => openDeptModal(null)}>
                ＋ 新建部门
              </button>
            </div>
          </aside>

          <main className="org-panel">
            <div className="org-p-head">
              {deptId != null ? deptPath(deptId) : '全部成员'}
              <span className="org-p-hint">
                {deptId != null ? `直属成员 ${visibleMembers.length} 人` : `共 ${visibleMembers.length} 人`}
              </span>
            </div>
            <div className="org-p-body">
              <div className="org-toolbar">
                <label className="org-search">
                  <span style={{ color: 'var(--text-muted)' }}>⌕</span>
                  <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索姓名或登录账号" />
                </label>
                <button onClick={() => openMemberModal(null)}>＋ 新增成员</button>
              </div>

              <div style={{ overflowX: 'auto' }}>
                <table>
                  <thead>
                    <tr>
                      <th>成员</th><th>部门</th><th>角色</th><th>数据权限</th>
                      <th>资源</th><th>状态</th><th style={{ textAlign: 'right' }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleMembers.length === 0 && (
                      <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '34px 0' }}>
                        {deptId != null ? '本部门暂无直属成员' : '没有匹配的成员'}
                      </td></tr>
                    )}
                    {visibleMembers.map((m) => (
                      <tr key={m.id}>
                        <td>
                          <div className="org-who">
                            <div className="org-ava" style={{ background: avatarColor(m.id) }}>
                              {(m.name || '?').slice(0, 1)}
                            </div>
                            <div>
                              <div className="org-who-n">
                                {m.name}
                                {m.platform_role && m.platform_role !== 'viewer' && (
                                  <span className="chip" style={{ marginLeft: 6, fontSize: 10 }}>
                                    {PLATFORM_ROLE_LABELS[m.platform_role] || m.platform_role}
                                  </span>
                                )}
                              </div>
                              <div className="org-who-a">{m.account}</div>
                            </div>
                          </div>
                        </td>
                        <td>{m.department_id ? deptMap.get(m.department_id)?.name || '-' : <span className="hint">未分配</span>}</td>
                        <td>
                          <div className="org-chips">
                            {(m.role_ids || []).length === 0
                              ? <span className="hint">未分配</span>
                              : m.role_ids.map((rid) => (
                                <span className="chip" key={rid}>{roleMap.get(rid)?.name || `#${rid}`}</span>
                              ))}
                          </div>
                        </td>
                        <td>
                          <span
                            className="chip"
                            style={{ cursor: 'pointer' }}
                            title="点击调整数据权限"
                            onClick={() => setScopeModal({ member: m, scope: m.scope })}
                          >
                            {(scopes.find((s) => s.code === m.scope) || {}).name || m.scope}
                          </span>
                        </td>
                        <td>
                          {m.devices > 0
                            ? <span className="badge st-pending">{m.devices} 个</span>
                            : <span className="hint">未分配</span>}
                        </td>
                        <td>
                          {m.active
                            ? <span className="badge st-running">启用</span>
                            : <span className="badge st-error">停用</span>}
                        </td>
                        <td style={{ textAlign: 'right' }}>
                          <div className="org-acts" style={{ justifyContent: 'flex-end' }}>
                            <button className="ghost org-link" onClick={() => openMemberModal(m)}>编辑</button>
                            <button className="ghost org-link" onClick={() => resetPassword(m)}>重置密码</button>
                            <button className="ghost org-link" onClick={() => openAssign(m)}>资源分派</button>
                            <button className={`ghost org-link ${m.active ? 'org-danger' : ''}`} onClick={() => toggleMember(m)}>
                              {m.active ? '停用' : '启用'}
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </main>
        </div>
      ) : (
        <div className="org-split role">
          <aside className="org-panel">
            <div className="org-p-head">
              角色
              <span className="org-p-hint">{roles.length} 个</span>
            </div>
            <div className="org-p-body">
              {roles.map((r) => (
                <div
                  key={r.id}
                  className={`org-r-item ${roleId === r.id ? 'active' : ''}`}
                  onClick={() => setRoleId(r.id)}
                >
                  <div className="org-r-name">{r.name}</div>
                  <div className="org-r-sub">{r.member_count || 0} 名成员 · {(r.perms || []).length} 项权限</div>
                </div>
              ))}
              <button className="ghost org-link" style={{ marginTop: 8 }} onClick={createRole}>＋ 新建角色</button>
            </div>
          </aside>

          <main className="org-panel">
            {!currentRole || !roleDraft ? (
              <div className="org-r-body hint">请选择左侧角色</div>
            ) : (
              <>
                <div className="org-r-head">
                  <input
                    value={roleDraft.name}
                    onChange={(e) => setRoleDraft({ ...roleDraft, name: e.target.value })}
                    style={{ font: 'inherit', fontSize: 14, fontWeight: 700, border: '1px solid transparent',
                      borderRadius: 7, padding: '4px 8px', minWidth: 180, background: 'transparent' }}
                  />
                  <span className="chip">{roleDraft.code || '未设编码'}</span>
                  <span className="org-p-hint" style={{ marginLeft: 'auto' }}>
                    已选 {roleDraft.perms.length} / {overview.all_perms.length} 项
                  </span>
                </div>
                <div className="org-r-body">
                  <div className="org-field">
                    <label>角色描述</label>
                    <input
                      type="text"
                      value={roleDraft.description}
                      onChange={(e) => setRoleDraft({ ...roleDraft, description: e.target.value })}
                      placeholder="这个角色负责什么"
                    />
                  </div>

                  <div className="org-sec-h">
                    功能权限
                    <span className="tip">勾选后该角色可执行对应操作（成员多角色时取并集）</span>
                  </div>

                  {modules.map((mod) => {
                    const codes = mod.items.map((i) => i[0])
                    const onCount = codes.filter((c) => roleDraft.perms.includes(c)).length
                    const allOn = onCount === codes.length && codes.length > 0
                    return (
                      <div className="org-pgroup" key={mod.name}>
                        <div className="org-pgroup-h">
                          <label style={{ display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
                            <input type="checkbox" checked={allOn} onChange={() => toggleGroup(mod)} />
                            {mod.name}
                          </label>
                          <span className="cnt">{onCount} / {codes.length}</span>
                        </div>
                        <div className="org-pgroup-b">
                          {mod.items.map(([code, label]) => {
                            const on = roleDraft.perms.includes(code)
                            return (
                              <label key={code} className={`org-pitem ${on ? 'on' : ''}`}>
                                <input type="checkbox" checked={on} onChange={() => togglePerm(code)} />
                                {label}
                              </label>
                            )
                          })}
                        </div>
                      </div>
                    )
                  })}

                  <div className="org-sec-h" style={{ marginTop: 4 }}>
                    该角色成员
                    <span className="tip">{currentRole.member_count || 0} 人</span>
                  </div>
                  <div className="org-chips">
                    {members.filter((m) => (m.role_ids || []).includes(currentRole.id)).map((m) => (
                      <span className="chip" key={m.id}>{m.name}</span>
                    ))}
                    {currentRole.member_count === 0 && <span className="hint">暂无成员使用该角色</span>}
                  </div>

                  <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
                    <button className="ghost org-danger" onClick={deleteRole}>删除角色</button>
                    <div style={{ flex: 1 }} />
                    <button onClick={saveRole}>保存角色</button>
                  </div>
                </div>
              </>
            )}
          </main>
        </div>
      )}

      {/* 成员编辑弹窗 */}
      {memberModal && (
        <Modal
          title={memberModal.mode === 'create' ? '新增成员' : `编辑成员 · ${memberModal.name}`}
          onClose={() => setMemberModal(null)}
          width={560}
          footer={(
            <>
              {memberModal.mode === 'edit' && memberModal.id !== me?.id && (
                <button className="ghost org-danger" style={{ marginRight: 'auto' }}
                  onClick={() => deleteMember({ id: memberModal.id, name: memberModal.name,
                    account: memberModal.account,
                    devices: members.find((x) => x.id === memberModal.id)?.devices || 0 })}>
                  删除成员
                </button>
              )}
              <button className="ghost" onClick={() => setMemberModal(null)}>取消</button>
              <button onClick={saveMember}>保存</button>
            </>
          )}
        >
          <div className="org-field">
            <label>姓名</label>
            <input type="text" value={memberModal.name}
              onChange={(e) => setMemberModal({ ...memberModal, name: e.target.value })} placeholder="请输入姓名" />
          </div>
          <div className="org-field">
            <label>登录账号</label>
            <input type="text" value={memberModal.account} disabled={memberModal.mode === 'edit'}
              onChange={(e) => setMemberModal({ ...memberModal, account: e.target.value })}
              placeholder="用于登录云手机平台的账号" />
            {memberModal.mode === 'edit' && <div className="org-hint">账号创建后不可修改。</div>}
          </div>
          {memberModal.mode === 'create' && (
            <div className="org-field">
              <label>初始密码（可留空自动生成）</label>
              <input type="text" value={memberModal.password || ''}
                onChange={(e) => setMemberModal({ ...memberModal, password: e.target.value })}
                placeholder="至少 6 位，含字母和数字" />
            </div>
          )}
          <div className="org-field">
            <label>所属部门</label>
            <select value={memberModal.department_id ?? ''}
              onChange={(e) => setMemberModal({ ...memberModal, department_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">未分配</option>
              {depts.filter((d) => d.parent_id != null).map((d) => (
                <option key={d.id} value={d.id}>{deptPath(d.id)}</option>
              ))}
            </select>
          </div>
          <div className="org-field">
            <label>平台登录角色</label>
            <select
              value={memberModal.platform_role || 'viewer'}
              disabled={memberModal.id === me?.id}
              onChange={(e) => setMemberModal({ ...memberModal, platform_role: e.target.value })}
            >
              {platformRoleOptions.map((r) => (
                <option key={r} value={r}>{PLATFORM_ROLE_LABELS[r]}（{r}）</option>
              ))}
              {!platformRoleOptions.includes(memberModal.platform_role) && memberModal.platform_role && (
                <option value={memberModal.platform_role}>
                  {PLATFORM_ROLE_LABELS[memberModal.platform_role] || memberModal.platform_role}
                </option>
              )}
            </select>
            <div className="org-hint">
              决定能否调用管理接口（管理员及以上）；与上面「角色」的业务权限是两套东西。{memberModal.id === me?.id ? '不能修改自己的角色。' : ''}
            </div>
          </div>
          <div className="org-field">
            <label>角色（可多选）</label>
            {roles.map((r) => {
              const on = memberModal.role_ids.includes(r.id)
              return (
                <label key={r.id} className={`org-pick ${on ? 'on' : ''}`}>
                  <input type="checkbox" checked={on} onChange={() => {
                    const ids = on
                      ? memberModal.role_ids.filter((x) => x !== r.id)
                      : [...memberModal.role_ids, r.id]
                    setMemberModal({ ...memberModal, role_ids: ids })
                  }} />
                  <span>{r.name}</span>
                  <span className="sp">{r.perms.length} 项权限</span>
                </label>
              )
            })}
          </div>
          <div className="org-field">
            <label>数据权限</label>
            <div className="org-scopes">
              {scopes.map((s) => (
                <div key={s.code}
                  className={`org-scope ${memberModal.scope === s.code ? 'on' : ''}`}
                  onClick={() => setMemberModal({ ...memberModal, scope: s.code })}>
                  <h4>{s.name}</h4>
                  <p>{s.desc}</p>
                </div>
              ))}
            </div>
          </div>
          <label className="org-pick" style={{ marginTop: 4 }}>
            <input type="checkbox" checked={memberModal.active}
              onChange={(e) => setMemberModal({ ...memberModal, active: e.target.checked })} />
            <span>启用该成员（停用后无法登录）</span>
          </label>
        </Modal>
      )}

      {/* 数据权限调整 */}
      {scopeModal && (
        <Modal
          title={`数据权限 · ${scopeModal.member.name}`}
          onClose={() => setScopeModal(null)}
          width={520}
          footer={(
            <>
              <button className="ghost" onClick={() => setScopeModal(null)}>取消</button>
              <button onClick={async () => {
                try {
                  await api.orgUpdateMember(scopeModal.member.id, { scope: scopeModal.scope })
                  setScopeModal(null)
                  await reload()
                  flash('数据权限已更新')
                } catch (e) {
                  setErr(e.response?.data?.error || '保存失败')
                }
              }}>保存</button>
            </>
          )}
        >
          <div className="org-scopes">
            {scopes.map((s) => (
              <div key={s.code}
                className={`org-scope ${scopeModal.scope === s.code ? 'on' : ''}`}
                onClick={() => setScopeModal({ ...scopeModal, scope: s.code })}>
                <h4>{s.name}</h4>
                <p>{s.desc}</p>
              </div>
            ))}
          </div>
        </Modal>
      )}

      {/* 初始/重置密码 */}
      {pwdModal && (
        <Modal
          title="账号密码"
          onClose={() => setPwdModal(null)}
          width={400}
          footer={<button onClick={() => setPwdModal(null)}>知道了</button>}
        >
          <div className="org-hint" style={{ marginBottom: 10 }}>
            {pwdModal.name} 的新密码（只显示这一次，请立即转交并提醒对方修改）：
          </div>
          <div className="org-pwd-row">
            <div className="org-pwd">{pwdModal.password}</div>
            <button
              className={`ghost org-pwd-copy${copied ? ' done' : ''}`}
              onClick={async () => {
                const ok = await copyText(pwdModal.password || '')
                setCopied(ok)
                setTimeout(() => setCopied(false), 1500)
                if (!ok) {
                  const el = document.querySelector('.org-pwd')
                  if (el) {
                    const range = document.createRange()
                    range.selectNodeContents(el)
                    const sel = window.getSelection()
                    sel.removeAllRanges()
                    sel.addRange(range)
                  }
                }
              }}
              title="复制密码"
            >{copied ? '已复制' : '复制'}</button>
          </div>
        </Modal>
      )}

      {/* 资源分派 */}
      {assignModal && (
        <Modal
          title={`资源分派 · ${assignModal.member.name}`}
          onClose={() => setAssignModal(null)}
          width={560}
          footer={(
            <>
              <button className="ghost" onClick={() => setAssignModal(null)}>取消</button>
              <button onClick={saveAssign}>保存</button>
            </>
          )}
        >
          <div className="org-hint" style={{ marginBottom: 10 }}>
            勾选要分给该成员的云手机；取消勾选会把设备收回公共池（设备上的用户数据不受影响）。
          </div>
          <div style={{ maxHeight: 320, overflow: 'auto', border: '1px solid var(--border-light)', borderRadius: 9, padding: 8 }}>
            {[...assignModal.owned, ...assignModal.free].length === 0 && (
              <div className="org-hint" style={{ padding: 8 }}>暂无可分派的云手机</div>
            )}
            {[...assignModal.owned, ...assignModal.free].map((d) => {
              const on = assignModal.picked.has(d.id)
              const owned = assignModal.owned.some((x) => x.id === d.id)
              return (
                <label key={d.id} className={`org-pick ${on ? 'on' : ''}`} style={{ marginBottom: 4 }}>
                  <input type="checkbox" checked={on} onChange={() => {
                    const next = new Set(assignModal.picked)
                    if (on) next.delete(d.id)
                    else next.add(d.id)
                    setAssignModal({ ...assignModal, picked: next })
                  }} />
                  <span>{d.name}</span>
                  <span className="sp">{owned ? '已分配 · ' : ''}{d.status} · {d.ip}</span>
                </label>
              )
            })}
          </div>
        </Modal>
      )}

      {/* 部门编辑 */}
      {deptModal && (
        <Modal
          title={deptModal.mode === 'create' ? '新建部门' : `编辑部门 · ${deptModal.dept.name}`}
          onClose={() => setDeptModal(null)}
          width={480}
          footer={(
            <>
              {deptModal.mode === 'edit' && deptModal.dept.parent_id != null && (
                <button className="ghost org-danger" style={{ marginRight: 'auto' }} onClick={deleteDept}>删除部门</button>
              )}
              <button className="ghost" onClick={() => setDeptModal(null)}>取消</button>
              <button onClick={saveDept}>保存</button>
            </>
          )}
        >
          <div className="org-field">
            <label>部门名称</label>
            <input
              type="text"
              value={deptModal.mode === 'create' ? deptModal.form.name : (deptModal.name ?? deptModal.dept.name)}
              onChange={(e) => setDeptModal(deptModal.mode === 'create'
                ? { ...deptModal, form: { ...deptModal.form, name: e.target.value } }
                : { ...deptModal, name: e.target.value })}
              placeholder="如：华东区"
            />
          </div>
          <div className="org-field">
            <label>上级部门</label>
            <select
              disabled={deptModal.mode === 'edit' && deptModal.dept.parent_id == null}
              value={deptModal.mode === 'create'
                ? (deptModal.form.parent_id ?? rootDept?.id ?? '')
                : (deptModal.parent_id ?? deptModal.dept.parent_id ?? rootDept?.id ?? '')}
              onChange={(e) => setDeptModal(deptModal.mode === 'create'
                ? { ...deptModal, form: { ...deptModal.form, parent_id: Number(e.target.value) } }
                : { ...deptModal, parent_id: Number(e.target.value) })}
            >
              {depts.map((d) => (
                <option key={d.id} value={d.id}>{deptPath(d.id)}</option>
              ))}
            </select>
            <div className="org-hint">
              {deptModal.mode === 'edit' && deptModal.dept.parent_id == null
                ? '根部门的上级不可调整。'
                : '调整层级后，该部门及下级成员的权限范围会按新位置重算。'}
            </div>
          </div>
          {deptModal.mode === 'edit' && (
            <div className="org-hint">
              下级部门 {depts.filter((d) => d.parent_id === deptModal.dept.id).length} 个 ·
              直属成员 {deptModal.dept.member_count || 0} 人
            </div>
          )}
        </Modal>
      )}

      {toast && (
        <div style={{
          position: 'fixed', left: '50%', bottom: 32, transform: 'translateX(-50%)',
          background: 'rgba(30,42,56,.94)', color: '#fff', padding: '9px 18px', borderRadius: 9,
          fontSize: 12.5, zIndex: 3000, boxShadow: '0 8px 24px rgba(0,0,0,.22)',
        }}>{toast}</div>
      )}
    </div>
  )
}
