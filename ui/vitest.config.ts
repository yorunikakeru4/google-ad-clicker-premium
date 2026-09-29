import { defineConfig } from "vitest/config";

// Отдельный конфиг, а не секция `test` в vite.config.ts: vite.config.ts
// тащит tauri/vuetify-плагины, а юнит-тестам нужен чистый node-окружение
// без браузерных трансформаций.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
