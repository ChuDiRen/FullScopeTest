import { useEffect, useState } from 'react'

/** 与 styles/responsive.css 的 mobile 断点保持一致：< 768px 视为移动端 */
const MOBILE_QUERY = '(max-width: 767px)'

/**
 * 移动端断点侦听（matchMedia）。
 * 用于布局层（MainLayout 抽屉侧边栏、顶栏收纳）的条件渲染，
 * 纯样式响应仍走 responsive.css 的 media query。
 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(
    () => typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches
  )

  useEffect(() => {
    const mql = window.matchMedia(MOBILE_QUERY)
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches)
    mql.addEventListener('change', onChange)
    setIsMobile(mql.matches)
    return () => mql.removeEventListener('change', onChange)
  }, [])

  return isMobile
}
