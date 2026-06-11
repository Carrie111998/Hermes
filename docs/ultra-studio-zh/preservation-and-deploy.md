# 信息保留与上线说明

状态：文档网站运维说明  
日期：2026-06-11

## 信息如何保留

这个站点按“Markdown 主源，静态构建输出可再生”的方式保留信息。

| 层级 | 路径 | 是否提交 | 用途 |
|---|---|---|---|
| Markdown 主源 | `docs/ultra-studio-zh/**/*.md` | 是 | 文档网站的主要内容源。 |
| 图谱资源 | `docs/ultra-studio-zh/assets/*.svg` | 是 | 架构图、路线图、阅读路径图。 |
| VitePress 配置 | `docs/ultra-studio-zh/.vitepress/config.mts` | 是 | 导航、侧边栏、搜索、站点标题。 |
| 构建生成的 HTML | `docs/ultra-studio-zh/.vitepress/dist/**/*.html` | 否 | 上线产物，可通过构建命令重新生成。 |
| 依赖目录 | `docs/ultra-studio-zh/node_modules/` | 否 | 本地安装产物，不提交。 |
| 构建输出 | `docs/ultra-studio-zh/.vitepress/dist/` | 否 | 可通过 `npm run docs:build` 重新生成。 |

## 不丢信息的原则

- 新结论先写 Markdown，再让 VitePress 构建网站。
- 不把手写 HTML 当主源；如果历史 HTML 和 Markdown 冲突，以 Markdown 为准。
- 图谱必须和文字页互相链接，不能只有图片没有上下文。
- “已实现 / 部分实现 / 已规定未构建”必须明确，不能把文档能力说成运行时能力。
- 代码/API 名保留英文，读者解释用中文。
- TokenRouter、CometAPI、Sandbox lifecycle、Asset Service、Memory、Marketplace、Ledger 等长期能力必须保留入口，即使 P0 不实现。

## 本地预览

```bash
cd docs/ultra-studio-zh
npm install
npm run docs:dev
```

默认本地地址：

```text
http://127.0.0.1:5173/
```

如果端口被占用，VitePress 会提示新的端口。

## 生产构建

```bash
cd docs/ultra-studio-zh
npm run docs:build
```

构建产物在：

```text
docs/ultra-studio-zh/.vitepress/dist
```

## 上线方式

### GitHub Pages

1. 在 CI 中进入 `docs/ultra-studio-zh`。
2. 执行 `npm install`。
3. 执行 `npm run docs:build`。
4. 发布 `.vitepress/dist` 到 Pages。

### Vercel / Netlify

| 字段 | 值 |
|---|---|
| Root Directory | `docs/ultra-studio-zh` |
| Build Command | `npm run docs:build` |
| Output Directory | `.vitepress/dist` |

## 提交前检查

```bash
cd docs/ultra-studio-zh
npm run docs:build
cd ../..
git diff --check -- docs/ultra-studio-zh
```

还要检查：

- `node_modules/` 没有被加入 git。
- `.vitepress/dist/` 没有被加入 git。
- 新页面已加入侧边栏或文档地图。
- 中文页面没有只剩英文标题和英文缩写。
