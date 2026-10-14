# newstats

Собственная БД статистики и аналитические скрипты для ставок, построенные
над выводом внешнего скрапера
[M3MONs/FlashscoreScraper](https://github.com/M3MONs/FlashscoreScraper).
Скрапер — отдельный проект, его код сюда не копируется и не форкается; этот
репозиторий только читает его JSON-вывод с stdout.

## Как реально работает скрапер (важно для интеграции)

`FlashscoreScraper` — CLI на один URL матча за раз, без собственных файлов
вывода — печатает один JSON-объект в stdout на вызов:

```bash
python main.py event      <match_url>                                   # базовая инфа + счёт + статус
python main.py odds       <match_url>                                   # коэффициенты (вызов на каждый тип рынка)
python main.py event-info <match_url>/standings/standings/overall/      # таблица лиги
python main.py event-info <match_url>/draw/                            # сетка плей-офф (пока не импортируется)
```

В нём **нет** данных по игрокам и **нет** детальной командной статистики
матча (владение, угловые и т.п.) — таких полей скрапер не выдаёт. Счёт и
статус — свободный текст (`"2 - 1"`, `"Finished"`), не готовые числа/enum.
ID матча не передаётся явным полем — это `?mid=...` в самом URL, который вы
скрейпили.

Наш импортёр (`newstats/importer/flashscore.py`) — это адаптер именно под
эту реальную форму вывода, не под придуманный контракт. Если формат
скрапера поменяется, править нужно только этот файл.

## Данные, которых у FlashscoreScraper нет (составы, угловые, карточки)

У самого FlashscoreScraper нет и не будет: игроков, голов по игрокам,
угловых/штрафных/карточек — на сайте это не часть `event`/`odds`/`event-info`
выдачи, а отдельные вкладки страницы матча (`Summary` — таймлайн событий,
`Stats` — командная статистика), которые рендерятся JS и требуют прямого
скрейпинга HTML, а не CLI-инструмента.

Для этого в `newstats/importer/flashscore.py` есть ещё два импортёра,
рассчитанные на такой прямой скрейп (не на вывод `main.py`):

- `ingest_match_stats(session, match_url, stats)` — командная статистика
  матча (угловые, штрафные, владение, удары и т.д.) со страницы
  `<match_url>/summary/stats/overall/`. Ожидает
  `[{"label": "Corner kicks", "home": "2", "away": "5"}, ...]`.
- `ingest_match_events(session, match_url, incidents)` — события матча
  (голы, передачи, жёлтые/красные карточки) с таймлайна на самой странице
  матча. Ожидает
  `[{"type": "goal"|"assist"|"yellow_card"|"red_card", "side": "home"|"away",
  "player_name": "...", "player_link": "/player/.../..."}, ...]`. Голы одного
  игрока агрегируются в одну строку (`stat_name="goals"`, `value=2` при дубле),
  не создают дубликаты.

CLI: `newstats ingest-stats-cmd <url> --file ...` и
`newstats ingest-events-cmd <url> --file ...` — тот же `--file`/stdin
паттерн, что и у остальных команд.

Пример скрейпера под эту форму (Playwright + BeautifulSoup, не трогает код
FlashscoreScraper) — `scripts/collect_match_details.py`.

## Скрипты массового сбора (`scripts/`)

```bash
uv pip install -e ".[scraping]"
python -m playwright install chromium
```

- `scripts/collect_cl_teams.py` — для списка команд (сейчас зашит на 36
  команд лиги чемпионов 2026/27, но список в начале файла можно заменить
  на любой) находит последние N матчей каждой команды **во всех турнирах**
  через их страницы `/team/<slug>/<id>/results/`, прогоняет каждый
  уникальный матч через настоящий парсер `FootballEventParser` из
  клона `M3MONs/FlashscoreScraper` и грузит в БД через `ingest_event`.
  Требует `FLASHSCORE_SCRAPER=/path/to/FlashscoreScraper` (с его
  собственным venv, playwright-браузерами и зависимостями).
- `scripts/collect_match_details.py` — второй проход по уже загруженным
  матчам: досчитывает `kickoff_at`, тянет командную статистику
  (`ingest_match_stats`) и события матча/голы/карточки
  (`ingest_match_events`) напрямую с HTML страниц матча.

Оба — asyncio + Playwright, идут по 5 страниц одновременно, идемпотентны
(повторный запуск просто обновляет те же строки, не плодит дубли).

## Прогноз (xG-модель) и бэктест

`newstats/stats/xg_model.py` — простая пуассоновская модель по среднему
xG команды за/против (не Диксон-Коулз, не подгонка, осознанно минимальная
отправная точка). `newstats/stats/value_bets.py` — сравнение модели с
девигованными коэффициентами букмекеров.

```bash
newstats predict <home_team_id> <away_team_id>
newstats value-bets <match_id>   # нужны уже загруженные через ingest-odds-cmd коэффициенты
```

Проверка модели на фактических результатах — `scripts/backtest.py`.
Для каждого матча прогноз строится **только на данных до его начала**
(`predict_match(..., before=match.kickoff_at)`), иначе бэктест был бы
нечестным (матч предсказывался бы с учётом собственного же результата).

```bash
DATABASE_URL=... python scripts/backtest.py                              # все матчи в базе
DATABASE_URL=... python scripts/backtest.py --since 2026-10-13 --until 2026-10-14
DATABASE_URL=... python scripts/backtest.py --quiet                      # только сводка
```

Текущий результат на всех 311 загруженных матчах (2026-08 — 2026-10):
**67/104 (64.4%)** точных исходов 1X2 против наивного базлайна «всегда
дома» (50.0%), Brier 0.536 и log loss 0.942 — оба лучше равновероятного
базлайна (Brier 0.667, log loss 1.099). 207 из 311 матчей пропущены — это
каждая команда до её первого матча с историей в базе (у нас не полный
сезон, а «последние 10 матчей» на команду, так что для разовых соперников
вне ЛЧ истории часто просто нет).

**n=104 всё ещё мало для статистической значимости** — это ориентир, а
не подтверждённая модель. По мере накопления данных (`scripts/
collect_match_details.py` на новые туры) точность модели можно
перепроверять этим же скриптом без правок кода.

## Схема

```
Sport
  └── Tournament
        ├── Match ── home_team / away_team ──> Team
        │     └── Odds   (история коэффициентов по рынкам/букмекерам)
        └── Standing     (снэпшот таблицы лиги на момент скрейпа)
```

`Player`/`PlayerTeam`/`MatchStat`/`PlayerMatchStat` в схеме оставлены как
задел на случай другого источника данных с составами и детальной
статистикой — текущий флэшскор-импортёр их не заполняет, потому что в
скрапере такие данные просто отсутствуют.

## Быстрый старт

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env   # поправьте DATABASE_URL
docker compose -f docker/docker-compose.yml up -d   # локальный Postgres
alembic upgrade head
```

Пример полного пайплайна «скрапер → наша БД» для одного матча:

```bash
MATCH_URL="https://www.flashscore.com/match/football/team-a-.../team-b-.../?mid=XXXXXXX"

# из каталога FlashscoreScraper
python main.py event "$MATCH_URL" \
  | ( cd /path/to/newstats && newstats ingest-event-cmd "$MATCH_URL" --file /dev/stdin )

python main.py odds "$MATCH_URL" \
  | ( cd /path/to/newstats && newstats ingest-odds-cmd "$MATCH_URL" --file /dev/stdin )

python main.py event-info "$MATCH_URL/standings/standings/overall/" \
  | ( cd /path/to/newstats && newstats ingest-standings-cmd "$MATCH_URL" --file /dev/stdin )

newstats form <team_id>
newstats h2h <team_a_id> <team_b_id>
newstats predict <home_team_id> <away_team_id>
newstats value-bets <match_id>
```

`ingest-event-cmd` обязателен первым для матча — остальные `ingest-*-cmd`
ищут матч по его `mid` и падают с понятной ошибкой, если матч ещё не
импортирован. **`ingest-odds-cmd` принимает `$MATCH_URL` отдельным
аргументом, а не берёт его из `odds_result["url"]`** — на реальных данных
`FlashscoreUrlBuilder.build_odds_url` обрезает `?mid=` при сборке URL под
конкретный тип коэффициентов, так что этого поля недостаточно, чтобы найти
матч (подтверждено живым запросом, не только по фикстурам).

## Тесты

```bash
pytest
ruff check .
```

Фикстуры в `tests/fixtures/flashscore/` — JSON, построенный по реальным
dataclass-ам скрапера (`EventData`, `OddsResult`/`OddsParserRow`,
`EventInfoData`/`FootballStandingsEntry`), а не придуманный вручную формат.
