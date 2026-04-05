import * as esbuild from "esbuild";

const isWatch = process.argv.includes("--watch");
const isProd = process.argv.includes("--production");

/** @type {import('esbuild').BuildOptions} */
const buildOptions = {
  entryPoints: ["webview-ui/src/index.tsx"],
  bundle: true,
  outfile: "media/webview.js",
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  jsx: "automatic",
  minify: isProd,
  sourcemap: !isProd,
  define: {
    "process.env.NODE_ENV": isProd ? '"production"' : '"development"',
  },
  loader: {
    ".tsx": "tsx",
    ".ts": "ts",
    ".css": "css",
  },
};

if (isWatch) {
  const ctx = await esbuild.context(buildOptions);
  await ctx.watch();
  console.log("[esbuild] Watching webview-ui...");
} else {
  await esbuild.build(buildOptions);
  console.log("[esbuild] WebView bundle built.");
}
