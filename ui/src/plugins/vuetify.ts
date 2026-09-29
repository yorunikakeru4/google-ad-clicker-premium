import "vuetify/styles";
import { createVuetify } from "vuetify";

export const LIGHT_THEME = "gruvboxLight";
export const DARK_THEME = "gruvboxDark";

const gruvboxDark = {
  dark: true,
  colors: {
    background: "#282828",
    surface: "#32302f",
    "surface-variant": "#3c3836",
    "on-surface": "#ebdbb2",
    "on-primary": "#282828",
    primary: "#fabd2f",
    success: "#b8bb26",
    warning: "#fe8019",
    error: "#fb4934",
    info: "#8ec07c",
  },
  variables: {
    "border-color": "#504945",
    "border-opacity": "1",
    muted: "#a89984",
  },
};

const gruvboxLight = {
  dark: false,
  colors: {
    background: "#fbf1c7",
    surface: "#f2e5bc",
    "surface-variant": "#ebdbb2",
    "on-surface": "#3c3836",
    "on-primary": "#fbf1c7",
    primary: "#b57614",
    success: "#79740e",
    warning: "#af3a03",
    error: "#9d0006",
    info: "#427b58",
  },
  variables: {
    "border-color": "#d5c4a1",
    "border-opacity": "1",
    muted: "#7c6f64",
  },
};

export default createVuetify({
  theme: {
    defaultTheme: DARK_THEME,
    themes: {
      [DARK_THEME]: gruvboxDark,
      [LIGHT_THEME]: gruvboxLight,
    },
  },
  defaults: {
    VBtn: { variant: "flat", rounded: "sm" },
    VCard: { variant: "flat", border: true, rounded: "sm" },
    VChip: { variant: "tonal", rounded: "sm", size: "small" },
    VTextField: { variant: "outlined", density: "comfortable", rounded: "sm" },
    VSelect: { variant: "outlined", density: "comfortable", rounded: "sm" },
    VDataTable: { density: "comfortable", hover: true },
    VIcon: { size: "1.25em" },
  },
});
