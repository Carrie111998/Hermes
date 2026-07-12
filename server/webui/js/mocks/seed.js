/* ============================================================
   Silverine demo dataset.
   Narrative: Silverine (Turkish kitchen-appliance manufacturer /
   exporter) is ~3 weeks into using interfaze-agent. The Company
   Brain is approved, scans for Germany and UAE are complete, a
   Saudi Arabia scan is running live, and Netherlands + UK are
   untouched — ready for a live lead-map demo.
   Deterministic (seeded RNG) so every reload shows the same world.
   ============================================================ */

export const COUNTRY_NAMES = {
  AE: 'United Arab Emirates', AF: 'Afghanistan', AL: 'Albania', AM: 'Armenia', AO: 'Angola',
  AR: 'Argentina', AT: 'Austria', AU: 'Australia', AZ: 'Azerbaijan', BA: 'Bosnia and Herzegovina',
  BD: 'Bangladesh', BE: 'Belgium', BG: 'Bulgaria', BR: 'Brazil', BY: 'Belarus',
  CA: 'Canada', CH: 'Switzerland', CL: 'Chile', CN: 'China', CO: 'Colombia',
  CZ: 'Czechia', DE: 'Germany', DK: 'Denmark', DZ: 'Algeria', EE: 'Estonia',
  EG: 'Egypt', ES: 'Spain', ET: 'Ethiopia', FI: 'Finland', FR: 'France',
  GB: 'United Kingdom', GE: 'Georgia', GH: 'Ghana', GR: 'Greece', HR: 'Croatia',
  HU: 'Hungary', ID: 'Indonesia', IE: 'Ireland', IL: 'Israel', IN: 'India',
  IQ: 'Iraq', IR: 'Iran', IT: 'Italy', JO: 'Jordan', JP: 'Japan',
  KE: 'Kenya', KR: 'South Korea', KW: 'Kuwait', KZ: 'Kazakhstan', LB: 'Lebanon',
  LT: 'Lithuania', LV: 'Latvia', LY: 'Libya', MA: 'Morocco', MD: 'Moldova',
  MK: 'North Macedonia', MX: 'Mexico', MY: 'Malaysia', NG: 'Nigeria', NL: 'Netherlands',
  NO: 'Norway', NZ: 'New Zealand', OM: 'Oman', PE: 'Peru', PH: 'Philippines',
  PK: 'Pakistan', PL: 'Poland', PT: 'Portugal', QA: 'Qatar', RO: 'Romania',
  RS: 'Serbia', RU: 'Russia', SA: 'Saudi Arabia', SE: 'Sweden', SG: 'Singapore',
  SK: 'Slovakia', SI: 'Slovenia', TH: 'Thailand', TN: 'Tunisia', TR: 'Türkiye',
  UA: 'Ukraine', US: 'United States', UZ: 'Uzbekistan', VN: 'Vietnam', ZA: 'South Africa',
};

export const BUYER_INDUSTRIES = [
  'Appliance distributor',
  'Kitchen appliance importer',
  'Hotel equipment supplier',
  'Construction project supplier',
  'Kitchen design company',
  'Retail chain',
  'White goods dealer',
];

export const SCAN_DATA_SOURCES = [
  { value: 'web_search', label: 'Web directories' },
  { value: 'trade_data', label: 'Trade databases' },
  { value: 'exhibitor_lists', label: 'Trade fair exhibitors' },
  { value: 'linkedin_reference', label: 'LinkedIn references' },
  { value: 'company_registries', label: 'Company registries' },
];

export const LANGUAGES = [
  { value: 'en', label: 'English' },
  { value: 'de', label: 'German' },
  { value: 'ar', label: 'Arabic' },
  { value: 'nl', label: 'Dutch' },
  { value: 'tr', label: 'Turkish' },
];

/* Deterministic RNG (mulberry32) */
function rng(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const now = Date.now();
const DAY = 86400000;
const iso = (msAgo) => new Date(now - msAgo).toISOString();

/* ---------------- Lead name pools ---------------- */
const LEAD_POOLS = {
  DE: {
    cities: ['Berlin', 'Hamburg', 'Munich', 'Cologne', 'Frankfurt', 'Düsseldorf', 'Stuttgart'],
    names: [
      'Küchenprofi Handels GmbH', 'Rheinland Küchentechnik GmbH', 'NordHaus Geräte GmbH',
      'MetroKüche Distribution', 'EuroKitchen Import GmbH', 'Hanse Appliance Partners',
      'KüchenWelt Vertriebs GmbH', 'Süddeutsche Elektrogeräte', 'Westfalen Home Tech',
      'Objektküchen München GmbH', 'Gastrotechnik Hamburg', 'Prime Interieur Handel',
      'Bergmann Küchensysteme', 'Elbe Home Appliances',
    ],
    contacts: [
      ['Anna Müller', 'f'], ['Stefan Weber', 'm'], ['Julia Hoffmann', 'f'], ['Markus Braun', 'm'],
      ['Katrin Schulz', 'f'], ['Thomas Wagner', 'm'], ['Sabine Fischer', 'f'], ['Jan Becker', 'm'],
      ['Lena Krause', 'f'], ['Felix Neumann', 'm'],
    ],
    domainTld: 'de', lang: 'de',
  },
  AE: {
    cities: ['Dubai', 'Abu Dhabi', 'Sharjah', 'Ajman'],
    names: [
      'Gulf Horeca Trading LLC', 'Emirates Kitchen Solutions', 'Al Manara Appliances LLC',
      'Dubai Hospitality Supplies', 'Marina Home Concepts', 'Oasis Building Materials',
      'Sharjah White Goods Co', 'Khaleej Kitchen Imports', 'Al Fardan Home Retail',
      'Desert Gate Projects LLC', 'Falcon Interiors Trading',
    ],
    contacts: [
      ['Omar Al Rashid', 'm'], ['Fatima Hassan', 'f'], ['Khalid Mansoor', 'm'], ['Layla Ibrahim', 'f'],
      ['Yusuf Karim', 'm'], ['Mariam Saleh', 'f'], ['Ahmed Nasser', 'm'], ['Rania Aziz', 'f'],
    ],
    domainTld: 'ae', lang: 'en',
  },
  SA: {
    cities: ['Riyadh', 'Jeddah', 'Dammam', 'Al Khobar', 'Mecca'],
    names: [
      'Riyadh Kitchen House', 'Al Salem Trading Est', 'Jeddah Home Appliances Co',
      'Najd Distribution Group', 'Red Sea Hotel Supplies', 'Al Khobar Interiors',
      'Kingdom Kitchen Projects', 'Dammam Import House', 'Saudi Retail Partners',
      'Arabian Gulf Equipment Co',
    ],
    contacts: [
      ['Abdullah Al Qahtani', 'm'], ['Noura Al Otaibi', 'f'], ['Salman Al Harbi', 'm'],
      ['Huda Al Zahrani', 'f'], ['Fahad Al Mutairi', 'm'], ['Sara Al Ghamdi', 'f'],
    ],
    domainTld: 'com.sa', lang: 'ar',
  },
};

const TITLES = ['Purchasing Manager', 'Procurement Director', 'Head of Imports', 'Category Manager', 'Managing Director', 'Sales Director', 'Owner', 'Senior Buyer'];
const SOURCES = ['web_search', 'trade_data', 'exhibitor_lists', 'company_registries', 'linkedin_reference'];

function slugify(name) {
  return name.toLowerCase()
    .replace(/ü/g, 'ue').replace(/ö/g, 'oe').replace(/ä/g, 'ae').replace(/ß/g, 'ss')
    .replace(/gmbh|llc|est|co\b|group|partners/g, '')
    .replace(/[^a-z0-9]+/g, '')
    .slice(0, 18) || 'company';
}

/* ---------------- Seed builder ---------------- */
export function makeSeed() {
  const rand = rng(1911);
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];

  /* --- identity --- */
  const user = {
    id: 'user_meltem',
    name: 'Meltem Aydın',
    email: 'meltem@silverine.com.tr',
    role: 'owner',
    company_id: 'company_silverine',
  };

  const company = {
    id: 'company_silverine',
    name: 'Silverine',
    legal_name: 'Silverine Ev Aletleri A.Ş.',
    website: 'https://silverine.com.tr',
    headquarters_country: 'TR',
    city: 'Istanbul',
    founded_year: 2004,
    industry: 'Kitchen appliances',
    business_model: 'Manufacturer / exporter / supplier',
    employee_count: '250-500',
    main_language: 'tr',
    sales_regions_current: ['TR', 'AZ', 'GE', 'IQ'],
    sales_regions_target: ['DE', 'AE', 'SA', 'NL', 'GB'],
    positioning: {
      what_company_sells: 'Built-in kitchen appliances and professional kitchen equipment for distributors, importers and project suppliers.',
      main_value_proposition: 'European-certified quality at 25–35% below Western European factory prices, with 3-week delivery to EU and GCC.',
      quality_position: 'Upper-mid market',
      price_position: 'Value / competitive',
      premium_or_mass_market: 'Premium-value hybrid',
      main_differentiators: ['CE + TSE certified production', 'OEM/white-label program', 'Flexible MOQ from 50 units', 'In-house R&D and tooling'],
      certifications: ['CE', 'ISO 9001', 'TSE', 'RoHS'],
      manufacturing_capacity: '420,000 units / year',
      export_capacity: '60% of production',
      delivery_capabilities: 'FOB Istanbul, CIF EU main ports, door delivery GCC',
      after_sales_support: 'Regional service partners + 24-month warranty',
    },
    sales_preferences: {
      default_send_mode: 'create_draft',
      default_language: 'en',
      languages: ['en', 'de', 'ar'],
      default_cc_rule_id: 'ccrule_default',
      connected_mailbox: 'sales@silverine.com.tr',
    },
  };

  /* --- products --- */
  const products = [
    { id: 'prod_ovens', name: 'Built-in oven series (SVN-B)', category: 'Built-in ovens', description: '60cm multifunction built-in ovens, A+ energy, 9 programs, telescopic rails.', moq: '50 units', price_band: '€165–240 FOB', certifications: ['CE', 'RoHS'], buyer_roles: ['Purchasing Manager', 'Category Manager'], },
    { id: 'prod_cooktops', name: 'Induction cooktop line (SVN-I)', category: 'Cooktops', description: '4-zone induction cooktops with booster, slider touch control, 7.2kW.', moq: '100 units', price_band: '€88–130 FOB', certifications: ['CE'], buyer_roles: ['Purchasing Manager', 'Senior Buyer'], },
    { id: 'prod_hoods', name: 'Range hood collection (SVN-H)', category: 'Range hoods', description: 'Wall-mount and telescopic hoods, 650 m³/h, low-noise EC motors.', moq: '100 units', price_band: '€42–95 FOB', certifications: ['CE'], buyer_roles: ['Senior Buyer'], },
    { id: 'prod_dish', name: 'Dishwasher series (SVN-D)', category: 'Dishwashers', description: '60cm freestanding and fully-integrated dishwashers, 14 place settings.', moq: '80 units', price_band: '€180–260 FOB', certifications: ['CE', 'RoHS'], buyer_roles: ['Category Manager'], },
    { id: 'prod_fridge', name: 'Refrigerator line (SVN-R)', category: 'Refrigeration', description: 'Combi no-frost refrigerators 320–420L, inverter compressors.', moq: '60 units', price_band: '€270–390 FOB', certifications: ['CE'], buyer_roles: ['Purchasing Manager'], },
    { id: 'prod_compact', name: 'Compact kitchen program (SVN-C)', category: 'Compact kitchens', description: 'Mini-kitchen units for studios, hotels and modular housing projects.', moq: '30 units', price_band: '€420–780 FOB', certifications: ['CE'], buyer_roles: ['Project Buyer', 'Managing Director'], },
    { id: 'prod_horeca', name: 'Professional HoReCa series (SVN-P)', category: 'Professional kitchen', description: 'Heavy-duty convection ovens and ranges for hotels and catering.', moq: '20 units', price_band: '€520–1,450 FOB', certifications: ['CE', 'ISO 9001'], buyer_roles: ['F&B Procurement', 'Hotel Equipment Buyer'], },
  ];
  const marketFit = {
    DE: [['prod_ovens', 92], ['prod_dish', 87], ['prod_cooktops', 84]],
    AE: [['prod_horeca', 94], ['prod_compact', 86], ['prod_fridge', 78]],
    SA: [['prod_horeca', 90], ['prod_ovens', 82], ['prod_fridge', 80]],
    NL: [['prod_cooktops', 88], ['prod_ovens', 85], ['prod_compact', 76]],
    GB: [['prod_ovens', 86], ['prod_compact', 83], ['prod_dish', 79]],
  };
  for (const p of products) {
    p.market_fit = Object.entries(marketFit)
      .filter(([, list]) => list.some(([id]) => id === p.id))
      .map(([cc, list]) => ({ country: cc, score: list.find(([id]) => id === p.id)[1] }));
  }

  /* --- onboarding --- */
  const onboarding = {
    status: 'in_progress',
    current_step: 4,
    steps: [
      { key: 'company-identity', label: 'Company identity', status: 'done' },
      { key: 'positioning', label: 'Positioning', status: 'done' },
      { key: 'products', label: 'Product catalog', status: 'done' },
      { key: 'internal-sales-data', label: 'Internal sales data', status: 'done' },
      { key: 'current-contacts', label: 'Current contacts', status: 'pending' },
      { key: 'target-markets', label: 'Target markets', status: 'pending' },
      { key: 'integrations', label: 'Integration setup', status: 'pending' },
      { key: 'brain-review', label: 'Company Brain review', status: 'pending' },
    ],
  };

  /* --- documents --- */
  const documents = [
    { id: 'doc_catalog', name: 'Silverine_Export_Catalog_2026.pdf', type: 'product_catalog', size_kb: 8420, status: 'processed', uploaded_at: iso(19 * DAY) },
    { id: 'doc_pricelist', name: 'FOB_Pricelist_Q3.xlsx', type: 'price_list', size_kb: 240, status: 'processed', uploaded_at: iso(19 * DAY) },
    { id: 'doc_pastsales', name: 'Export_sales_2023-2025.xlsx', type: 'past_sales', size_kb: 1130, status: 'processed', uploaded_at: iso(18 * DAY) },
    { id: 'doc_distributors', name: 'Current_distributor_list.csv', type: 'distributor_list', size_kb: 88, status: 'processed', uploaded_at: iso(18 * DAY) },
    { id: 'doc_certs', name: 'CE_TSE_certificates.pdf', type: 'certificate', size_kb: 3900, status: 'processed', uploaded_at: iso(17 * DAY) },
  ];

  /* --- company brain --- */
  const brain = {
    status: 'approved',
    built_at: iso(16 * DAY),
    approved_at: iso(15 * DAY),
    version: 3,
    sections: {
      product_understanding: [
        'Core strength is built-in cooking appliances (ovens, cooktops, hoods) with EU certification and OEM flexibility.',
        'HoReCa series opens a second buyer universe: hotel equipment suppliers and F&B procurement, strongest in GCC.',
        'Compact kitchen program fits hotel/studio construction projects — a differentiator few Turkish exporters offer.',
      ],
      ideal_customer_profile: [
        'Importers/distributors of kitchen appliances, 10–200 employees, with dealer networks or project business.',
        'Hotel equipment suppliers serving 4–5★ hospitality projects in GCC.',
        'Retail chains and white goods dealers seeking white-label built-in lines with EU certificates.',
      ],
      buyer_roles: ['Purchasing Manager', 'Procurement Director', 'Head of Imports', 'Category Manager', 'Managing Director / Owner'],
      market_assumptions: [
        'Germany: large replacement market; price-sensitive mid segment growing; buyers demand CE + energy labels and stable delivery.',
        'UAE: hospitality construction boom drives HoReCa demand; re-export hub for wider GCC and East Africa.',
        'Saudi Arabia: giga-projects create bulk project demand; local distribution partners are essential.',
        'Netherlands: strong appliance import channel; sustainability arguments (energy class, repairability) matter.',
        'UK: post-Brexit sourcing diversification favors non-EU suppliers with fast logistics.',
      ],
      sales_arguments: [
        '25–35% landed-cost advantage vs Western European brands at comparable spec.',
        '3-week delivery to EU main ports; 2 weeks to Jebel Ali.',
        'OEM / white-label program with MOQ from 50 units.',
        '24-month warranty with regional service partners.',
      ],
      missing_data: [
        'No pricing sheet uploaded for the HoReCa series.',
        'Current contact list not imported yet (onboarding step 5).',
        'Lost-deal history absent — win/loss reasoning is inferred only.',
      ],
    },
    snapshots: [
      { id: 'snap_3', version: 3, created_at: iso(16 * DAY), note: 'Rebuilt after price list + distributor list upload', approved: true },
      { id: 'snap_2', version: 2, created_at: iso(17.5 * DAY), note: 'Rebuilt with past-sales data', approved: false },
      { id: 'snap_1', version: 1, created_at: iso(19 * DAY), note: 'Initial build from catalog only', approved: false },
    ],
  };

  /* --- lead map --- */
  const opportunity = {
    DE: 88, AE: 84, SA: 81, NL: 77, GB: 75, FR: 71, PL: 69, ES: 66, IT: 65, QA: 72,
    KW: 68, OM: 64, EG: 58, MA: 55, RO: 61, CZ: 60, AT: 63, CH: 62, SE: 59, NO: 56,
    DK: 58, BE: 64, PT: 54, GR: 57, HU: 55, BG: 52, RS: 50, UA: 42, KZ: 51, AZ: 60,
    GE: 57, UZ: 48, IQ: 53, JO: 52, LB: 44, IL: 49, US: 46, CA: 44, MX: 38, BR: 36,
    AU: 41, NZ: 34, JP: 32, KR: 35, CN: 22, IN: 40, ID: 37, MY: 42, TH: 39, VN: 38,
    SG: 45, PH: 33, PK: 36, BD: 30, NG: 43, KE: 41, GH: 37, ZA: 47, DZ: 46, TN: 48,
    LY: 35, ET: 31, RU: 20, BY: 18, TR: 0,
  };
  const leadMap = {
    countries: Object.entries(COUNTRY_NAMES).map(([code, name]) => ({
      code, name,
      opportunity_score: opportunity[code] ?? 25,
      recommended: ['DE', 'AE', 'SA', 'NL', 'GB'].includes(code),
    })),
    selected: ['DE', 'AE', 'SA'],
    max_selected: 5,
    summaries: {
      DE: {
        market_size: 'Europe’s largest kitchen appliance market (~€5.4B yearly), with a strong independent kitchen-studio and distributor channel.',
        trade_note: 'Türkiye is already a top-3 appliance import origin for Germany — buyers know and trust Turkish manufacturing.',
        top_industries: ['Appliance distributor', 'Kitchen design company', 'Retail chain'],
        buyer_categories: ['Appliance distributor', 'Kitchen appliance importer', 'Kitchen design company', 'Retail chain'],
      },
      AE: {
        market_size: 'Fast-growing GCC hub; hospitality pipeline of 40k+ hotel rooms drives professional kitchen demand.',
        trade_note: 'Dubai re-exports across GCC and East Africa — one distributor can open six markets.',
        top_industries: ['Hotel equipment supplier', 'Construction project supplier', 'Kitchen appliance importer'],
        buyer_categories: ['Hotel equipment supplier', 'Construction project supplier', 'Kitchen appliance importer', 'White goods dealer'],
      },
      SA: {
        market_size: 'Largest GCC economy; Vision-2030 giga-projects (NEOM, Qiddiya) create bulk project procurement.',
        trade_note: 'Import duties favor CE-certified goods; local agent or distributor is essential for project tenders.',
        top_industries: ['Construction project supplier', 'Hotel equipment supplier', 'Retail chain'],
        buyer_categories: ['Construction project supplier', 'Hotel equipment supplier', 'Retail chain', 'Appliance distributor'],
      },
      NL: {
        market_size: 'Compact but high-value market; Rotterdam is the EU logistics gateway with mature import houses.',
        trade_note: 'Dutch importers often buy for Benelux + Nordics; sustainability and energy class are decisive.',
        top_industries: ['Kitchen appliance importer', 'Appliance distributor', 'Kitchen design company'],
        buyer_categories: ['Kitchen appliance importer', 'Appliance distributor', 'Kitchen design company'],
      },
      GB: {
        market_size: '£3.1B appliance market; post-Brexit sourcing diversification favors fast non-EU suppliers.',
        trade_note: 'UK buyers value 3-week delivery vs 8+ weeks from Asia; UKCA marking accepted alongside CE until further notice.',
        top_industries: ['Retail chain', 'Appliance distributor', 'Kitchen design company'],
        buyer_categories: ['Retail chain', 'Appliance distributor', 'White goods dealer', 'Kitchen design company'],
      },
      _default: {
        market_size: 'Limited market intelligence available for this country yet.',
        trade_note: 'Run a lead scan to build coverage — the agent will gather trade data, directories and buyer signals.',
        top_industries: [],
        buyer_categories: BUYER_INDUSTRIES.slice(0, 3),
      },
    },
  };

  /* --- leads + contacts + research --- */
  const leads = [];
  const contacts = [];
  const research = [];
  const STATUS_FLOW = ['new', 'researched', 'contacted', 'replied', 'interested'];

  function makeLeads(cc, count, scanId, baseAgeDays) {
    const pool = LEAD_POOLS[cc];
    for (let i = 0; i < count; i++) {
      const name = pool.names[i % pool.names.length];
      const domain = `${slugify(name)}.${pool.domainTld}`;
      const statusRoll = rand();
      const status = statusRoll < 0.30 ? 'new' : statusRoll < 0.55 ? 'researched' : statusRoll < 0.8 ? 'contacted' : statusRoll < 0.93 ? 'replied' : 'interested';
      const score = Math.round(38 + rand() * 58);
      const lead = {
        id: `lead_${cc.toLowerCase()}_${i + 1}`,
        company_name: name,
        country: cc,
        city: pick(pool.cities),
        industry: pick(BUYER_INDUSTRIES),
        website: `https://${domain}`,
        size_hint: pick(['10-50', '50-100', '100-250', '250+']),
        source: pick(SOURCES),
        status,
        scan_id: scanId,
        created_at: iso((baseAgeDays - i * 0.15) * DAY),
        score: {
          value: score,
          band: score >= 75 ? 'high' : score >= 50 ? 'mid' : 'low',
          factors: [
            { label: 'Industry fit', weight: 30, note: 'Matches Silverine buyer categories' },
            { label: 'Import activity', weight: 25, note: 'Trade data shows appliance import volume' },
            { label: 'Company size', weight: 20, note: 'Within ICP employee range' },
            { label: 'Market opportunity', weight: 15, note: `${COUNTRY_NAMES[cc]} opportunity score` },
            { label: 'Web signals', weight: 10, note: 'Product pages and brand portfolio' },
          ],
        },
      };
      leads.push(lead);

      const flowIdx = STATUS_FLOW.indexOf(status);
      if (flowIdx >= 1) {
        research.push({
          id: `res_${lead.id}`,
          lead_id: lead.id,
          status: 'completed',
          created_at: iso((baseAgeDays - 1 - i * 0.1) * DAY),
          summary: `${name} is a ${lead.industry.toLowerCase()} based in ${lead.city}, active for 10+ years with an estimated ${lead.size_hint} employees. Current portfolio includes mid-range European and Asian appliance brands; recent signals suggest they are broadening their built-in range.`,
          insights: [
            { title: 'Portfolio gap', body: `Carries competing brands but no Turkish manufacturer — a landed-cost pitch of 25–35% below current suppliers is credible.` },
            { title: 'Buying window', body: `${pick(['Range refresh listed for next quarter', 'Attending Ambiente next season', 'New showroom opening announced', 'Tendering for a hotel project'])} — timing favors first contact now.` },
            { title: 'Entry product', body: `Best door-opener: ${pick(products).name} given local demand and their current assortment.` },
          ],
        });
      }
      if (flowIdx >= 1 && i % 3 !== 2) {
        const n = 1 + (i % 2);
        for (let c = 0; c < n; c++) {
          const [fullName] = pool.contacts[(i + c) % pool.contacts.length];
          const first = fullName.split(' ')[0].toLowerCase()
            .replace(/ü/g, 'ue').replace(/ö/g, 'oe').replace(/ä/g, 'ae').replace(/ı/g, 'i');
          contacts.push({
            id: `contact_${lead.id}_${c + 1}`,
            lead_id: lead.id,
            name: fullName,
            title: pick(TITLES),
            email: `${first}@${domain}`,
            email_status: rand() < 0.62 ? 'verified' : rand() < 0.85 ? 'unverified' : 'not_found',
            linkedin_url: `https://www.linkedin.com/in/${first}-${slugify(fullName.split(' ').slice(-1)[0])}`,
            phone: rand() < 0.5 ? `+${cc === 'DE' ? '49' : cc === 'AE' ? '971' : '966'} ${Math.floor(100 + rand() * 899)} ${Math.floor(1000000 + rand() * 8999999)}` : null,
            do_not_contact: false,
            created_at: lead.created_at,
          });
        }
      }
    }
  }
  makeLeads('DE', 14, 'scan_de', 12);
  makeLeads('AE', 11, 'scan_ae', 8);

  /* --- lead scans --- */
  const leadScans = [
    {
      id: 'scan_de', name: 'Germany — distributors & importers', countries: ['DE'],
      depth: 'standard', sources: ['web_search', 'trade_data', 'exhibitor_lists'],
      products: ['prod_ovens', 'prod_dish', 'prod_cooktops'],
      industries: ['Appliance distributor', 'Kitchen appliance importer', 'Retail chain'],
      leads_per_country: 14,
      status: 'completed', leads_found: 14, run_id: 'run_scan_de',
      created_at: iso(12.5 * DAY), completed_at: iso(12.2 * DAY),
    },
    {
      id: 'scan_ae', name: 'UAE — HoReCa & projects', countries: ['AE'],
      depth: 'standard', sources: ['web_search', 'trade_data', 'linkedin_reference'],
      products: ['prod_horeca', 'prod_compact'],
      industries: ['Hotel equipment supplier', 'Construction project supplier', 'Kitchen appliance importer'],
      leads_per_country: 11,
      status: 'completed', leads_found: 11, run_id: 'run_scan_ae',
      created_at: iso(8.4 * DAY), completed_at: iso(8.1 * DAY),
    },
    {
      id: 'scan_sa', name: 'Saudi Arabia — projects & retail', countries: ['SA'],
      depth: 'standard', sources: ['web_search', 'trade_data', 'company_registries'],
      products: ['prod_horeca', 'prod_ovens'],
      industries: ['Construction project supplier', 'Hotel equipment supplier', 'Retail chain'],
      leads_per_country: 8,
      status: 'running', leads_found: 0, run_id: 'run_scan_sa',
      created_at: iso(0.02 * DAY), completed_at: null,
    },
  ];

  /* --- campaigns + messages --- */
  const campaigns = [
    {
      id: 'camp_de', name: 'DE distributors — built-in ovens intro',
      country: 'DE', product_id: 'prod_ovens', language: 'de',
      send_mode: 'create_draft', cc_rule_id: 'ccrule_default',
      status: 'completed', created_at: iso(10 * DAY),
      stats: { messages: 18, sent: 18, replied: 3, interested: 1 },
    },
    {
      id: 'camp_ae', name: 'UAE HoReCa — hotel projects intro',
      country: 'AE', product_id: 'prod_horeca', language: 'en',
      send_mode: 'create_draft', cc_rule_id: 'ccrule_gcc',
      status: 'awaiting_approval', created_at: iso(1.2 * DAY),
      stats: { messages: 6, sent: 0, replied: 0, interested: 0 },
    },
  ];

  const messages = [];
  const deLeads = leads.filter(l => l.country === 'DE' && ['contacted', 'replied', 'interested'].includes(l.status));
  deLeads.slice(0, 6).forEach((lead, i) => {
    const contact = contacts.find(c => c.lead_id === lead.id);
    if (!contact) return;
    const replied = lead.status === 'replied' || lead.status === 'interested';
    messages.push({
      id: `msg_de_${i + 1}`, campaign_id: 'camp_de', lead_id: lead.id, contact_id: contact.id,
      channel: 'email', language: 'de',
      subject: `Einbaugeräte mit CE-Zertifizierung — Partnerschaft mit ${lead.company_name}`,
      body: `Guten Tag ${contact.name.split(' ')[0]} ${contact.name.split(' ').slice(1).join(' ')},\n\nbei der Durchsicht des deutschen Fachhandels ist mir ${lead.company_name} als etablierter ${lead.industry === 'Retail chain' ? 'Händler' : 'Distributor'} in ${lead.city} aufgefallen.\n\nSilverine fertigt seit 2004 Einbaugeräte in Istanbul — CE- und TSE-zertifiziert, mit A+ Energieklassen. Unsere Partner in der EU erzielen 25–35% niedrigere Einkaufspreise gegenüber westeuropäischen Marken bei vergleichbarer Spezifikation, mit 3 Wochen Lieferzeit ab Werk.\n\nBesonders interessant für Ihr Sortiment: unsere SVN-B Backofenserie (60cm, 9 Funktionen, Teleskopauszug) ab 50 Stück MOQ, auch als White-Label.\n\nHätten Sie kommende Woche 20 Minuten für ein kurzes Gespräch?\n\nMit freundlichen Grüßen\nMeltem Aydın\nExport Sales — Silverine`,
      status: replied ? 'replied' : 'sent',
      cc: ['export-team@silverine.com.tr'],
      sent_at: iso((9.5 - i * 0.3) * DAY),
      created_at: iso((9.8 - i * 0.3) * DAY),
    });
  });
  const aeLeads = leads.filter(l => l.country === 'AE').slice(0, 6);
  aeLeads.forEach((lead, i) => {
    const contact = contacts.find(c => c.lead_id === lead.id);
    messages.push({
      id: `msg_ae_${i + 1}`, campaign_id: 'camp_ae', lead_id: lead.id, contact_id: contact ? contact.id : null,
      channel: 'email', language: 'en',
      subject: `CE-certified professional kitchen equipment for ${lead.company_name} projects`,
      body: `Dear ${contact ? contact.name : 'Sir or Madam'},\n\nI came across ${lead.company_name} while researching hospitality suppliers in ${lead.city} — your project portfolio stands out.\n\nSilverine manufactures professional kitchen equipment in Istanbul: heavy-duty convection ovens, ranges and compact kitchen units, all CE and ISO 9001 certified. We currently deliver to Jebel Ali within two weeks, which our GCC partners use to win time-critical hotel fit-outs.\n\nFor projects like yours, our SVN-P HoReCa series offers a 20–30% landed-cost advantage against European brands, with a 24-month warranty backed by regional service partners.\n\nWould you be open to a brief call this week? I can also send our GCC project reference list.\n\nBest regards,\nMeltem Aydın\nExport Sales — Silverine`,
      status: 'draft_generated',
      cc: ['mena@silverine.com.tr'],
      sent_at: null,
      created_at: iso(1.1 * DAY),
    });
  });

  /* --- cc rules --- */
  const ccRules = [
    { id: 'ccrule_default', name: 'Default — export team', market_country: null, market_region: null, product_id: null, industry: null, cc_emails: ['export-team@silverine.com.tr'], is_default: true },
    { id: 'ccrule_gcc', name: 'GCC region', market_country: null, market_region: 'GCC', product_id: null, industry: null, cc_emails: ['mena@silverine.com.tr'], is_default: false },
  ];

  /* --- integrations --- */
  const integrations = {
    email: [
      { id: 'int_google', provider: 'google', label: 'Google Workspace', mailbox: 'sales@silverine.com.tr', status: 'connected', connected_at: iso(15 * DAY), last_test: { ok: true, at: iso(0.5 * DAY) } },
    ],
    whatsapp: [],
    linkedin_actions: [
      { id: 'li_1', contact_id: contacts[2] ? contacts[2].id : null, lead_id: leads[2] ? leads[2].id : null, profile_url: contacts[2] ? contacts[2].linkedin_url : '', status: 'connected', note: 'Hallo! Wir liefern CE-zertifizierte Einbaugeräte an deutsche Distributoren — ich würde mich gern vernetzen.', created_at: iso(6 * DAY) },
      { id: 'li_2', contact_id: contacts[5] ? contacts[5].id : null, lead_id: leads[4] ? leads[4].id : null, profile_url: contacts[5] ? contacts[5].linkedin_url : '', status: 'connection_sent', note: 'Great meeting point: appliance sourcing from Türkiye with 3-week EU delivery.', created_at: iso(3 * DAY) },
      { id: 'li_3', contact_id: contacts[8] ? contacts[8].id : null, lead_id: leads[7] ? leads[7].id : null, profile_url: contacts[8] ? contacts[8].linkedin_url : '', status: 'note_generated', note: 'Hospitality projects in the Gulf need faster kitchen equipment lead times — happy to share how we deliver in 2 weeks.', created_at: iso(1 * DAY) },
      { id: 'li_4', contact_id: contacts[11] ? contacts[11].id : null, lead_id: leads[10] ? leads[10].id : null, profile_url: contacts[11] ? contacts[11].linkedin_url : '', status: 'opened', note: 'Connecting with procurement leaders across the GCC hospitality sector.', created_at: iso(0.8 * DAY) },
    ],
  };

  const agent = {
    adapter: 'mock',
    status: 'not_configured',
    capabilities: {
      runs: false,
      streaming: false,
      tool_events: false,
    },
    detail: 'A server-side Hermes adapter has not been configured for this workspace.',
  };

  /* --- agent runs (history; the live SA scan is started by db.reset) --- */
  const agentRuns = [
    { id: 'run_brain_1', type: 'company_brain_build', label: 'Company Brain — initial build', status: 'completed', progress: 100, created_at: iso(19 * DAY), finished_at: iso(19 * DAY - 220000), related: {}, logs: [] },
    { id: 'run_doc_1', type: 'document_processing', label: 'Process Silverine_Export_Catalog_2026.pdf', status: 'completed', progress: 100, created_at: iso(19 * DAY), finished_at: iso(19 * DAY - 180000), related: { document_id: 'doc_catalog' }, logs: [] },
    { id: 'run_prod_1', type: 'product_extraction', label: 'Extract products from catalog', status: 'completed', progress: 100, created_at: iso(18.8 * DAY), finished_at: iso(18.8 * DAY - 240000), related: {}, logs: [] },
    { id: 'run_doc_2', type: 'document_processing', label: 'Process Export_sales_2023-2025.xlsx', status: 'completed', progress: 100, created_at: iso(18 * DAY), finished_at: iso(18 * DAY - 130000), related: { document_id: 'doc_pastsales' }, logs: [] },
    { id: 'run_brain_2', type: 'company_brain_build', label: 'Company Brain — rebuild v2', status: 'completed', progress: 100, created_at: iso(17.5 * DAY), finished_at: iso(17.5 * DAY - 260000), related: {}, logs: [] },
    { id: 'run_brain_3', type: 'company_brain_build', label: 'Company Brain — rebuild v3', status: 'completed', progress: 100, created_at: iso(16 * DAY), finished_at: iso(16 * DAY - 250000), related: {}, logs: [] },
    { id: 'run_scan_de', type: 'lead_scan', label: 'Lead scan — Germany', status: 'completed', progress: 100, created_at: iso(12.5 * DAY), finished_at: iso(12.2 * DAY), related: { scan_id: 'scan_de' }, logs: [] },
    { id: 'run_research_de', type: 'lead_research', label: 'Research 9 German leads', status: 'completed', progress: 100, created_at: iso(11.8 * DAY), finished_at: iso(11.6 * DAY), related: { scan_id: 'scan_de' }, logs: [] },
    { id: 'run_contacts_de', type: 'contact_discovery', label: 'Discover contacts — German leads', status: 'completed', progress: 100, created_at: iso(11.2 * DAY), finished_at: iso(11 * DAY), related: {}, logs: [] },
    { id: 'run_outreach_de', type: 'outreach_generation', label: 'Generate DE campaign messages', status: 'completed', progress: 100, created_at: iso(10 * DAY), finished_at: iso(10 * DAY - 200000), related: { campaign_id: 'camp_de' }, logs: [] },
    { id: 'run_send_de', type: 'email_send', label: 'Create drafts — DE campaign (18 emails)', status: 'completed', progress: 100, created_at: iso(9.6 * DAY), finished_at: iso(9.5 * DAY), related: { campaign_id: 'camp_de' }, logs: [] },
    { id: 'run_scan_ae', type: 'lead_scan', label: 'Lead scan — UAE', status: 'completed', progress: 100, created_at: iso(8.4 * DAY), finished_at: iso(8.1 * DAY), related: { scan_id: 'scan_ae' }, logs: [] },
    { id: 'run_research_ae', type: 'lead_research', label: 'Research 8 UAE leads', status: 'completed', progress: 100, created_at: iso(7.6 * DAY), finished_at: iso(7.4 * DAY), related: { scan_id: 'scan_ae' }, logs: [] },
    { id: 'run_outreach_ae', type: 'outreach_generation', label: 'Generate UAE HoReCa messages', status: 'completed', progress: 100, created_at: iso(1.2 * DAY), finished_at: iso(1.15 * DAY), related: { campaign_id: 'camp_ae' }, logs: [] },
    { id: 'run_li_1', type: 'linkedin_note_generation', label: 'Generate LinkedIn notes (4 contacts)', status: 'completed', progress: 100, created_at: iso(1 * DAY), finished_at: iso(1 * DAY - 90000), related: {}, logs: [] },
    { id: 'run_analytics_1', type: 'analytics_refresh', label: 'Refresh analytics', status: 'completed', progress: 100, created_at: iso(0.6 * DAY), finished_at: iso(0.6 * DAY - 45000), related: {}, logs: [] },
  ];

  /* --- analytics --- */
  const analytics = {
    pipeline: {
      leads_by_status: STATUS_FLOW.concat(['do_not_contact']).map(s => ({ status: s, count: leads.filter(l => l.status === s).length })),
      emails_sent_weekly: { labels: ['W-7', 'W-6', 'W-5', 'W-4', 'W-3', 'W-2', 'W-1', 'Now'], values: [0, 0, 0, 4, 9, 5, 6, 2] },
      replies_weekly: { labels: ['W-7', 'W-6', 'W-5', 'W-4', 'W-3', 'W-2', 'W-1', 'Now'], values: [0, 0, 0, 0, 1, 1, 1, 0] },
      funnel: [
        { stage: 'Leads discovered', value: 25 },
        { stage: 'Researched', value: 17 },
        { stage: 'Contacts found', value: 14 },
        { stage: 'Emails sent', value: 18 },
        { stage: 'Replies', value: 3 },
        { stage: 'Interested', value: 1 },
      ],
    },
    market: {
      country_scores: [
        { country: 'DE', score: 88 }, { country: 'AE', score: 84 }, { country: 'SA', score: 81 },
        { country: 'NL', score: 77 }, { country: 'GB', score: 75 }, { country: 'QA', score: 72 },
        { country: 'FR', score: 71 }, { country: 'PL', score: 69 },
      ],
      product_market_fit: Object.entries(marketFit).map(([cc, list]) => ({
        country: cc,
        products: list.map(([pid, score]) => ({ product_id: pid, name: products.find(p => p.id === pid).name, score })),
      })),
      top_industries: [
        { label: 'Appliance distributor', value: 9 },
        { label: 'Kitchen appliance importer', value: 5 },
        { label: 'Hotel equipment supplier', value: 4 },
        { label: 'Construction project supplier', value: 3 },
        { label: 'Retail chain', value: 2 },
        { label: 'Kitchen design company', value: 1 },
        { label: 'White goods dealer', value: 1 },
      ],
      source_performance: [
        { label: 'Trade databases', value: 9 },
        { label: 'Web directories', value: 7 },
        { label: 'Trade fair exhibitors', value: 4 },
        { label: 'Company registries', value: 3 },
        { label: 'LinkedIn references', value: 2 },
      ],
    },
  };

  /* --- activity feed --- */
  const activity = [];
  let actSeq = 0;
  const act = (msAgo, kind, label, ref = {}) => activity.push({ id: `act_${++actSeq}`, kind, label, ref, at: iso(msAgo) });
  act(19 * DAY, 'document', 'Uploaded product catalog and price list', { document_id: 'doc_catalog' });
  act(18.8 * DAY, 'agent', 'Agent extracted 7 products from the export catalog', { run_id: 'run_prod_1' });
  act(16 * DAY, 'agent', 'Company Brain rebuilt (v3) with past-sales and distributor data', { run_id: 'run_brain_3' });
  act(15 * DAY, 'user', 'Meltem approved the Company Brain', {});
  act(12.5 * DAY, 'agent', 'Lead scan started — Germany (standard depth)', { run_id: 'run_scan_de' });
  act(12.2 * DAY, 'agent', 'Lead scan completed — 14 German leads discovered', { scan_id: 'scan_de' });
  act(11.6 * DAY, 'agent', 'Research completed for 9 German leads', { run_id: 'run_research_de' });
  act(11 * DAY, 'agent', 'Contact discovery found 12 buyer contacts in Germany', { run_id: 'run_contacts_de' });
  act(10 * DAY, 'agent', 'Generated 18 German outreach emails for review', { campaign_id: 'camp_de' });
  act(9.6 * DAY, 'user', 'Meltem approved DE campaign — drafts created in Gmail', { campaign_id: 'camp_de' });
  act(8.4 * DAY, 'agent', 'Lead scan started — United Arab Emirates', { run_id: 'run_scan_ae' });
  act(8.1 * DAY, 'agent', 'Lead scan completed — 11 UAE leads discovered', { scan_id: 'scan_ae' });
  act(7 * DAY, 'reply', 'Reply received from Küchenprofi Handels GmbH', { lead_id: 'lead_de_1' });
  act(5.5 * DAY, 'reply', 'Reply received from EuroKitchen Import GmbH', { lead_id: 'lead_de_5' });
  act(4 * DAY, 'reply', 'NordHaus Geräte marked interested — sample order discussed', { lead_id: 'lead_de_3' });
  act(1.2 * DAY, 'agent', 'Generated 6 UAE HoReCa emails — awaiting approval', { campaign_id: 'camp_ae' });
  act(1 * DAY, 'agent', 'LinkedIn notes generated for 4 GCC contacts', { run_id: 'run_li_1' });
  act(0.6 * DAY, 'agent', 'Analytics refreshed', { run_id: 'run_analytics_1' });

  const admin = {
    companies: [
      { id: company.id, name: company.name, legal_name: company.legal_name, website: company.website, status: 'active', plan: 'pilot', users: 1, created_at: iso(22 * DAY), last_seen_at: iso(0.2 * DAY) },
      { id: 'company_marmara', name: 'Marmara Hotel Supply', legal_name: 'Marmara Hotel Supply A.S.', website: 'https://marmarahotelsupply.example', status: 'access_pending', plan: 'trial', users: 0, created_at: iso(2.4 * DAY), last_seen_at: null },
      { id: 'company_anatolia', name: 'Anatolia Ceramics Export', legal_name: 'Anatolia Ceramics Export Ltd.', website: 'https://anatoliaceramics.example', status: 'suspended', plan: 'demo', users: 2, created_at: iso(31 * DAY), last_seen_at: iso(12 * DAY) },
    ],
    users: [
      { ...user, status: 'active', last_login_at: iso(0.15 * DAY), created_at: iso(22 * DAY) },
      { id: 'user_admin', name: 'Tugrap Efe', email: 'admin@interfaze.local', role: 'admin', company_id: null, status: 'active', last_login_at: iso(0.05 * DAY), created_at: iso(40 * DAY) },
      { id: 'user_ops', name: 'Ops Reviewer', email: 'ops@interfaze.local', role: 'support', company_id: 'company_anatolia', status: 'disabled', last_login_at: iso(16 * DAY), created_at: iso(32 * DAY) },
    ],
    errors: [
      { id: 'err_1', level: 'warning', area: 'email_send', message: 'Draft creation retried once for Gmail mock provider', at: iso(0.4 * DAY) },
      { id: 'err_2', level: 'info', area: 'lead_scan', message: 'LinkedIn source skipped for compliance-safe manual workflow', at: iso(0.8 * DAY) },
    ],
    logs: activity.map(a => ({ id: `log_${a.id}`, area: a.kind, message: a.label, at: a.at })),
  };

  return {
    user, company, products, documents, onboarding, brain, leadMap, leadScans,
    leads, research, contacts, campaigns, messages, ccRules, integrations, agent,
    agentRuns, analytics, activity, admin,
  };
}
