import { createContext, useContext, useEffect, useState } from 'react'
import { api } from '../api/client.js'

const AuthContext = createContext(null)

function readStoredUser() {
  try {
    return JSON.parse(localStorage.getItem('user') || 'null')
  } catch {
    return null
  }
}

/**
 * 权限判断 hook：平台账号直通全部；企业账号用后端 /auth/me 返回的有效权限。
 * 用法：const can = useCan(); can('device.create')
 */
export function useCan() {
  const { user } = useAuth()
  const perms = Array.isArray(user?.perms) ? user.perms : []
  const isPlatformAdmin = !!user?.is_platform && ['admin', 'superadmin'].includes(user?.role)
  return (code) => {
    if (!code) return true
    if (isPlatformAdmin) return true
    return perms.includes(code)
  }
}

export function AuthProvider({ children }) {
  const initialUser = readStoredUser()
  const initialToken = localStorage.getItem('token') || ''
  const [user, setUser] = useState(initialUser)
  const [token, setToken] = useState(initialToken)
  // 已缓存 token + user 时直接放行首帧；/auth/me 在校验失败后再登出。
  // 旧逻辑无论本地登录态是否完整都要等一次 effect，刷新页面必然闪一下全屏加载态。
  const [ready, setReady] = useState(() => !initialToken || !!initialUser)

  // 启动时校验 token，并在本地用户信息缺失时补拉 /auth/me。
  useEffect(() => {
    if (!token) return undefined
    let alive = true
    api.me()
      .then((r) => {
        if (!alive) return
        setUser(r.data)
        localStorage.setItem('user', JSON.stringify(r.data))
      })
      .catch((e) => {
        if (alive && e.response?.status === 401) logout()
      })
      .finally(() => {
        if (alive) setReady(true)
      })
    return () => { alive = false }
  }, [])

  // 监听后端 401 触发的登出事件
  useEffect(() => {
    const onLogout = () => logout()
    window.addEventListener('auth:logout', onLogout)
    return () => window.removeEventListener('auth:logout', onLogout)
  }, [])

  async function login(username, password, extra = {}) {
    const { data } = await api.login(username, password, extra)
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('user', JSON.stringify(data.user))
    setToken(data.access_token)
    setUser(data.user)
    return data.user
  }

  function logout() {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    setToken('')
    setUser(null)
  }

  const isAuthed = !!token && !!user

  // 注意：路由 chunk 的预热交给 Layout（按角色只预热常用页）。
  // 这里以前是 prefetchAllRoutes() 无参调用 = 登录后把 14 个页面全下下来，
  // 个人用户首屏会白拉「用户管理/企业列表/计费/设置」约 450KB。

  return (
    <AuthContext.Provider value={{ user, token, isAuthed, ready, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
