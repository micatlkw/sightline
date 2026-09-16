module.exports = {
  content: [
    "./templates/**/*.html",
    "./templates/*.html",
  ],
  safelist: [
    "grid-cols-1",
    "grid-cols-2",
    "grid-cols-3",
    "grid-cols-4",
    "sm:grid-cols-2",
    "md:grid-cols-2",
    "md:grid-cols-3",
    "lg:grid-cols-2",
    "lg:grid-cols-3",
    "lg:grid-cols-4",
    "xl:grid-cols-2",
    "xl:grid-cols-3",
    "xl:grid-cols-4",
    "2xl:grid-cols-2",
    "2xl:grid-cols-3",
    "2xl:grid-cols-4",
    "border-emerald-500/40",
    "border-rose-500/40",
    "border-amber-500/40",
    "border-brand-500/40",
    "text-emerald-400",
    "text-rose-400",
    "text-amber-400",
    "text-brand-400",
    "translate-y-2",
    "opacity-0",
    "pulse-dot",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#ecfeff',
          100: '#cffafe',
          400: '#22d3ee',
          500: '#06b6d4',
          600: '#0891b2',
          700: '#0e7490',
        },
        darkbg: '#0b0f19',
        darkcard: '#131b2e',
        darkborder: '#1e293b',
      }
    }
  }
};
