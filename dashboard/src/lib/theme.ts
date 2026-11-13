export type Theme = "system" | "light" | "dark"

const KEY = "dashboard.theme"
const media = () => window.matchMedia("(prefers-color-scheme: dark)")

export function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(KEY)
    return value === "light" || value === "dark" ? value : "system"
  } catch {
    return "system"
  }
}

export function applyTheme(theme: Theme) {
  const dark = theme === "dark" || (theme === "system" && media().matches)
  document.documentElement.classList.toggle("dark", dark)
  document.documentElement.dataset.theme = dark ? "dark" : "light"
}

export function setTheme(theme: Theme) {
  try {
    if (theme === "system") localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, theme)
  } catch {
    // private windows and blocked storage: the choice lasts for this page only
  }
  applyTheme(theme)
}

export function initTheme() {
  applyTheme(storedTheme())
  media().addEventListener("change", () => applyTheme(storedTheme()))
}
