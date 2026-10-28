import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../store/auth.jsx'
import { api } from '../api/client.js'

const FEATURES = [
  { icon: 'M4 5h16v6H4z M4 13h10v6H4z M16 13h4v6h-4z', title: '企业级数据隔离', desc: '部门、成员、设备、订单各归各企业' },
  { icon: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', title: '部门与角色', desc: '按部门分权，成员按角色授予功能' },
  { icon: 'M12 20h9 M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z', title: '统一设备运营', desc: '批量创建、远程控制、轮次运行' },
  { icon: 'M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z M9 12l2 2 4-4', title: '安全与审计', desc: '操作留痕，越权访问自动拦截' },
]

/**
 * 企业版登录页。
 *
 * 与个人版共用同一套视觉（.login-shell 青绿主题），
 * 只是多一个「企业编码」字段，避免同名账号串企业。
 */
export default function EnterpriseLogin() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [code, setCode] = useState(() => {
    try {
      return localStorage.getItem('ent_code') || ''
    } catch {
      return ''
    }
  })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [entName, setEntName] = useState('')

  const canSubmit = useMemo(
    () => code.trim() && username.trim() && password && !busy,
    [code, username, password, busy],
  )

  async function lookup(name) {
    const c = (name ?? code).trim()
    if (!c) {
      setEntName('')
      return
    }
    try {
      const { data } = await api.enterpriseLookup(c)
      setEntName(data?.name || '')
    } catch {
      setEntName('')
    }
  }

  async function handleSubmit(e) {
    e.preventDefault()
    if (!canSubmit) return
    setErr('')
    setBusy(true)
    try {
      await login(username.trim(), password, { enterprise_code: code.trim().toUpperCase() })
      try {
        if (remember) localStorage.setItem('ent_code', code.trim().toUpperCase())
        else localStorage.removeItem('ent_code')
      } catch { /* 忽略 */ }
      navigate('/dashboard')
    } catch (e2) {
      const d = e2.response?.data || {}
      setErr(d.error || '登录失败，请检查企业编码与账号密码')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <aside className="login-story" aria-label="企业版能力介绍">
        <div className="login-story-brand">
          <span className="login-logo" aria-hidden="true" />
          <div className="login-story-brand-text">
            <strong>薪约云手机</strong>
            <span>ENTERPRISE CONSOLE</span>
          </div>
        </div>

        <div className="login-story-main">
          <h1>
            企业云手机控制台
            <br />
            <span>部门、成员与设备统一管理</span>
          </h1>
          <p className="login-story-description">
            每个企业一套独立数据：部门、成员、设备、套餐与订单互不可见。
            管理员可按部门与角色下发权限，成员登录即可上手。
          </p>

          <div className="login-feature-list">
            {FEATURES.map((f) => (
              <div className="login-feature" key={f.title}>
                <span className="login-feature-icon">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
                    strokeLinecap="round" strokeLinejoin="round">
                    <path d={f.icon} />
                  </svg>
                </span>
                <div className="login-feature-copy">
                  <strong>{f.title}</strong>
                  <span>{f.desc}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="login-story-footer">
          <span><i />企业数据独立隔离</span>
          <span>© 2026</span>
        </div>
      </aside>

      <main className="login-panel">
        <div className="login-panel-inner">
          <div className="login-mobile-brand">
            <span className="login-logo" aria-hidden="true" />
            <div>
              <strong>薪约云手机</strong>
              <span>ENTERPRISE CONSOLE</span>
            </div>
          </div>

          <form className="login-card" onSubmit={handleSubmit}>
            <div className="login-card-head">
              <p className="login-kicker"><span aria-hidden="true" />企业版登录</p>
              <h2>企业控制台</h2>
              <p>请输入企业编码与你的账号，进入所在企业的管理后台。</p>
            </div>

            <label className="login-field">
              <span className="login-field-label">企业编码</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 21h18 M6 21V7l6-4 6 4v14 M9 9h.01 M15 9h.01 M9 13h.01 M15 13h.01" />
                </svg>
                <input
                  value={code}
                  onChange={(e) => { setCode(e.target.value); setEntName('') }}
                  onBlur={(e) => lookup(e.target.value)}
                  placeholder="如 SY-001，向平台管理员索取"
                  autoComplete="organization"
                />
              </span>
              {entName && <span className="login-field-label" style={{ color: 'var(--login-teal-dark)' }}>{entName}</span>}
            </label>

            <label className="login-field">
              <span className="login-field-label">账号</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z" />
                </svg>
                <input value={username} onChange={(e) => setUsername(e.target.value)}
                  placeholder="请输入企业账号" autoComplete="username" />
              </span>
            </label>

            <label className="login-field">
              <span className="login-field-label">密码</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4" />
                </svg>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                  placeholder="请输入登录密码" autoComplete="current-password" />
              </span>
            </label>

            <label className="login-remember">
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
              <span className="login-checkbox" aria-hidden="true" />
              记住企业编码与登录状态
            </label>

            {err && (
              <div className="login-alert" role="alert">
                <span className="login-alert-icon" aria-hidden="true">!</span>
                <span>{err}</span>
              </div>
            )}

            <button className="login-submit" type="submit" disabled={!canSubmit}>
              {busy ? (<><span className="login-spinner" aria-hidden="true" />登录中…</>)
                : (<>进入企业控制台<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14 M13 6l6 6-6 6" /></svg></>)}
            </button>

            <div className="login-footer-actions">
              <span>个人用户？</span>
              <Link className="login-link" to="/login">返回个人版登录</Link>
              <span className="login-action-divider" aria-hidden="true" />
              <Link className="login-link login-link-muted" to="/forgot-password">找回密码</Link>
            </div>
          </form>

          <p className="login-service-note">
            企业编码由平台管理员开通企业时生成，忘记可联系平台运营。
          </p>
        </div>
      </main>
    </div>
  )
}
