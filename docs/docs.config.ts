import { defineConfig } from "@the-docs/preset-default"

export default defineConfig({
  contentRoot: new URL("./docs", import.meta.url),
  basePath: process.env.THE_DOCS_BASE_PATH,
})
