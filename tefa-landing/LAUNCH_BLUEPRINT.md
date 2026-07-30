# Homeschool Commerce Launch Blueprint

Operator-grade Definition of Done and UI/UX psychology blueprint for taking the
TEFA/homeschool storefront from approved prototype status to a live site with a
completed first sale.

## Completion signal

The site is not complete just because the HTML is published. It is complete when
a real parent can finish one of the three intended journeys without the operator
improvising a missing process:

1. **Shop Resources**: land on the site, find an age-appropriate product, pay,
   receive confirmation, and receive shipment/tracking.
2. **Use Funding**: land on the site, understand the Texas/future funding path,
   request an itemized quote or documentation, and receive accurate next steps
   without any promise of eligibility, approval, or reimbursement.
3. **Get Help Getting Set Up**: land on the site, book or buy guidance, submit
   intake details, receive reminders, and receive post-call follow-up.

The launch is proven when the first real customer order or qualified service lead
moves from first visit to confirmation, CRM record, operator task, and follow-up.

## Deployment and shareable URL plan

There is no public link until the built `tefa-landing/` folder is deployed to a
web host and attached to a domain. The current storefront is a static site, so it
can be hosted anywhere that serves HTML, CSS, JavaScript, and assets.

### Recommended launch setup

- **Preview link for review**: deploy `tefa-landing/` to Netlify Drop, Vercel, or
  GitHub Pages and use the generated preview URL. This is the link to share
  privately before the business is ready to transact.
- **Production link for customers**: connect a purchased domain such as
  `mmminvestment.com`, `shop.mmminvestment.com`, or the final brand domain to the
  hosting provider. This is the public link for families, ads, vendor profiles,
  QR codes, email signatures, and social profiles.
- **Commerce path**: a static host can display the catalog and run client-side
  carts, but real payment, taxes, order records, and fulfillment require a
  commerce backend or hosted checkout such as Shopify, Stripe Checkout/Payment
  Links, Square, or another PCI-compliant processor. Do not collect raw card data
  in the static pages.
- **Funding path**: quote/documentation requests can start as forms that send to
  CRM/email, but the live process must create an operator task and customer
  confirmation every time.

### Fastest path to a public review link

1. Run `python3 build.py` from `tefa-landing/`.
2. Upload or deploy the `tefa-landing/` directory with `index.html` at the publish
   root.
3. Confirm these paths load from the generated host URL:
   - `/`
   - `/store/shop.html`
   - `/store/product.html`
   - `/store/order.html`
   - `/general-store/shop.html`
   - `/general-store/product.html`
   - `/general-store/checkout.html`
4. Share the generated preview URL only after prototype banners/placeholders are
   acceptable for the audience.

### Production launch URL gate

A URL is customer-shareable only when all of these are true:

- The custom domain resolves over HTTPS.
- No live customer path contains placeholders, `TBD`, or prototype-only labels.
- Payment, tax, shipping, order confirmation, and support flows work for direct
  purchases.
- Funding quote/request forms create a customer confirmation and internal CRM or
  operator task.
- Policies for shipping, returns, privacy, terms, and support are linked from the
  footer and checkout.
- Analytics records page views, CTA clicks, product views, add-to-cart events,
  checkout starts, purchases, quote requests, and service bookings.

### Deployment provider decision

- **Netlify**: best for the fastest manual preview because a static folder can be
  drag-and-dropped and shared from a generated `netlify.app` URL. Use Git-based
  deploys once the site becomes operational.
- **Vercel**: strong option if future builds move toward a frontend app or Git
  workflow with preview deployments for every branch.
- **GitHub Pages**: acceptable for a simple static public site, especially if the
  repository or a separate deploy repository should be the source of truth.
- **Shopify or another commerce platform**: preferred once live checkout, taxes,
  inventory, fulfillment, and abandoned-cart automation need to be managed by a
  commerce system instead of custom static-page glue.

### Operator answer to "what link do I share?"

- Before launch, share the host-generated preview URL and label it **review only**.
- At launch, share the custom production domain.
- For funded purchases, share the funding or quote-request page, not a checkout
  page, unless the program's current rules allow that purchase path.
- For direct retail products, share the relevant category or product URL and let
  the parent complete normal checkout.

## Definition of Done gates

### 1. Customer-facing readiness

- No live page contains `[___]`, `[__]`, `TBD`, or "prototype" language unless it
  is intentionally hidden from customers.
- The homepage routes visitors into **Shop Resources**, **Use Funding**, and
  **Get Help Getting Set Up** within the first viewport on mobile and desktop.
- Each product has a real SKU, title, price, age/grade fit, subject/skill fit,
  images, contents list, shipping expectation, return rule, and primary CTA.
- Direct-purchase products can be added to cart and purchased through a real
  payment processor.
- Funding-supported products can be requested through an itemized quote or
  documentation workflow that does not collect payment unless the relevant
  program process allows it.
- Consultation or setup-help offers have a working booking/payment/intake flow.
- Every critical path sends a customer confirmation and creates an internal
  operator record.

### 2. Operational readiness

- Inventory or made-to-order rules are documented for every SKU.
- Product cost, retail price, margin, packaging, weight, dimensions, supplier,
  reorder threshold, and fulfillment rule are recorded.
- Tax calculation is configured according to actual nexus and filing obligations.
- Shipping methods, rates, handling times, lost-package rules, and damaged-item
  rules are published and operational.
- Return/refund policy is live and support staff can execute it.
- Support inbox, CRM, analytics, and fulfillment notifications are connected.
- First-sale rehearsal has been completed end to end with a real payment test or
  controlled live low-dollar transaction.

### 3. Compliance readiness

- The site never implies guaranteed program eligibility, approval, award amount,
  reimbursement, or vendor acceptance.
- Funding copy states that program administrators make eligibility, approval,
  reimbursement, and purchasing-rule decisions.
- Product badges distinguish **Direct purchase**, **Documentation available**,
  **Potentially funding eligible where approved**, **Requires program approval**,
  and **Not funding eligible**.
- Service pages state that setup, funding-navigation, and family-resource support
  are educational/administrative services, not legal, tax, financial, investment,
  or official program advice.
- Trump Accounts, if included later, are separated from product-funding claims and
  described only as a family-resource guidance topic after legal/operational
  review.

## Brand positioning and narrative

### Positioning

A premium homeschool and family-resource store helping parents choose practical
educational kits, books, learning tools, and guided support for children, with
clear paths for direct purchase, approved-purchase documentation, and family setup
assistance.

### Core promise

Make it easier for families to find, fund, and use practical educational
resources without getting lost in program rules, vendor paperwork, generic school
supplies, or unclear product claims.

### Narrative arc

1. Parents want better resources for their child.
2. The product and funding landscape is confusing.
3. The site makes the next best step obvious.
4. Families can shop now, explore funding, or get guided setup help.
5. Every purchase or service path includes documentation, support, and follow-up.

### Voice

- Warm, practical, and parent-first.
- Specific instead of hype-driven.
- Premium without feeling cold.
- Credible enough for education-funding conversations.
- Clear about what is known, what varies by program, and what the business does
  not control.

## Information architecture

### Primary navigation

- Shop Resources
- Use Funding
- Get Help
- How It Works
- About
- Contact

### Utility navigation

- Cart
- Track Order
- Book a Call
- Help Center

### Core pages

- `/` - Homepage
- `/shop` - Shop landing page
- `/shop/kits` - Educational kits
- `/shop/books` - Books and activity books
- `/shop/homeschool-essentials` - Homeschool supplies/resources
- `/shop/life-skills` - Practical skills and self-command
- `/shop/ai-tech` - AI, technology, robotics, electronics, and 3D design
- `/shop/bundles` - Starter, semester, and full-year bundles
- `/product/[slug]` - Product detail pages
- `/cart` - Cart drawer/full cart route
- `/checkout` - Direct checkout
- `/order-confirmation` - Order confirmation

### Funding pages

- `/funding` - Funding hub
- `/funding/texas` - Texas TEFA page
- `/funding/texas-homeschool` - Homeschool-specific Texas pathway
- `/funding/provider-status` - Vendor/provider status and instructions
- `/funding/eligible-expenses` - Eligibility explainer
- `/funding/document-checklist` - Required-document checklist
- `/funding/future-states` - Waitlist and cloned future-state framework

### Service pages

- `/services` - Service overview
- `/services/funding-setup-help` - Guided funding setup support
- `/services/eligibility-review` - Eligibility/document readiness review
- `/services/family-onboarding` - Family onboarding support
- `/services/homeschool-resource-planning` - Resource planning consultation
- `/services/trump-accounts-guidance` - Optional future page after review only

### Policy/support pages

- `/about`
- `/contact`
- `/faq`
- `/shipping`
- `/returns`
- `/privacy`
- `/terms`
- `/accessibility`
- `/support`

## Homepage conversion blueprint

The homepage must behave as a conversion router, not a brochure. It should answer
what is sold, who it is for, why it is trustworthy, and which next step is right.

### Hero

- **Purpose**: Route the visitor into the correct path immediately.
- **Why it works**: Parents are time constrained; clarity reduces cognitive load
  and bounce risk.
- **Friction removed**: "What is this?" and "Where do I start?"
- **Placement**: First viewport on mobile and desktop.
- **Implementation**: Use a product/family visual, one clear headline, one
  support sentence, and three CTAs: **Shop Resources**, **Use Funding**, and
  **Get Help Choosing**.

Recommended copy:

> Practical homeschool resources families can shop, fund, and actually use.
>
> Premium educational kits, books, tools, and guided support for families
> navigating homeschool learning, direct purchase, and approved education funding
> programs.

### Three front-door cards

- **Purpose**: Match the visitor's intent.
- **Why it works**: Parents arrive with different jobs to be done.
- **Friction removed**: Confusion between shopping, funding, and consulting.
- **Placement**: Immediately below the hero.
- **Implementation**: Three cards with short copy and separate CTAs:
  - **Shop Resources**: Browse kits, books, and learning tools.
  - **Use Funding**: Learn how documentation and approved-purchase workflows may
    work.
  - **Get Help Getting Set Up**: Book support for choosing resources, preparing
    documents, or navigating family setup.

### Shop by age, grade, subject, or goal

- **Purpose**: Let parents narrow by how they naturally think about the child.
- **Why it works**: Parents usually start with age, grade, ability, or need, not
  internal product taxonomy.
- **Friction removed**: Choice overload.
- **Placement**: Above featured products.
- **Implementation**: Tile grid for ages, grades, subjects, and learning goals.
  Each tile includes an icon/image, benefit line, and product count.

### Featured products and bundles

- **Purpose**: Create shopping momentum.
- **Why it works**: Curated choices simplify decisions and increase confidence.
- **Friction removed**: "Which product should I start with?"
- **Placement**: Mid-homepage.
- **Implementation**: Feature a starter kit, parent favorite, budget-friendly
  pick, premium bundle, and newest resource. Explain why each is featured.

### Funding explainer

- **Purpose**: Capture funding-motivated families without distracting direct
  buyers.
- **Why it works**: Funding uncertainty blocks action unless the next step is
  safe and specific.
- **Friction removed**: Fear of doing the wrong thing with program rules.
- **Placement**: After product discovery blocks.
- **Implementation**: Explain direct purchase vs. funded purchase, state that
  program rules vary, and offer documentation/quote/support CTAs.

### Service CTA block

- **Purpose**: Monetize guidance and rescue uncertain buyers.
- **Why it works**: Some parents need decision support before purchasing.
- **Friction removed**: "I do not know what fits my child or funding situation."
- **Placement**: Below funding explainer and again near the final CTA.
- **Implementation**: Present clear service cards with prices or starting points,
  time commitments, deliverables, and disclaimers.

### Trust and reassurance

- **Purpose**: Reduce risk before the cart.
- **Why it works**: Parent purchases require confidence in fit, quality, support,
  shipping, and policies.
- **Friction removed**: Safety, legitimacy, and support doubts.
- **Placement**: Trust strip under hero, policy blurbs near product sections, and
  full support block near footer.
- **Implementation**: Use real business identity, secure checkout, itemized
  receipt/documentation availability, shipping/returns summary, support contact,
  and parent reviews.

## Category and navigation UX

Category pages should balance discovery with fast path-to-product selection.

### Required hierarchy

1. Breadcrumbs.
2. Clear H1 label.
3. One-paragraph category explanation.
4. Prominent subcategory tiles.
5. Filter/sort system.
6. Product grid.
7. Buying guide or fit guide.
8. Relevant FAQ.
9. Secondary service/funding CTA.

### Navigation labels

Use parent-friendly labels:

- Shop by Age
- Shop by Grade
- Shop by Subject
- Shop by Skill
- Shop by Need
- Starter Kits
- Bundles
- Funding Help

Avoid clever labels that require interpretation.

### Filters

- Age range
- Grade band
- Subject
- Skill/learning goal
- Developmental stage
- Price
- Product type
- Parent-guided vs. independent
- Direct purchase
- Documentation available
- Ships now
- Bundle eligible

### Product cards

Each card must include image, age/grade badge, title, one-line outcome, price,
review rating when available, funding/documentation badge when accurate, and a
clear CTA.

Example card pattern:

> **AI Literacy Bench Kit**
> Ages 10-14 · Builds safe AI use and prompt-thinking habits
> $92.95 · Documentation available
> View Kit

### Mobile category UX

- Sticky filter button.
- Bottom-sheet filters.
- Visible active filter chips.
- One-tap reset.
- Large tap targets.
- No tiny desktop-style dropdowns.

## Parent buyer psychology

### Trust and risk avoidance

- **Why it works**: Children's products carry higher perceived risk around age
  fit, usefulness, safety, and wasted money.
- **Friction removed**: Fear of buying the wrong thing.
- **Funnel location**: Homepage, category cards, PDPs, cart, checkout.
- **Implementation**: Prominent age/grade fit, outcomes, real images, visible
  returns, parent reviews, and support access.

### Educational value and practical usefulness

- **Why it works**: Parents need to justify the purchase as useful, not merely
  fun.
- **Friction removed**: "Is this just another toy or impulse item?"
- **Funnel location**: Category pages, PDPs, cart reassurance, post-purchase.
- **Implementation**: Every product explains what it teaches, what skill it
  builds, how to use it, and what outcome to expect.

### Convenience and reduced decision fatigue

- **Why it works**: Busy parents convert when the site makes the next step easy.
- **Friction removed**: Overwhelm.
- **Funnel location**: Navigation, homepage, filters, quizzes, bundles.
- **Implementation**: Starter kits, shop-by-age tiles, best-for labels,
  comparison charts, and short guided quiz results.

### Budget sensitivity and value perception

- **Why it works**: Parents will pay for quality when value is concrete.
- **Friction removed**: Price anxiety.
- **Funnel location**: PDP, bundle pages, cart, funding pages.
- **Implementation**: Show contents, activities/sessions included, bundle savings,
  longevity, documentation availability, and shipping/return clarity.

### Emotional motivation

- **Why it works**: Parents want to feel they are doing right by their child.
- **Friction removed**: Guilt and uncertainty.
- **Funnel location**: Hero, PDP outcomes, service pages, post-purchase.
- **Implementation**: Use calm, affirming language about confidence,
  independence, learning, and practical progress. Avoid fear-based claims.

## Child influence in discovery

Children can influence visual interest, wishlists, and product preference while
parents control trust, relevance, safety, value, payment, and final purchase.

### Ethical discovery pattern

- **Why it works**: It lets children engage without pressuring them to purchase.
- **Friction removed**: Parent discomfort with manipulative children's retail UX.
- **Funnel location**: Category browsing, product cards, wishlists.
- **Implementation**: Use engaging product visuals, activity previews, and
  family-list/wishlist features while keeping checkout and funding workflows
  parent-oriented.

### Guardrails

Use:

- visual product previews,
- "save for parent" actions,
- age-appropriate labels,
- approachable icons,
- honest popularity/review signals.

Avoid:

- fake countdowns,
- child-targeted urgency,
- manipulative scarcity,
- cartoon pressure to buy,
- hidden add-ons,
- confusing recurring charges.

## Product detail page blueprint

### Above-the-fold PDP layout

1. Product gallery with real-life and scale images.
2. Product title.
3. Rating/review summary.
4. Age/grade/subject fit strip.
5. Price.
6. Primary CTA.
7. Shipping, return, and support reassurance near CTA.
8. Secondary CTA for funding documentation or help choosing.

### Product title

- **Why it works**: Clear titles reduce interpretation effort.
- **Friction removed**: "What is this exactly?"
- **Placement**: Top of PDP.
- **Implementation**: Use descriptive names such as "Interview & First Job Prep
  Kit for Teens," not abstract bundle names.

### Image strategy

- **Why it works**: Parents need to judge contents, quality, use case, and scale.
- **Friction removed**: Uncertainty about what arrives.
- **Placement**: Top gallery and lower content.
- **Implementation**: Include flat lay, in-use photo, scale shot, close-ups,
  contents image, packaging image, and family usage context.

### Age, grade, and subject fit

- **Why it works**: Fit is the main parent risk filter.
- **Friction removed**: Wrong-product anxiety.
- **Placement**: Near title, beside buy box, and in details.
- **Implementation**: Use a concise fit panel: ages, grades, subject, format,
  time required, parent-guided vs. independent.

### Learning outcomes

- **Why it works**: Outcomes justify educational value.
- **Friction removed**: "Why does this matter?"
- **Placement**: Above fold and in product details.
- **Implementation**: Use bullets that start with verbs: builds, teaches, helps,
  practices, supports.

### Safety, materials, and authenticity

- **Why it works**: Children's products need stronger trust signals.
- **Friction removed**: Safety and quality concerns.
- **Placement**: Below buy box and in accordion/details.
- **Implementation**: Include materials, certifications where relevant, choking or
  age warnings, publisher/author background, and quality assurance notes.

### Price and total cost visibility

- **Why it works**: Surprise costs trigger abandonment.
- **Friction removed**: Price and shipping uncertainty.
- **Placement**: Buy box, cart, and checkout.
- **Implementation**: Show item price, shipping estimate, tax timing, free-shipping
  threshold if true, and delivery estimate.

### Shipping and returns

- **Why it works**: Visible policy reduces perceived risk.
- **Friction removed**: "What if this is late or not right?"
- **Placement**: Near CTA and in policy accordion.
- **Implementation**: Short summaries near the buy button with links to full
  policies.

### Reviews

- **Why it works**: Parent proof reduces uncertainty.
- **Friction removed**: Lack of confidence.
- **Placement**: Rating summary near title; full reviews below details.
- **Implementation**: Filter reviews by child age, grade, use case, product type,
  and homeschool/direct purchase/funding documentation.

### Negative review handling

- **Why it works**: Honest, calm responses increase credibility.
- **Friction removed**: Suspicion that reviews are curated or fake.
- **Placement**: Review section.
- **Implementation**: Respond with fit guidance, support offers, and product-page
  improvements rather than defensiveness.

### Variant selection

- **Why it works**: Parents need confidence choosing the right level/version.
- **Friction removed**: Variant confusion.
- **Placement**: Near CTA.
- **Implementation**: Use visible buttons for age level, format, and bundle size.
  Avoid burying important choices in a generic dropdown.

### Bundles and cross-sells

- **Why it works**: Relevant bundles simplify complete purchases and raise AOV.
- **Friction removed**: "What else do I need?"
- **Placement**: Below primary buy area, cart drawer, and post-purchase.
- **Implementation**: Use "Complete the learning set," "Starter bundle," and
  "Often bought with" modules. Do not overload the main buy box.

## Trust architecture

Trust must be layered across the entire site.

### Homepage trust

- Business identity.
- Parent promise.
- Secure checkout.
- Support contact.
- Shipping/returns summary.
- Funding disclaimer.

### Category trust

- Age/fit labels.
- Review badges.
- Documentation badges.
- Ships-now badges.
- Buying guides.

### PDP trust

- Reviews.
- Returns.
- Shipping.
- Safety/materials.
- What is included.
- Outcomes.
- Support access.

### Cart trust

- Secure checkout.
- Clear total.
- Delivery estimate.
- Return reminder.
- Support link.

### Checkout trust

- Payment security.
- Guest checkout.
- No surprise fees.
- Support access.
- Clear confirmation.

### Post-purchase trust

- Confirmation.
- Tracking.
- Usage tips.
- Support link.
- Review request.

## Visual psychology

### Color

Use warm, calm, premium colors: cream, soft charcoal, muted clay, forest green,
deep navy, and gentle gold accents. Avoid neon, toy-store rainbow palettes, too
much red, or a cold luxury aesthetic that feels inaccessible.

### Typography

Use readable headings, warm editorial body type, clean UI labels, strong contrast,
and large mobile sizes. Avoid childish fonts, body script fonts, tiny gray text,
and too many typefaces.

### Layout

Use clear spacing, predictable grids, short paragraphs, strong headings, and calm
white space. Parents should be able to scan without feeling sold to aggressively.

### CTA design

Use one dominant CTA style for buying and quieter styles for help/funding. Buttons
must be large, high contrast, and verb-specific.

### Cards

Cards should include a strong image, age/grade badge, title, one-line outcome,
price, review/documentation badge when accurate, and CTA.

### Photography

Prioritize real contents, in-use context, scale shots, detail shots, consistent
lighting, and premium tabletop scenes. Do not rely only on mockups or generic
stock imagery.

### Illustration and icons

Use illustrations for explanations, empty states, and funding steps. Use icons as
scanning aids, not decoration overload.

### Motion

Use subtle hovers, accordions, cart drawer transitions, and loading states. Avoid
bouncing CTAs, autoplay distractions, or child-targeted urgency animations.

## Decision simplification system

### Starter kits

- **Why it works**: Parents want a safe first step.
- **Friction removed**: Choice overload.
- **Funnel location**: Homepage, category pages, quiz results.
- **Implementation**: Label by age and goal, such as "Best Starter Kit for Ages
  8-10."

### Bundles

- **Why it works**: Bundles make the purchase feel complete.
- **Friction removed**: Fear of missing needed materials.
- **Funnel location**: Category pages, PDP, cart.
- **Implementation**: Show bundle contents, savings, and intended use case.

### Comparison charts

- **Why it works**: Parents need rational justification.
- **Friction removed**: Uncertainty between similar options.
- **Funnel location**: Category pages and PDPs.
- **Implementation**: Compare age, skill, parent involvement, time, contents, and
  price.

### Guided quiz

- **Why it works**: A quiz converts uncertainty into a recommendation.
- **Friction removed**: Overwhelm.
- **Funnel location**: Homepage, category pages, exit-intent replacement, email
  capture.
- **Implementation**: Ask five to seven questions and return a product bundle or
  service recommendation.

### Staff picks and most-loved items

- **Why it works**: Curated proof reduces risk.
- **Friction removed**: Lack of confidence.
- **Funnel location**: Homepage, category pages, product recommendations.
- **Implementation**: Use only real picks and real popularity signals.

## Cart and checkout UX

### Cart drawer and full cart

- **Why it works**: Drawer preserves browsing; full cart supports review.
- **Friction removed**: Disruption and uncertainty.
- **Funnel location**: Add-to-cart and pre-checkout.
- **Implementation**: Use a cart drawer for quick confirmation and a full cart for
  totals, shipping estimate, support, and funding quote paths.

### Shipping clarity

- **Why it works**: Hidden shipping costs cause abandonment.
- **Friction removed**: Surprise cost anxiety.
- **Funnel location**: PDP, cart, checkout.
- **Implementation**: Show shipping estimates, delivery windows, and free-shipping
  thresholds if true.

### Coupon behavior

- **Why it works**: Large coupon boxes send users hunting for codes.
- **Friction removed**: Checkout distraction.
- **Funnel location**: Cart and checkout.
- **Implementation**: Collapse coupon entry under "Have a code?" and auto-apply
  known promotions.

### Guest checkout and wallets

- **Why it works**: Parents want speed and low friction.
- **Friction removed**: Account-creation and typing fatigue.
- **Funnel location**: Checkout.
- **Implementation**: Enable guest checkout, Apple Pay, Google Pay, and address
  autocomplete.

### Error prevention

- **Why it works**: Form errors create frustration and abandonment.
- **Friction removed**: Form fatigue.
- **Funnel location**: Checkout.
- **Implementation**: Inline validation, clear messages, preserved field values,
  and obvious next-step buttons.

## Mobile-first parent UX

Mobile is the priority conversion environment for busy parents.

### Requirements

- Thumb-friendly navigation.
- Sticky PDP CTA.
- Sticky cart access.
- Bottom-sheet filters.
- Large tap targets.
- Collapsible details.
- Fast media.
- Minimal popups.
- Wallet payments.
- Mobile-visible trust cues.

### Mobile PDP order

1. Image.
2. Title.
3. Age/fit.
4. Price.
5. CTA.
6. Shipping/returns.
7. What is included.
8. Learning outcomes.
9. Reviews.
10. FAQ.
11. Related products.

## Ethical persuasion tactics

### Appropriate

- Real social proof.
- Real inventory scarcity.
- Real shipping cutoffs.
- Bundle savings.
- Free guides/checklists.
- Saved carts and wishlists.
- Clear return policy.
- Creator or educational credibility.
- Familiar, predictable checkout patterns.

### Risky or prohibited

- Fake countdowns.
- Fake popularity.
- Child-targeted pressure.
- Fear-based parenting copy.
- Inflated anchor pricing.
- Hidden recurring charges.
- Program approval guarantees.
- Implied state/federal affiliation without approval.

## Funding and consultation CTA UX

### Placement rules

- Homepage: one front-door card and one mid-page service block.
- Category pages: small "Need help choosing?" module below product discovery.
- PDPs: secondary CTA near buy area for documentation/help.
- Cart: "Using education funding? Request an itemized quote instead" only when
  relevant.
- Funding pages: primary CTA to checklist, quote request, or booking.
- Resource pages: CTA after useful content.

### Separation rules

- Buying CTAs are primary.
- Service CTAs are secondary unless the page is a service page.
- Funding CTAs are informational and compliance-safe.
- Service pricing and deliverables are explicit.

### Service copy pattern

> We can help you organize your next steps, compare resources, and prepare
> documentation. Program eligibility, approval, reimbursement, and purchasing
> rules are decided by the relevant program administrator.

## Common conversion killers and fixes

| Conversion killer | Why it hurts | Fix |
|---|---|---|
| Visual chaos | Parents feel overwhelmed or distrustful | Use calm hierarchy, fewer colors, fewer animations |
| Weak PDPs | Parents cannot justify the purchase | Add age fit, outcomes, contents, reviews, shipping, returns, FAQ |
| Hidden shipping costs | Surprise costs trigger abandonment | Show estimates near CTA and cart |
| Poor mobile UX | Busy parents shop on phones | Use sticky CTA, fast media, wallet pay, easy filters |
| Generic copy | Does not answer parent objections | Write around child fit, value, support, outcomes |
| Weak social proof | Parents lack confidence | Collect parent reviews with child age/use case |
| Too many choices | Creates decision paralysis | Use starter kits, quiz, best-for labels, curated collections |
| Unclear age guidance | Creates wrong-product anxiety | Show age/grade/skill fit everywhere |
| Weak return reassurance | Increases perceived risk | Put return summary near CTA |
| Low-quality imagery | Makes products feel cheap or unclear | Use real photos, scale shots, and contents images |
| Slow page speed | Reduces trust and causes abandonment | Compress images, minimize JS, optimize Core Web Vitals |

## Highest-impact implementation priorities

### Quick wins

1. Replace all placeholders and prototype labels on live paths.
2. Add the three homepage front doors.
3. Add age/grade/goal badges to every product card.
4. Add shipping and return reassurance near buy buttons.
5. Add "Need help choosing?" secondary CTAs.
6. Add parent-focused FAQ blocks.
7. Add visible support contact and response-time promise.
8. Add mobile sticky CTA.
9. Add trust strip below the hero.
10. Compress images and remove unnecessary scripts.

### Medium-effort, high-impact improvements

1. Build complete PDP templates.
2. Add review capture and review filtering.
3. Add a guided shopping quiz.
4. Add bundle comparison charts.
5. Add shipping estimator.
6. Add abandoned-cart email.
7. Add funding request/quote workflow.
8. Add CRM tags for buyer, funding lead, service lead, and future-state interest.
9. Add product documentation PDFs.
10. Add a launch analytics dashboard.

### Strategic optimizations

1. Personalized recommendations.
2. State-by-state funding hub.
3. Provider/vendor marketplace integrations.
4. Parent review/community engine.
5. Subscription/replenishment for consumables.
6. Homeschool group partnerships.
7. Service packages and onboarding playbooks.
8. Retargeting campaigns.
9. SEO content engine.
10. Lifecycle email/SMS journeys.

## A/B testing roadmap

| Test | Hypothesis | Principle | Expected behavior change | Primary KPI |
|---|---|---|---|---|
| Hero CTA copy | "Shop Resources" beats "Browse Store" | Clarity and relevance | More hero clicks | Hero CTA CTR |
| Shop by age vs. shop by subject | Age-first navigation matches parent mental models | Cognitive fit | More category engagement | Category CTR, product views |
| Review summary placement | Reviews near title increase confidence | Social proof | More add-to-cart | Add-to-cart rate |
| Trust badge placement | Reassurance near CTA beats checkout-only badges | Risk reduction | Higher PDP and cart progression | Add-to-cart, checkout start |
| Shipping/returns near CTA | Visible policy copy reduces hesitation | Risk reversal | Lower abandonment | Cart abandonment rate |
| Bundles vs. standalone emphasis | Starter bundles reduce decision fatigue | Choice simplification | Higher conversion/AOV | Conversion rate, AOV |
| Mobile sticky add-to-cart | Sticky CTA reduces scroll friction | Convenience | More mobile adds | Mobile add-to-cart rate |

## First-sale workflow SOP

### Direct purchase

1. Parent lands on homepage.
2. Parent chooses **Shop Resources**.
3. Parent filters by age, subject, goal, or budget.
4. Parent opens PDP.
5. Parent reviews fit, contents, outcomes, shipping, returns, and reviews.
6. Parent adds to cart.
7. Cart shows product, shipping estimate, returns, support, and total-cost path.
8. Parent checks out as guest or account holder.
9. Payment succeeds.
10. Confirmation page displays.
11. Confirmation email sends.
12. Internal fulfillment task is created.
13. Product is picked, packed, and shipped.
14. Tracking email sends.
15. Delivery follow-up sends.
16. Review request sends.
17. Customer enters post-purchase nurture flow.

### Funding-supported quote/documentation

1. Parent lands on homepage.
2. Parent chooses **Use Funding**.
3. Parent reads Texas/funding explainer and disclaimer.
4. Parent selects products or submits a quote/documentation request.
5. CRM tags the lead by state, program interest, products, and status.
6. Operator sends itemized quote/documentation.
7. Parent verifies rules in the official program portal.
8. Business fulfills only after the appropriate approved purchase/payment path is
   complete.
9. Follow-up asks whether more documentation or setup help is needed.

### Service path

1. Parent chooses **Get Help Getting Set Up**.
2. Parent selects a defined service.
3. Parent reads deliverables and disclaimer.
4. Parent books and pays if applicable.
5. Intake form is completed.
6. Confirmation/reminders send.
7. Call/session is completed.
8. Operator sends checklist, product recommendations, or next-step plan.
9. Parent is routed to shop, quote request, or future-state waitlist.

## Phased rollout

### Phase 1: immediate direct-to-consumer launch

- Finalize product catalog and pricing.
- Replace all placeholders.
- Connect payment, tax, shipping, CRM, email, support, and analytics.
- Publish homepage, shop, PDPs, cart, checkout, policies, and support.
- Run first-sale rehearsal.
- Complete first real direct sale.

### Phase 2: funding-program readiness and provider expansion

- Publish funding hub and Texas-specific pages.
- Build quote/documentation workflow.
- Prepare vendor/provider documentation packet.
- Track provider approval status and state expansion opportunities.
- Add future-state templates and waitlist.
- Review all funding claims before publication.

### Phase 3: consulting, service monetization, and partnerships

- Publish service pages and booking/payment flows.
- Create intake and post-call SOPs.
- Add family onboarding packages.
- Build partner/referral page.
- Add Trump Accounts guidance only after legal/operational review.
- Build strategic homeschool, local, and provider partnerships.

## Recommendation format for builders

For every recommendation and backlog item, capture:

1. **Why it works**: the motivation or behavioral principle.
2. **What friction it removes**: confusion, risk, price anxiety, choice overload,
   lack of trust, or time pressure.
3. **Where it belongs in the funnel**: homepage, category, PDP, cart, checkout,
   funding page, service page, or post-purchase.
4. **What good implementation looks like**: exact copy, layout, visual hierarchy,
   interaction, integration, or operating procedure.
