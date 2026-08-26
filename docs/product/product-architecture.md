# Updated Product Architecture: Company Brain and Agent Family

## Core Concept

**interfaze-agent** is not only a sales automation product. It is a company intelligence platform that starts with an AI Sales Agent and later expands into multiple specialized business agents.

The long-term product family is called:

**interfaze-agent family**

The first agent in the family is:

**Sales Agent**

The shared intelligence layer behind all agents is:

**Company Brain**

---

## Company Brain Definition

Company Brain is the central knowledge and reasoning layer that understands the company.

It stores and continuously improves knowledge about:

* Company identity
* Products
* Services
* Past sales
* Current customers
* Leads
* Contacts
* Internal documents
* Sales performance
* Stock data
* Operations data
* Supplier information
* Market data
* Customer objections
* Pricing information
* Distribution channels
* Business rules
* Company preferences
* Historical decisions
* Agent activity

Company Brain is not a single dashboard tab only. It is the core context engine that every future agent will use.

---

## First Branch: Sales Agent

The first product branch is the **Sales Agent**.

The Sales Agent uses Company Brain to:

* Understand what the company sells
* Understand which products have sales potential
* Analyze past sales and current customers
* Discover global leads
* Research companies
* Find the right buyer contacts
* Generate personalized cold emails
* Send approved outreach
* Create WhatsApp messages
* Prepare LinkedIn connection notes
* Store sales intelligence
* Track outreach and market performance

The Sales Agent is the MVP focus.

---

## Future Branches

After the Sales Agent, the same Company Brain can power other business agents.

### Operations Agent

Possible capabilities:

* Understand internal operations data
* Detect operational bottlenecks
* Analyze workflow inefficiencies
* Suggest process improvements
* Track recurring operational issues
* Generate operational reports

### Stock Optimization Agent

Possible capabilities:

* Analyze inventory data
* Predict stock shortages
* Identify slow-moving products
* Recommend reorder timing
* Detect overstock risks
* Connect sales demand with inventory planning
* Suggest country/product-based stock allocation

### Procurement Agent

Possible capabilities:

* Track suppliers
* Compare supplier performance
* Detect better sourcing options
* Analyze purchase history
* Suggest negotiation points
* Monitor supplier risks

### Customer Support Agent

Possible capabilities:

* Learn from support tickets
* Detect common complaints
* Suggest product improvements
* Generate support replies
* Identify recurring customer issues
* Feed insights back into sales and operations

### Finance / Reporting Agent

Possible capabilities:

* Analyze revenue and costs
* Generate executive summaries
* Track product profitability
* Compare markets
* Identify financial risks
* Create monthly business reports

---

## Product Family Structure

```text
interfaze-agent
  |
  |-- Company Brain
  |     |-- Company profile
  |     |-- Product knowledge
  |     |-- Internal documents
  |     |-- Past sales
  |     |-- Current contacts
  |     |-- Market intelligence
  |     |-- Agent memory
  |     |-- Business rules
  |
  |-- Sales Agent
  |     |-- Lead discovery
  |     |-- Company research
  |     |-- Contact discovery
  |     |-- Email outreach
  |     |-- WhatsApp outreach
  |     |-- LinkedIn note generation
  |     |-- Sales analytics
  |
  |-- Operations Agent
  |     |-- Future module
  |
  |-- Stock Optimization Agent
  |     |-- Future module
  |
  |-- Procurement Agent
  |     |-- Future module
  |
  |-- Support Agent
  |     |-- Future module
```

---

## Important Product Naming Change

The MVP should not say:

“Company Brain is the dashboard that shows sales insights.”

Instead, it should say:

“Company Brain is the shared company intelligence layer. The first agent using it is the Sales Agent.”

This gives the product room to expand.

---

## Updated MVP Scope

The MVP should be called:

**interfaze-agent Sales Agent MVP**

It includes:

* Company Brain setup
* Internal company data ingestion
* Product understanding
* Past sales and current contact analysis
* Lead discovery
* Lead research
* Contact discovery
* Email outreach
* WhatsApp outreach
* LinkedIn note generation
* Sales analytics
* Market intelligence analytics

The MVP should prepare the data structure for future agents, but it should not build operations or stock optimization features yet.

---

## Updated Long-Term Vision

interfaze-agent becomes a family of AI agents that operate on top of one shared Company Brain.

The Sales Agent is the first branch because it has the clearest immediate business value.

Later, the same Company Brain can support operations, stock optimization, procurement, support, reporting, and other company-specific agents.
