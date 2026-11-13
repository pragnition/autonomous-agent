import { Route, Routes } from "react-router"
import { Layout } from "@/components/Layout"
import { Accounts } from "@/pages/Accounts"
import { Activity } from "@/pages/Activity"
import { Foundry } from "@/pages/Foundry"
import { Kpis } from "@/pages/Kpis"
import { NotFound } from "@/pages/NotFound"
import { Overview } from "@/pages/Overview"
import { SessionDetail } from "@/pages/SessionDetail"
import { Sessions } from "@/pages/Sessions"

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="sessions" element={<Sessions />} />
        <Route path="sessions/:id" element={<SessionDetail />} />
        <Route path="accounts" element={<Accounts />} />
        <Route path="foundry" element={<Foundry />} />
        <Route path="kpis" element={<Kpis />} />
        <Route path="activity" element={<Activity />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
