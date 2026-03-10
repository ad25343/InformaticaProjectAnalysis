# FirstBank Digital — Informatica PowerCenter Test Estate

This directory contains a complete, fictional test estate for Informatica PowerCenter based on "FirstBank Digital," a retail banking platform.

## Structure

```
firstbank/
├── mappings/
│   ├── simple/      (15 mapping XML files)
│   ├── medium/      (20 mapping XML files)
│   ├── complex/     (15 workflow XML files)
├── workflows/
│   ├── simple/      (15 workflow XML files)
│   ├── medium/      (20 workflow XML files)
│   ├── complex/     (15 workflow XML files)
├── parameter_files/
│   ├── params_firstbank_dev.xml
│   ├── params_firstbank_uat.xml
│   ├── params_firstbank_prod.xml
├── all_mappings/    (flat directory with all 50 mapping files)
├── firstbank_full.manifest.json
└── README.md
```

## Mapping Tiers

### SIMPLE (15 mappings)
Single-source, straightforward ETL patterns with minimal transformations:
- Dimension loads (Customer, Branch, Product, Date, Currency, Employee, Channel)
- Staging extracts (Customer, Account, Transaction)
- Reference data loads (Interest Rates, Fees, Country Codes, ATM Transactions, Exchange Rates)

Typical pattern: Source → Source Qualifier → Expression → Target

### MEDIUM (20 mappings)
Multi-source patterns with lookups, aggregations, and SCD2:
- SCD Type 2 tracking (Customer, Account, Product)
- Fact tables with lookups (Daily Transactions, Loan Payments, Account Fees, Interest Accrual, FX Conversions, Wire Transfers, Card Transactions, Check Processing)
- Aggregations (Monthly Customer Summary, Branch Daily Totals, Product Performance)
- Complex logic (Loan Origination, Customer Segmentation, Overdraft Events, Direct Deposits, Statements)
- Bridge dimensions (Account-Customer)

Typical pattern: Source → SQ → Lookup → Expression → Aggregator/Router → Target

### COMPLEX (15 mappings)
Multi-source, enterprise-scale patterns with joiners, multiple targets, and sophisticated business logic:
- Fraud Detection — scoring with multiple data streams and routing
- Regulatory Capital — Basel III compliance calculations
- AML Monitoring — sanctions list matching and suspicious activity reporting
- Credit Risk — rating and classification
- Hierarchies (Customer, Organization)
- Reconciliation (GL to sub-ledger to bank)
- Derivatives Valuation — mark-to-market with multiple calculation engines
- Mortgage Pipeline — multi-step origination with underwriting decisions
- Liquidity Coverage — LCR calculations for Basel
- Counterparty Exposure — netting and collateral agreements
- Trade Settlement — T+2 with settlement calendars
- Portfolio Attribution — performance analysis
- ETL Control Framework — audit and SLA tracking
- Stress Testing — scenario analysis

Typical pattern: Multiple Sources → Joiners → Lookups → Expression → Router → Multiple Targets

## Workflows

Each mapping has a corresponding workflow XML in `workflows/[tier]/wf_[mapping_name].xml`:
- Contains a SESSION task referencing the mapping
- Includes default configuration (commit interval, rollback policy)
- Can be deployed to Informatica Workflow Manager

## Parameter Files

Environment-specific parameters in `parameter_files/`:
- `params_firstbank_dev.xml` — Development (FIRSTBANK_OLTP_DEV → FIRSTBANK_DWH_DEV)
- `params_firstbank_uat.xml` — User Acceptance Testing (FIRSTBANK_OLTP_UAT → FIRSTBANK_DWH_UAT)
- `params_firstbank_prod.xml` — Production (FIRSTBANK_OLTP_PROD → FIRSTBANK_DWH_PROD)

Each parameter file can be applied to any session using Informatica's parameter override mechanism.

## Manifest

`firstbank_full.manifest.json` provides:
- List of all 50 mapping files
- Metadata: version, description, creation date
- Distribution summary (simple/medium/complex breakdown)

This manifest can be consumed by automated ETL watchers, CI/CD pipelines, or documentation generators.

## Validation

All XML files are valid PowerCenter format (DTD: powrmart.dtd):
- Proper hierarchy: POWERMART → REPOSITORY → FOLDER → SOURCE/TARGET/TRANSFORMATION/MAPPING
- Valid transformation chains with CONNECTOR elements
- Target load order definitions
- Session and workflow configurations

## Key Features

1. **Diverse Business Logic** — Real-world financial services scenarios:
   - Customer relationship management (CRM)
   - Transactional banking (deposits, withdrawals, transfers)
   - Credit and lending (loans, origination, payments)
   - Trading and derivatives (positions, valuations, risk)
   - Regulatory compliance (capital adequacy, AML, fraud)
   - Data quality and audit

2. **Realistic Scale** — 50 mappings across 3 complexity tiers:
   - Simple: foundational dimension/fact loading
   - Medium: multi-source with business transformations
   - Complex: enterprise integration with multiple targets

3. **Complete Documentation** — Each mapping includes:
   - Descriptive names tied to business functions
   - Source, target, and intermediate transformation definitions
   - Realistic field definitions (IDs, names, dates, decimal amounts, etc.)
   - Proper transformation chains with valid connectors

## Usage

To integrate into Informatica PowerCenter:

1. Import all mapping XMLs into a PowerCenter repository
2. Import all workflow XMLs into the same repository
3. Apply environment-specific parameter files to sessions
4. Schedule workflows in Informatica Workflow Manager
5. Monitor execution via Informatica logs and audit tables

## Notes

- All data is fictional; any resemblance to real banks is coincidental
- Mappings are representative of common ETL patterns, not production-ready code
- This estate is suitable for testing, training, and Informatica platform evaluation
- Field definitions and transformations follow PowerCenter naming conventions and best practices

---

**Generated:** 09/03/2026
**Version:** 1.0
**Repository:** FIRSTBANK_DWH
**Database Type:** Oracle
