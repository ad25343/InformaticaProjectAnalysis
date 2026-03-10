# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- PRD.md — full product requirements document with pipeline architecture, API surface,
  data model, success metrics, security architecture, and version roadmap (v0.1.0–v1.0.0)
- DESIGN.md — complete design document covering all 5 phases, pattern grouping,
  variation tiers, structural classification, strategy document format, human gate,
  project config, integration contract with InformaticaConversion, and UI design
- SECURITY.md — security policy covering input validation, infrastructure, generated
  output, and API security headers
- Sample project config (`firstbank_migration.project.yaml`) for the 50-mapping
  FirstBank test estate
- InformaticaConversion v2.15.0 reference codebase in `ConversionFolder/` for
  parser reuse and integration point analysis
- Project baseline: LICENSE (CC BY-NC 4.0), .gitignore, CHANGELOG.md, .env.example
