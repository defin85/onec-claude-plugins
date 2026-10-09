# onec-bsl

Claude Code plugin for 1C:Enterprise (BSL) source navigation through `bsl-analyzer`.

Плагин Claude Code для работы с исходниками 1С через анализатор, а не через текстовый поиск. Выгрузки конфигураций содержат десятки тысяч файлов: `rg` по `src/cf/<конфигурация>` не укладывается в 120 с и расходует контекст.

Состав:

| Компонент | Файл | Назначение |
|---|---|---|
| LSP-сервер BSL | `.lsp.json` | `bsl-analyzer-app --stdio` для `.bsl`: инструмент `LSP` получает definition, references, hover, documentSymbol, workspaceSymbol, call hierarchy |
| Хук `PreToolUse` | `hooks/hooks.json`, `hooks/bsl_grep_guard.py` | запрет широкого `rg`/`grep`/`ugrep`/`git grep`/`find -exec grep` и инструмента `Grep` по выгрузкам |
| Навык | `skills/bsl-navigation/SKILL.md` | выбор инструмента по виду искомого факта |
| Проверка LSP | `scripts/lsp_smoke.py` | `initialize` и запросы навигации по stdio с таймаутом |

## Установка

```bash
claude plugin marketplace add defin85/onec-claude-plugins
claude plugin install onec-bsl@onec-claude-plugins
```

В сессии Claude Code: `/plugin marketplace add defin85/onec-claude-plugins`, `/plugin install onec-bsl@onec-claude-plugins`, затем `/reload-plugins`.

## Требования

- `bsl-analyzer-app` в `PATH` (тот же бинарник, что использует расширение VS Code `bsl-analyzer-lsp`). Устанавливается отдельно и в плагин не входит. Без него LSP-сервер не запустится; хук и навык работают и без него. Проверено с `bsl-analyzer 0.2.86`.
- `python3` (3.10+) для хука; только стандартная библиотека.
- Claude Code 2.1.288+ (поля `lspServers`).

## LSP

Сервер запускается при первом обращении к `.bsl`-файлу. Корень рабочего пространства — каталог проекта. Аргументов для корня конфигурации у `bsl-analyzer-app lsp` нет: сервер читает `bsl-analyzer.toml`, `.bsl-analyzer.json` или `.bsl-language-server.json` в корне рабочего пространства, например:

```toml
[source]
root = "src/cf/MyConfig"
extensions = ["src/cfe/MyExtension"]
```

Это отдельный процесс: MCP-сервер анализатора, если он подключён, и языковой сервер VS Code он не затрагивает. На большой конфигурации процесс занимает заметную память, как и экземпляр в VS Code.

`.os` (OneScript) не подключён: сервер отвечает `File is not BSL, request unsupported`.

## Хук

Блокирует вызов, если путь поиска (явный или текущий каталог, включая `cd X && …`) лежит внутри выгрузки или накрывает её:

- `src/cf/**`, `src/cfe/**` относительно корня проекта или ближайшего git-корня (worktree);
- любой каталог, у которого выше по дереву есть `Configuration.xml`;
- каталог-предок выгрузки (`rg foo` из корня проекта, `rg foo src`, `.worktrees`) — его можно разрешить исключением `-g '!src/**'`, `--exclude-dir=src` или фильтром только не-1С файлов (`-t py`, `-g '*.md'`).

Разрешено:

- поиск в явно указанных файлах (до 5 файлов, без шаблонов `*`);
- команда с префиксом `BSL_GREP_REASON='<непустая причина>'` (также `env BSL_GREP_REASON=…` или `export BSL_GREP_REASON=…` ранее в той же команде);
- список имён (`rg --files`, `find`, `fd`) в выгрузке только с ограничением вывода: конвейер в `head`, `tail`, `wc` или `sed -n`, либо глубина `--max-depth`/`-maxdepth` не больше 3. Обоснование: имена не читают содержимое файлов, поэтому стоимость в основном в объёме вывода; ограничитель делает её предсказуемой, а `rg --files | rg Имя | head` остаётся удобным способом найти модуль по имени;
- фильтры stdin (`… | grep x`), нерекурсивный `grep` по каталогу, пути вне выгрузок, проекты без выгрузок.

Инструмент `Glob` не перехватывается: его вывод уже ограничен самим Claude Code, а на Linux `Glob`/`Grep` по умолчанию отключены, и поиск идёт через `Bash` (встроенные `bfs`/`ugrep`), который хук проверяет.

При запрете агент получает краткую таблицу «вид факта → инструмент». Инструменты анализатора в ней записаны как `mcp__<сервер>__symbol_info` и т. п.: `<сервер>` — имя, под которым MCP-сервер bsl-analyzer подключён в вашем проекте (например, `bsl-analyzer-workspace`); плагин MCP-сервер не устанавливает и конкретного имени не требует. При внутренней ошибке (битый JSON события, незакрытые кавычки, битая настройка) хук пропускает вызов и выводит `systemMessage` (fail-open).

### Настройка проекта

Необязательный `.claude/onec-bsl.json` в корне проекта:

```json
{
  "sourceRoots": ["vendor/my-config", "**/exchange-config"],
  "useDefaults": true,
  "detectConfigurationXml": true
}
```

- `sourceRoots` — дополнительные корни выгрузок относительно корня проекта; поддерживаются `*` и `**`. Конкретные пути (без шаблонов) также защищают каталоги-предки.
- `useDefaults: false` отключает встроенные `src/cf`, `src/cfe`.
- `detectConfigurationXml: false` отключает распознавание по `Configuration.xml`.

Ограничение: шаблон вида `**/exchange-config` не распознаётся при поиске по каталогу-предку (например, `rg foo vendor`); для такой защиты добавьте конкретный путь в `sourceRoots`.

## Проверки

```bash
python3 -m unittest discover -s tests -v
claude plugin validate --strict .
python3 scripts/lsp_smoke.py --root <каталог с bsl-analyzer.toml> \
  --file <путь>/Module.bsl --symbol <ИмяМетода> --query <ИмяМетода> --wait-index 60 --timeout 55
```

Для разовой проверки без установки: `claude --plugin-dir <путь к onec-bsl>`.
