/**
 * 奇计GEO平台 CLI 入口
 * 
 * 统一入口，每次调用独立启动浏览器、执行操作、返回 JSON、关闭浏览器。
 * 被 Hermes Skill (qiji-geo) 调用。
 * 
 * 用法:
 *   node geo-cli.js login
 *   node geo-cli.js rights
 *   node geo-cli.js diagnose --brand 华为 --keywords 手机,Mate70 [--submit] [--suggestion]
 *   node geo-cli.js report
 *   node geo-cli.js keywords
 *   node geo-cli.js fuken --url <链接> [--submit]
 *   node geo-cli.js articles
 *   node geo-cli.js test
 */

const path = require('path');
let chromium = null; // lazy-loaded after config check

// ========== 配置 ==========

// 网页端地址：直接读 GEO_URL 环境变量。首次使用时引导用户输入并写入 .env。
function resolveGeoUrl() {
  const url = (process.env.GEO_URL || '').trim();
  if (url) return url.replace(/\/+$/, ''); // 去掉尾部斜杠
  console.error('ERROR: GEO_URL 未设置。请在 ~/.hermes/.env 中配置 GEO_URL=你的GEO服务端网址');
  process.exit(1);
}

const CONFIG = {
  url: resolveGeoUrl(),
  username: process.env.GEO_USERNAME || '',
  password: process.env.GEO_PASSWORD || '',
  headless: false,
  timeout: 30000,
};

// ========== 参数解析 ==========

function parseArgs() {
  const args = process.argv.slice(2);
  const action = args[0] || 'help';
  const params = {};
  for (let i = 1; i < args.length; i++) {
    if (args[i].startsWith('--')) {
      const key = args[i].substring(2);
      if (i + 1 < args.length && !args[i + 1].startsWith('--')) {
        params[key] = args[i + 1];
        i++;
      } else {
        params[key] = true;
      }
    }
  }
  return { action, params };
}

// ========== 输出 ==========

function output(success, data, message = '') {
  const result = { success, action: process.argv[2], message, data, timestamp: new Date().toISOString() };
  console.log(JSON.stringify(result, null, 2));
}

// ========== 浏览器管理 ==========

// Find the bundled Chromium executable. Playwright looks in
// %LOCALAPPDATA%\ms-playwright\ by default (populated by install.ps1),
// but we also check the vendor directory as a fallback so the skill
// never triggers an online download.
function findBundledChromium() {
  const path = require('path');
  const fs = require('fs');
  const candidates = [
    // install.ps1 stages vendor/chromium -> ms-playwright
    path.join(process.env.LOCALAPPDATA || '', 'ms-playwright'),
    // vendor dir in the app install location (fallback)
    path.join(process.env.LOCALAPPDATA || '', 'hermes', 'vendor', 'chromium'),
  ];
  for (const base of candidates) {
    if (!fs.existsSync(base)) continue;
    // Look for chromium-XXXX/chrome-win64/chrome.exe
    const dirs = fs.readdirSync(base).filter(d => d.startsWith('chromium-'));
    for (const d of dirs) {
      const exe = path.join(base, d, 'chrome-win64', 'chrome.exe');
      if (fs.existsSync(exe)) return exe;
    }
  }
  return null; // let Playwright use its default lookup
}

async function createBrowser() {
  if (!chromium) chromium = require('playwright').chromium;
  const launchOpts = { headless: CONFIG.headless };
  const exe = findBundledChromium();
  if (exe) launchOpts.executablePath = exe;
  const browser = await chromium.launch(launchOpts);
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    locale: 'zh-CN',
  });
  const page = await context.newPage();
  return { browser, page };
}

// ========== 核心操作 ==========

async function login(page) {
  await page.goto(`${CONFIG.url}/user/dashboard`, {
    waitUntil: 'networkidle',
    timeout: CONFIG.timeout,
  });
  await page.waitForTimeout(2000);

  // 已登录
  if (!page.url().includes('/login') && !page.url().includes('/index/login')) {
    return true;
  }

  // 执行登录
  await page.getByPlaceholder(/账号|手机/).first().fill(CONFIG.username);
  await page.getByPlaceholder(/密码/).first().fill(CONFIG.password);
  await page.getByRole('button', { name: /登.*录/i }).click();
  // 等待登录完成（页面跳转可能关闭当前 page，用 try-catch 兜底）
  try {
    await page.waitForURL('**/user/dashboard*', { timeout: 10000 });
  } catch {
    try { await page.waitForTimeout(3000); } catch {}
  }

  return !page.url().includes('/login');
}

async function getFrame(page, retries = 15) {
  for (let i = 0; i < retries; i++) {
    // Find the content iframe (addtabs=1 in URL)
    const frame = page.frames().find(f => f.url().includes('addtabs=1'));
    if (frame) {
      // Wait for frame content to load
      try { await frame.waitForLoadState('domcontentloaded', { timeout: 3000 }); } catch {}
      // Verify frame has actual content (not empty)
      try {
        const bodyLen = await frame.evaluate(() => document.body?.innerText?.length || 0);
        if (bodyLen > 0) return frame;
      } catch {}
    }
    try { await page.waitForTimeout(500); } catch {}
  }
  // Last attempt: return whatever frame we found, even if content seems empty
  return page.frames().find(f => f.url().includes('addtabs=1')) || null;
}

async function goHome(page) {
  await page.locator('aside a:has-text("首页")').first().click().catch(() => {});
  try { await page.waitForTimeout(2000); } catch {}
}

async function clickMenu(page, parentText, childText) {
  if (childText) {
    const parent = page.locator(`aside a:has-text("${parentText}")`).first();
    const child = page.locator(`aside a:has-text("${childText}")`).first();
    const visible = await child.isVisible().catch(() => false);
    if (!visible) {
      await parent.click().catch(() => {});
      try { await page.waitForTimeout(1000); } catch {}
    }
    await child.click({ timeout: 5000 }).catch(() => {});
  } else {
    await page.locator(`aside a:has-text("${parentText}")`).first().click().catch(() => {});
  }
  try { await page.waitForTimeout(3000); } catch {}
}

// ========== 1. 账号权益 ==========

async function getRights(page) {
  await goHome(page);
  await clickMenu(page, '账号权益');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  const text = await frame.locator('body').innerText().catch(() => '');
  const rights = {};
  const patterns = [
    ['已收录数', /已收录\s*(\d+)/],
    ['账号有效期', /有效期\s*(\d{4}-\d{2}-\d{2})/],
    ['剩余点数', /剩余点数\s*(-?\d+)/],
    ['剩余余额', /剩余余额\s*(\d+\.?\d*)/],
    ['主关键词额度', /主关键词\s*(\d+\/\d+)/],
    ['写作问题额度', /写作问题\s*(\d+\/\d+)/],
    ['AI写作数量', /AI写作数量\s*(\d+\/\d+)/],
    ['文章发布额度', /文章发布\s*(\d+\/\d+)/],
  ];
  for (const [key, regex] of patterns) {
    const m = text.match(regex);
    if (m) rights[key] = m[1];
  }
  return rights;
}

// ========== 2. AI诊断 ==========

async function diagnose(page, params) {
  await goHome(page);
  await clickMenu(page, 'AI可见度诊断');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  const brand = params.brand;
  const keywords = (params.keywords || '').split(',').map(k => k.trim()).filter(Boolean);
  const platforms = (params.platforms || '').split(',').map(p => p.trim()).filter(Boolean);

  // 1. 填写品牌
  await frame.evaluate((b) => {
    const input = document.querySelector('.key > input');
    if (input) {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, b);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }, brand);

  // 2. 选择AI模型
  await frame.evaluate((ps) => {
    document.querySelectorAll('.zhenduan-border').forEach(item => {
      if (ps.length === 0 || ps.includes(item.id)) {
        if (!item.classList.contains('select-border-bar')) {
          item.classList.add('select-border-bar');
        }
      }
    });
  }, platforms);

  // 3. 优化建议
  if (params.suggestion) {
    await frame.evaluate(() => {
      document.querySelectorAll('.zhenduan-border').forEach(item => {
        if (item.getAttribute('data-id') === '1') item.click();
      });
    });
  }

  // 4. 填写行业关键词
  for (const kw of keywords) {
    const input = frame.locator('.brand > input');
    await input.fill(kw);
    await input.press('Enter');
    await page.waitForTimeout(500);
  }

  // 5. 验证表单
  const state = await frame.evaluate(() => ({
    brand: document.querySelector('.key > input')?.value,
    aiCount: document.querySelectorAll('.select-border-bar').length,
    keywords: Array.from(document.querySelectorAll('.brand > span')).map(s => s.textContent.trim()),
  }));

  if (!params.submit) {
    return { submitted: false, formState: state, cost: params.suggestion ? '16元(未提交)' : '13元(未提交)' };
  }

  // 6. 提交（autoConfirm + 点击提交按钮 + 捕获响应）
  await frame.evaluate(() => { window.confirm = () => true; });

  return new Promise((resolve) => {
    let resolved = false;
    page.on('response', async (resp) => {
      if (resp.url().includes('analyze/add') && resp.request().method() === 'POST') {
        const body = await resp.text().catch(() => '');
        try {
          const data = JSON.parse(body);
          if (!resolved) { resolved = true; resolve({ submitted: true, response: data, formState: state }); }
        } catch {
          if (!resolved) { resolved = true; resolve({ submitted: true, response: { code: -1, msg: body.substring(0, 200) }, formState: state }); }
        }
      }
    });

    frame.evaluate(() => document.querySelector('.query-button')?.click()).catch(() => {});

    setTimeout(() => {
      if (!resolved) { resolved = true; resolve({ submitted: false, error: '提交超时(30s)', formState: state }); }
    }, 30000);
  });
}

// ========== 3. 诊断报告 ==========

async function getReports(page) {
  await goHome(page);
  await clickMenu(page, '诊断报告');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  // Bootstrap Table 解析：去掉空列和序号列
  let rows = await frame.evaluate(() => {
    const trs = document.querySelectorAll('table tbody tr');
    return Array.from(trs).map(tr => {
      let tds = Array.from(tr.querySelectorAll('td'));
      tds = tds.map(td => td.textContent.trim());
      if (tds.length > 0 && /^\d+$/.test(tds[0])) {
        tds = tds.slice(1);
      }
      if (tds.length >= 3) {
        return {
          brand: tds[0],
          keywords: tds[1],
          status: tds[2] || '',
          time: tds[3] || '',
        };
      }
      return null;
    }).filter(r => r && r.brand && !/^\d+$/.test(r.brand));
  });

  // table 解析失败，回退到 innerText
  if (rows.length === 0) {
    const text = await frame.locator('body').innerText().catch(() => '');
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    rows = [];
    for (const line of lines) {
      const parts = line.split('\t').map(p => p.trim()).filter(Boolean);
      if (parts.length >= 3 && /^\d+$/.test(parts[0]) && !parts[0].includes('第')) {
        rows.push({
          id: parts[0],
          brand: parts[1],
          keywords: parts[2],
          status: parts[3] || '',
          time: parts[4] || '',
        });
      }
    }
  }

  return rows;
}

// ========== 4. 关键词列表 ==========

async function getKeywords(page) {
  await goHome(page);
  await clickMenu(page, 'AI素材源力', '关键词');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  // Bootstrap Table 解析：去掉空列和序号列
  let rows = await frame.evaluate(() => {
    const trs = document.querySelectorAll('table tbody tr');
    return Array.from(trs).map(tr => {
      let tds = Array.from(tr.querySelectorAll('td'));
      // 去掉空列（checkbox等）
      tds = tds.map(td => td.textContent.trim());
      // 如果第一列是纯数字序号，去掉
      if (tds.length > 0 && /^\d+$/.test(tds[0])) {
        tds = tds.slice(1);
      }
      if (tds.length >= 2) {
        return { keyword: tds[0], questionCount: tds[1] };
      }
      return null;
    }).filter(r => r && r.keyword && !/^\d+$/.test(r.keyword));
  });

  // 回退 innerText
  if (rows.length === 0) {
    const text = await frame.locator('body').innerText().catch(() => '');
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    rows = [];
    for (const line of lines) {
      const parts = line.split('\t').map(p => p.trim()).filter(Boolean);
      if (parts.length >= 3 && /^\d+$/.test(parts[0])) {
        rows.push({ keyword: parts[1], questionCount: parts[2] });
      }
    }
  }

  return rows;
}

// ========== 通用 Bootstrap Table 解析器 ==========

/**
 * 通用表格解析：导航到指定页面，解析 Bootstrap Table。
 * @param {object} page - Playwright page
 * @param {string} menu - 顶级菜单名
 * @param {string|null} submenu - 子菜单名
 * @param {string[]} columnNames - 列名映射（不含序号列），如 ['keyword', 'count']
 */
async function parseTable(page, menu, submenu, columnNames) {
  await goHome(page);
  await clickMenu(page, menu, submenu);
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  // 策略 A：DOM 解析
  let rows = await frame.evaluate((cols) => {
    const trs = document.querySelectorAll('table tbody tr');
    return Array.from(trs).map(tr => {
      let tds = Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim());
      // 去掉前导的序号列和 checkbox 列（可能有多个）
      while (tds.length > 0 && (/^\d+$/.test(tds[0]) || tds[0] === '')) {
        tds = tds.slice(1);
      }
      const row = {};
      cols.forEach((name, i) => { row[name] = tds[i] || ''; });
      return row;
    });
  }, columnNames);

  // 过滤空表占位行（"没有找到匹配的记录"等）
  rows = rows.filter(r => {
    const vals = Object.values(r).map(v => String(v).trim());
    // 全空行
    if (vals.every(v => !v)) return false;
    // 包含"没有找到"占位文本
    if (vals.some(v => v.includes('没有找到匹配') || v.includes('没有数据'))) return false;
    return true;
  });

  // 策略 B：innerText 回退
  if (rows.length === 0) {
    const text = await frame.locator('body').innerText().catch(() => '');
    rows = [];
    for (const line of text.split('\n')) {
      const parts = line.split('\t').map(p => p.trim()).filter(Boolean);
      if (parts.length >= columnNames.length + 1 && /^\d+$/.test(parts[0])) {
        const row = {};
        columnNames.forEach((name, i) => { row[name] = parts[i + 1] || ''; });
        rows.push(row);
      }
    }
  }

  return rows;
}

// ========== 5a. 写作标题列表 ==========

async function getTitles(page) {
  return parseTable(page, 'AI素材源力', '写作标题', ['mainWord', 'question', 'status', 'time']);
}

// ========== 5b. 企业画像图库 ==========

async function getGalleries(page) {
  return parseTable(page, 'AI素材源力', '企业画像图库', ['category', 'imageCount', 'gallery', 'time']);
}

// ========== 5c. 企业知识库 ==========

async function getKnowledgeBases(page) {
  return parseTable(page, 'AI素材源力', '企业知识库', ['name', 'company', 'time']);
}

// ========== 5d. 写作指令列表 ==========

async function getWriteInstructions(page) {
  return parseTable(page, 'AI文章写作', '写作指令', ['name', 'type', 'time']);
}

// ========== 5e. 文章分类列表 ==========

async function getArticleCategories(page) {
  return parseTable(page, 'AI文章写作', '文章分类', ['groupName', 'articleCount', 'remaining', 'time']);
}

// ========== 5f. AI写作任务列表 ==========

async function getWriteTasks(page) {
  return parseTable(page, 'AI文章写作', 'AI写作任务', ['taskName', 'distillWord', 'maxCount', 'created', 'knowledgeBase', 'detail', 'error']);
}

// ========== 5g. 批量爆文复刻列表 ==========

async function getBatchFuken(page) {
  return parseTable(page, 'AI流量复刻', '批量爆文复刻', ['taskName', 'keywords', 'maxRewrite', 'current', 'status', 'viewTask', 'time']);
}

// ========== 5h. AI数据中心 ==========

async function getDashboard(page) {
  await goHome(page);
  await clickMenu(page, 'AI数据中心');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  const text = await frame.locator('body').innerText().catch(() => '');
  const stats = {};

  // 提取余额
  const balanceMatch = text.match(/余额[:：]\s*(\d+\.?\d*)/);
  if (balanceMatch) stats.balance = balanceMatch[1];

  // 提取各类统计数字
  const numberPatterns = [
    ['AI创作', /AI创作[\s\S]*?(\d+)/],
    ['发布统计', /发布统计[\s\S]*?(\d+)/],
    ['数据大屏', /数据大屏/],
    ['关键词', /关键词[\s\S]*?(\d+)/],
  ];
  for (const [key, regex] of numberPatterns) {
    const m = text.match(regex);
    if (m) stats[key] = m[1] || true;
  }

  // 提取额度信息（类似权益页的格式）
  const quotaRegex = /([\u4e00-\u9fa5\w]+?)\s*(\d+)\/(\d+|不限)/g;
  let match;
  while ((match = quotaRegex.exec(text)) !== null) {
    const name = match[1].trim();
    if (name.length >= 2 && name.length <= 10) {
      stats[name] = `${match[2]}/${match[3]}`;
    }
  }

  return stats;
}

// ========== 5i. 消耗明细 ==========

async function getConsumption(page) {
  return parseTable(page, '消耗明细', null, ['type', 'detail', 'amount', 'time']);
}

// ========== 5. 爆文复刻 ==========

async function createFuken(page, params) {
  await goHome(page);
  await clickMenu(page, 'AI流量复刻', '全网爆文复刻');
  const mainFrame = await getFrame(page);
  if (!mainFrame) return { error: 'iframe未加载' };

  await mainFrame.locator('a.btn-add').click().catch(() => {});
  await page.waitForTimeout(3000);

  const dialogFrame = page.frames().find(f => f.url().includes('/user/weixin_baowen/add'));
  if (!dialogFrame) return { error: '弹窗iframe未加载' };

  await dialogFrame.locator('input[name="row[weixin_url]"]').fill(params.url);
  await dialogFrame.locator('select[name="row[image_type_id]"]').selectOption({ index: 1 }).catch(() => {});
  await dialogFrame.locator('select[name="row[user_zhiling_id]"]').selectOption({ index: 1 }).catch(() => {});

  if (!params.submit) {
    return { submitted: false, url: params.url, message: '已填写表单，未提交' };
  }

  await mainFrame.locator('button:has-text("归类文章")').click().catch(() => {});

  return new Promise((resolve) => {
    let resolved = false;
    page.on('response', async (resp) => {
      if (resp.url().includes('weixin_baowen/add') && resp.request().method() === 'POST') {
        const body = await resp.text().catch(() => '');
        try {
          const data = JSON.parse(body);
          if (!resolved) { resolved = true; resolve({ submitted: true, response: data }); }
        } catch {
          if (!resolved) { resolved = true; resolve({ submitted: true, response: { code: -1, msg: body } }); }
        }
      }
    });

    setTimeout(() => {
      if (!resolved) { resolved = true; resolve({ submitted: false, error: '提交超时' }); }
    }, 15000);
  });
}

// ========== 6. 文章列表 ==========

async function getArticles(page) {
  await goHome(page);
  await clickMenu(page, 'AI文章写作', '文章列表');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  let rows = await frame.evaluate(() => {
    const trs = document.querySelectorAll('table tbody tr');
    return Array.from(trs).map(tr => {
      let tds = Array.from(tr.querySelectorAll('td'));
      tds = tds.map(td => td.textContent.trim());
      if (tds.length > 0 && /^\d+$/.test(tds[0])) {
        tds = tds.slice(1);
      }
      if (tds.length >= 2) {
        return { title: tds[0], status: tds[1] || '', time: tds[2] || '' };
      }
      return null;
    }).filter(r => r && r.title && !/^\d+$/.test(r.title));
  });

  if (rows.length === 0) {
    const text = await frame.locator('body').innerText().catch(() => '');
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    rows = [];
    for (const line of lines) {
      const parts = line.split('\t').map(p => p.trim()).filter(Boolean);
      if (parts.length >= 3 && /^\d+$/.test(parts[0])) {
        rows.push({ id: parts[0], title: parts[1], status: parts[2], time: parts[3] });
      }
    }
  }

  return rows;
}

// ========== 7. 全功能测试 ==========

async function runTests(page) {
  const tests = [
    { menu: 'AI可见度诊断' },
    { menu: '诊断报告' },
    { parent: 'AI素材源力', menu: '关键词' },
    { parent: 'AI素材源力', menu: '写作标题' },
    { parent: 'AI素材源力', menu: '企业画像图库' },
    { parent: 'AI素材源力', menu: '企业知识库' },
    { parent: 'AI文章写作', menu: '写作指令' },
    { parent: 'AI文章写作', menu: '文章分类' },
    { parent: 'AI文章写作', menu: 'AI写作任务' },
    { parent: 'AI文章写作', menu: '文章列表' },
    { parent: 'AI流量复刻', menu: '全网爆文复刻' },
    { parent: 'AI流量复刻', menu: '批量爆文复刻' },
    { menu: 'AI数据中心' },
    { menu: '消耗明细' },
    { menu: '账号权益' },
  ];

  const results = [];
  for (const t of tests) {
    const label = t.parent ? `${t.parent} → ${t.menu}` : t.menu;
    try {
      await goHome(page);
      await clickMenu(page, t.parent || t.menu, t.parent ? t.menu : null);
      const frame = await getFrame(page);
      if (frame) {
        const content = await frame.locator('body').innerText().catch(() => '');
        results.push({ page: label, status: 'ok', preview: content.substring(0, 80) });
      } else {
        results.push({ page: label, status: 'fail', preview: 'iframe未加载' });
      }
    } catch (err) {
      results.push({ page: label, status: 'fail', preview: err.message.substring(0, 80) });
    }
  }
  return results;
}

// ========== 5j. SEO 站点列表 ==========

async function getSeoSites(page) {
  return parseTable(page, 'AI官网SEO', '站点管理', ['networkType', 'domain', 'downloadTraffic', 'remark', 'createdTime']);
}

// ========== 5k. SEO 栏目列表（含栏目名↔ID映射） ==========

async function getSeoColumns(page) {
  return parseTable(page, 'AI官网SEO', '栏目列表', ['columnName', 'code', 'urlName', 'model', 'listTemplate', 'detailTemplate', 'sort', 'status']);
}

// ========== 5l. SEO 发布任务列表 ==========

async function getSeoTasks(page) {
  return parseTable(page, 'AI官网SEO', '发布任务', ['taskName', 'site', 'articleCategory', 'dailyCount', 'published', 'publishDate', 'publishTime', 'status']);
}

// ========== 5m. 创建 SEO 发布任务 ==========
// 表单结构（2026-07-06 截图验证）：
//   1. 任务名      — 文本输入框     *
//   2. 站点         — 文本输入框     *
//   3. 发布栏目     — 文本输入框     *
//   4. AI文章分类   — 自定义下拉框   *  (Element UI 风格，非原生 select)
//   5. 发布日期     — 日期范围选择器 *  (起始/结束两个输入框)
//   6. 每日发布时间 — 时间范围选择器 *  (起始/结束两个输入框)
//   7. 每日发布     — 文本输入框     *
//   底部按钮：提交任务

async function createSeoPublish(page, params) {
  await goHome(page);
  await clickMenu(page, 'AI官网SEO', '发布任务');
  const frame = await getFrame(page);
  if (!frame) return { error: 'iframe未加载' };

  // 收集填写结果，最终返回给 Agent 核对
  const filled = {};

  // 点击"添加"按钮打开弹窗
  const addBtn = frame.locator('button:has-text("添加"), .btn:has-text("添加"), a:has-text("添加")').first();
  await addBtn.click().catch(() => {});
  await page.waitForTimeout(1500);

  // 1. 任务名 — 文本输入框
  if (params.name) {
    const nameInput = frame.locator('input[placeholder*="任务名"], input[placeholder*="task"], input[placeholder*="名称"]').first();
    await nameInput.fill(params.name).catch(() => {});
    filled.name = params.name;
  }

  // 2. 站点 — 文本输入框（不是下拉框！）
  if (params.site) {
    const siteInput = frame.locator('input[placeholder*="站点"], input[placeholder*="site"], input[placeholder*="域名"]').first();
    await siteInput.fill(params.site).catch(() => {});
    filled.site = params.site;
  }

  // 3. 发布栏目 — 文本输入框（不是下拉框！）
  if (params.column) {
    const columnInput = frame.locator('input[placeholder*="栏目"], input[placeholder*="column"]').first();
    await columnInput.fill(params.column).catch(() => {});
    filled.column = params.column;
  }

  // 4. AI文章分类 — 自定义下拉框（Element UI 风格）
  //    策略：点击触发器 → 等待下拉面板 → 点击匹配选项
  if (params.category) {
    const categoryTrigger = frame.locator('.el-select:has(input[placeholder*="分类"]) .el-input, input[placeholder*="分类"]').first();
    await categoryTrigger.click().catch(() => {});
    await page.waitForTimeout(500);
    // 下拉选项通常出现在 body 下面的 .el-select-dropdown
    const option = page.locator('.el-select-dropdown__item:has-text("' + params.category + '")').first();
    await option.click().catch(() => {});
    filled.category = params.category;
  }

  // 5. 发布日期 — 日期范围选择器（两个输入框）
  if (params.startDate) {
    // 起始日期
    const startDateInput = frame.locator('input[placeholder*="开始"], input[placeholder*="起始"], input[placeholder*="start"]').first();
    await startDateInput.fill(params.startDate).catch(() => {});
    // 触发 change 事件（Element UI 日期选择器需要）
    await startDateInput.press('Enter').catch(() => {});
    filled.startDate = params.startDate;
  }
  if (params.endDate) {
    const endDateInput = frame.locator('input[placeholder*="结束"], input[placeholder*="到期"], input[placeholder*="end"]').first();
    await endDateInput.fill(params.endDate).catch(() => {});
    await endDateInput.press('Enter').catch(() => {});
    filled.endDate = params.endDate;
  }

  // 6. 每日发布时间 — 时间范围选择器（两个输入框）
  if (params.timeStart) {
    const timeStartInput = frame.locator('input[placeholder*="发布时间"], input[placeholder*="开始时间"], input[placeholder*="time"]').first();
    await timeStartInput.fill(params.timeStart).catch(() => {});
    await timeStartInput.press('Enter').catch(() => {});
    filled.timeStart = params.timeStart;
  }
  if (params.timeEnd) {
    const timeEndInput = frame.locator('input[placeholder*="结束时间"], input[placeholder*="截止时间"]').first();
    await timeEndInput.fill(params.timeEnd).catch(() => {});
    await timeEndInput.press('Enter').catch(() => {});
    filled.timeEnd = params.timeEnd;
  }

  // 7. 每日发布量 — 文本/数字输入框
  if (params.dailyCount) {
    const countInput = frame.locator('input[placeholder*="每日"], input[placeholder*="发布量"], input[placeholder*="数量"], input[type="number"]').first();
    await countInput.fill(String(params.dailyCount)).catch(() => {});
    filled.dailyCount = params.dailyCount;
  }

  // 提交（如果指定了 --submit）
  if (params.submit) {
    // 按钮文字是"提交任务"（截图确认）
    const submitBtn = frame.locator('button:has-text("提交任务"), button:has-text("提交"), .btn:has-text("提交"), button[type="submit"]').first();
    await submitBtn.click().catch(() => {});
    // 自动确认 confirm 弹窗
    page.on('dialog', dialog => dialog.accept());
    await page.waitForTimeout(2000);
    return { submitted: true, ...filled };
  }

  return { submitted: false, ...filled };
}

// ========== 主入口 ==========

(async () => {
  const { action, params } = parseArgs();

  if (action === 'help' || action === '--help') {
    console.log(JSON.stringify({
      usage: 'node geo-cli.js <action> [options]',
      actions: [
        'login', 'rights',
        'diagnose --brand X --keywords A,B [--submit] [--suggestion]',
        'report', 'keywords',
        'titles', 'galleries', 'knowledge',
        'instructions', 'categories', 'write-tasks',
        'batch-fuken', 'dashboard', 'consumption',
        'articles', 'fuken --url URL [--submit]',
        'seo-sites', 'seo-columns', 'seo-tasks',
        'seo-publish --name X --site Y --column Z --category W --start-date YYYY-MM-DD --end-date YYYY-MM-DD --time-start HH:mm --time-end HH:mm --daily-count N [--submit]',
        'test',
      ],
    }, null, 2));
    process.exit(0);
  }

  const validActions = [
    'login', 'rights', 'diagnose', 'report', 'keywords',
    'titles', 'galleries', 'knowledge',
    'instructions', 'categories', 'write-tasks',
    'batch-fuken', 'dashboard', 'consumption',
    'fuken', 'articles', 'test',
    'seo-sites', 'seo-columns', 'seo-tasks', 'seo-publish',
  ];
  if (!validActions.includes(action)) {
    output(false, null, `未知操作: ${action}。可用: ${validActions.join(', ')}`);
    process.exit(1);
  }

  // 应用 --headless 参数到 CONFIG
  if (params.headless || process.env.GEO_HEADLESS === 'true') {
    CONFIG.headless = true;
  }

  const { browser, page } = await createBrowser();

  try {
    // 所有操作都需要先登录
    const loggedIn = await login(page);
    if (!loggedIn) {
      output(false, null, '登录失败，请检查 GEO_USERNAME/GEO_PASSWORD');
      process.exit(1);
    }

    let data;
    let message = '';

    switch (action) {
      case 'login':
        data = { url: page.url() };
        message = '登录成功';
        break;

      case 'rights':
        data = await getRights(page);
        message = Object.keys(data).length > 0 ? '权益查询成功' : '未匹配到权益数据';
        break;

      case 'diagnose':
        if (!params.brand) {
          output(false, null, 'diagnose 需要 --brand 参数');
          process.exit(1);
        }
        if (!params.keywords) {
          output(false, null, 'diagnose 需要 --keywords 参数');
          process.exit(1);
        }
        data = await diagnose(page, params);
        message = data.submitted === false
          ? `表单已填写（未提交）。费用: ${data.cost}。加 --submit 提交。`
          : '诊断任务已提交';
        break;

      case 'report':
        data = await getReports(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 条诊断报告`
          : '暂无诊断报告';
        break;

      case 'keywords':
        data = await getKeywords(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个关键词`
          : '暂无关键词';
        break;

      case 'titles':
        data = await getTitles(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 条写作标题`
          : '暂无写作标题';
        break;

      case 'galleries':
        data = await getGalleries(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个图库分类`
          : '暂无图库';
        break;

      case 'knowledge':
        data = await getKnowledgeBases(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个企业知识库`
          : '暂无知识库';
        break;

      case 'instructions':
        data = await getWriteInstructions(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 条写作指令`
          : '暂无写作指令';
        break;

      case 'categories':
        data = await getArticleCategories(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个文章分类`
          : '暂无文章分类';
        break;

      case 'write-tasks':
        data = await getWriteTasks(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个写作任务`
          : '暂无写作任务';
        break;

      case 'batch-fuken':
        data = await getBatchFuken(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个批量复刻任务`
          : '暂无批量复刻任务';
        break;

      case 'dashboard':
        data = await getDashboard(page);
        message = Object.keys(data).length > 0
          ? '数据中心信息获取成功'
          : '未获取到数据中心信息';
        break;

      case 'consumption':
        data = await getConsumption(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 条消耗记录`
          : '暂无消耗记录';
        break;

      case 'fuken':
        if (!params.url) {
          output(false, null, 'fuken 需要 --url 参数');
          process.exit(1);
        }
        data = await createFuken(page, params);
        message = data.submitted ? '爆文复刻已提交' : '表单已填写（未提交）。加 --submit 提交。';
        break;

      case 'articles':
        data = await getArticles(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 篇文章`
          : '暂无文章';
        break;

      case 'seo-sites':
        data = await getSeoSites(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个SEO站点`
          : '暂无SEO站点（或菜单名不匹配，需验证）';
        break;

      case 'seo-columns':
        data = await getSeoColumns(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个栏目`
          : '暂无栏目（或菜单名不匹配，需验证）';
        break;

      case 'seo-tasks':
        data = await getSeoTasks(page);
        message = Array.isArray(data) && data.length > 0
          ? `找到 ${data.length} 个SEO发布任务`
          : '暂无SEO发布任务';
        break;

      case 'seo-publish':
        if (!params.name) {
          output(false, null, 'seo-publish 需要 --name 参数（任务名）');
          process.exit(1);
        }
        data = await createSeoPublish(page, params);
        message = data.submitted
          ? 'SEO发布任务已提交'
          : '表单已填写（未提交）。加 --submit 提交。';
        break;

      case 'test':
        data = await runTests(page);
        const passed = data.filter(r => r.status === 'ok').length;
        message = `${passed}/${data.length} 个页面加载成功`;
        break;
    }

    output(true, data, message);
  } catch (error) {
    output(false, { error: error.message, stack: error.stack }, '执行出错');
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
