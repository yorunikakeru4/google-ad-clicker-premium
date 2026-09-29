import { createRouter, createWebHashHistory } from "vue-router";

// Hash history on purpose: Tauri serves the frontend from a custom protocol
// with no SPA fallback, so history mode breaks on reload and deep links.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: "/",
      name: "dashboard",
      component: () => import("../views/DashboardView.vue"),
      meta: { section: "Dashboard" },
    },
    {
      path: "/profiles",
      name: "profiles",
      component: () => import("../views/ProfilesView.vue"),
      meta: { section: "Profiles" },
    },
    {
      path: "/proxies",
      name: "proxies",
      component: () => import("../views/ProxiesView.vue"),
      meta: { section: "Proxies" },
    },
    {
      path: "/tasks",
      name: "tasks",
      component: () => import("../views/TasksView.vue"),
      meta: { section: "Tasks" },
    },
    {
      path: "/logs",
      name: "logs",
      component: () => import("../views/LogsView.vue"),
      meta: { section: "Logs" },
    },
    {
      path: "/settings",
      name: "settings",
      component: () => import("../views/SettingsView.vue"),
      meta: { section: "Settings" },
    },
    {
      path: "/diagnostics",
      name: "diagnostics",
      component: () => import("../views/DiagnosticsView.vue"),
      meta: { section: "Diagnostics" },
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: { name: "dashboard" },
    },
  ],
});

export default router;
