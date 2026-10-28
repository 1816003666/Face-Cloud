import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../store/auth.jsx'
import { prefetchRoute, prefetchRouteData } from '../lib/routePrefetch.js'

const REMEMBER_KEY = 'cp_login_remember'
const b64encode = (s) => btoa(String.fromCharCode(...new TextEncoder().encode(s)))
const b64decode = (b) => new TextDecoder().decode(Uint8Array.from(atob(b), (c) => c.charCodeAt(0)))

const FEATURES = [
  ['设备集中管理', '百台云手机统一监控与管理', 'M4 5.5h16v11H4z M8 20h8 M9 8.5h6 M9 12h4'],
  ['实时设备操控', '高清投屏，远程操作如临其境', 'M8 5l11 7-11 7z M4 5v14'],
  ['任务批量调度', '批量任务一键下发，轮次执行', 'M12 7v5l3 2 M5 12a7 7 0 1 0 2-4.9 M5 4v4h4'],
  ['统一安全管理', '账号权限与设备操作统一管控', 'M12 3l7 3v5c0 4.6-2.9 8-7 10-4.1-2-7-5.4-7-10V6z M9 12l2 2 4-4'],
]

function loadRemembered() {
  try {
    const raw = localStorage.getItem(REMEMBER_KEY)
    if (!raw) return null
    const obj = JSON.parse(b64decode(raw))
    if (obj && typeof obj.u === 'string' && typeof obj.p === 'string') return obj
  } catch { /* ignore */ }
  return null
}

function UserIcon() {
  return (
    <svg
      className="login-field-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="8" r="4" />
      <path d="M4.5 21a7.5 7.5 0 0115 0" />
    </svg>
  )
}

function LockIcon() {
  return (
    <svg
      className="login-field-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="10" width="16" height="11" rx="3" />
      <path d="M8 10V7a4 4 0 018 0v3 M12 14v3" />
    </svg>
  )
}

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [captcha, setCaptcha] = useState(null)   // {captcha_id, question}
  const [captchaAns, setCaptchaAns] = useState('')
  const [remember, setRemember] = useState(false)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const saved = loadRemembered()
    if (saved) {
      setUsername(saved.u)
      setPassword(saved.p)
      setRemember(true)
    }
  }, [])

  async function handleSubmit(e) {
    e.preventDefault()
    if (loading) return
    setErr('')
    setLoading(true)
    try {
      await login(username, password, captcha ? { captcha_id: captcha.captcha_id, captcha: captchaAns } : {})
      if (remember) {
        localStorage.setItem(REMEMBER_KEY, b64encode(JSON.stringify({ u: username, p: password })))
      } else {
        localStorage.removeItem(REMEMBER_KEY)
      }
      const redirect = params.get('redirect') || '/dashboard'
      const redirectPath = redirect.split('?')[0]
      prefetchRoute(redirectPath).catch(() => {})
      prefetchRouteData(redirectPath)
      navigate(redirect)
    } catch (e) {
      const data = e.response?.data
      setErr(data?.error || '登录失败')
      if (data?.need_captcha && data?.captcha) {
        setCaptcha(data.captcha)
        setCaptchaAns('')
      }
      if (e.response?.status === 423) {
        setCaptcha(null)
        setCaptchaAns('')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-shell">
      <aside className="login-story" aria-label="产品能力介绍">
        <div className="login-story-brand">
          <span className="login-logo" aria-hidden="true" />
          <div className="login-story-brand-text">
            <strong>薪约云手机</strong>
            <span>CLOUD DEVICE PLATFORM</span>
          </div>
        </div>

        <div className="login-story-main">
          <h1>云手机管理平台<br /><span>多服务器设备统一纳管</span></h1>
          <p className="login-story-description">
            统一的云端设备工作台，让批量管理、实时操控与自动化任务更加高效。
          </p>
          <div className="login-feature-list">
            {FEATURES.map(([title, desc, path], i) => (
              <div className="login-feature" key={i}>
                <span className="login-feature-icon">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d={path} />
                  </svg>
                </span>
                <div className="login-feature-copy">
                  <strong>{title}</strong>
                  <span>{desc}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="login-story-footer">
          <span><i aria-hidden="true" />云端设备管理平台</span>
          <span>© 2026</span>
        </div>
      </aside>

      <main className="login-panel">
        <div className="login-panel-inner">
          <div className="login-mobile-brand">
            <span className="login-logo" aria-hidden="true" />
            <div>
              <strong>薪约云手机</strong>
              <span>云端设备管理平台</span>
            </div>
          </div>

          <form className="login-card" onSubmit={handleSubmit}>
            <div className="login-card-head">
              <div className="login-kicker"><span aria-hidden="true" />账号登录</div>
              <h2>登录</h2>
              <p>用账号密码进入控制台</p>
            </div>

            {err && (
              <div className="err login-alert" role="alert">
                <span className="login-alert-icon" aria-hidden="true">!</span>
                <span>{err}</span>
              </div>
            )}

            <label className="login-field">
              <span className="login-field-label">账号</span>
              <div className="login-input-wrap">
                <UserIcon />
                <input
                  placeholder="请输入用户名"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </div>
            </label>

            <label className="login-field">
              <span className="login-field-label">密码</span>
              <div className="login-input-wrap">
                <LockIcon />
                <input
                  type="password"
                  placeholder="请输入登录密码"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            </label>

            <label className="login-remember">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => {
                  const checked = e.target.checked
                  setRemember(checked)
                  if (!checked) localStorage.removeItem(REMEMBER_KEY)
                }}
              />
              <span className="login-checkbox" aria-hidden="true" />
              <span>记住登录信息</span>
            </label>

            {captcha && (
              <div className="login-captcha">
                <div className="login-captcha-prompt">
                  <span>安全验证</span>
                  <strong className="captcha-q">{captcha.question}</strong>
                </div>
                <div className="login-input-wrap login-captcha-input">
                  <span className="login-captcha-mark" aria-hidden="true">#</span>
                  <input
                    placeholder="请输入验证码答案"
                    value={captchaAns}
                    onChange={(e) => setCaptchaAns(e.target.value)}
                  />
                </div>
              </div>
            )}

            <button type="submit" className="login-submit" disabled={loading}>
              {loading && <span className="login-spinner" aria-hidden="true" />}
              <span>{loading ? '正在进入…' : '登录到控制台'}</span>
              {!loading && (
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M5 12h14 M13 6l6 6-6 6" />
                </svg>
              )}
            </button>

            <div className="login-footer-actions">
              <span>
                还没有账号？
                <Link to="/register" className="login-link">立即注册</Link>
              </span>
              <span className="login-action-divider" aria-hidden="true" />
              <Link to="/forgot-password" className="login-link login-link-muted">找回密码</Link>
            </div>
          </form>

          {/* 企业用户入口：跳到企业版登录页（要求填写企业编码） */}
          <Link className="login-switch-ent" to="/enterprise-login">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M3 21h18 M6 21V7l6-4 6 4v14 M9 9h.01 M15 9h.01 M9 13h.01 M15 13h.01" />
            </svg>
            <span>企业用户？<strong>登录企业版</strong></span>
          </Link>

          <p className="login-service-note">批量设备管理 · 实时操控 · 任务调度</p>
        </div>
      </main>
    </div>
  )
}
