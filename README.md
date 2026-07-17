# KinderPlanet Varle catalogue automation

This repository refreshes the public Varle XML every 30 minutes from the KinderPlanet
Verskis full export. It changes only validated `price` and `quantity` fields.

## Stable catalogue address

https://github.com/AlanButylkin/kinderplanet-varle-automation/releases/latest/download/varle.xml

The address stays the same when a new validated release is published.

## Safety behaviour

- XML is validated before and after every update.
- At least 95% of catalogue IDs must match a unique source code.
- Duplicate source codes are reported and left unchanged.
- Missing products are set to zero stock only after two consecutive valid runs.
- Products missing source price or quantity are left unchanged.
- A run changing more than 25% of products is blocked.
- An unexpected source-catalogue drop of more than 10% is blocked.
- If a run fails, the last valid release remains publicly available.
- The latest eight versions are retained for recovery.

The workflow can also be started manually from the repository's **Actions** tab.
