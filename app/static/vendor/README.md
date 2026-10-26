# Vendored front-end libraries

Served locally so the web UI works without internet access. Files are unmodified upstream builds.

| Library | Version | Source |
|---|---|---|
| Bootstrap | 5.3.1 | `https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/` (`css/bootstrap.min.css`, `js/bootstrap.bundle.min.js`) |
| jQuery | 3.6.0 | `https://code.jquery.com/jquery-3.6.0.min.js` |
| Bootstrap Icons | 1.11.1 | `https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/` (`bootstrap-icons.min.css`, `fonts/bootstrap-icons.woff2`, `fonts/bootstrap-icons.woff`) |
| Archivo | v25 (variable, `wdth` 62-125, `wght` 100-900) | `https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900` (latin and latin-ext `.woff2` subsets, `@font-face` rules in `archivo.css`). SIL Open Font License, see `archivo/OFL.txt` |

To upgrade, download the new version's files over the existing ones, keeping `bootstrap-icons/fonts/` next to its CSS, and update this table.
