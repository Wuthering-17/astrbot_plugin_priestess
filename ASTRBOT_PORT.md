# Priestess-bot → AstrBot port

Source: `Billtranscend/Priestess-bot`, commit `59e2be8ee0006a031a903cce1d7b2d72138ba8f5`.

This branch targets a native AstrBot plugin for an existing QQ deployment. The
original NoneBot application remains the behavior reference. A second NoneBot
service is not considered completion of this port.

## Compatibility boundary

- Original entry point: `bot.py`, NoneBot 2.5.0, OneBot v11.
- Upstream feature packages: `nonebot-plugin-skland==0.7.2` and
  `nonebot-plugin-pErithacus==1.4.4`. Their commands, account models, and
  persistence cannot be loaded as AstrBot plugins directly.
- Local code: `plugins/` contains commands, scheduled jobs, database access,
  rendering, and OneBot-specific group actions. Pure data processing code can
  be reused after moving it behind a framework-neutral interface.
- AstrBot entry point: `main.py` with a `Star` subclass and `metadata.yaml`.
  Persistent files belong in AstrBot's plugin data directory.

## Port status

| Area | AstrBot port status | Remaining work |
| --- | --- | --- |
| Account binding and role selection | QR/token binding, role sync, role listing and default switching, `-r` temporary role selection are wired in `main.py`. The AstrBot database still stores one Skland account per QQ ID. | Migrate the source account-owner model so one QQ ID can manage multiple Skland accounts; port add-account and multi-account unbind flows. |
| Arknights and Endfield sign-in | Manual sign-in for both games; scheduled daily sign-in is wired for 00:15 and 00:20 China time. | Verify exact API behavior on NapCat; handle missed scheduler runs and multi-account credentials. |
| Cards, roster, gacha history | Cards, roster, gacha history (including the Endfield rerun pool), recruitment, roguelike and daily Endfield gacha synchronization are present. Heybox import/undo rebuilds only negative-position rows, verifies the game UID and retains a protected snapshot for reconciliation. | Check Playwright/Chromium availability on the deployed AstrBot host. |
| Wiki and Endfield data | Operator, weapon and equipment lookup, AKEData sync, version notifications and usage/team summaries are wired. | Check renderer availability and group notification behavior on the deployed AstrBot host. |
| High-difficulty guide | Guide, enemy attributes, speed boards, collection and opt-out commands are wired. | Runtime-check source data refresh and QQ image delivery. |
| Calendar and scheduled notices | Both game calendars, activity notices and admin preview are wired in Beijing time. | Runtime-check the OneBot broadcast APIs and delivery windows. |
| Sanity reminder | `/理智`, group subscriptions and a 10-minute background reminder loop are wired. The earlier 90% stamina warning loop remains available too. | Verify mentions, persistence and token refresh through the deployed adapter. |
| Weekly report | Scheduled Sunday 19:00 report, daily 06:00 admin refresh, preview command, group allowlist at `data/plugin_data/astrbot-plugin-priestess/weekly_report/config.json`, and @all disabled to match the current source setting. | Check report image rendering and NapCat essence/quota API responses on the deployed host. |
| Account health | `/森空岛体检` checks bindings and roles; only `/森空岛体检 清理` removes invalid data after writing a 0600 backup. Monday 04:00 job checks and notifies AstrBot admins without deleting data. Missing role defaults are repaired at startup. | The source's unattended scheduled deletion is disabled pending explicit authorization. Existing single-account schema remains; migrated health checks cannot inspect multiple Skland accounts per QQ ID. |
| Keyword dictionary replies | Not ported. The NoneBot repo depends on external `nonebot-plugin-pErithacus`; its rule data and management implementation are not included in this repository. | Port or replace that external plugin and plan a rule-data migration. |
| QQ group behavior | OneBot invitation approval and membership change handling are wired. Ordinary image replies are sent as separate AstrBot messages. | Bison subscription queue integration, text-mention command tolerance, and source-style merged-forward labeling are not ported. |
| Group gacha leaderboards | Text boards for the top/bottom Endfield character and weapon pull averages, group opt-out, and personal totals are wired. | Compare the ranking calculation with the source on real records; the AstrBot model currently supports one account per QQ ID. |

## Data compatibility and verification

The old NoneBot database uses `nonebot-plugin-user` owner records and can store
multiple Skland accounts for one QQ ID. The AstrBot database currently stores a
single account under the QQ ID. Existing account data therefore needs an explicit
migration before it can be reused; copying the SQLite file is not sufficient.

The port was prepared against AstrBot 4.28.2 and the OneBot v11 adapter source.
The v0.1.4 archive was extracted into an isolated workspace and checked using the
local AstrBot instance's Python 3.12 environment and AstrBot 4.28.2 source. Checks
passed for AstrBot archive validation, namespaced entry import, plugin object
construction, empty database initialization, all packaged Python source files,
and an actual headless Edge launch with a fresh temporary browser context.
Browser fallback and Heybox fetch failure handling were also checked with
controlled responses. Whitespace checks passed.

No end-to-end validation of live Skland sign-in, real Heybox account records,
or the complete QQ group feature set has been completed.

## Attribution

Original project: **Billtranscend/Priestess-bot**, with original copyright and MIT
license retained. AstrBot Skland implementation: **SciNancy/astrbot_plugin_sklands**.
The publishing repository's maintainer provides the AstrBot interface adaptation
and compatibility maintenance. See `THIRD_PARTY_NOTICES.md` for all sources.
