import { computed } from "vue";
import { useTheme } from "vuetify";
import { DARK_THEME, LIGHT_THEME } from "../plugins/vuetify";

const STORAGE_KEY = "adclicker:theme";

let restored = false;

function readStoredTheme(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function useThemeToggle() {
  const theme = useTheme();

  const isDark = computed(() => theme.global.current.value.dark);

  const current = computed(() => (isDark.value ? DARK_THEME : LIGHT_THEME));

  function setTheme(name: string) {
    theme.change(name);
    try {
      window.localStorage.setItem(STORAGE_KEY, name);
    } catch {
      // storage unavailable (private mode): theme still applies for this session
    }
  }

  function toggleTheme() {
    setTheme(isDark.value ? LIGHT_THEME : DARK_THEME);
  }

  if (!restored) {
    restored = true;
    const stored = readStoredTheme();
    if (stored === DARK_THEME || stored === LIGHT_THEME) {
      setTheme(stored);
    }
  }

  return { theme, isDark, current, setTheme, toggleTheme };
}
