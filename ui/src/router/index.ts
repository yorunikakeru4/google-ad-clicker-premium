import {
  createRouter,
  createWebHashHistory,
  type RouteRecordRaw,
} from "vue-router";

export interface NavItem {
  path: string;
  title: string;
  icon: string;
  component: NonNullable<RouteRecordRaw["component"]>;
}

// Hash history on purpose: Tauri serves the frontend from a custom protocol
// with no SPA fallback, so history mode breaks on reload and deep links.
const history = createWebHashHistory();

// Единственный источник для роутера и бокового меню: семь экранов
// дизайн-спецификации §3.1, порядок — как в ТЗ. Роуты ленивые.
export const NAV_ITEMS: NavItem[] = [
  {
    path: "/",
    title: "Dashboard",
    icon: "mdi-view-dashboard",
    component: () => import("../views/DashboardView.vue"),
  },
  {
    path: "/profiles",
    title: "Profiles",
    icon: "mdi-account",
    component: () => import("../views/ProfilesView.vue"),
  },
  {
    path: "/proxies",
    title: "Proxies",
    icon: "mdi-earth",
    component: () => import("../views/ProxiesView.vue"),
  },
  {
    path: "/tasks",
    title: "Tasks",
    icon: "mdi-format-list-checks",
    component: () => import("../views/TasksView.vue"),
  },
  {
    path: "/logs",
    title: "Logs",
    icon: "mdi-format-list-bulleted",
    component: () => import("../views/LogsView.vue"),
  },
  {
    path: "/settings",
    title: "Settings",
    icon: "mdi-cog",
    component: () => import("../views/SettingsView.vue"),
  },
  {
    path: "/diagnostics",
    title: "Diagnostics",
    icon: "mdi-pulse",
    component: () => import("../views/DiagnosticsView.vue"),
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

const router = createRouter({ history, routes });

export default router;
