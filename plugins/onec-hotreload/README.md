# onec-hotreload

Claude Code plugin for 1C:Enterprise (BSL) live hot reload without restarting processes or dropping active sessions.

Плагин Claude Code для горячей замены модулей BSL в 1С:Предприятии (как на сервере `rphost`, так и в тонких клиентах `1cv8`) без перезапуска процессов, без разрыва пользовательских сеансов и без потери открытых экранных форм.

Состав:

| Компонент | Файл | Назначение |
|---|---|---|
| Навык | `skills/1c-hotreload/SKILL.md` | Инструкция для Claude Code по безопасной работе с командами `./hr`, safe-point дисциплине, замене запросов и авто-режиму `watch` |

## Требования

- **Операционная система**: Linux x86_64 (требуется доступ к `/proc/PID/mem` и остановке потоков).
- **Платформа 1С**: 1C:Enterprise 8.3.27.x.
- **Привилегии**: `sudo -n` для доступа к памяти рабочих процессов `rphost` (для тонких клиентов `1cv8` текущего пользователя root не требуется).
- Утилиты сборки и отладки: `python3` (3.10+), `gdb` (для аллокации буферов клиента).

## Установка

Из shell:

```bash
claude plugin marketplace add defin85/onec-claude-plugins
claude plugin install onec-hotreload@onec-claude-plugins            # для пользователя
claude plugin install onec-hotreload@onec-claude-plugins -s project # только для проекта
```

Или в сессии Claude Code:

```text
/plugin marketplace add defin85/onec-claude-plugins
/plugin install onec-hotreload@onec-claude-plugins
/reload-plugins
```

## Основные возможности

### 1. Серверный Hot-Swap (`rphost`)
- **`server-poke-str OLD NEW [--pad]`**: хирургический in-place patch строк и текстов запросов под `safe_point` (~200 мс). Запрос выполняется в **родной сессии** пользователя с сохранением RLS, прав и активных транзакций. Флаг `--pad` автоматически дополняет укороченный запрос пробелами.
- **`server-swap --module Name`**: hot-swap логики методов модуля через низкоуровневый диспетчер `hrhook2` с проверкой выравнивания методов и причинным переключением `enable`/`disable`/`detach`.

### 2. Клиентский Hot-Swap (`1cv8`)
- **`client-swap --module Name [--target PID]`**: замена скомпилированного байткода и пула литералов прямо в памяти запущенного тонкого клиента без закрытия открытых окон и диалогов.

### 3. Автоматический режим (`watch`)
- **`watch --dir <dump_dir> --module Name`**: непрерывное отслеживание изменений BSL-исходников. Автоматически определяет область действия модуля (`Server` / `ClientManagedApplication` / смешанный) по XML-дескриптору, обновляет базу и синхронно применяет hot-swap к серверу и клиентам.

## Безопасность (Safe-Point)

Любая модификация памяти в `onec-hotreload` выполняется строго под safe-point:
1. `SIGSTOP` целевого процесса;
2. Проверка instruction pointer всех потоков процесса (`/proc/PID/task/*/stat`): ни один поток не должен находиться внутри исполняемого кода `bsl.so`;
3. Атомарная запись и контрольное чтение;
4. Гарантированный `SIGCONT` в блоке `finally`.

## Лицензия

[MIT](LICENSE)
