import { Link } from "react-router"
import { Page } from "@/components/Page"

export function NotFound() {
  return (
    <Page title="Not found">
      <p className="text-muted-foreground">
        Nothing lives here. <Link className="underline underline-offset-4" to="/">Back to the overview</Link>.
      </p>
    </Page>
  )
}
