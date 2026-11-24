import "@xterm/xterm/css/xterm.css"
import { FitAddon } from "@xterm/addon-fit"
import { Terminal as XTerm } from "@xterm/xterm"
import { RotateCcw } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { query } from "@/api"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** A shell in a container, through agentd's /api/workloads/shell websocket. Input goes as {"i": text}, a resize as
 * {"r": [cols, rows]}; output comes back as binary frames. */
export function Terminal({ kind, target, className }: { kind: "container" | "services" | "app"; target: string; className?: string }) {
  const host = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<"connecting" | "open" | "closed">("connecting")
  const [generation, setGeneration] = useState(0)

  useEffect(() => {
    if (!host.current) return
    const dark = document.documentElement.classList.contains("dark")
    const term = new XTerm({
      cursorBlink: true, fontSize: 12.5, scrollback: 5000, convertEol: false,
      fontFamily: '"JetBrains Mono Variable", ui-monospace, SFMono-Regular, Menlo, monospace',
      theme: dark ? { background: "#0f1115", foreground: "#e6e6e6" } : { background: "#fbfbfa", foreground: "#1d1d1b", cursor: "#1d1d1b", selectionBackground: "#cfe0f7" },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current)
    fit.fit()
    const proto = location.protocol === "https:" ? "wss" : "ws"
    const ws = new WebSocket(`${proto}://${location.host}/api/workloads/shell${query({ kind, target })}`)
    ws.binaryType = "arraybuffer"
    const send = (v: unknown) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(v))
    const resize = () => {
      try { fit.fit() } catch { /* hidden */ }
      send({ r: [term.cols, term.rows] })
    }
    ws.onopen = () => {
      setState("open")
      resize()
      term.focus()
    }
    ws.onmessage = (e) => term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data as ArrayBuffer))
    ws.onclose = (e) => {
      setState("closed")
      if (e.code !== 1000 && e.code !== 1005) term.write(`\r\n[closed${e.reason ? `: ${e.reason}` : ""}]\r\n`)
    }
    const input = term.onData((d) => send({ i: d }))
    const observer = new ResizeObserver(() => resize())
    observer.observe(host.current)
    return () => {
      observer.disconnect()
      input.dispose()
      ws.close()
      term.dispose()
    }
  }, [kind, target, generation])

  return (
    <div className={cn("flex min-h-0 flex-col overflow-hidden rounded-lg border", className)}>
      <div className="flex items-center gap-2 border-b px-2 py-1.5 text-xs">
        <span className={cn("inline-flex items-center gap-1.5", state === "open" ? "text-[var(--status-good)]" : "text-muted-foreground")}>
          <span className="size-1.5 rounded-full bg-current" />{state === "open" ? "connected" : state}
        </span>
        <span className="text-muted-foreground">as the agent user · everything you type runs with the agent's credentials</span>
        {state === "closed" && (
          <Button size="xs" variant="outline" className="ml-auto" onClick={() => { setState("connecting"); setGeneration((g) => g + 1) }}>
            <RotateCcw /> New shell
          </Button>
        )}
      </div>
      <div ref={host} className="min-h-0 flex-1 p-1.5" />
    </div>
  )
}
