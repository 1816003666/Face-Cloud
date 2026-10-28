import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../store/auth.jsx'
import { api } from '../api/client.js'

const STORY = [
  { icon: 'M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z M9 12l2 2 4-4', title: '按需开通', desc: '注册即可试用，套餐随时升降配' },
  { icon: 'M5 2h14a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm7 18h.01', title: '真机云端', desc: 'Android 容器直连，扫码即用' },
  { icon: 'M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11', title: '自动化任务', desc: '脚本编排、批量执行、轮次运行' },
]

/**
 * 注册页（与登录页同一套视觉骨架）。
 */
export default function Register() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const canSubmit = username.trim() && password && confirm && !busy

  async function handleSubmit(e) {
    e.preventDefault()
    setErr('')
    if (username.trim().length < 1) { setErr('用户名必填'); return }
    if (password.length < 6) { setErr('密码至少 6 位'); return }
    if (!/[a-zA-Z]/.test(password) || !/\d/.test(password)) {
      setErr('密码须同时包含字母和数字')
      return
    }
    if (password !== confirm) { setErr('两次密码不一致'); return }

    setBusy(true)
    try {
      const { data } = await api.register(username.trim(), password)
      localStorage.setItem('token', data.access_token)
      localStorage.setItem('user', JSON.stringify(data.user))
      await login(username.trim(), password)
      navigate('/dashboard')
    } catch (e2) {
      setErr(e2.response?.data?.error || '注册失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <aside className="login-story" aria-label="注册说明">
        <div className="login-story-brand">
          <span className="login-logo" aria-hidden="true" />
          <div className="login-story-brand-text">
            <strong>薪约云手机</strong>
            <span>CLOUD DEVICE PLATFORM</span>
          </div>
        </div>

        <div className="login-story-main">
          <h1>注册账号<br /><span>创建后即可开通云手机</span></h1>
          <p className="login-story-description">
            注册账号 → 选一个套餐 → 打开网页就能用。无需装模拟器，也不用管服务器。
          </p>
          <div className="login-feature-list">
            {STORY.map((f) => (
              <div className="login-feature" key={f.title}>
                <span className="login-feature-icon">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
                    strokeLinecap="round" strokeLinejoin="round"><path d={f.icon} /></svg>
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
          <span><i />云端设备管理平台</span>
          <span>© 2026</span>
        </div>
      </aside>

      <main className="login-panel">
        <div className="login-panel-inner">
          <div className="login-mobile-brand">
            <span className="login-logo" aria-hidden="true" />
            <div>
              <strong>薪约云手机</strong>
              <span>CLOUD DEVICE PLATFORM</span>
            </div>
          </div>

          <form className="login-card" onSubmit={handleSubmit}>
            <div className="login-card-head">
              <p className="login-kicker"><span aria-hidden="true" />创建账号</p>
              <h2>注册新账号</h2>
              <p>填写账号与密码即可开始，企业账号请让企业管理员分配。</p>
            </div>

            <label className="login-field">
              <span className="login-field-label">用户名</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z" />
                </svg>
                <input value={username} onChange={(e) => setUsername(e.target.value)}
                  placeholder="登录用的用户名" autoComplete="username" />
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
                  placeholder="至少 6 位，需含字母和数字" autoComplete="new-password" />
              </span>
            </label>

            <label className="login-field">
              <span className="login-field-label">确认密码</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
                <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                  placeholder="再输入一次密码" autoComplete="new-password" />
              </span>
            </label>

            <label className="login-remember">
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
              <span className="login-checkbox" aria-hidden="true" />
              注册后保持登录状态
            </label>

            {err && (
              <div className="login-alert" role="alert">
                <span className="login-alert-icon" aria-hidden="true">!</span>
                <span>{err}</span>
              </div>
            )}

            <button className="login-submit" type="submit" disabled={!canSubmit}>
              {busy ? (<><span className="login-spinner" aria-hidden="true" />创建中…</>)
                : (<>注册并登录<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14 M13 6l6 6-6 6" /></svg></>)}
            </button>

            <div className="login-footer-actions">
              <span>已有账号？</span>
              <Link className="login-link" to="/login">去登录</Link>
              <span className="login-action-divider" aria-hidden="true" />
              <Link className="login-link login-link-muted" to="/enterprise-login">企业版入口</Link>
            </div>
          </form>

          <p className="login-service-note">
            注册即表示同意平台服务条款；企业成员账号由所在企业管理员创建。
          </p>
        </div>
      </main>
    </div>
  )
}
