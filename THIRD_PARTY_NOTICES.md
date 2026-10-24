# Third-Party Notices

This repository contains material derived from, or distributed together with, the following
third-party works. Each remains under its own license.

## Group sign-in presentation reference

- Reference: https://github.com/Azincc/astrbot_plugin_skland (`main.py`, `skd` group output).
- Author: Azincc and contributors.
- The group sign-in output follows its three-column presentation (Arknights / Endfield / nickname).
- No source code from this project is included; status aggregation uses this adapter's own implementation.

## Priestess-bot

- Source: https://github.com/Billtranscend/Priestess-bot
- Original author: Billtranscend and upstream contributors.
- Adaptation base: commit `59e2be8ee0006a031a903cce1d7b2d72138ba8f5`.
- Collection and rendering-timeout improvements synchronized from `10419ecb8ea970326dcff5be3513c5ffb4edffda` (2026-10-24); adapted for AstrBot in v0.1.18.
- Used in: AstrBot adaptations under `priestess_features/` and the derived
  help image in `resources/help/`, with the upstream Git history retained.
- The duplicate NoneBot runtime tree was removed from the AstrBot release in
  v0.1.16; original source remains available in the upstream repository and Git history.
- License: MIT. The original copyright and full license text remain in `LICENSE`.
- The maintainer of this repository provides AstrBot interface adaptation,
  compatibility fixes, packaging, and distribution. Original authorship of the
  upstream features remains with the original authors and contributors.

## astrbot_plugin_sklands

- Source: https://github.com/SciNancy/astrbot_plugin_sklands
- Used in: the AstrBot Skland account and game-data implementation (`main.py`,
  `api/`, `schemas/`, `resources/`, and related modules).
- License: MIT

```
MIT License

Copyright (c) 2025 SciNancy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## nonebot-plugin-skland

- Source: https://github.com/FrostN0v0/nonebot-plugin-skland
- The Arknights Heybox export format and pool-name mapping in
  `priestess_features/ark_heybox.py` follow this project's `services/gacha.py`.
- Static images bundled under `resources/images/ark_card/`, `endfield/`, `rogue/`
  and `background/endfield/simple/` are from its resource tree; original artwork
  remains attributed to its respective game rights holders.
- Used in: `plugins/skland_ef_theme/templates/` (`endfield_card.html.jinja2`, `endfield_macros.html.jinja2`,
  `ef_gacha.html.jinja2`, `ef_gacha_macros.html.jinja2`). These files are restyled versions of the
  templates of the same names shipped with nonebot-plugin-skland 0.7.1; the data fields and template
  logic come from the original work.
- License: MIT

```
MIT License

Copyright (c) 2025 FrostN0v0

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Barlow Condensed

- Source: https://github.com/jpt/barlow
- Used in: `plugins/ef_theme/fonts/` (`BarlowCondensed-Medium.ttf`, `BarlowCondensed-SemiBold.ttf`,
  `BarlowCondensed-Bold.ttf`), unmodified.
- License: SIL Open Font License 1.1. The full license text is included as
  `plugins/ef_theme/fonts/OFL.txt`.

## Game data and assets

Operator, weapon, stage, enemy, activity and gacha pool data are downloaded at runtime from
AKEData (https://www.akedata.wiki/) and from Skland; they are not stored in this repository.
Arknights activity and gacha pool times are downloaded at runtime from
yuanyan3060/ArknightsGameResource (https://github.com/yuanyan3060/ArknightsGameResource) and from
the public API of PRTS Wiki (https://prts.wiki/, content under CC BY-NC-SA 4.0); they are not stored
in this repository either. The banners shown in the activity calendar are fetched at runtime as well:
Endfield activity pictures from AKEData, Endfield pool banners from the game's own pool page, and
Arknights activity and pool banners from PRTS Wiki. They are cached locally and never committed.
The screenshots under `docs/images/` and `plugins/skl_help/help.webp` contain names and artwork from
Arknights and Arknights: Endfield, which are the property of their respective rights holders and
are shown only to illustrate the bot's output.
