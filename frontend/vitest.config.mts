import { defineConfig } from "vitest/config"
import tsconfigPaths from "vite-tsconfig-paths"

// No @vitejs/plugin-react: esbuild's automatic JSX runtime compiles these
// components without pulling a second Babel toolchain into the tree, and the
// tests here assert on behaviour rather than on Fast Refresh.
export default defineConfig({
  plugins: [tsconfigPaths()],
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    // jsdom only provides localStorage for a real origin; on the default
    // about:blank it is undefined, which surfaces as an unrelated TypeError in
    // whatever test happens to touch storage first.
    environmentOptions: { jsdom: { url: "http://localhost:3000" } },
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    restoreMocks: true,
  },
})
