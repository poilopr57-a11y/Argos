# ls20 — Scorecard ID на three.arcprize.org

API key: `3948a5ff-26ba-44d4-a...` (из `.env`).
Все карточки получили **score 3.5714% (Level 1: 13 actions / baseline 22)**.

## Smart-BFS забеги (через `arc_play.play_game_smart`)

| Card ID                                    | Дата       | Tags             |
|--------------------------------------------|------------|------------------|
| 8bd39105-9f02-4759-bd16-4d4d0ca348cb       | 2026-04-26 | (без тегов)      |
| bb44d532-6030-49c1-a431-c519e97830f6       | 2026-04-26 | (без тегов)      |
| c823aa11-f2b3-49ae-b308-44a648c8183e       | 2026-04-26 | (без тегов)      |
| 1d36c433-e9e0-4083-a068-83ca02dc3fa1       | 2026-04-26 | (без тегов)      |
| d039e6d0-14d6-4016-a370-cbb73ab2cf1c       | 2026-04-26 | tags=None (баг)  |
| 4c414b92-e113-465e-8ef3-75c0993288dd       | 2026-04-26 | tags=None (баг)  |
| 99bf7d5b-1529-4bac-95e4-208fb0f02653       | 2026-04-26 | tags=None        |
| 29d0a40d-114d-475b-80fc-1e4e05d02ae6       | 2026-04-26 | tags=None        |
| ff729239-d4ad-40d9-bbaf-de6c7a3cd7f6       | 2026-04-26 | tags=None        |
| 94ba0396-518a-4872-9f06-ea611e0b8f38       | 2026-04-26 | tags=None        |
| b37b9913-581b-496b-be65-ef0400de9265       | 2026-04-26 | tags=None        |

URL для просмотра: `https://three.arcprize.org/scorecards/<card_id>`

## ARGOS-забеги со старым кодом (до hotfix)

| Card ID                                    | Score | Levels | Actions | Tags                          |
|--------------------------------------------|-------|--------|---------|-------------------------------|
| 1a677a78-8b0c-4e21-baeb-73fc3ac4e4cc       | 0.00  | 0/7    | -       | -                             |
| 6d7e88f9-47f5-44e7-95e0-10eebe2b99c8       | 0.00  | 0/29   | 200     | wrapper, agent                |
| 273ed639-0848-456a-aa02-d42c33042ab9       | 0.00  | 0/7    | 100     | argos, trained-solver, agent  |
| b54ff57f-6450-4c0d-84f7-b4d0e8ddaf8e       | 0.00  | 0/7    | 0       | argos, terminal-analyzer, agent |
| 1a31bfea-0e76-41c7-b75e-7bc0222abf96       | 0.00  | 0/7    | 25      | argos, grid-analyzer, agent   |
| 317db144-be75-4cea-a165-c8df850db72f       | 0.00  | 0/7    | 20      | argos, llm-agent, agent       |
| 96150f74-2a6c-4a51-830d-f428e1f98626       | 0.00  | 0/7    | 0       | argos, smart-agent, agent     |
| b546566a-5409-4c88-ab25-370e177c0adc       | 0.00  | 0/183  | 0       | argos, competition, agent     |
| 10ae2cb2-a19f-4bd5-a122-3cf1e70297cb       | 0.00  | 0/7    | 20      | argos, auto-play, test, agent |

## Известная проблема с тегами

`Arcade.open_scorecard(tags=[...])` создаёт **отдельный** scorecard с тегами,
но `arc.make()` делает **другой** scorecard для игры. `get_scorecard()` возвращает
тот, что от make() — без тегов.

TODO: исследовать как привязать теги к игровой карточке.
