import { useState, useRef } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client.js'

const STORY = [
  { icon: 'M4 4h16v16H4z M8 10h8 M8 14h5', title: '验证码找回', desc: '输入账号获取验证码，10 分钟内有效' },
  { icon: 'M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z M9 12l2 2 4-4', title: '安全重置', desc: '新密码需含字母与数字，立即生效' },
  { icon: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', title: '个人与企业通用', desc: '企业账号同样可用，找回后可继续登录企业版' },
]

/**
 * 找回密码页（与登录页同一套视觉骨架）。
 *
 * 流程：填账号 → 获取验证码 → 填新密码 → 重置。
 */
export default function ForgotPassword() {
  const [username, setUsername] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [err, setErr] = useState('')
  const [okMsg, setOkMsg] = useState('')
  const [sent, setSent] = useState(false)         // 验证码是否已发送
  const [debugCode, setDebugCode] = useState('')  // 测试环境直接展示的验证码
  const [cooldown, setCooldown] = useState(0)     // 重发倒计时（秒）
  const timerRef = useRef(null)

  async function handleSendCode() {
    setErr('')
    if (!username.trim()) return setErr('请先填写用户名')
    try {
      const { data } = await api.forgotPassword(username.trim())
      setSent(true)
      setDebugCode(data.debug_code || '')
      setOkMsg(data.note || '验证码已发送')
      startCooldown(60)
    } catch (e) {
      const msg = e.response?.data?.error || '获取验证码失败'
      if (e.response?.status === 429) startCooldown(60)
      setErr(msg)
    }
  }

  function startCooldown(sec) {
    setCooldown(sec)
    clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setCooldown((c) => {
        if (c <= 1) {
          clearInterval(timerRef.current)
          return 0
        }
        return c - 1
      })
    }, 1000)
  }

  async function handleReset(e) {
    e.preventDefault()
    setErr('')
    setOkMsg('')
    if (!username.trim() || !code.trim()) return setErr('请填写用户名和验证码')
    if (password.length < 6) return setErr('新密码至少 6 位')
    if (!/[a-zA-Z]/.test(password) || !/\d/.test(password))
      return setErr('新密码须同时包含字母和数字')
    if (password !== confirm) return setErr('两次密码不一致')
    try {
      const { data } = await api.resetPassword(username.trim(), code.trim(), password)
      setOkMsg(data.message || '密码已重置')
      setSent(false)
      setCode('')
      setPassword('')
      setConfirm('')
    } catch (e) {
      setErr(e.response?.data?.error || '重置失败')
    }
  }

  const canSubmit = username.trim() && code.trim() && password && confirm

  return (
    <div className="login-shell">
      <aside className="login-story" aria-label="找回密码说明">
        <div className="login-story-brand">
          <span className="login-logo" aria-hidden="true" />
          <div className="login-story-brand-text">
            <strong>薪约云手机</strong>
            <span>CLOUD DEVICE PLATFORM</span>
          </div>
        </div>

        <div className="login-story-main">
          <h1>重置登录密码<br /><span>验证账号后设置新密码</span></h1>
          <p className="login-story-description">
            填账号 → 拿验证码 → 设新密码。整个过程不到一分钟，重置后立刻可以登录。
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

          <form className="login-card" onSubmit={handleReset}>
            <div className="login-card-head">
              <p className="login-kicker"><span aria-hidden="true" />找回密码</p>
              <h2>重置登录密码</h2>
              <p>填写账号获取验证码，验证通过后即可设置新密码。</p>
            </div>

            <label className="login-field">
              <span className="login-field-label">账号</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z" />
                </svg>
                <input value={username} onChange={(e) => setUsername(e.target.value)}
                  placeholder="注册时使用的用户名" autoComplete="username" />
              </span>
            </label>

            <label className="login-field">
              <span className="login-field-label">验证码</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 6h16v12H4z M7 10h4 M7 14h7" />
                </svg>
                <input className="login-input-code" value={code} onChange={(e) => setCode(e.target.value)}
                  placeholder="6 位验证码" autoComplete="one-time-code" />
                <button type="button" className="login-code-btn" onClick={handleSendCode}
                  disabled={cooldown > 0}>
                  {cooldown > 0 ? `${cooldown}s 后重发` : '获取验证码'}
                </button>
              </span>
            </label>

            {sent && debugCode && (
              <div className="login-code-notice">
                验证码：<b>{debugCode}</b>（10 分钟内有效）
              </div>
            )}

            <label className="login-field">
              <span className="login-field-label">新密码</span>
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
              <span className="login-field-label">确认新密码</span>
              <span className="login-input-wrap">
                <svg className="login-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
                <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                  placeholder="再输入一次新密码" autoComplete="new-password" />
              </span>
            </label>

            {err && (
              <div className="login-alert" role="alert">
                <span className="login-alert-icon" aria-hidden="true">!</span>
                <span>{err}</span>
              </div>
            )}
            {okMsg && (
              <div className="login-ok" role="status">
                <span className="login-ok-icon" aria-hidden="true"></span>
                <span>{okMsg}</span>
              </div>
            )}

            <button className="login-submit" type="submit" disabled={!canSubmit}>
              重置密码
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M5 12h14 M13 6l6 6-6 6" />
              </svg>
            </button>

            <div className="login-footer-actions">
              <span>想起密码了？</span>
              <Link className="login-link" to="/login">去登录</Link>
              <span className="login-action-divider" aria-hidden="true" />
              <Link className="login-link login-link-muted" to="/register">去注册</Link>
              <span className="login-action-divider" aria-hidden="true" />
              <Link className="login-link login-link-muted" to="/enterprise-login">企业版入口</Link>
            </div>
          </form>

          <p className="login-service-note">
            验证码 10 分钟内有效；连续获取有 60 秒间隔限制。
          </p>
        </div>
      </main>
    </div>
  )
}
