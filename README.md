# KinderPlanet Varle catalogue automation

[![Update Varle XML](https://github.com/AlanButylkin/kinderplanet-varle-automation/actions/workflows/sync.yml/badge.svg)](https://github.com/AlanButylkin/kinderplanet-varle-automation/actions/workflows/sync.yml)

This repository refreshes the public Varle XML every 30 minutes from the
KinderPlanet Verskis full export. It changes only validated `price` and
`quantity` fields in the Varle feed. It also maintains an autonomous catalogue
history and Excel workbook for inspection.

## Stable catalogue address

https://alanbutylkin.github.io/kinderplanet-varle-automation/varle.xml

The address stays the same when a new validated feed is published.

## Latest inspection files

- [Excel catalogue workbook](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/catalog-latest.xlsx)
- [Latest field changes](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/catalog-latest-changes.csv)
- [New products](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/new-products.csv)
- [Removed products](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/removed-products.csv)
- [Source products not currently in Varle](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/not-in-varle.csv)
- [Catalogue report](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/catalog-report.json)
- [Compressed current catalogue CSV](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/catalog-current.csv.gz)
- [Compressed retained event history](https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/catalog-events.csv.gz)

The workbook contains `Current Products`, `New Products`, `Removed Products`,
`Latest Changes`, `Change History`, `Not in Varle`, and `Issues` sheets. New products and
field-level changes are detected automatically from the Verskis XML. The first
run creates a baseline and deliberately does not label the whole catalogue as
new.

On the first UTC day of each month the workflow also creates a permanent
`catalog-monthly-YYYY-MM` release containing that month's autonomous snapshot.
Monthly releases are excluded from the eight-version feed cleanup.

## Safety behaviour

- XML is validated before and after every update.
- At least 95% of catalogue IDs must match a unique source code.
- Duplicate source codes are reported and left unchanged.
- Missing products are set to zero stock only after two consecutive valid runs.
- Removed products remain in the inspection history with `active=false`,
  quantity zero, and a removal timestamp.
- Products that reappear are restored automatically and logged.
- New Verskis products and options are recorded even when they are not yet in
  the Varle XML.
- Catalogue changes are retained for 400 days in compressed state.
- Products missing source price or quantity are left unchanged.
- A run changing more than 25% of products is blocked.
- An unexpected source-catalogue drop of more than 10% is blocked.
- If a run fails, the last valid release remains publicly available.
- The latest eight versions are retained for recovery.

## Collected fields

The autonomous workbook mirrors the existing scraper's current 51-column
export where the values exist in Verskis: product and parent codes, EAN,
category, Lithuanian name and description, VAT, prime cost, current and old
prices, gross price, quantity, unit, weight, dimensions, manufacturer, brand,
model, up to 28 images, and image count. It additionally records product versus
option type, short description, minimum purchase quantity, publication flags,
supplier, stock location, all source attributes, active status, missing-run
count, and first/last/removal timestamps.

The browser-based admin scraper is no longer required for routine new-product
detection. It remains useful only for admin fields that are absent from the
Verskis export, such as a public URL when the XML does not provide one.

This is a public repository. Its release inspection files must therefore be
treated as public data. Move the tracking portion to a private repository
before adding any information that is not already present in the Verskis
export.

The workflow can also be started manually from the repository's **Actions** tab.

## Operational status

- A green **passing** badge above means the latest automation run succeeded.
- A red **failing** badge means the latest run failed; the previous valid XML
  remains online.
- The latest report is available at:
  https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/report.json
- The `generated_at` value in that report should normally be less than one hour
  old. If it is more than 90 minutes old, inspect the Actions page.
