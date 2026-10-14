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
  | ( cd /path/to/newstats && newstats ingest-odds-cmd --file /dev/stdin )

python main.py event-info "$MATCH_URL/standings/standings/overall/" \
  | ( cd /path/to/newstats && newstats ingest-standings-cmd "$MATCH_URL" --file /dev/stdin )

newstats form <team_id>
newstats h2h <team_a_id> <team_b_id>
```

`ingest-event-cmd` обязателен первым для матча — `ingest-odds-cmd` и
`ingest-standings-cmd` ищут матч по его `mid` и падают с понятной ошибкой,
если матч ещё не импортирован.

## Тесты

```bash
pytest
ruff check .
```

Фикстуры в `tests/fixtures/flashscore/` — JSON, построенный по реальным
dataclass-ам скрапера (`EventData`, `OddsResult`/`OddsParserRow`,
`EventInfoData`/`FootballStandingsEntry`), а не придуманный вручную формат.
