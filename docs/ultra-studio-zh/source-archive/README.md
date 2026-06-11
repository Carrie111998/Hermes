---
outline: deep
---
# 源文档归档总览

这个区域回答一个问题：除了当前中文 VitePress 主站，仓库里还有哪些历史设计、Notion/Lark 导出、旧版 HTML、专题 PRD 和计划文档。

归档策略是保守的：

- Markdown、HTML、CSS 这类可直接阅读的原文，已复制到 `public/source-archive/raw/docs/...`，可以从清单打开。
- Markdown 在 public 中使用 `.md.txt` 镜像，避免被 VitePress 当作页面编译。
- Notion/Lark 的 `raw.json` 多数是 API 原始 payload，部分超过 800 行，先进入目录登记，不直接公开镜像。
- PDF 先登记路径，不复制到站点 public，避免把二进制和文本规则混在一起。
- 主站中文设计文档仍是阅读入口；这里是底账和溯源入口。

## 覆盖摘要

| 指标 | 数量 |
| --- | ---: |
| 已扫描站点外文件 | 217 |
| 已复制可读原文 | 184 |
| Markdown | 68 |
| HTML | 114 |
| JSON / raw JSON / meta JSON | 32 |
| raw JSON payload | 15 |
| PDF | 1 |

## 分类入口

| 分类 | 文件数 | 已复制原文 |
| --- | ---: | ---: |
| 旧产品规格源 | 68 | 68 |
| 旧调研分析源 | 16 | 16 |
| 旧基建设计源 | 8 | 8 |
| 旧中文 HTML 阅读层 | 31 | 31 |
| 开源架构 HTML 包 | 17 | 17 |
| 历史计划文档 | 3 | 3 |
| Hermes 专题文档 | 16 | 15 |
| Lark 源文档 | 29 | 14 |
| Notion 源文档 | 25 | 8 |
| Ultra Studio 专题源 | 4 | 4 |

## 阅读建议

1. 先读主站的 [可视化导读](/visual-guide) 和 [完整建设图谱](/architecture-blueprint)。
2. 需要查源头时，到 [完整清单](./inventory) 按原路径定位。
3. Notion 和 Lark 的导出优先看 Markdown / HTML 镜像；raw JSON 只在需要重建 API 字段时处理。
