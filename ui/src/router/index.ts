import { createRouter, createWebHashHistory } from "vue-router";
import HomeView from "../views/HomeView.vue";

// Hash history on purpose: Tauri serves the frontend from a custom protocol
// with no SPA fallback, so history mode breaks on reload and deep links.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: "/",
      name: "home",
      component: HomeView,
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: { name: "home" },
    },
  ],
});

export default router;
