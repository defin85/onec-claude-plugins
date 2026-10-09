# onec-claude-plugins

Claude Code plugin marketplace for 1C:Enterprise (BSL) development: BSL language server via `bsl-analyzer-app`, a guard hook against broad grep over configuration dumps, and a navigation skill.

Marketplace плагинов Claude Code для разработки на 1С:Предприятие (BSL).

| Плагин | Назначение |
|---|---|
| [onec-bsl](plugins/onec-bsl/README.md) | LSP-сервер BSL (`bsl-analyzer-app`), запрет широкого grep по выгрузкам 1С, навык `bsl-navigation` |

## Требования

- `bsl-analyzer-app` в `PATH`. Устанавливается отдельно и в этот репозиторий не входит. Без него LSP-сервер не запустится; хук и навык работают и без него.
- `python3` (3.10+) для хука; только стандартная библиотека.
- Claude Code с поддержкой плагинов и LSP-серверов в плагинах.

## Установка

Из shell:

```bash
claude plugin marketplace add defin85/onec-claude-plugins
claude plugin install onec-bsl@onec-claude-plugins            # для пользователя
claude plugin install onec-bsl@onec-claude-plugins -s project # только для текущего проекта (.claude/settings.json)
```

Или внутри сессии Claude Code:

```text
/plugin marketplace add defin85/onec-claude-plugins
/plugin install onec-bsl@onec-claude-plugins
/reload-plugins
```

Удаление: `claude plugin uninstall onec-bsl@onec-claude-plugins`, затем при необходимости `claude plugin marketplace remove onec-claude-plugins`.

Проверка из локального клона без установки: `claude --plugin-dir ./plugins/onec-bsl`.

## Обход хука

Если анализатор не видит нужные файлы (worktree, свежая выгрузка вне индекса), широкий поиск выполняется с явной причиной:

```bash
BSL_GREP_REASON='worktree вне индекса анализатора' rg -n 'ИмяМетода' .worktrees/x/src/cf
```

Пустая причина не принимается. Дополнительные корни выгрузок задаются в `.claude/onec-bsl.json` проекта, см. [README плагина](plugins/onec-bsl/README.md#настройка-проекта).

## Проверки

```bash
(cd plugins/onec-bsl && python3 -m unittest discover -s tests -v)
claude plugin validate --strict .
claude plugin validate --strict plugins/onec-bsl
```

## Лицензия

[MIT](LICENSE)
