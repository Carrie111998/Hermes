// Quantum Plasma Galaxy Engine - Module Export
// This module provides the QuantumGalaxy class and engine classes for Hermes integration

// ===== QUANTUM PLASMA GALAXY ENGINE =====


class QuantumGalaxy {
  constructor(options = {}) {
    this.options = {
      container: options.container || document.body,
      autoStart: options.autoStart !== false,
      ...options
    };
    
    this.canvas = document.createElement('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.options.container.appendChild(this.canvas);
    
    this.resize();
    window.addEventListener('resize', () => this.resize());
    
    // Initialize systems
    this.plasmaEngine = new PlasmaEngine();
    this.liquidEngine = new LiquidEngine();
    this.vortexEngine = new VortexEngine();
    this.helpSystem = new HelpSystem();
    this.performanceMonitor = new PerformanceMonitor();
    
    // Load vault data
    this.vaultData = null;
    this.nodes = [];
    this.connections = [];
    this.labels = [];
    
    // State
    this.isPaused = false;
    this.helpVisible = false;
    this.performanceLocked = true;
    this.targetFPS = 60;
    this.lastTime = 0;
    this.frameCount = 0;
    this.fps = 0;
    
    // Interaction
    this.mouse = { x: 0, y: 0, down: false };
    this.initMouseEvents();
    
    // Initialize
    if (this.options.autoStart) {
      this.init();
    }
  }
  
  resize() {
    this.canvas.width = window.innerWidth;
    this.canvas.height = window.innerHeight;
    this.width = this.canvas.width;
    this.height = this.canvas.height;
  }
  
  async init() {
    // Load vault data
    await this.loadVaultData();
    
    // Initialize engines
    this.plasmaEngine.init(this);
    this.liquidEngine.init(this);
    this.vortexEngine.init(this);
    
    // Create visual elements
    this.createVisualElements();
    
    // Start animation loop
    this.lastTime = performance.now();
    requestAnimationFrame(this.animate.bind(this));
  }
  
  async loadVaultData(vaultData) {
    // If vaultData is provided, use it; otherwise use embedded data
    if (vaultData) {
      this.vaultData = vaultData;
    } else {
      // Use the real vault data we generated earlier
      this.vaultData = this.getDefaultVaultData();
    }
    
    // Process nodes
    this.nodes = this.vaultData.nodes.map((node, index) => ({
      id: node.id,
      name: node.name,
      category: node.category,
      path: node.path,
      full_path: node.full_path,
      // Initialize physics properties
      x: Math.random() * 800 - 400,
      y: Math.random() * 600 - 300,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null
    }));
    
    // Process links
    this.links = this.vaultData.links.map(link => ({
      source: link.source,
      target: link.target,
      type: link.type
    }));
  }
  
  getDefaultVaultData() {
    // Return the embedded vault data from user's Obsidian vault
    return {
  "nodes": [
    {
      "id": "dashboard",
      "name": "Dashboard",
      "category": "Notes",
      "path": "Dashboard.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Dashboard.md"
    },
    {
      "id": "untitled",
      "name": "Untitled",
      "category": "Notes",
      "path": "Untitled.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Untitled.md"
    },
    {
      "id": "00-memory-orbits-dashboard",
      "name": "00-Memory-Orbits-Dashboard",
      "category": "Notes",
      "path": "00-Memory-Orbits-Dashboard.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/00-Memory-Orbits-Dashboard.md"
    },
    {
      "id": "home",
      "name": "Home",
      "category": "Notes",
      "path": "Home.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Home.md"
    },
    {
      "id": "test-note",
      "name": "Test note",
      "category": "Notes",
      "path": "Test note.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Test note.md"
    },
    {
      "id": "2026-08-03",
      "name": "2026-08-03",
      "category": "Notes",
      "path": "2026-08-03.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/2026-08-03.md"
    },
    {
      "id": "00-vault-organization",
      "name": "00-Vault-Organization",
      "category": "Notes",
      "path": "00-Vault-Organization.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/00-Vault-Organization.md"
    },
    {
      "id": "rlay-auto-index",
      "name": "Auto-Index",
      "category": "Notes",
      "path": "RLay/Auto-Index.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/RLay/Auto-Index.md"
    },
    {
      "id": "rlay-index",
      "name": "Index",
      "category": "Notes",
      "path": "RLay/Index.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/RLay/Index.md"
    },
    {
      "id": "inbox-create-a-link",
      "name": "create a link",
      "category": "Notes",
      "path": "Inbox/create a link.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Inbox/create a link.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-rewrite-as-tweet",
      "name": "Rewrite as tweet",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Rewrite as tweet.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Rewrite as tweet.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-clip-web-page",
      "name": "Clip Web Page",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Clip Web Page.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Clip Web Page.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-fix-grammar-and-spelling",
      "name": "Fix grammar and spelling",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Fix grammar and spelling.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Fix grammar and spelling.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-make-longer",
      "name": "Make longer",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Make longer.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Make longer.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-explain-like-i-am-5",
      "name": "Explain like I am 5",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Explain like I am 5.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Explain like I am 5.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-translate-to-chinese",
      "name": "Translate to Chinese",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Translate to Chinese.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Translate to Chinese.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-clip-youtube-transcript",
      "name": "Clip YouTube Transcript",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Clip YouTube Transcript.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Clip YouTube Transcript.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-make-shorter",
      "name": "Make shorter",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Make shorter.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Make shorter.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-rewrite-as-tweet-thread",
      "name": "Rewrite as tweet thread",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Rewrite as tweet thread.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Rewrite as tweet thread.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-simplify",
      "name": "Simplify",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Simplify.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Simplify.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-emojify",
      "name": "Emojify",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Emojify.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Emojify.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-generate-table-of-contents",
      "name": "Generate table of contents",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Generate table of contents.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Generate table of contents.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-summarize",
      "name": "Summarize",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Summarize.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Summarize.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-remove-urls",
      "name": "Remove URLs",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Remove URLs.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Remove URLs.md"
    },
    {
      "id": "copilot-copilot-custom-prompts-generate-glossary",
      "name": "Generate glossary",
      "category": "Copilot",
      "path": "copilot/copilot-custom-prompts/Generate glossary.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/copilot/copilot-custom-prompts/Generate glossary.md"
    },
    {
      "id": "projects-mg-win-food-delivery-app-mg-win-food-delivery-app",
      "name": "Mg Win Food Delivery App",
      "category": "Projects",
      "path": "Projects/Mg Win Food Delivery App/Mg Win Food Delivery App.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Projects/Mg Win Food Delivery App/Mg Win Food Delivery App.md"
    },
    {
      "id": "resources-freetiers",
      "name": "FREE_TIERS",
      "category": "Resources",
      "path": "Resources/FREE_TIERS.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/FREE_TIERS.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-gettags",
      "name": "getTags",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getTags.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getTags.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-getoutline",
      "name": "getOutline",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getOutline.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getOutline.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-getideas",
      "name": "getIdeas",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getIdeas.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getIdeas.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-rewrite",
      "name": "rewrite",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/rewrite.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/rewrite.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-gettitles",
      "name": "getTitles",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getTitles.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getTitles.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-summarizelarge",
      "name": "summarizeLarge",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/summarizeLarge.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/summarizeLarge.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-simplify",
      "name": "simplify",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/simplify.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/simplify.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-getparagraph",
      "name": "getParagraph",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getParagraph.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getParagraph.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-getemailneg",
      "name": "getEmailNeg",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getEmailNeg.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getEmailNeg.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-summarize",
      "name": "summarize",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/summarize.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/summarize.md"
    },
    {
      "id": "resources-ai-prompts-templates-default-getemailpos",
      "name": "getEmailPos",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/default/getEmailPos.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/default/getEmailPos.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artvangogh",
      "name": "artVanGogh",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artVanGogh.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artVanGogh.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artwatercolor",
      "name": "artWatercolor",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artWatercolor.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artWatercolor.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-modfanart",
      "name": "modFanart",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/modFanart.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/modFanart.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artstock",
      "name": "artStock",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artStock.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artStock.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artdeco",
      "name": "artDeco",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artDeco.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artDeco.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-quamacro",
      "name": "quaMacro",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/quaMacro.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/quaMacro.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-ligstudio",
      "name": "ligStudio",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/ligStudio.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/ligStudio.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artspraypainted",
      "name": "artSprayPainted",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artSprayPainted.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artSprayPainted.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-quabokeh",
      "name": "quaBokeh",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/quaBokeh.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/quaBokeh.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-ligflare",
      "name": "ligFlare",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/ligFlare.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/ligFlare.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-art3d",
      "name": "art3D",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/art3D.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/art3D.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artbauhaus",
      "name": "artBauhaus",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artBauhaus.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artBauhaus.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artsalvadordali",
      "name": "artSalvadorDali",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artSalvadorDali.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artSalvadorDali.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-sitnature",
      "name": "sitNature",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/sitNature.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/sitNature.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-getphotos",
      "name": "getPhotos",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/getPhotos.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/getPhotos.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artchildrendrawing",
      "name": "artChildrenDrawing",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artChildrenDrawing.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artChildrenDrawing.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artcyberpunk",
      "name": "artCyberpunk",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artCyberpunk.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artCyberpunk.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artnormanrockwell",
      "name": "artNormanRockwell",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artNormanRockwell.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artNormanRockwell.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-qua15mm",
      "name": "qua15mm",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/qua15mm.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/qua15mm.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artline",
      "name": "artLine",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artLine.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artLine.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artmodern",
      "name": "artModern",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artModern.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artModern.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-modphotorealistic",
      "name": "modPhotorealistic",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/modPhotorealistic.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/modPhotorealistic.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-qua4k",
      "name": "qua4K",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/qua4K.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/qua4K.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artukiyoe",
      "name": "artUkiyoe",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artUkiyoe.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artUkiyoe.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-getphoto2",
      "name": "getPhoto2",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/getPhoto2.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/getPhoto2.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-arttakashimurakami",
      "name": "artTakashiMurakami",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artTakashiMurakami.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artTakashiMurakami.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artwarhol",
      "name": "artWarhol",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artWarhol.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artWarhol.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artvaporwave",
      "name": "artVaporwave",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artVaporwave.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artVaporwave.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-liggoldenhour",
      "name": "ligGoldenHour",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/ligGoldenHour.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/ligGoldenHour.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-getphoto",
      "name": "getPhoto",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/getPhoto.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/getPhoto.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-qua35mm",
      "name": "qua35mm",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/qua35mm.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/qua35mm.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-dall-e-core",
      "name": "dall-e-core",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/dall-e-core.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/dall-e-core.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artlowpoly",
      "name": "artLowPoly",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artLowPoly.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artLowPoly.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artclaymation",
      "name": "artClaymation",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artClaymation.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artClaymation.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artcartoon",
      "name": "artCartoon",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artCartoon.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artCartoon.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artbanksy",
      "name": "artBanksy",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artBanksy.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artBanksy.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-ligambient",
      "name": "ligAmbient",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/ligAmbient.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/ligAmbient.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artpixel",
      "name": "artPixel",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artPixel.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artPixel.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artdigital",
      "name": "artDigital",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artDigital.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artDigital.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artgraffiti",
      "name": "artGraffiti",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artGraffiti.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artGraffiti.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artpencilsketch",
      "name": "artPencilSketch",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artPencilSketch.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artPencilSketch.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artpsychedelic",
      "name": "artPsychedelic",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artPsychedelic.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artPsychedelic.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-modawardwinning",
      "name": "modAwardWinning",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/modAwardWinning.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/modAwardWinning.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-arttimburton",
      "name": "artTimBurton",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artTimBurton.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artTimBurton.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artsteampunk",
      "name": "artSteampunk",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artSteampunk.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artSteampunk.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artcoloringbook",
      "name": "artColoringBook",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artColoringBook.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artColoringBook.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artsynthwave",
      "name": "artSynthwave",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artSynthwave.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artSynthwave.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-modartstation",
      "name": "modArtStation",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/modArtStation.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/modArtStation.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-quacinematic",
      "name": "quaCinematic",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/quaCinematic.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/quaCinematic.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-qua200mm",
      "name": "qua200mm",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/qua200mm.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/qua200mm.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artanime",
      "name": "artAnime",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artAnime.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artAnime.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-quatiltshift",
      "name": "quaTiltShift",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/quaTiltShift.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/quaTiltShift.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-moddetailed",
      "name": "modDetailed",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/modDetailed.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/modDetailed.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artballpointpen",
      "name": "artBallPointPen",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artBallPointPen.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artBallPointPen.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-qua85mm",
      "name": "qua85mm",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/qua85mm.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/qua85mm.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-ligcinematic",
      "name": "ligCinematic",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/ligCinematic.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/ligCinematic.md"
    },
    {
      "id": "resources-ai-prompts-templates-dalle-artglitchcore",
      "name": "artGlitchcore",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/dalle/artGlitchcore.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/dalle/artGlitchcore.md"
    },
    {
      "id": "resources-ai-prompts-templates-tts-speakalloy",
      "name": "speakAlloy",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/tts/speakAlloy.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/tts/speakAlloy.md"
    },
    {
      "id": "resources-ai-prompts-templates-tts-listen",
      "name": "listen",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/tts/listen.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/tts/listen.md"
    },
    {
      "id": "resources-ai-prompts-templates-huggingface-summarizebart",
      "name": "summarizeBART",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/huggingface/summarizeBART.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/huggingface/summarizeBART.md"
    },
    {
      "id": "resources-ai-prompts-templates-huggingface-classify-bart-large-mnli",
      "name": "classify-bart-large-mnli",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/huggingface/classify-bart-large-mnli.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/huggingface/classify-bart-large-mnli.md"
    },
    {
      "id": "resources-ai-prompts-templates-huggingface-completetextbloom",
      "name": "completeTextBloom",
      "category": "Resources",
      "path": "Resources/AI Prompts/templates/huggingface/completeTextBloom.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Resources/AI Prompts/templates/huggingface/completeTextBloom.md"
    },
    {
      "id": "daily-notes-2026-07-26",
      "name": "2026-07-26",
      "category": "Notes",
      "path": "Daily Notes/2026-07-26.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Daily Notes/2026-07-26.md"
    },
    {
      "id": "daily-notes-2026-07-29",
      "name": "2026-07-29",
      "category": "Notes",
      "path": "Daily Notes/2026-07-29.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Daily Notes/2026-07-29.md"
    },
    {
      "id": "daily-notes-2026-07-28",
      "name": "2026-07-28",
      "category": "Notes",
      "path": "Daily Notes/2026-07-28.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Daily Notes/2026-07-28.md"
    },
    {
      "id": "daily-notes-2026-08-01",
      "name": "2026-08-01",
      "category": "Notes",
      "path": "Daily Notes/2026-08-01.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Daily Notes/2026-08-01.md"
    },
    {
      "id": "daily-notes-2026-07-21",
      "name": "2026-07-21",
      "category": "Notes",
      "path": "Daily Notes/2026-07-21.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Daily Notes/2026-07-21.md"
    },
    {
      "id": "daily-notes-2026-07-14",
      "name": "2026-07-14",
      "category": "Notes",
      "path": "Daily Notes/2026-07-14.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Daily Notes/2026-07-14.md"
    },
    {
      "id": "templates-weekly-orbit-review",
      "name": "Weekly Orbit Review",
      "category": "Templates",
      "path": "Templates/Weekly Orbit Review.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Templates/Weekly Orbit Review.md"
    },
    {
      "id": "templates-project",
      "name": "Project",
      "category": "Templates",
      "path": "Templates/Project.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Templates/Project.md"
    },
    {
      "id": "templates-daily-note",
      "name": "Daily Note",
      "category": "Templates",
      "path": "Templates/Daily Note.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Templates/Daily Note.md"
    },
    {
      "id": "templates-resource",
      "name": "Resource",
      "category": "Templates",
      "path": "Templates/Resource.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Templates/Resource.md"
    },
    {
      "id": "templates-project-plan",
      "name": "Project Plan",
      "category": "Templates",
      "path": "Templates/Project Plan.md",
      "full_path": "/Users/icaretradein/Library/Mobile Documents/iCloud~md~obsidian/Documents/RLay\"s second brain/RLay/Templates/Project Plan.md"
    }
  ],
  "links": [
    {
      "source": "dashboard",
      "target": "00-vault-organization",
      "type": "wikilink"
    },
    {
      "source": "dashboard",
      "target": "templates-daily-note",
      "type": "wikilink"
    },
    {
      "source": "dashboard",
      "target": "templates-project",
      "type": "wikilink"
    },
    {
      "source": "dashboard",
      "target": "templates-resource",
      "type": "wikilink"
    },
    {
      "source": "00-memory-orbits-dashboard",
      "target": "templates-daily-note",
      "type": "wikilink"
    },
    {
      "source": "00-memory-orbits-dashboard",
      "target": "templates-project",
      "type": "wikilink"
    },
    {
      "source": "00-memory-orbits-dashboard",
      "target": "templates-resource",
      "type": "wikilink"
    },
    {
      "source": "rlay-auto-index",
      "target": "templates-daily-note",
      "type": "wikilink"
    },
    {
      "source": "rlay-auto-index",
      "target": "templates-project",
      "type": "wikilink"
    },
    {
      "source": "rlay-auto-index",
      "target": "templates-weekly-orbit-review",
      "type": "wikilink"
    },
    {
      "source": "rlay-auto-index",
      "target": "templates-resource",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "templates-daily-note",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "templates-project",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "templates-weekly-orbit-review",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "templates-resource",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-freetiers",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-summarize",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-simplify",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-rewrite",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-getoutline",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-getideas",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-gettags",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-gettitles",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-getparagraph",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-getemailpos",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-default-getemailneg",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-modphotorealistic",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-quacinematic",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-quabokeh",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-quamacro",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-quatiltshift",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-qua15mm",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-qua35mm",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-qua85mm",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-qua200mm",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-qua4k",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-dalle-sitnature",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-huggingface-completetextbloom",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-huggingface-classify-bart-large-mnli",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-tts-speakalloy",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "resources-ai-prompts-templates-tts-listen",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-translate-to-chinese",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-rewrite-as-tweet",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-remove-urls",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-make-shorter",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-make-longer",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-generate-table-of-contents",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-generate-glossary",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-fix-grammar-and-spelling",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-explain-like-i-am-5",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-emojify",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-clip-youtube-transcript",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "copilot-copilot-custom-prompts-clip-web-page",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "test-note",
      "type": "wikilink"
    },
    {
      "source": "rlay-index",
      "target": "untitled",
      "type": "wikilink"
    },
    {
      "source": "templates-daily-note",
      "target": "dashboard",
      "type": "wikilink"
    },
    {
      "source": "templates-project-plan",
      "target": "templates-project",
      "type": "wikilink"
    }
  ]
};
  }
  
  createVisualElements() {
    // Create particle field
    this.particles = [];
    const particleCount = 150;
    for (let i = 0; i < particleCount; i++) {
      this.particles.push({
        x: Math.random() * this.width,
        y: Math.random() * this.height,
        vx: (Math.random() - 0.5) * 0.5,
        vy: (Math.random() - 0.5) * 0.5,
        size: Math.random() * 2 + 0.5,
        color: this.getPlasmaColor(),
        life: Math.random() * 100,
        maxLife: 100 + Math.random() * 200
      });
    }
    
    // Create DOM elements for connections
    this.connectionElements = [];
    this.links.forEach(() => {
      const line = document.createElement('div');
      line.className = 'connection-line';
      line.style.position = 'absolute';
      line.style.pointerEvents = 'none';
      line.style.zIndex = '1';
      this.options.container.appendChild(line);
      this.connectionElements.push(line);
    });
    
    // Create DOM elements for node labels
    this.nodeLabelsContainer = document.createElement('div');
    this.nodeLabelsContainer.id = 'node-labels-container';
    this.nodeLabelsContainer.style.position = 'absolute';
    this.nodeLabelsContainer.style.top = '0';
    this.nodeLabelsContainer.style.left = '0';
    this.nodeLabelsContainer.style.width = '100%';
    this.nodeLabelsContainer.style.height = '100%';
    this.nodeLabelsContainer.style.pointerEvents = 'none';
    this.nodeLabelsContainer.style.zIndex = '10';
    this.options.container.appendChild(this.nodeLabelsContainer);
  }
  
  getPlasmaColor() {
    const hue = 180 + Math.random() * 120; // Cyan to magenta range
    return `hsl(${hue}, 80%, 60%)`;
  }
  
  initMouseEvents() {
    this.canvas.addEventListener('mousedown', (e) => {
      this.mouse.down = true;
      this.mouse.x = e.clientX;
      this.mouse.y = e.clientY;
    });
    
    this.canvas.addEventListener('mousemove', (e) => {
      this.mouse.x = e.clientX;
      this.mouse.y = e.clientY;
    });
    
    this.canvas.addEventListener('mouseup', () => {
      this.mouse.down = false;
    });
    
    this.canvas.addEventListener('mouseleave', () => {
      this.mouse.down = false;
    });
  }
  
  animate(currentTime) {
    if (this.isPaused) {
      this.lastTime = currentTime;
      requestAnimationFrame(this.animate.bind(this));
      return;
    }
    
    const deltaTime = Math.min((currentTime - this.lastTime) / 1000, 0.1);
    this.lastTime = currentTime;
    
    // FPS calculation
    this.frameCount++;
    if (currentTime - this.lastTime >= 1000) {
      this.fps = this.frameCount;
      this.frameCount = 0;
      this.lastTime = currentTime;
    }
    
    // Update
    this.update(deltaTime);
    
    // Render
    this.render();
    
    requestAnimationFrame(this.animate.bind(this));
  }
  
  update(deltaTime) {
    // Update engines
    this.plasmaEngine.update(deltaTime, this);
    this.liquidEngine.update(deltaTime, this);
    this.vortexEngine.update(deltaTime, this);
    
    // Update nodes with physics
    this.updateNodes(deltaTime);
    
    // Update particles
    this.updateParticles(deltaTime);
    
    // Update help system
    this.helpSystem.update();
    
    // Update performance monitor
    this.performanceMonitor.update();
  }
  
  updateNodes(deltaTime) {
    const magneticStrength = parseFloat(document.getElementById('magnetic-strength')?.value) || 1.0;
    const vortexIntensity = parseFloat(document.getElementById('vortex-intensity')?.value) || 1.0;
    
    this.nodes.forEach(node => {
      // Apply forces from engines
      let fx = 0, fy = 0;
      
      // Magnetic pinch forces
      this.plasmaEngine.pinches.forEach(pinch => {
        const dx = pinch.x - node.x;
        const dy = pinch.y - node.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist > 0 && dist < pinch.radius) {
          const force = pinch.strength * magneticStrength * (1 - dist / pinch.radius);
          fx += (dx / dist) * force;
          fy += (dy / dist) * force;
        }
      });
      
      // Liquid vortex forces
      this.liquidEngine.vortices.forEach(vortex => {
        const dx = vortex.x - node.x;
        const dy = vortex.y - node.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist > 0 && dist < vortex.radius) {
          const force = vortex.strength * vortexIntensity * (1 - dist / vortex.radius);
          // Tangential force for swirling
          fx += (-dy / dist) * force;
          fy += (dx / dist) * force;
        }
      });
      
      // Vortex engine black holes
      this.vortexEngine.blackHoles.forEach(bh => {
        const dx = bh.x - node.x;
        const dy = bh.y - node.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist > bh.eventHorizon && dist < bh.accretionDisk) {
          const force = bh.mass / (dist * dist) * 10000;
          fx += (dx / dist) * force;
          fy += (dy / dist) * force;
        }
      });
      
      // Apply forces
      node.vx += fx * deltaTime;
      node.vy += fy * deltaTime;
      
      // Damping
      node.vx *= 0.99;
      node.vy *= 0.99;
      
      // Update position
      node.x += node.vx * deltaTime * 60;
      node.y += node.vy * deltaTime * 60;
      
      // Boundary wrapping
      if (node.x < -100) node.x = this.width + 100;
      if (node.x > this.width + 100) node.x = -100;
      if (node.y < -100) node.y = this.height + 100;
      if (node.y > this.height + 100) node.y = -100;
    });
  }
  
  updateParticles(deltaTime) {
    this.particles.forEach(p => {
      p.x += p.vx * deltaTime * 60;
      p.y += p.vy * deltaTime * 60;
      p.life -= deltaTime * 60;
      
      if (p.life <= 0) {
        p.x = Math.random() * this.width;
        p.y = Math.random() * this.height;
        p.life = p.maxLife;
        p.color = this.getPlasmaColor();
      }
      
      // Boundary wrapping
      if (p.x < 0) p.x = this.width;
      if (p.x > this.width) p.x = 0;
      if (p.y < 0) p.y = this.height;
      if (p.y > this.height) p.y = 0;
    });
  }
  
  render() {
    const ctx = this.ctx;
    const w = this.width, h = this.height;
    
    // Clear with dark background
    ctx.fillStyle = '#0a0a0f';
    ctx.fillRect(0, 0, w, h);
    
    // Render connections
    this.renderConnections();
    
    // Render particles
    this.renderParticles();
    
    // Render nodes
    this.renderNodes();
    
    // Render labels
    this.renderLabels();
    
    // Render performance monitor
    if (this.performanceMonitor.visible) {
      this.performanceMonitor.render(ctx);
    }
  }
  
  renderConnections() {
    const ctx = this.ctx;
    this.links.forEach((link, index) => {
      const sourceNode = this.nodes.find(n => n.id === link.source);
      const targetNode = this.nodes.find(n => n.id === link.target);
      
      if (sourceNode && targetNode) {
        const dx = targetNode.x - sourceNode.x;
        const dy = targetNode.y - sourceNode.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        
        if (dist < 300) { // Only render nearby connections
          ctx.beginPath();
          ctx.moveTo(sourceNode.x, sourceNode.y);
          ctx.lineTo(targetNode.x, targetNode.y);
          
          const alpha = Math.max(0.1, 1 - dist / 300);
          const color = this.getNodeColor(sourceNode.category);
          ctx.strokeStyle = this.hexToRgba(color, alpha * 0.3);
          ctx.lineWidth = 1;
          ctx.stroke();
        }
      }
    });
  }
  
  renderParticles() {
    const ctx = this.ctx;
    this.particles.forEach(p => {
      const alpha = Math.min(1, p.life / p.maxLife);
      ctx.fillStyle = this.hexToRgba(p.color, alpha * 0.6);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fill();
    });
  }
  
  renderNodes() {
    const ctx = this.ctx;
    this.nodes.forEach(node => {
      const size = this.getNodeSize(node.category);
      const color = this.getNodeColor(node.category);
      
      // Outer glow
      const glowGradient = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, size * 3);
      glowGradient.addColorStop(0, this.hexToRgba(color, 0.3));
      glowGradient.addColorStop(1, this.hexToRgba(color, 0));
      ctx.fillStyle = glowGradient;
      ctx.beginPath();
      ctx.arc(node.x, node.y, size * 3, 0, Math.PI * 2);
      ctx.fill();
      
      // Inner glow
      const innerGradient = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, size);
      innerGradient.addColorStop(0, this.hexToRgba(color, 1));
      innerGradient.addColorStop(1, this.hexToRgba(color, 0.3));
      ctx.fillStyle = innerGradient;
      ctx.beginPath();
      ctx.arc(node.x, node.y, size, 0, Math.PI * 2);
      ctx.fill();
      
      // Core
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, size * 0.5, 0, Math.PI * 2);
      ctx.fill();
    });
  }
  
  renderLabels() {
    // Labels are handled via DOM elements
    this.nodeLabelsContainer.innerHTML = '';
    
    this.nodes.forEach(node => {
      const dist = Math.hypot(node.x - this.mouse.x, node.y - this.mouse.y);
      if (dist < 100) {
        const label = document.createElement('div');
        label.className = 'node-label';
        label.textContent = node.name;
        label.style.position = 'absolute';
        label.style.left = node.x + 'px';
        label.style.top = node.y + 'px';
        label.style.transform = 'translate(-50%, -100%)';
        label.style.color = this.getNodeColor(node.category);
        label.style.fontSize = '12px';
        label.style.fontFamily = 'JetBrains Mono, monospace';
        label.style.whiteSpace = 'nowrap';
        label.style.textShadow = '0 0 8px currentColor';
        label.style.pointerEvents = 'none';
        label.style.opacity = 1 - dist / 100;
        this.nodeLabelsContainer.appendChild(label);
      }
    });
  }
  
  getNodeMass(category) {
    switch(category) {
      case "Projects": return 5.0;
      case "Templates": return 3.0;
      case "Resources": return 1.0;
      case "Copilot": return 2.0;
      case "Notes": return 1.5;
      default: return 1.0;
    }
  }
  
  getNodeCharge(category) {
    switch(category) {
      case "Projects": return 2.0;
      case "Templates": return 1.5;
      case "Resources": return 0.5;
      case "Copilot": return 1.2;
      case "Notes": return 1.0;
      default: return 1.0;
    }
  }
  
  getNodeSize(category) {
    switch(category) {
      case "Projects": return 12;
      case "Templates": return 10;
      case "Resources": return 6;
      case "Copilot": return 8;
      case "Notes": return 7;
      default: return 6;
    }
  }
  
  getNodeColor(category) {
    switch(category) {
      case "Projects": return "#00ffff";
      case "Templates": return "#ff00ff";
      case "Resources": return "#ffff00";
      case "Copilot": return "#a855f7";
      case "Notes": return "#ffffff";
      default: return "#ffffff";
    }
  }
  
  hexToRgb(hex) {
    hex = hex.replace('#', '');
    const bigint = parseInt(hex, 16);
    const r = (bigint >> 16) & 255;
    const g = (bigint >> 8) & 255;
    const b = bigint & 255;
    return `${r},${g},${b}`;
  }
  
  hexToRgba(hex, alpha) {
    const rgb = this.hexToRgb(hex);
    return `rgba(${rgb},${alpha})`;
  }
  
  destroy() {
    // Clean up
    if (this.canvas && this.canvas.parentNode) {
      this.canvas.parentNode.removeChild(this.canvas);
    }
    if (this.nodeLabelsContainer && this.nodeLabelsContainer.parentNode) {
      this.nodeLabelsContainer.parentNode.removeChild(this.nodeLabelsContainer);
    }
    this.connectionElements.forEach(el => {
      if (el.parentNode) el.parentNode.removeChild(el);
    });
    window.removeEventListener('resize', () => this.resize());
  }
}

// ===== PLASMA ENGINE =====

class PlasmaEngine {
  init(galaxy) {
    this.galaxy = galaxy;
    this.pinches = [];
    this.createPinches();
  }
  
  createPinches() {
    const pinchCount = 3;
    for (let i = 0; i < pinchCount; i++) {
      this.pinches.push({
        x: Math.random() * this.galaxy.width,
        y: Math.random() * this.galaxy.height,
        radius: 200 + Math.random() * 200,
        strength: 0.5 + Math.random() * 0.5,
        phase: Math.random() * Math.PI * 2,
        speed: 0.01 + Math.random() * 0.02
      });
    }
  }
  
  update(deltaTime, galaxy) {
    this.pinches.forEach(pinch => {
      pinch.phase += pinch.speed * deltaTime * 60;
      pinch.x += Math.sin(pinch.phase) * 0.5;
      pinch.y += Math.cos(pinch.phase) * 0.3;
      
      // Wrap around
      if (pinch.x < -pinch.radius) pinch.x = galaxy.width + pinch.radius;
      if (pinch.x > galaxy.width + pinch.radius) pinch.x = -pinch.radius;
      if (pinch.y < -pinch.radius) pinch.y = galaxy.height + pinch.radius;
      if (pinch.y > galaxy.height + pinch.radius) pinch.y = -pinch.radius;
    });
  }
}

// ===== LIQUID ENGINE =====

class LiquidEngine {
  init(galaxy) {
    this.galaxy = galaxy;
    this.vortices = [];
    this.createVortices();
  }
  
  createVortices() {
    const vortexCount = 2;
    for (let i = 0; i < vortexCount; i++) {
      this.vortices.push({
        x: Math.random() * this.galaxy.width,
        y: Math.random() * this.galaxy.height,
        radius: 150 + Math.random() * 150,
        strength: 0.3 + Math.random() * 0.4,
        direction: Math.random() > 0.5 ? 1 : -1,
        phase: Math.random() * Math.PI * 2
      });
    }
  }
  
  update(deltaTime, galaxy) {
    this.vortices.forEach(vortex => {
      vortex.phase += 0.01 * deltaTime * 60;
      vortex.x += Math.sin(vortex.phase) * 0.3;
      vortex.y += Math.cos(vortex.phase) * 0.2;
      
      if (vortex.x < -vortex.radius) vortex.x = galaxy.width + vortex.radius;
      if (vortex.x > galaxy.width + vortex.radius) vortex.x = -vortex.radius;
      if (vortex.y < -vortex.radius) vortex.y = galaxy.height + vortex.radius;
      if (vortex.y > galaxy.height + vortex.radius) vortex.y = -vortex.radius;
    });
  }
}

// ===== VORTEX ENGINE =====

class VortexEngine {
  init(galaxy) {
    this.galaxy = galaxy;
    this.blackHoles = [];
    this.createBlackHoles();
  }
  
  createBlackHoles() {
    this.blackHoles.push({
      x: this.galaxy.width * 0.3,
      y: this.galaxy.height * 0.5,
      mass: 50000,
      eventHorizon: 50,
      accretionDisk: 200,
      rotationSpeed: 0.02
    });
    
    this.blackHoles.push({
      x: this.galaxy.width * 0.7,
      y: this.galaxy.height * 0.5,
      mass: 30000,
      eventHorizon: 40,
      accretionDisk: 180,
      rotationSpeed: -0.015
    });
  }
  
  update(deltaTime, galaxy) {
    this.blackHoles.forEach(bh => {
      // Black holes are stationary but their effects rotate
    });
  }
}

// ===== HELP SYSTEM =====

class HelpSystem {
  constructor() {
    this.visible = false;
    this.lastUpdate = 0;
    this.tooltip = null;
    this.createTooltip();
  }
  
  createTooltip() {
    this.tooltip = document.createElement('div');
    this.tooltip.style.position = 'fixed';
    this.tooltip.style.background = 'rgba(0, 0, 0, 0.9)';
    this.tooltip.style.border = '1px solid #00ffff';
    this.tooltip.style.borderRadius = '8px';
    this.tooltip.style.padding = '12px';
    this.tooltip.style.color = '#ffffcc';
    this.tooltip.style.fontSize = '13px';
    this.tooltip.style.fontFamily = 'JetBrains Mono, monospace';
    this.tooltip.style.zIndex = '10000';
    this.tooltip.style.pointerEvents = 'none';
    this.tooltip.style.display = 'none';
    this.tooltip.style.maxWidth = '300px';
    this.tooltip.style.boxShadow = '0 0 20px rgba(0, 255, 234, 0.3)';
    document.body.appendChild(this.tooltip);
  }
  
  update() {
    if (this.visible && Date.now() - this.lastUpdate > 5000) {
      this.hide();
    }
  }
  
  updateTooltip(x, y, info) {
    if (!info) {
      this.tooltip.style.display = 'none';
      return;
    }
    
    this.tooltip.innerHTML = `
      <div style="font-weight: bold; color: #00ffff; margin-bottom: 8px;">${info.title}</div>
      <div style="color: #ffffcc; line-height: 1.4;">${info.content}</div>
    `;
    this.tooltip.style.left = (x + 15) + 'px';
    this.tooltip.style.top = (y - 10) + 'px';
    this.tooltip.style.display = 'block';
  }
  
  getInfoAtPosition(x, y) {
    if (this.galaxy && this.galaxy.nodes) {
      for (const node of this.galaxy.nodes) {
        const dist = Math.hypot(node.x - x, node.y - y);
        if (dist < node.size + 20) {
          return {
            title: node.name,
            content: `Category: ${node.category}<br>Connections: ${node.connections?.length || 0}`
          };
        }
      }
    }
    return null;
  }
  
  hide() {
    this.visible = false;
    if (this.tooltip) {
      this.tooltip.style.display = 'none';
    }
  }
  
  show() {
    this.visible = true;
    this.lastUpdate = Date.now();
  }
  
  toggle() {
    if (this.visible) this.hide();
    else this.show();
  }
}

// ===== PERFORMANCE MONITOR =====

class PerformanceMonitor {
  constructor() {
    this.visible = true;
    this.fps = 0;
    this.frameTime = 0;
    this.lastTime = 0;
    this.frameCount = 0;
  }
  
  update() {
    const now = performance.now();
    this.frameCount++;
    
    if (now - this.lastTime >= 1000) {
      this.fps = this.frameCount;
      this.frameTime = 1000 / this.fps;
      this.frameCount = 0;
      this.lastTime = now;
    }
  }
  
  render(ctx) {
    ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
    ctx.fillRect(10, 10, 180, 60);
    ctx.strokeStyle = '#00ffff';
    ctx.strokeRect(10, 10, 180, 60);
    
    ctx.fillStyle = '#00ffff';
    ctx.font = '12px JetBrains Mono, monospace';
    ctx.fillText(`FPS: ${this.fps}`, 20, 30);
    ctx.fillText(`Frame: ${this.frameTime.toFixed(2)}ms`, 20, 50);
  }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    QuantumGalaxy,
    PlasmaEngine,
    LiquidEngine,
    VortexEngine,
    HelpSystem,
    PerformanceMonitor
  };
}

// Export for browser global usage
if (typeof window !== 'undefined') {
  window.QuantumGalaxy = QuantumGalaxy;
  window.PlasmaEngine = PlasmaEngine;
  window.LiquidEngine = LiquidEngine;
  window.VortexEngine = VortexEngine;
  window.HelpSystem = HelpSystem;
  window.PerformanceMonitor = PerformanceMonitor;
}
