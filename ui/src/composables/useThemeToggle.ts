import { computed } from "vue";
import { useTheme } from "vuetify";
import { DARK_THEME, LIGHT_THEME } from "../plugins/vuetify";

const STORAGE_KEY = "autodesk:theme";

let restored = false;

function readStoredTheme(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function prefersLight(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: light)").matches;
}

export function useThemeToggle() {
  const theme = useTheme();

  const isDark = computed(() => theme.global.current.value.dark);

  const current = computed(() => (isDark.value ? DARK_THEME : LIGHT_THEME));

  function applyTheme(name: string) {
    theme.change(name);
  }

  function storeTheme(name: string) {
    try {
      window.localStorage.setItem(STORAGE_KEY, name);
    } catch {
      // no storage: theme still applies for this session
    }
  }

  function toggleTheme() {
    const next = isDark.value ? LIGHT_THEME : DARK_THEME;
    applyTheme(next);
    storeTheme(next);
  }

  if (!restored) {
    restored = true;
    const stored = readStoredTheme();
    if (stored === DARK_THEME || stored === LIGHT_THEME) {
      applyTheme(stored);
    } else if (prefersLight()) {
      applyTheme(LIGHT_THEME);
    }
  }

  return { theme, isDark, current, applyTheme, toggleTheme };
}
