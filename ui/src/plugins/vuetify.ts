import "vuetify/styles";
import { createVuetify } from "vuetify";

export const LIGHT_THEME = "adclickerLight";
export const DARK_THEME = "adclickerDark";

export default createVuetify({
  theme: {
    defaultTheme: LIGHT_THEME,
    themes: {
      [LIGHT_THEME]: {
        dark: false,
        colors: {
          background: "#f4f6fb",
          surface: "#ffffff",
          primary: "#1a73e8",
          secondary: "#5f6368",
          accent: "#34a853",
          error: "#d93025",
          warning: "#f9ab00",
          info: "#1a73e8",
          success: "#34a853",
        },
      },
      [DARK_THEME]: {
        dark: true,
        colors: {
          background: "#121417",
          surface: "#1c1f24",
          primary: "#8ab4f8",
          secondary: "#9aa0a6",
          accent: "#81c995",
          error: "#f28b82",
          warning: "#fdd663",
          info: "#8ab4f8",
          success: "#81c995",
        },
      },
    },
  },
  defaults: {
    VBtn: {
      variant: "text",
    },
    VCard: {
      rounded: "lg",
    },
  },
});
