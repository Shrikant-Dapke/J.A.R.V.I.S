/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: ["./index.html", "./src/renderer/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // JARVIS HUD palette — dark, futuristic, high-contrast.
        void: "#05070D",
        abyss: "#0A0F1A",
        hull: "#0F172A",
        line: "#1E293B",
        accent: {
          DEFAULT: "#22D3EE",
          soft: "#67E8F9",
        },
        violet: {
          DEFAULT: "#A78BFA",
          soft: "#C4B5FD",
        },
        ok: "#34D399",
        warn: "#FBBF24",
        danger: "#FB7185",
      },
      fontFamily: {
        display: ['"Segoe UI"', "Inter", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "Consolas", '"Cascadia Mono"', "monospace"],
      },
      boxShadow: {
        "glow-sm": "0 0 12px rgba(34, 211, 238, 0.14)",
        glow: "0 0 28px rgba(34, 211, 238, 0.22)",
      },
      keyframes: {
        "fade-in-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.55" },
        },
      },
      animation: {
        "fade-in-up": "fade-in-up 220ms ease-out both",
        "pulse-soft": "pulse-soft 2.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
