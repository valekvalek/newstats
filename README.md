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

Текущий результат на 310 `FINISHED` матчах (311 загруженных минус 1
решённый по пенальти — см. «Известные ограничения»), 2026-08 — 2026-10:
**67/104 (64.4%, 95% ДИ [54.8%, 74.0%])** точных исходов 1X2 против
наивного базлайна «всегда дома» (50.0%), Brier 0.536 [0.444, 0.630] и
log loss 0.942 [0.792, 1.102] — оба лучше равновероятного базлайна
(Brier 0.667, log loss 1.099). Over/Under 2.5: 62.5% точность. 206 из
310 матчей пропущены — это каждая команда до её первого матча с
историей в базе (у нас не полный сезон, а «последние 10 матчей» на
команду, так что для разовых соперников вне ЛЧ истории часто просто
нет). Сравнение с рынком (Δ log loss против девигованных коэффициентов)
пока не считается ни разу — исторических коэффициентов в базе нет
(см. ниже про снапшоты).

**Широкие доверительные интервалы — это буквальный ответ на вопрос «значимо
ли это».** На n=104 ДИ по точности — почти 20 п.п., так что сами числа —
ориентир, не подтверждённая модель. По мере накопления данных (`scripts/
collect_match_details.py` на новые туры) точность модели можно
перепроверять этим же скриптом без правок кода — ДИ будут сужаться.

## Время, снапшоты коэффициентов и CLV

`kickoff_at` хранится в UTC: `parse_kickoff` переводит исходную дату
из `SOURCE_TZ` (см. `.env.example` — по умолчанию `Europe/Moscow`, часовой
пояс нашей скрейпящей машины) в UTC. Это осознанный выбор, а не
догадка — см. «Известные ограничения» про то, как это проверялось.
Скрипты в `scripts/` сами пин­ят Playwright-браузер на `timezone_id="UTC"`,
так что для них `SOURCE_TZ=UTC` (по умолчанию) корректен; `SOURCE_TZ`
нужен только если дату берёте напрямую из `main.py event` внешнего
скрапера с другой машины/без явного `timezone_id`.

Без фиксации **момента** снятия коэффициентов «модель против рынка» —
просто шум: неизвестно, была эта цена доступна за сутки до матча или
за минуту. `scripts/snapshot_odds.py` берёт коэффициенты в трёх окнах
перед стартом (`t-24h`, `t-1h`, `close`) для каждого матча со статусом
`SCHEDULED` в ближайшие ~25 часов, идемпотентно (повторный запуск не
дублирует уже взятые окна) — рассчитан на cron каждые 5 минут на
постоянно включённой машине (GitHub Actions не подходит: нужен живой
Playwright и доступ к БД, а не разовый CI-джоб):

```bash
*/5 * * * * cd /path/to/newstats && \
    FLASHSCORE_SCRAPER=/path/to/FlashscoreScraper DATABASE_URL=... \
    .venv/bin/python scripts/snapshot_odds.py >> snapshot_odds.log 2>&1
```

CLV (closing line value) для ставки по цене `o` на исход, чья
де-виг­ованная вероятность на закрытии — `p_close`: `clv = o·p_close − 1`
(`newstats.stats.value_bets.clv`). Положительное значение — цена была
лучше закрытия, независимо от того, зашла ставка или нет.

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

## Известные ограничения

**Часовой пояс.** `event["date"]` от скрапера — это не UTC и не "время
матча" в абстрактном смысле: Flashscore рендерит его в часовом поясе
*браузера, который его скрейпил* (клиентский JS-рендеринг). Наша
скрейпящая машина — `Europe/Moscow` (MSK, UTC+3), без явного
`timezone_id` ни в одном из Playwright-вызовов. Проверено: матч
Лиги чемпионов с официальным киком 21:00 CEST (= 19:00 UTC) у нас в
`date` даёт `22:00` — ровно 19:00 UTC + 3 часа. Так что весь `kickoff_at`,
который уже лежит в базе, фактически хранится в MSK, не в UTC. Это
нужно явно учитывать (SOURCE_TZ=Europe/Moscow), пока не внедрён
`timezone_id="UTC"` на самом скрейпере — см. раздел «Прогноз».

**Счёт при доп. времени и пенальти.** `detail_status` для решённых в
доп. время матчей — `"After Extra Time"` (не `"After ET"`, как
предполагалось изначально), и `score` в этом случае — это уже итоговый
счёт по итогам 120 минут, с ним можно работать как с обычным `FINISHED`.
Но для матчей, решённых по пенальти, `detail_status` — `"After 90
minutes"` (для добавочного времени — предположительно так же, другой
текст не встречался), а `score` — это счёт **до серии пенальти**: кто
реально прошёл дальше/выиграл, в данных скрапера нигде не отражено
(кто бил последний пенальти туда не попадает). Пока это не исправлено
на уровне модели, такие матчи **исключаются** из бэктеста
(`scripts/backtest.py`), а не считаются обычной ничьёй — см.
`_STATUS_MAP` в `newstats/importer/flashscore.py`.
