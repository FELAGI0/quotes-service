# quotes-service

Витрина каталога цитат: держит содержимое каталога и отдаёт цитаты читателям
по одной. Каталог присылает полный снимок (`POST /import`), между снимками
витрина может дочитать одну цитату по id. Всё состояние — в памяти одного
процесса; внешних БД, кэшей и брокеров нет.

## Стек

- Python 3.13, FastAPI, uvicorn (1 воркер), orjson, httpx, uvloop.
- Сборка через `pip`, non-root пользователь, multi-stage образ.

## Архитектура (кратко)

- **Приём снимка — streaming.** Тело до 110 МБ читается чанками, режется по
  строкам, каждая запись парсится `orjson` построчно; тело целиком в память не
  поднимается. Битая запись пропускается, битый конверт (`{"quotes":[` / `]}`)
  → 400.
- **Хранилище — packed-буфер.** Тела записей лежат в одном `bytearray`
  (length-prefix на запись), индекс `id → offset`; `set` всех id отдельно —
  поэтому витрина знает id даже после вытеснения тела. LRU-бюджет по байтам
  работает как предохранитель, а не как штатный путь.
- **Catalog client — circuit breaker.** 5 подряд транспортных сбоев
  (timeout/refused) → open с backoff и jitter; half-open → одна проба. 503 **не**
  открывает breaker, уважается `Retry-After` (один повтор). Семафор на 10,
  single-flight по id.
- **Читатель.** `LOCAL` если тело есть у витрины, `CATALOG` если дочитано из
  каталога. 404 из каталога **не** запоминается. DELETE ставит tombstone до
  следующего снимка.

## Ручки контракта

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/health` | `200 {"status":"healthy"}` — сразу после старта и всегда |
| POST | `/catalog/source` | `{"url": ...}` → `200 {"source": url}` (последний побеждает) |
| POST | `/import` | `200 {"imported": N, "dropped": M}`; `400` на битый конверт |
| PUT | `/catalog/{id}` | `{"author","text"}` → `200 {id,author,text}`; `422` вне границ |
| DELETE | `/catalog/{id}` | `200 {"deleted":true}` / `404 {"detail":"not found"}` |
| GET | `/quotes/{id}` | `200` запись целиком + `X-Source: LOCAL|CATALOG`; `404` |
| GET | `/stats` | `served_local, served_from_catalog, catalog_reads, evictions, local, bytes, catalog` |

Границы PUT: `author` 1–200 знаков, `text` 1–16384 знаков (Unicode).

## Сборка и запуск

```
docker build -t quotes-service .
docker run -p 8000:8000 quotes-service
```

С лимитами прогона:

```
docker run --memory 256m --cpus 0.5 -p 8000:8000 quotes-service
```

Проверка:

```
curl http://localhost:8000/health
# {"status":"healthy"}
```

## Лимиты прогона

256 МБ RAM, 0.5 CPU, ≤ 64 потоков, `/tmp` 64 МБ (запуск файлов запрещён), 1
воркер uvicorn. Пик памяти на снимке 340 000 записей / 114 МБ — ~220 МБ.

## Скрипты

- `scripts/gen_snapshot.py` — генератор тестового снимка.
- `scripts/fake_catalog.py` — заглушка каталога (режимы 200/404/503/…).
- `scripts/contract_test.py`, `scripts/test_client.py` — проверки контракта.