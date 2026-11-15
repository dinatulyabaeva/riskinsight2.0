# Проверка расширения XLSX

Фактический прогон 13.11.2026: Linux, Python 3.12.14, Chromium /usr/bin/chromium, нативная браузерная сеть. Текущая проверка дополняет исторический TEST_REPORT.md исходного проекта.

## Выполненные проверки

- Полный pytest с RUN_BROWSER_TESTS=1: validation, import, analytics, API, agent, настоящий MCP subprocess и браузер. Точный результат и длительность — [вывод pytest](examples/xlsx-pytest-output.txt) и [JUnit](examples/xlsx-test-results.xml).
- compileall для app/scripts — успешно.
- node --check static/app.js — успешно.
- ruff для app и новых/изменённых тестов XLSX — успешно.
- scripts.smoke против настоящего Uvicorn на localhost:8000 — health, ready, context, dashboard, chat успешны; DEMO выполнил три MCP-вызова. Все шесть DEMO-сценариев также проходят в pytest.
- scripts.ui_smoke с нативным Chromium — без JS-ошибок; фильтры, раскрытие источников, три экспорта, мобильная ширина 390px и презентация проверены. [Отчёт браузера](examples/xlsx-browser-report.json).

## Сквозной сценарий XLSX

Браузер отклоняет файл >10 МБ. Файл с дубликатами показывает ошибки строк. Корректный файл сохраняет 2 строки, становится доступен в переключателе. После выбора: остаток 4000 ₽, числитель DPD90+ 3000 ₽, доля 75%, as_of=2026-10-31, собственный dataset_id. При отсутствующих PD/LGD EL недоступен, причины показаны.

Свободный вопрос идёт через production agent loop → Chat Completions test double → настоящий stdio MCP → SQLite. Первый вызов получает context, второй summary; финальный текст содержит правильные числа, формулу, дату, ID и [T2]. Источник раскрывается в UI. После возврата к builtin восстанавливаются исходные показатели, прежний ответ удаляется.

HTTP E2E отдельно проверяет всё то же, effective dataset_id в каждом trace и отказ модели при попытке подмены. Контрольные XLSX созданы из вымышленных данных. Проверены нулевые остатки, дробные копейки, граничные DPD, даты, пропуски, частичные PD/LGD, изменение продукта между датами, внешние связи/объекты, макросы, формулы, XML entities, ограничения распаковки, атомарный отказ, журнал и сохранность встроенной БД. Проверяется путь с пробелами и кириллицей.

## Границы доказательства

Сеть LLM в автоматических тестах заменена httpx.MockTransport. Это не доказательство качества произвольного ответа живой модели. Production-провайдер, его OpenAI-compatible API и настройки Ollama сохранены. Здесь локальный Ollama http://127.0.0.1:11434 недоступен (ConnectError), секреты/удалённая модель не настроены. Живой Ollama-прогон не заявляется; повторить: python -m scripts.check_llm --dataset-id upload_<ID>.

CI расширен до Windows/Linux × Python 3.11/3.12/3.13, браузерный E2E — обе ОС на Python 3.12. Результаты удалённого Windows-прогона следует смотреть в checks draft PR; они не выводятся из успеха Linux-тестов. Docker/Render и публичный хостинг в этом изменении не запускались.

Во время локального pytest есть предупреждение установленной Starlette о будущем переходе TestClient с httpx на httpx2; тесты проходят. Это предупреждение зависимости, не скрытая ошибка импорта.

## Версии текущего прогона

Python 3.12.14; fastapi 0.162.7; uvicorn 0.54.0; httpx 0.28.1; pydantic 2.14.3; openpyxl 3.1.5; defusedxml 0.7.1; pytest 9.2.0; pytest-asyncio 1.5.0; playwright 1.65.1.

## Повторение

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-ui.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$env:RUN_BROWSER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app scripts
```

Без RUN_BROWSER_TESTS основной pytest не требует браузера и пропускает ровно один browser test. Чтобы проверить существующий HTTP smoke, запустите обычный app.main:app и выполните python -m scripts.smoke. scripts.ui_smoke выполняется отдельно против запущенного приложения.
