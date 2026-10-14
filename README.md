# newstats

Собственная БД статистики и аналитические скрипты, независимые от инструмента
скрапинга. Скрапер — отдельный проект, пишущий JSON-файлы по матчам; этот
репозиторий их импортирует в свою схему и строит на них статистику,
используемую как основа для анализа ставок.

## Схема

```
Sport
  └── Tournament
        └── Match ── home_team / away_team ──> Team
              ├── MatchStat        (командная статистика: possession, corners, ...)
              ├── PlayerMatchStat  (статистика игрока: goals, assists, ...)
              └── Odds             (история коэффициентов по рынкам/букмекерам)

Team ──< PlayerTeam >── Player   (состав команды во времени)
```

Статистика хранится как узкие строки key/value (`MatchStat`,
`PlayerMatchStat`), а не фиксированными колонками — у разных видов спорта
разный набор метрик, и список будет расти; так не нужна миграция под каждую
новую метрику.

## Контракт входных данных

Импортёр ожидает один JSON-файл на матч в формате, описанном в
[`newstats/importer/schema.py`](newstats/importer/schema.py). Подправьте
маппинг в [`newstats/importer/loader.py`](newstats/importer/loader.py) под
реальный формат вывода вашего скрапера — сейчас это задокументированный
контракт с примерами фикстур в `tests/fixtures/scraper_sample/`.

## Быстрый старт

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env   # поправьте DATABASE_URL / SCRAPER_OUTPUT_DIR

docker compose -f docker/docker-compose.yml up -d   # локальный Postgres
alembic upgrade head

newstats import-data --directory /path/to/scraper/output
newstats form <team_id>
newstats h2h <team_a_id> <team_b_id>
```

## Тесты

```bash
pytest
ruff check .
```
