# v1.6.0 implementation and source audit

Date: 2026-10-17

## Scope of this change

The starting point is the supplied `astrbot_plugin_daka_v1.5.0.zip`, not a new QQ transport implementation. The user has reported working buttons and images with that version.

- `accounts.py`: existing AppID/OpenID identity resolution retained.
- `qq_transport.py`: authenticated QQ v2 request and media upload paths retained. Keyboard success no longer duplicates the entire command-link list. Keyboard failure still falls back to native command links, then plain text. Button labels now show multi-count progress or a completion mark. Type=2 group/C2C semantics are unchanged.
- `scoring.py`: new pure functions for a 04:00 business day, time coefficients, partial-count scoring, weights and aggregate ratios.
- `scored_store.py`: extension of the existing identity store, not a replacement identity system. Adds dated rule revisions and an `occurred_at` column while retaining `checked_at` as the operation timestamp.
- `panel.py`: compact plain/native-Markdown body, without duplicating the interactive action controls.
- `weighted_calendar.py`: actual seven-column PNG and text fallback using the same computed score metrics.
- `main.py`: new commands, new store and renderers, same explicit-send-before-stop lifecycle.
- `update_source.py`: source-only updater changed to validate version 1.6.0 and require all new modules. Existing backup, mount checks and failure handling retained.
- `store.py` still contains OLD defaults for legacy data compatibility. Live v1.6 defaults are in `scoring.DEFAULT_RULES` and are applied by `ScoredStore`, once per existing account.

## Scoring interpretation

These are product rules based on the user's request, not medical guidance or features provided by QQ:

1. The first interval after the best time is grace, including the right endpoint at recorded second precision.
2. Each subsequent started interval halves the contribution of that check-in.
3. Multi-count tasks distribute one total task weight across the target number of check-ins.
4. A zero-weight task has no effect on aggregate denominators.
5. Setting changes apply to the current business day and future dates; previously ended dates keep their rule versions.
6. Monthly completion is SUM(earned points) / SUM(possible points), not an unweighted average of percentages.
7. Old date assignments are not silently reclassified when the new 04:00 boundary is introduced.
8. A task name describing an hour of work is a self-reported counter, not evidence of elapsed work duration.

## Official references rechecked

AstrBot plugin layout, persistent-data location and loading conventions:
https://docs.astrbot.app/dev/star/plugin-new.html

QQ custom Markdown structure:
https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html

AstrBot official QQ event implementation, authenticated media handling and sends:
https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py

The following are the v1.5 transport references, retained for context rather than newly implemented mechanisms:

QQ command text links:
https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html

QQ group message payload and keyboard action structure:
https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html

No new callback event handler, AppID discovery method, privileged QQ capability or third-party bot account is introduced by this release.

## Verification limits

See TEST_REPORT.txt. Tests exercise real SQLite databases and image rendering; AstrBot events, QQ HTTP responses and Docker actions are fixtures. The tests do not connect to the user's host, do not authenticate to QQ, and do not prove how a particular QQ client renders the new layout.

No font files, tokens, personal records or user screenshots are included in the plugin archive.
