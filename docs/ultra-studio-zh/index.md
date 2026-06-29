---
layout: home

hero:
  name: Ultra Studio
  text: 创意智能体文档网站
  tagline: 把产品规格、组件设计、基础设施、长期路线和调研结果保存在一个可浏览、可搜索、可上线的 Markdown 文档站里。
  actions:
    - theme: brand
      text: 先看图
      link: /visual-guide
    - theme: alt
      text: 实现地图
      link: /implementation-map
    - theme: alt
      text: 文档地图
      link: /site-map
    - theme: alt
      text: 权限边界
      link: /permission-boundary-design
    - theme: alt
      text: 源文档归档
      link: /source-archive/README

features:
  - title: 可搜索
    details: VitePress 本地搜索直接覆盖中文 Markdown 文档，不再靠一堆 file:// HTML 手动跳转。
  - title: 可保留
    details: Markdown 是主源，图谱、文档地图和状态词保留上下文，构建产物可随时再生。
  - title: 可上线
    details: 构建结果是静态站点，可以部署到 GitHub Pages、Vercel、Netlify 或任意静态文件服务。
  - title: 可维护
    details: 文档地图、侧边栏和状态词统一维护，后续新增组件或基建设计不容易散。
  - title: 可追溯
    details: 源文档归档保留历史 Markdown、HTML、Notion/Lark 导出和专题文档清单，方便核对旧资料。
---

## 推荐阅读路径

1. 先看 [可视化导读](visual-guide)，用图理解 P0 闭环、产品外壳、系统层级和阅读路线。
2. 再看 [设计主线](00-design-spine)，确认产品边界、P0 闭环、目标架构和当前状态。
3. 接着看 [当前实现地图](implementation-map)，把当前代码事实、spec-only 缺口和下一步连接点对齐。
4. 然后看 [权限边界](permission-boundary-design)，把 Prompt、UI、Router、Worker 和真实授权边界拆开。
5. 再看 [文档网站地图](site-map)，知道每一类文档解决什么问题。
6. 再看 [完整建设图谱](architecture-blueprint)，确认控制面、执行面、数据面、安全运维和路线图。
7. 如果要排长期能力，看 [完整长期参考](long-term-reference)，里面覆盖 TokenRouter、CometAPI、Sandbox lifecycle、Asset Service、Memory、Marketplace、Ledger 和 Cloud tenant layer。
8. 如果要核对旧资料和 Notion/Lark 来源，看 [源文档归档](source-archive/README) 和 [完整源文档清单](source-archive/inventory)。
9. 如果要开始落地，直接进 [P0 MVP 垂直切片](research-analysis/01-p0-mvp-vertical-slice) 和 [工作流路由器](product-specs/components/12-workflow-router)。
10. 如果要上线或交接，看 [信息保留与上线说明](preservation-and-deploy)。

## 本地使用

```bash
cd docs/ultra-studio-zh
npm install
npm run docs:dev
```

生产构建：

```bash
npm run docs:build
npm run docs:preview
```
