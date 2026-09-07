import type { ReactNode } from 'react'

type ConsolePageHeaderProps = {
  title: string
  description?: string
  actions?: ReactNode
  children?: ReactNode
}

export function ConsolePageHeader({ title, description, actions, children }: ConsolePageHeaderProps) {
  return <header className="console-page-header">
    <div className="console-page-heading"><h1>{title}</h1>{description && <p>{description}</p>}{children}</div>
    {actions && <div className="console-page-actions">{actions}</div>}
  </header>
}

export default ConsolePageHeader
