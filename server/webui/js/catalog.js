/* Company-agnostic UI catalogs. Tenant records come only from the API. */

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
  'Appliance distributor', 'Kitchen appliance importer', 'Hotel equipment supplier',
  'Construction project supplier', 'Kitchen design company', 'Retail chain', 'White goods dealer',
];

export const SCAN_DATA_SOURCES = [
  { value: 'web_search', label: 'Web directories' },
  // ponytail: database-backed sources (trade_data) stay out of the UI until a
  // licensed data deal exists; re-add this row when one lands.
  { value: 'exhibitor_lists', label: 'Trade fair exhibitors' },
  { value: 'linkedin_reference', label: 'LinkedIn references' },
  { value: 'company_registries', label: 'Company registries' },
];

export const LANGUAGES = [
  { value: 'en', label: 'English' }, { value: 'de', label: 'German' },
  { value: 'ar', label: 'Arabic' }, { value: 'nl', label: 'Dutch' },
  { value: 'tr', label: 'Turkish' },
];
