# Запуск Partner Monitor в Railway

Сервис работает как постоянный фоновый процесс в Linux-контейнере. Windows на компьютере оператора этому не мешает. Публичный домен и HTTP healthcheck не требуются.

## Настройки сервиса

- Источник: этот GitHub-репозиторий, ветка с файлами Railway.
- Root Directory: `/project`; Dockerfile: `Dockerfile`.
- Постоянный Volume: **`/app/data`**. В нём находятся сессия Telegram, SQLite, Excel и скриншоты. Не удаляйте и не очищайте его при обновлении кода.
- Одна реплика, один регион. Serverless / Sleep отключён; Restart Policy — `ALWAYS`; Overlap — `0`, Draining — `30` секунд.
- Start Command оставьте пустым: используется CMD из Dockerfile. Pre-deploy command не нужен.
- Переменная `MONITOR_MODE=setup` для первого запуска. В этом режиме контейнер готов к настройке, но Telegram не читает.

Образ собирает Python 3.12, зависимости из существующего lock-файла и Chromium для настоящих скриншотов. Реальные конфигурация и доступы в образ не копируются.

## Вход в отдельный рабочий Telegram-аккаунт

В Variables выбранного сервиса пользователь самостоятельно сохраняет `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`, полученные на https://my.telegram.org в API development tools. Не передавайте значения, пароль и код входа через чат или GitHub.

При `MONITOR_MODE=setup` откройте интерактивную SSH-сессию через Railway CLI на своём компьютере (`railway ssh` после входа и выбора проекта/сервиса). Внутри контейнера выполните:

```sh
cd /app
partner-monitor --config /app/config.yaml login
partner-monitor --config /app/config.yaml dialogs
```

Телефон, код и пароль двухэтапной проверки вводятся непосредственно в терминале авторизации. Файл `/app/data/telegram.session` сохраняется на постоянном томе. Убедитесь по сообщению входа, что выбран именно рабочий аккаунт. До запуска мониторинга можно дополнительно проверить ID и тип аккаунта:

```sh
python - <<'PY'
import asyncio
from pathlib import Path
from partner_monitor.config import load_config
from partner_monitor.engine import client_for

async def show_account():
    client = client_for(load_config(Path('/app/config.yaml')))
    try:
        await client.connect()
        me = await client.get_me()
        if me is None:
            raise SystemExit('Сначала выполните login')
        print('ID:', me.id, 'username:', me.username, 'bot:', me.bot)
    finally:
        await client.disconnect()

asyncio.run(show_account())
PY
```

При желании сохраните ID как `TELEGRAM_ACCOUNT_ID`: старт будет отклонён, если сессия другого аккаунта. Не запускайте одновременно login и мониторинг или два процесса с одной сессией.

## Проверка группы и включение

Выберите доступную рабочую группу из вывода `dialogs`. Сохраните её числовой ID в Variables как `SOURCE_GROUP`, оставив `MONITOR_MODE=setup`, и примените переменные. Сервис создаёт `/app/config.yaml` при каждом запуске; ID группы берётся из Variables. Токены в этот файл не записываются.

В SSH-терминале выполните один проверочный цикл:

```sh
partner-monitor --config /app/config.yaml check-config
partner-monitor --config /app/config.yaml run --once
```

Это читает группу и указанные в заявках каналы без вступления и без отправки сообщений. Проверьте реальную публикацию, просмотры, файл `/app/data/report.xlsx` и наличие настоящего снимка в `/app/data/screenshots`. Первый проход по умолчанию читает последние 7 дней группы.

Затем измените `MONITOR_MODE` на `run` и примените переменные. В логах должны появиться подтверждение проверки аккаунта и завершение цикла с путём отчёта. Убедитесь после одного перезапуска/развёртывания, что сессия и база сохранились и нет повторных заявок. Для паузы верните `MONITOR_MODE=setup`.

Для получения Excel используйте скачивание файлов тома через Railway CLI; путь `/report.xlsx` внутри смонтированного тома соответствует `/app/data/report.xlsx` в контейнере. Google Sheets подключается отдельно по README проекта.

Зелёный статус Railway в режиме `setup` означает готовность контейнера к настройке. Он не подтверждает вход в Telegram или работающий мониторинг. Для изолированного теста используйте [TESTING.md](TESTING.md). В версии 0.5 реализована постоянная очередь первого, 24- и 48-часового замеров; прежнее отдельное пожелание о 20 часах не включено в это расписание.

Документация: [Dockerfiles](https://docs.railway.com/builds/dockerfiles), [Volumes](https://docs.railway.com/volumes), [SSH](https://docs.railway.com/cli/ssh).

