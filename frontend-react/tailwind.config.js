/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#0b1020',
        panel: '#121a33',
        panel2: '#172143',
        line: '#243156',
        cyan: '#34d3ee',
        grn: '#3ddc97',
        amber: '#f5c451',
        danger: '#ff6b6b',
        violet: '#8b7bff',
        muted: '#93a0c2',
        dim: '#67739a',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
};
