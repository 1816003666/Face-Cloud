import { Routes, Route, Navigate } from 'react-router-dom'
import { createElement, lazy, Suspense } from 'react'
import Layout from './components/Layout.jsx'
import ProtectedRoute from './components/ProtectedRoute.jsx'
import Login from './views/Login.jsx'
import { getLoadedRouteModule, routeModules } from './lib/routePrefetch.js'
import { useAuth } from './store/auth.jsx'

// 登录页是未登录状态下的入口，直接放进主包，避免首屏再等一轮 chunk 请求。
const Register = lazy(() => import('./views/Register.jsx'))
const EnterpriseLogin = lazy(() => import('./views/EnterpriseLogin.jsx'))
const ForgotPassword = lazy(() => import('./views/ForgotPassword.jsx'))
const DeviceControl = lazy(() => import('./views/DeviceControl.jsx'))

const lazyRoutes = Object.fromEntries(
  Object.entries(routeModules).map(([path, load]) => [path, lazy(load)])
)

/**
 * 平台专属页面守卫：企业管理员本质是"普通用户"，直接用 URL 访问
 * 计费/告警/审计/线路/设置/用户/企业列表 时退回看板，避免打开一个全是 403 的空页面。
 */
function PlatformOnly({ children }) {
  const { user } = useAuth()
  const isPlatform = ['admin', 'superadmin'].includes(user?.role) && !user?.enterprise
  if (!isPlatform) return <Navigate to="/dashboard" replace />
  return children
}

function PageLoading() {
  return (
    <div className="page-loading app-loading" role="status" aria-live="polite">
      <span className="app-loading-mark" aria-hidden="true" />
      <span>正在加载…</span>
    </div>
  )
}

function SuspendedPage({ children }) {
  return <Suspense fallback={<PageLoading />}>{children}</Suspense>
}

/**
 * 已经预取完成的模块直接同步渲染；尚未完成时交给 React.lazy。
 * 这样首访页面时只会在内容区等待，不会再替换整个应用外壳。
 */
function RouteView({ path }) {
  const module = getLoadedRouteModule(path)
  const Component = module?.default
  return Component ? <Component /> : createElement(lazyRoutes[path])
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/enterprise-login" element={<SuspendedPage><EnterpriseLogin /></SuspendedPage>} />
      <Route path="/register" element={<SuspendedPage><Register /></SuspendedPage>} />
      <Route path="/forgot-password" element={<SuspendedPage><ForgotPassword /></SuspendedPage>} />
      {/* 全屏远程控制（无侧边栏） */}
      <Route path="/devices/:id/control" element={
        <ProtectedRoute>
          <SuspendedPage><DeviceControl /></SuspendedPage>
        </ProtectedRoute>
      } />
      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route path="/dashboard" element={<RouteView path="/dashboard" />} />
        <Route path="/devices" element={<RouteView path="/devices" />} />
        <Route path="/tasks" element={<RouteView path="/tasks" />} />
        <Route path="/files" element={<RouteView path="/files" />} />
        <Route path="/scripts" element={<RouteView path="/scripts" />} />
        <Route path="/groups" element={<RouteView path="/groups" />} />
        {/* 以下页面仅平台账号可进（Layout 里也隐藏了菜单，这里再兜一层） */}
        <Route path="/users" element={<PlatformOnly><RouteView path="/users" /></PlatformOnly>} />
        <Route path="/audit" element={<PlatformOnly><RouteView path="/audit" /></PlatformOnly>} />
        <Route path="/alerts" element={<PlatformOnly><RouteView path="/alerts" /></PlatformOnly>} />
        <Route path="/settings" element={<PlatformOnly><RouteView path="/settings" /></PlatformOnly>} />
        <Route path="/diagnostics" element={<PlatformOnly><RouteView path="/diagnostics" /></PlatformOnly>} />
        <Route path="/billing" element={<PlatformOnly><RouteView path="/billing" /></PlatformOnly>} />
        {<Route path="/org" element={<RouteView path="/org" />} />}
        <Route path="/enterprises" element={<PlatformOnly><RouteView path="/enterprises" /></PlatformOnly>} />
        <Route path="/vpc-lines" element={<PlatformOnly><RouteView path="/vpc-lines" /></PlatformOnly>} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
