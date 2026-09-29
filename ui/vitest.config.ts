import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import vuetify from "vite-plugin-vuetify";

// Отдельный конфиг, а не секция `test` в vite.config.ts: vite.config.ts
// тащит dev-сервер Tauri, а тестам нужен чистый node-окружение.
//
// vue/vuetify-плагины здесь нужны компонентным смоук-тестам: они рендерят
// экраны через vue/server-renderer в node, без браузера и без CSS
// (vitest по умолчанию отдаёт пустые модули стилей). Юнит-тесты логики
// от плагинов не зависят.
export default defineConfig({
  plugins: [vue(), vuetify({ autoImport: true })],
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    server: { deps: { inline: ["vuetify", "vue-chartjs", "chart.js"] } },
  },
});
