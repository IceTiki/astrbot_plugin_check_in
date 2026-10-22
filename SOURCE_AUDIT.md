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

## v1.9.4 monthly-calendar rendering correction (2026-10-21)

- Task marker fill is now driven by each task's scored `score` ratio, not `amount / target`. A task completed late can therefore display a partial marker even when its count target was reached.
- Default eight-task views use a four-column, two-row marker layout with larger squares. Larger custom task lists adapt their marker size and row count.
- The monthly completion percentage and its progress bar are more prominent; per-day progress bars are thicker.
- Empty markers use a high-contrast outline. Inactive/future markers use a low-contrast border and a short dash.
- No scoring formula, database schema, task history, or stored records were changed.
- Focused regression tests for score-proportional fill, empty/faint contrast, and the monthly summary bar pass.
- The bundled legacy suite still has pre-existing mismatches with the current v1.9.3 code: the original archive had 49 failures / 97 passes; after this change it has the same 49 failing test identifiers and 100 passes (three new focused tests). These existing failures are not represented as passing, and the comparison does not replace live AstrBot/QQ verification.

## v1.9.5 gentle-decay update (2026-10-22)

- Scoring: newly effective date rules use `smooth_decay` and evaluate
  `(1 + (t/H)*ln(2)) * 2**(-t/H)` for lateness `t>0`, with full credit
  at/before best time. In contrast, historical `half_life` and `weighted`
  snapshots keep the original formulas.
- Migration: `v1.9.5-smooth-decay` marker converts only the current business
  day's active and disabled `half_life` rule revisions. Earlier dates do not
  change; event timestamps/counts are never altered. New accounts start
  directly on `smooth_decay`. Existing databases receive a one-time SQLite
  backup before this rule migration in normal AstrBot plugin startup.
- Presentation: CLI help, task listing, native text panel, and shared
  image-scoring inputs use the new curve and label `H` as a decay scale,
  not an actual 50%-after-H half-life. Retired independent commands remain
  retired; the unified `/打卡 任务 时间 ...` entry is unchanged.
- Packaging: v1.9.5 metadata and source-only ZIP updater version checks.
  The migration script `migrate_defaults_v193.py` now writes `smooth_decay`
  when it intentionally resets default task rules for existing accounts.
- Documentation: completely refreshed README with scaled, standalone
  script-free SVG illustrations for month and daily sample status, current
  math and scored progression tables, command reference, permissions,
  migration and safe upgrade paths.
- Verification: see `TEST_REPORT.txt` for focused tests and the disclosed
  pre-existing failures of the old bundled regression suites. No live
  AstrBot / QQ / Docker deployment has been performed.
