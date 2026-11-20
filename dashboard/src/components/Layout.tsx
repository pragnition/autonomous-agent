import { Activity, ChartLine, Gauge, LayoutDashboard, ListTree, Receipt } from "lucide-react"
import { NavLink, Outlet } from "react-router"
import { useMe } from "@/api"
import { AgentState } from "@/components/AgentState"
import { KillSwitch } from "@/components/KillSwitch"
import { ThemeToggle } from "@/components/ThemeToggle"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/sessions", label: "Sessions", icon: ListTree, end: false },
  { to: "/accounts", label: "Accounts", icon: Gauge, end: false },
  { to: "/foundry", label: "Foundry spend", icon: Receipt, end: false },
  { to: "/kpis", label: "KPIs", icon: ChartLine, end: false },
  { to: "/activity", label: "Activity", icon: Activity, end: false },
]

function navClass({ isActive }: { isActive: boolean }) {
  return cn("flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors",
    isActive ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground")
}

export function Layout() {
  const { data: me } = useMe()
  const name = me?.agent ?? "Agent"
  return (
    <div className="flex min-h-svh">
      <aside className="sticky top-0 hidden h-svh w-56 shrink-0 flex-col border-r bg-sidebar md:flex">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <img src="/favicon.svg" alt="" className="size-7" />
          <div className="min-w-0 leading-tight">
            <div className="truncate text-sm font-semibold capitalize">{name}</div>
            <div className="text-xs text-muted-foreground">Agent dashboard</div>
          </div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2" aria-label="Main">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={navClass}>
              <Icon className="size-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto flex items-center gap-2 border-t px-3 py-3">
          <div className="min-w-0 flex-1 leading-tight">
            <div className="truncate text-sm">{me?.name ?? "…"}</div>
            {me && <Badge variant="outline" className="mt-1 text-[10px]">{me.role ?? "viewer"}</Badge>}
          </div>
          <ThemeToggle />
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b bg-background/85 px-4 py-2.5 backdrop-blur md:px-8">
          <nav className="-mx-1 flex min-w-0 flex-1 gap-1 overflow-x-auto md:hidden" aria-label="Main">
            {NAV.map(({ to, label, end }) => (
              <NavLink key={to} to={to} end={end} className={navClass}>{label}</NavLink>
            ))}
          </nav>
          <div className="hidden flex-1 md:block" />
          <AgentState />
          <KillSwitch />
          <div className="md:hidden"><ThemeToggle /></div>
        </header>
        <main className="flex-1">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
