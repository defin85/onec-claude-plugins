#!/usr/bin/env python3
"""PreToolUse-хук плагина onec-bsl: не пускает широкий текстовый поиск по выгрузкам 1С.

Читает событие хука из stdin и печатает решение в формате Claude Code.
Только стандартная библиотека, без сети. Любая внутренняя ошибка не блокирует
работу: хук пропускает вызов и сообщает о сбое (fail-open).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field

REASON_VAR = "BSL_GREP_REASON"
CONFIG_REL = os.path.join(".claude", "onec-bsl.json")
DEFAULT_ROOTS = ("src/cf", "src/cfe")
# Каталоги, наличие которых под целью поиска означает, что поиск накрывает выгрузку.
DEFAULT_CONCRETE_ROOTS = ("src/cf", "src/cfe")
CONFIG_MARKER = "Configuration.xml"
MAX_EXPLICIT_FILES = 5
MAX_NAME_DEPTH = 3
MAX_UPWARD_STEPS = 40
MAX_CHILD_SCAN = 200

SEPARATORS = {"|", "||", "&&", ";", "&", "(", ")", "\n", ";;", "|&"}
WRAPPERS = {"sudo", "nice", "nohup", "time", "command", "builtin", "exec", "stdbuf", "ionice"}
LIMITERS = {"head", "tail", "wc"}
GREP_NAMES = {"grep", "egrep", "fgrep", "ugrep", "ug", "rg", "ripgrep"}
ONEC_EXT_RE = re.compile(r"\.(bsl|os|xml|mdo|form|mxl)\b|\*\*?$|^\*$", re.IGNORECASE)

RG_VALUE_OPTS = {
    "-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not",
    "--type-add", "--type-clear", "-m", "--max-count", "-A", "--after-context", "-B",
    "--before-context", "-C", "--context", "-j", "--threads", "-M", "--max-columns", "-d",
    "--max-depth", "--maxdepth", "--max-filesize", "-E", "--encoding", "--pre", "--pre-glob", "-r",
    "--replace", "--sort", "--sortr", "--color", "--colors", "--context-separator",
    "--field-context-separator", "--field-match-separator", "--path-separator", "--dfa-size-limit",
    "--regex-size-limit", "--engine", "--ignore-file", "--hostname-bin", "--hyperlink-format",
    "--generate", "--max-columns-preview",
}
GREP_VALUE_OPTS = {
    "-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "--after-context", "-B",
    "--before-context", "-C", "--context", "--include", "--exclude", "--exclude-dir",
    "--include-dir", "-d", "--directories", "-D", "--devices", "--label", "--exclude-from",
    "-g", "--glob", "-t", "--file-type", "-O", "--file-extension", "-M", "--file-magic",
    "--max-depth", "--depth", "-K", "--range", "--format", "--json", "--sort",
}
FIND_VALUE_PREDICATES = {
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-type", "-maxdepth", "-mindepth",
    "-newer", "-size", "-mtime", "-mmin", "-user", "-group", "-perm", "-printf", "-fprintf",
    "-wholename", "-iwholename", "-lname", "-ilname", "-samefile", "-links", "-inum",
}

DENY_TEXT = """Широкий текстовый поиск по выгрузке 1С заблокирован плагином onec-bsl: {what}.
Выгрузки огромные, grep/rg по ним тратит минуты и токены. Используйте анализатор
через его MCP-сервер (`<сервер>` — имя, под которым сервер bsl-analyzer подключён
в проекте, например `bsl-analyzer-workspace`):

| Вид факта | Инструмент |
|---|---|
| известный символ (процедура, функция, переменная) | `mcp__<сервер>__symbol_info` или `LSP` (definition, references, hover, workspaceSymbol) |
| кто вызывает, влияние изменения | `mcp__<сервер>__graph` |
| поиск по смыслу или неизвестному имени | `mcp__<сервер>__search` или семантический индекс |
| карта одного модуля | `mcp__<сервер>__outline` |
| объекты, реквизиты, формы | `mcp__<сервер>__metadata` |
| анализатор в статусе loading | повторить после `status`/retry-ответа, не переходить на grep |
| файлы worktree, которых анализатор не видит | команда с префиксом `BSL_GREP_REASON='<причина>'` |

Разрешено без пометки: поиск в одном явно указанном файле, поиск вне выгрузок, список имён (`rg --files`, `find`) с ограничением вывода (`| head`) или глубины."""


@dataclass
class Settings:
    project_dir: str
    patterns: list[str]
    concrete_roots: list[str]
    use_marker: bool = True


@dataclass
class Finding:
    what: str
    path: str


@dataclass
class Segment:
    words: list[str]
    assigns: dict[str, str] = field(default_factory=dict)
    pipe_index: int = 0
    pipeline_id: int = 0


# ---------------------------------------------------------------- настройки


def load_settings(cwd: str) -> Settings:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or find_git_root(cwd) or cwd
    patterns: list[str] = list(DEFAULT_ROOTS)
    concrete: list[str] = list(DEFAULT_CONCRETE_ROOTS)
    use_marker = True
    cfg_path = os.path.join(project_dir, CONFIG_REL)
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding="utf-8") as handle:
            cfg = json.load(handle)
        roots = [str(item).strip().strip("/") for item in cfg.get("sourceRoots", []) if str(item).strip()]
        if cfg.get("useDefaults", True) is False:
            patterns, concrete = [], []
        patterns.extend(roots)
        concrete.extend(r for r in roots if not has_magic(r))
        use_marker = bool(cfg.get("detectConfigurationXml", True))
    return Settings(project_dir, patterns, concrete, use_marker)


def has_magic(text: str) -> bool:
    return any(ch in text for ch in "*?[")


def find_git_root(start: str) -> str | None:
    cur = os.path.abspath(start)
    for _ in range(MAX_UPWARD_STEPS):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


# ---------------------------------------------------------------- классификация путей


def match_pattern(rel_parts: list[str], pat_parts: list[str]) -> bool:
    """Совпадение пути с шаблоном, где ** означает любое число каталогов."""
    if not pat_parts:
        return not rel_parts
    head = pat_parts[0]
    if head == "**":
        return any(match_pattern(rel_parts[i:], pat_parts[1:]) for i in range(len(rel_parts) + 1))
    if not rel_parts:
        return False
    return fnmatch.fnmatchcase(rel_parts[0], head) and match_pattern(rel_parts[1:], pat_parts[1:])


def split_rel(path: str, base: str) -> list[str] | None:
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return None
    if rel == ".":
        return []
    if rel.startswith(".."):
        return None
    return rel.split(os.sep)


def inside_root(path: str, settings: Settings) -> str | None:
    """Вернуть описание выгрузки, если путь лежит внутри неё."""
    bases = []
    for base in (settings.project_dir, find_git_root(path)):
        if base and base not in bases:
            bases.append(base)
    for base in bases:
        parts = split_rel(path, base)
        if parts is None:
            continue
        for end in range(1, len(parts) + 1):
            for pat in settings.patterns:
                if match_pattern(parts[:end], pat.split("/")):
                    return "/".join(parts[:end])
    if settings.use_marker:
        cur = path if os.path.isdir(path) else os.path.dirname(path)
        stop = os.path.dirname(settings.project_dir) if is_within(path, settings.project_dir) else None
        for _ in range(MAX_UPWARD_STEPS):
            if os.path.isfile(os.path.join(cur, CONFIG_MARKER)):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur or parent == stop:
                break
            cur = parent
    return None


def contains_root(path: str, settings: Settings) -> str | None:
    """Вернуть выгрузку, которую накрывает поиск по каталогу-предку."""
    if not os.path.isdir(path):
        return None
    bases = [settings.project_dir, path]
    try:
        # один уровень вниз: каталоги вроде .worktrees/<имя>/src/cf
        with os.scandir(path) as entries:
            for entry in entries:
                if len(bases) > MAX_CHILD_SCAN:
                    break
                if entry.is_dir(follow_symlinks=False):
                    bases.append(entry.path)
    except OSError:
        pass
    for rel in settings.concrete_roots:
        for base in bases:
            candidate = os.path.join(base, rel)
            if is_within(candidate, path) and os.path.isdir(candidate):
                return candidate
    return None


def is_within(path: str, base: str) -> bool:
    parts = split_rel(path, base)
    return parts is not None


def resolve(path: str, cwd: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


# ---------------------------------------------------------------- разбор shell


REDIRECT_CHARS = set("<>")


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def lex_line(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def logical_lines(command: str) -> list[str]:
    """Строки команды без тел heredoc; строка с незакрытой кавычкой
    склеивается со следующими (многострочный аргумент, например `-m "..."`)."""
    lines = command.replace("\\\n", " ").split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        while True:
            try:
                lex_line(line)
                break
            except ValueError:
                if i >= len(lines):
                    break  # кавычка так и не закрылась — разберём мягко
                line += "\n" + lines[i]
                i += 1
        result.append(line)
        for match in HEREDOC_RE.finditer(line):
            delimiter = match.group(2)
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1  # тело heredoc — данные, а не команды
            i += 1
    return result


def tokenize(command: str) -> list[str]:
    """Разбить команду на слова и операторы, отбросив перенаправления."""
    tokens: list[str] = []
    for line_no, line in enumerate(logical_lines(command)):
        if line_no:
            tokens.append("\n")
        try:
            raw = lex_line(line)
        except ValueError:
            raw = [part.strip("'\"") for part in line.split()]
        i = 0
        while i < len(raw):
            tok = raw[i]
            if tok and set(tok) <= set(";&|()<>") and set(tok) & REDIRECT_CHARS:
                if tokens and tokens[-1].isdigit():
                    tokens.pop()
                i += 2  # оператор и его цель
                continue
            tokens.append(tok)
            i += 1
    return tokens


def segments_of(command: str) -> list[Segment]:
    tokens = tokenize(command)
    result: list[Segment] = []
    current: list[str] = []
    pipe_index = 0
    pipeline_id = 0

    def flush() -> None:
        nonlocal current
        if current:
            result.append(make_segment(current, pipe_index, pipeline_id))
        current = []

    for tok in tokens:
        if tok in SEPARATORS or (tok and set(tok) <= set(";&|()")):
            flush()
            if "|" in tok and "||" not in tok:
                pipe_index += 1
            else:
                pipe_index = 0
                pipeline_id += 1
            continue
        current.append(tok)
    flush()
    return result


ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def make_segment(words: list[str], pipe_index: int, pipeline_id: int) -> Segment:
    assigns: dict[str, str] = {}
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGN_RE.match(word):
            key, _, value = word.partition("=")
            assigns[key] = value
            i += 1
            continue
        if word == "env":
            i += 1
            continue
        if word in WRAPPERS:
            i += 1
            continue
        if word == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += 1  # длительность
            continue
        break
    return Segment(words[i:], assigns, pipe_index, pipeline_id)


def name_of(word: str) -> str:
    return os.path.basename(word)


# ---------------------------------------------------------------- анализ команд


@dataclass
class Target:
    paths: list[str]
    kind: str  # content | names
    tool: str
    limited: bool = False
    recursive: bool = True


def parse_rg(args: list[str], pipe_index: int) -> Target | None:
    paths: list[str] = []
    have_pattern = False
    files_mode = False
    depth: int | None = None
    i = 0
    after_ddash = False
    while i < len(args):
        arg = args[i]
        if not after_ddash and arg == "--":
            after_ddash = True
        elif not after_ddash and arg.startswith("--") and "=" in arg:
            key, _, value = arg.partition("=")
            if key in {"--regexp", "--file"}:
                have_pattern = True
            if key in {"--max-depth", "--maxdepth"}:
                depth = to_int(value)
        elif not after_ddash and arg.startswith("--"):
            if arg in {"--regexp", "--file"}:
                have_pattern = True
            if arg in {"--max-depth", "--maxdepth"} and i + 1 < len(args):
                depth = to_int(args[i + 1])
            if arg in {"--files", "--type-list"}:
                files_mode = True
            if arg in RG_VALUE_OPTS:
                i += 1
        elif not after_ddash and arg.startswith("-") and arg != "-":
            flags = arg[1:]
            for pos, ch in enumerate(flags):
                if ch in "ef":
                    have_pattern = True
                if ch in "efgtTmABCjMdEr":
                    rest = flags[pos + 1:]
                    value = rest or (args[i + 1] if i + 1 < len(args) else "")
                    if ch == "d":
                        depth = to_int(value)
                    if not rest:
                        i += 1
                    break
        else:
            if files_mode or have_pattern:
                paths.append(arg)
            else:
                have_pattern = True
        i += 1
    if files_mode:
        return Target(paths, "names", "rg --files", limited=depth is not None and depth <= MAX_NAME_DEPTH)
    if not paths and pipe_index > 0:
        return None  # фильтр stdin
    return Target(paths, "content", "rg")


def parse_grep(prog: str, args: list[str], pipe_index: int) -> Target | None:
    paths: list[str] = []
    have_pattern = False
    recursive = prog in {"ugrep", "ug"} and pipe_index == 0
    i = 0
    after_ddash = False
    while i < len(args):
        arg = args[i]
        if not after_ddash and arg == "--":
            after_ddash = True
        elif not after_ddash and arg.startswith("--"):
            key, eq, value = arg.partition("=")
            if key in {"--recursive", "--dereference-recursive"}:
                recursive = True
            if key in {"--directories", "-d"} and (value if eq else "") == "recurse":
                recursive = True
            if key in {"--regexp", "--file"}:
                have_pattern = True
            if not eq and key in GREP_VALUE_OPTS:
                if key == "--directories" and i + 1 < len(args) and args[i + 1] == "recurse":
                    recursive = True
                i += 1
        elif not after_ddash and arg.startswith("-") and arg != "-":
            flags = arg[1:]
            for pos, ch in enumerate(flags):
                if ch in "rR":
                    recursive = True
                if ch in "ef":
                    have_pattern = True
                if ch in "efmABCdDgtOMK":
                    rest = flags[pos + 1:]
                    if ch == "d" and (rest == "recurse" or (not rest and i + 1 < len(args) and args[i + 1] == "recurse")):
                        recursive = True
                    if not rest:
                        i += 1
                    break
        else:
            if have_pattern:
                paths.append(arg)
            else:
                have_pattern = True
        i += 1
    if not paths and pipe_index > 0:
        return None
    if not recursive and not paths:
        return None  # без рекурсии и файлов grep читает только stdin
    return Target(paths, "content", prog, recursive=recursive)


def parse_git_grep(args: list[str], cwd: str) -> tuple[Target, str] | None:
    i = 0
    while i < len(args) and args[i] != "grep":
        if args[i] == "-C" and i + 1 < len(args):
            cwd = resolve(args[i + 1], cwd)
            i += 2
            continue
        i += 1
    if i >= len(args):
        return None
    rest = args[i + 1:]
    paths: list[str] = []
    if "--" in rest:
        paths = rest[rest.index("--") + 1:]
    else:
        positional = [a for a in rest if not a.startswith("-")]
        if any(a in {"-e", "-f"} for a in rest):
            paths = positional[1:] if len(positional) > 1 else []
        else:
            paths = positional[1:]
    return Target(paths, "content", "git grep"), cwd


def parse_find(args: list[str]) -> tuple[list[str], bool, int | None]:
    roots: list[str] = []
    i = 0
    while i < len(args) and not args[i].startswith("-") and args[i] not in {"(", "!"}:
        roots.append(args[i])
        i += 1
    exec_grep = False
    depth: int | None = None
    while i < len(args):
        arg = args[i]
        if arg in {"-exec", "-execdir", "-ok", "-okdir"} and i + 1 < len(args):
            if name_of(args[i + 1]) in GREP_NAMES:
                exec_grep = True
        if arg == "-maxdepth" and i + 1 < len(args):
            depth = to_int(args[i + 1])
        i += 1
    return roots, exec_grep, depth


def to_int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def has_reason(segment: Segment, exported: str) -> bool:
    return bool(segment.assigns.get(REASON_VAR, "").strip() or exported.strip())


def pipeline_limited(segments: list[Segment], index: int) -> bool:
    seg = segments[index]
    for later in segments[index + 1:]:
        if later.pipeline_id != seg.pipeline_id:
            break
        if later.pipe_index > 0 and later.words and name_of(later.words[0]) in LIMITERS:
            return True
        if later.pipe_index > 0 and later.words and name_of(later.words[0]) == "sed" and "-n" in later.words:
            return True
    return False


def check_targets(target: Target, cwd: str, settings: Settings, ancestor_ok: bool = False) -> Finding | None:
    """Найти цель поиска внутри выгрузки или каталог, накрывающий выгрузку.

    ancestor_ok разрешает поиск по каталогу-предку, если команда явно исключает выгрузки.
    """
    raw_paths = target.paths or ["."]
    resolved = [resolve(p, cwd) for p in raw_paths]
    if target.kind == "content":
        explicit = [p for p, raw in zip(resolved, raw_paths) if not has_magic(raw) and os.path.isfile(p)]
        if len(explicit) == len(resolved) and len(resolved) <= MAX_EXPLICIT_FILES:
            return None
    for raw, path in zip(raw_paths, resolved):
        magic = has_magic(raw)
        if target.kind == "content" and not magic and os.path.isfile(path) and len(resolved) <= MAX_EXPLICIT_FILES:
            continue
        if not target.recursive and not magic and os.path.isdir(path):
            continue  # grep без -r каталог не читает
        probe = path
        if magic:
            prefix = re.split(r"[*?\[]", raw, maxsplit=1)[0]
            probe = resolve(prefix or ".", cwd)
        root = inside_root(probe, settings)
        if root:
            return Finding(f"{target.tool} по `{raw}` внутри выгрузки `{root}`", path)
        if ancestor_ok or not target.recursive:
            continue
        covered = contains_root(probe, settings)
        if covered:
            return Finding(f"{target.tool} по `{raw}` накрывает выгрузку `{covered}`; сузьте путь или исключите её (`-g '!src/**'`)", path)
    return None


def excludes_dumps(args: list[str]) -> bool:
    """rg/grep с явным исключением выгрузок или фильтром только не-1С файлов."""
    values: list[tuple[str, str]] = []
    for i, arg in enumerate(args):
        for opt in ("-g", "--glob", "--iglob", "--exclude-dir", "--exclude", "--include", "-t", "--type"):
            if arg == opt and i + 1 < len(args):
                values.append((opt, args[i + 1]))
            elif arg.startswith(opt + "="):
                values.append((opt, arg.split("=", 1)[1]))
            elif opt in {"-g", "-t"} and arg.startswith(opt) and len(arg) > 2 and not arg.startswith("--"):
                values.append((opt, arg[2:]))
    for opt, value in values:
        if opt in {"-g", "--glob", "--iglob"} and value.startswith("!") and ("src" in value or "cf" in value):
            return True
        if opt == "--exclude-dir" and value.strip("/") in {"src", "cf", "cfe"}:
            return True
    includes = [v for o, v in values if o in {"-g", "--glob", "--iglob", "--include"} and not v.startswith("!")]
    types = [v for o, v in values if o in {"-t", "--type"}]
    if includes and not any(ONEC_EXT_RE.search(v) for v in includes):
        return True
    if types and not any(t in {"bsl", "xml", "all"} for t in types):
        return True
    return False


def analyze_bash(command: str, cwd: str, settings: Settings) -> Finding | None:
    segments = segments_of(command)
    exported = ""
    for idx, seg in enumerate(segments):
        words = seg.words
        if not words:
            continue
        prog = name_of(words[0])
        args = words[1:]
        if prog == "cd" and seg.pipe_index == 0:
            target = args[0] if args else os.path.expanduser("~")
            if target != "-":
                cwd = resolve(target, cwd)
            continue
        if prog == "export":
            for arg in args:
                if arg.startswith(REASON_VAR + "="):
                    exported = arg.split("=", 1)[1]
            continue
        if prog in {"bash", "sh", "zsh"} and "-c" in args:
            pos = args.index("-c")
            if pos + 1 < len(args):
                inner = analyze_bash(args[pos + 1], cwd, settings)
                if inner and not has_reason(seg, exported):
                    return inner
            continue
        if prog == "xargs":
            inner_words = [a for a in args if not a.startswith("-")]
            if inner_words and name_of(inner_words[0]) in GREP_NAMES and not has_reason(seg, exported):
                finding = xargs_source(segments, idx, cwd, settings)
                if finding:
                    return finding
            continue
        finding = analyze_simple(prog, args, seg, segments, idx, cwd, settings)
        if finding and not has_reason(seg, exported):
            return finding
    return None


def xargs_source(segments: list[Segment], idx: int, cwd: str, settings: Settings) -> Finding | None:
    seg = segments[idx]
    for prev in reversed(segments[:idx]):
        if prev.pipeline_id != seg.pipeline_id or not prev.words:
            continue
        prog = name_of(prev.words[0])
        if prog == "find":
            roots, _, _ = parse_find(prev.words[1:])
            return check_targets(Target(roots, "names", "find | xargs grep"), cwd, settings)
        if prog in {"rg", "fd", "fdfind"}:
            target = Target([a for a in prev.words[1:] if not a.startswith("-")], "names", f"{prog} | xargs grep")
            return check_targets(target, cwd, settings)
        if prog == "git":
            return check_targets(Target([], "names", "git ls-files | xargs grep"), cwd, settings)
    return check_targets(Target([], "names", "xargs grep"), cwd, settings)


def analyze_simple(prog, args, seg, segments, idx, cwd, settings) -> Finding | None:
    if prog in {"rg", "ripgrep"}:
        target = parse_rg(args, seg.pipe_index)
        if target is None:
            return None
        if target.kind == "names":
            if target.limited or pipeline_limited(segments, idx):
                return None
            return check_targets(target, cwd, settings)
        return check_targets(target, cwd, settings, ancestor_ok=excludes_dumps(args))
    if prog in {"grep", "egrep", "fgrep", "ugrep", "ug"}:
        target = parse_grep(prog, args, seg.pipe_index)
        if target is None:
            return None
        return check_targets(target, cwd, settings, ancestor_ok=excludes_dumps(args))
    if prog == "git":
        parsed = parse_git_grep(args, cwd)
        if parsed is None:
            return None
        target, git_cwd = parsed
        return check_targets(target, git_cwd, settings)
    if prog in {"find", "bfs"}:
        roots, exec_grep, depth = parse_find(args)
        pruned = "-prune" in args
        if exec_grep:
            return check_targets(Target(roots, "names", "find -exec grep"), cwd, settings, ancestor_ok=pruned)
        if (depth is not None and depth <= MAX_NAME_DEPTH) or pipeline_limited(segments, idx):
            return None
        return check_targets(Target(roots, "names", "find без ограничения вывода"), cwd, settings, ancestor_ok=pruned)
    if prog in {"fd", "fdfind"}:
        if pipeline_limited(segments, idx) or any(a in {"-d", "--max-depth"} or a.startswith("--max-depth=") for a in args):
            return None
        positional = [a for a in args if not a.startswith("-")]
        return check_targets(Target(positional[1:], "names", "fd без ограничения вывода"), cwd, settings)
    return None


def analyze_grep_tool(tool_input: dict, cwd: str, settings: Settings) -> Finding | None:
    raw = tool_input.get("path") or "."
    path = resolve(raw, cwd)
    if os.path.isfile(path):
        return None
    glob = tool_input.get("glob") or ""
    file_type = tool_input.get("type") or ""
    ancestor_ok = bool(
        (glob and not glob.startswith("!") and not ONEC_EXT_RE.search(glob))
        or (file_type and file_type not in {"bsl", "xml"})
    )
    return check_targets(Target([raw], "content", "Grep"), cwd, settings, ancestor_ok=ancestor_ok)


# ---------------------------------------------------------------- точка входа


def decide(event: dict) -> dict | None:
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    settings = load_settings(cwd)
    if tool == "Bash":
        finding = analyze_bash(str(tool_input.get("command", "")), cwd, settings)
    elif tool == "Grep":
        finding = analyze_grep_tool(tool_input, cwd, settings)
    else:
        return None
    if not finding:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_TEXT.format(what=finding.what),
        }
    }


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        result = decide(event)
    except Exception as exc:  # fail-open: хук не должен ломать работу
        message = f"onec-bsl: проверка поиска пропущена из-за внутренней ошибки хука ({type(exc).__name__}: {exc})"
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        return 0
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
