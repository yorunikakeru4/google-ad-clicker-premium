import { createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";
import type { Component } from "vue";
import DashboardView from "../views/DashboardView.vue";
import LogsView from "../views/LogsView.vue";
import ProfilesView from "../views/ProfilesView.vue";
import ProxiesView from "../views/ProxiesView.vue";
import TasksView from "../views/TasksView.vue";
import SettingsView from "../views/SettingsView.vue";
import DiagnosticsView from "../views/DiagnosticsView.vue";

// Hash history on purpose: Tauri serves the frontend from a custom protocol
// with no SPA fallback, so history mode breaks on reload and deep links.
const history = createWebHashHistory();

export interface NavItem {
  path: string;
  title: string;
  icon: string;
  component: Component;
}

// Единственный источник для роутера и бокового меню: семь экранов из
// плана §5 «Фаза 2», порядок — как в плане.
export const NAV_ITEMS: NavItem[] = [
  {
    path: "/",
    title: "Dashboard",
    icon: "mdi-view-dashboard-outline",
    component: DashboardView,
  },
  {
    path: "/logs",
    title: "Logs",
    icon: "mdi-text-box-search-outline",
    component: LogsView,
  },
  {
    path: "/profiles",
    title: "Profiles",
    icon: "mdi-account-key-outline",
    component: ProfilesView,
  },
  {
    path: "/proxies",
    title: "Proxies",
    icon: "mdi-lan-connect",
    component: ProxiesView,
  },
  {
    path: "/tasks",
    title: "Tasks",
    icon: "mdi-play-circle-outline",
    component: TasksView,
  },
  {
    path: "/settings",
    title: "Settings",
    icon: "mdi-cog-outline",
    component: SettingsView,
  },
  {
    path: "/diagnostics",
    title: "Diagnostics",
    icon: "mdi-heart-pulse",
    component: DiagnosticsView,
  },
];

const routes: RouteRecordRaw[] = NAV_ITEMS.map((item) => ({
  path: item.path,
  name: item.title.toLowerCase(),
  component: item.component,
  meta: { title: item.title, icon: item.icon },
}));

routes.push({
  path: "/:pathMatch(.*)*",
  redirect: { name: "dashboard" },
});

const router = createRouter({
  history,
  routes,
});

export default router;
