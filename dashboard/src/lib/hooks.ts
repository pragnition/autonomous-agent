import { useEffect, useState } from "react"

/** The current time, refreshed every `ms`: for elapsed times that tick between polls. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}
