import { defineConfig } from 'vitepress'

const productItems = [
  { text: '产品规格包', link: '/product-specs/00-index' },
  { text: '产品界面', link: '/product-specs/01-product-surface' },
  { text: 'Agent 运行时合约', link: '/product-specs/02-agent-runtime-contract' },
  { text: '媒体与资产合约', link: '/product-specs/03-media-asset-contract' },
  { text: '技能、工具与提示词合约', link: '/product-specs/04-skill-tool-prompt-contract' },
  { text: '记忆 / 应用市场 / 文件', link: '/product-specs/05-memory-marketplace-files' },
  { text: '交付计划', link: '/product-specs/06-delivery-plan' }
]

const componentItems = [
  { text: '组件索引', link: '/product-specs/components/README' },
  { text: '01 左侧导航外壳', link: '/product-specs/components/01-left-nav-shell' },
  { text: '02 创作聊天界面', link: '/product-specs/components/02-creative-chat-ui' },
  { text: '03 检查器 / 实时面板', link: '/product-specs/components/03-inspector-live-panel' },
  { text: '04 应用市场', link: '/product-specs/components/04-marketplace' },
  { text: '05 记忆系统', link: '/product-specs/components/05-memory' },
  { text: '06 文件 / 任务文件浏览器', link: '/product-specs/components/06-files-task-file-browser' },
  { text: '07 任务 / 会话历史', link: '/product-specs/components/07-tasks-session-history' },
  { text: '08 资产库界面', link: '/product-specs/components/08-asset-library-ui' },
  { text: '09 资产服务', link: '/product-specs/components/09-asset-service' },
  { text: '10 媒体任务服务', link: '/product-specs/components/10-media-job-service' },
  { text: '11 技能注册表', link: '/product-specs/components/11-skill-registry' },
  { text: '12 工作流路由器', link: '/product-specs/components/12-workflow-router' },
  { text: '13 提示词编译器', link: '/product-specs/components/13-prompt-compiler' },
  { text: '14 沙箱生命周期', link: '/product-specs/components/14-sandbox-lifecycle' },
  { text: '15 人工审批网关', link: '/product-specs/components/15-human-approval-gateway' },
  { text: '16 观察与溯源账本', link: '/product-specs/components/16-observation-provenance-ledger' },
  { text: '17 凭证路由（TokenRouter）', link: '/product-specs/components/17-tokenrouter' },
  { text: '18 CometAPI 媒体网关', link: '/product-specs/components/18-cometapi-media-gateway' },
  { text: '19 模型目录', link: '/product-specs/components/19-model-catalog-provider-constraints' }
]

const infraItems = [
  { text: '基建设计总览', link: '/infra-design/00-index' },
  { text: '参考调研', link: '/infra-design/01-reference-research' },
  { text: '基础设施边界图', link: '/infra-design/02-boundary-map' },
  { text: '控制面设计', link: '/infra-design/03-control-plane-design' },
  { text: '执行面设计', link: '/infra-design/04-execution-plane-design' },
  { text: '数据面设计', link: '/infra-design/05-data-plane-design' },
  { text: '安全与运维设计', link: '/infra-design/06-security-ops-design' },
  { text: '验证路线', link: '/infra-design/07-validation-roadmap' },
  { text: 'Hermes Fork 隔离迁移', link: '/infra-design/08-hermes-fork-isolation-migration' }
]

const researchItems = [
  { text: '调研总览', link: '/research-analysis/00-index' },
  { text: 'P0 MVP 垂直切片', link: '/research-analysis/01-p0-mvp-vertical-slice' },
  { text: 'P0 Agent / 技能 / 工具 / 媒体', link: '/research-analysis/02-p0-agent-skill-tool-media-contracts' },
  { text: '安全与凭证边界', link: '/research-analysis/03-p0-security-credential-boundaries' },
  { text: '后续云基础设施路线', link: '/research-analysis/04-later-cloud-infra-roadmap' },
  { text: '完整系统视角', link: '/research-analysis/05-complete-system-perspective' },
  { text: '扩展接口与迁移计划', link: '/research-analysis/06-extension-seams-migration-plan' },
  { text: '研究附录与开放问题', link: '/research-analysis/90-research-appendix-open-questions' }
]

const standaloneItems = [
  { text: '真实聊天 Agent UI', link: '/standalone/hermes-real-chat-agent-ui' },
  { text: 'Manus 差距调研', link: '/standalone/ultra-studio-agent-manus-gap-research' },
  { text: '技能 / 工具 / 提示词规格', link: '/standalone/ultra-studio-agent-skill-tool-prompt-design' }
]

const sourceArchiveItems = [
  { text: '归档总览', link: '/source-archive/README' },
  { text: '完整源文档清单', link: '/source-archive/inventory' },
  { text: 'Hermes 专题文档', link: '/source-archive/hermes-topic-docs' },
  { text: 'Notion 源文档', link: '/source-archive/notion' },
  { text: 'Lark 源文档', link: '/source-archive/lark' },
  { text: '旧版 Ultra Studio 文档包', link: '/source-archive/legacy-ultra-studio' },
  { text: '开源架构 HTML 包', link: '/source-archive/open-source-architecture' },
  { text: '历史计划文档', link: '/source-archive/plans' }
]

export default defineConfig({
  lang: 'zh-CN',
  title: 'Ultra Studio',
  description: 'Hermes Ultra Studio 中文产品与基建设计文档站',
  cleanUrls: false,
  lastUpdated: true,
  appearance: 'dark',
  ignoreDeadLinks: [
    (url) =>
      url.includes('../ultra-studio-docs-zh/') ||
      url.includes('../ultra-studio-agent-architecture.html') ||
      url.includes('/source-archive/raw/')
  ],
  markdown: {
    lineNumbers: true,
    theme: {
      light: 'github-light',
      dark: 'github-dark'
    }
  },
  themeConfig: {
    logo: '/assets/product-shell.svg',
    nav: [
      { text: '图谱', link: '/visual-guide' },
      { text: '实现地图', link: '/implementation-map' },
      { text: '地图', link: '/site-map' },
      { text: '产品', link: '/product-specs/00-index' },
      { text: '组件', link: '/product-specs/components/README' },
      { text: '基建', link: '/infra-design/00-index' },
      { text: '源文档', link: '/source-archive/README' },
      { text: '长期参考', link: '/long-term-reference' },
      { text: '上线', link: '/preservation-and-deploy' }
    ],
    sidebar: [
      {
        text: '开始',
        collapsed: false,
        items: [
          { text: '首页', link: '/' },
          { text: '设计主线', link: '/00-design-spine' },
          { text: '当前实现地图', link: '/implementation-map' },
          { text: '权限边界', link: '/permission-boundary-design' },
          { text: '中文总入口', link: '/README' },
          { text: '文档网站地图', link: '/site-map' },
          { text: '信息保留与上线', link: '/preservation-and-deploy' },
          { text: '可视化导读', link: '/visual-guide' },
          { text: '完整建设图谱', link: '/architecture-blueprint' },
          { text: '完整长期参考', link: '/long-term-reference' }
        ]
      },
      { text: '产品规格', collapsed: false, items: productItems },
      { text: '组件规格', collapsed: true, items: componentItems },
      { text: '基建设计', collapsed: false, items: infraItems },
      { text: '调研分析', collapsed: true, items: researchItems },
      { text: '独立专题', collapsed: true, items: standaloneItems },
      { text: '源文档归档', collapsed: false, items: sourceArchiveItems }
    ],
    search: {
      provider: 'local'
    },
    outline: {
      level: [2, 3],
      label: '本页目录'
    },
    docFooter: {
      prev: '上一篇',
      next: '下一篇'
    },
    lastUpdated: {
      text: '最后更新',
      formatOptions: {
        dateStyle: 'medium',
        timeStyle: 'short'
      }
    },
    footer: {
      message: 'Ultra Studio 文档由本地 Markdown 源文件生成。',
      copyright: 'Hermes Agent 本地文档'
    }
  }
})
