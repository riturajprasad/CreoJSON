import argparse
import ast
import bisect
import json
import re
import sys
from pathlib import Path


HEADER_EXTENSIONS = ('.h', '.hh', '.hpp', '.hxx')
DEFAULT_EXCLUDE_DIRS = {
    '.git',
    '.hg',
    '.svn',
    '__pycache__',
    '.mypy_cache',
    '.pytest_cache',
    'node_modules',
    'venv',
    '.venv',
}

COMMENT_RE = re.compile(r'//[^\n]*|/\*.*?\*/', re.DOTALL)
TYPE_START_RE = re.compile(r'\b(?:typedef\s+)?(?P<kind>class|struct|union|enum)\b')
IDENT_RE = re.compile(r'[A-Za-z_]\w*')
ACCESS_LABEL_RE = re.compile(r'\b(public|protected|private)\s*:')

CONTROL_NAMES = {
    'if',
    'for',
    'while',
    'switch',
    'catch',
    'return',
    'sizeof',
    'alignof',
    'decltype',
}

TYPE_NAME_STOPWORDS = {
    'typedef',
    'class',
    'struct',
    'union',
    'enum',
    'public',
    'private',
    'protected',
    'virtual',
    'final',
    'sealed',
    'abstract',
    'const',
    'volatile',
    'static',
    'extern',
}

ARGUMENT_TYPE_WORDS = {
    'void',
    'char',
    'short',
    'int',
    'long',
    'float',
    'double',
    'signed',
    'unsigned',
    'const',
    'volatile',
    'struct',
    'enum',
    'class',
    'union',
    'bool',
    'size_t',
}

SECTION_ALIASES = {
    'brief': 'description',
    'details': 'description',
    'description': 'description',
    'purpose': 'description',
    'summary': 'description',
    'parameters': 'parameters',
    'parameter': 'parameters',
    'arguments': 'parameters',
    'argument': 'parameters',
    'input': 'input',
    'inputs': 'input',
    'input arguments': 'input',
    'input parameters': 'input',
    'output': 'output',
    'outputs': 'output',
    'output arguments': 'output',
    'output parameters': 'output',
    'return': 'returns',
    'returns': 'returns',
    'return value': 'returns',
    'return values': 'returns',
    'retval': 'returns',
    'error': 'returns',
    'errors': 'returns',
    'hierarchy': 'hierarchy',
    'class hierarchy': 'hierarchy',
    'inheritance hierarchy': 'hierarchy',
    'inheritance': 'hierarchy',
}


def normalize_space(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def build_line_starts(text):
    starts = [0]
    for match in re.finditer('\n', text):
        starts.append(match.end())
    return starts


def line_for_offset(line_starts, offset):
    return bisect.bisect_right(line_starts, offset)


def column_for_offset(line_starts, offset):
    line = line_for_offset(line_starts, offset)
    return offset - line_starts[line - 1] + 1


def blank_preserving_newlines(text):
    return ''.join('\n' if char == '\n' else ' ' for char in text)


def clean_comment(raw):
    raw = raw.strip()
    if raw.startswith('//'):
        lines = [line.strip()[2:].strip() if line.strip().startswith('//') else line.strip()
                 for line in raw.splitlines()]
    else:
        body = raw[2:-2]
        lines = []
        for line in body.splitlines():
            stripped = line.strip()
            stripped = re.sub(r'^\*+\s?', '', stripped)
            stripped = re.sub(r'^[!<]+\s?', '', stripped)
            lines.append(stripped.rstrip())

    cleaned = []
    for line in lines:
        line = re.sub(r'^[!<]+\s?', '', line.strip())
        if line:
            cleaned.append(line)
        elif cleaned and cleaned[-1] != '':
            cleaned.append('')

    while cleaned and cleaned[-1] == '':
        cleaned.pop()
    return '\n'.join(cleaned).strip()


def strip_comments(text, line_starts):
    comments = []
    pieces = []
    last = 0

    for match in COMMENT_RE.finditer(text):
        pieces.append(text[last:match.start()])
        pieces.append(blank_preserving_newlines(match.group(0)))
        kind = 'line' if match.group(0).lstrip().startswith('//') else 'block'
        comments.append({
            'kind': kind,
            'text': clean_comment(match.group(0)),
            'start_offset': match.start(),
            'end_offset': match.end(),
            'start_line': line_for_offset(line_starts, match.start()),
            'end_line': line_for_offset(line_starts, max(match.end() - 1, match.start())),
            'start_column': column_for_offset(line_starts, match.start()),
        })
        last = match.end()

    pieces.append(text[last:])
    masked = ''.join(pieces)

    merged = []
    for comment in comments:
        if (
            comment['kind'] == 'line'
            and merged
            and merged[-1]['kind'] == 'line'
            and comment['start_line'] <= merged[-1]['end_line'] + 1
            and comment['start_column'] == merged[-1]['start_column']
        ):
            if comment['text']:
                merged[-1]['text'] = (merged[-1]['text'] + '\n' + comment['text']).strip()
            merged[-1]['end_line'] = comment['end_line']
            merged[-1]['end_offset'] = comment['end_offset']
        else:
            merged.append(comment)

    return masked, merged


def read_text(path):
    for encoding in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    return path.read_text(errors='replace'), 'unknown'


def discover_header_files(source, extensions, exclude_dirs):
    source = Path(source)
    normalized_exts = {ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in extensions}

    if source.is_file():
        return [source] if source.suffix.lower() in normalized_exts else []

    files = []
    for path in source.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in normalized_exts:
            continue
        if any(part in exclude_dirs for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def is_gap_line(line):
    stripped = line.strip()
    if not stripped:
        return True
    if ACCESS_LABEL_RE.fullmatch(stripped):
        return True
    if stripped.startswith('template') or stripped.startswith('[['):
        return True
    if stripped in {'extern "C" {', 'extern "C++" {'}:
        return True
    return False


def nearest_comment_text(comments, code_lines, line_number, max_gap=5):
    same_line = [
        comment['text'] for comment in comments
        if comment['start_line'] == line_number and comment['end_line'] == line_number and comment['text']
    ]
    if same_line:
        return '\n'.join(same_line).strip()

    for comment in reversed(comments):
        if comment['end_line'] >= line_number:
            continue
        gap = line_number - comment['end_line']
        if gap > max_gap:
            break
        between = code_lines[comment['end_line']:line_number - 1]
        if all(is_gap_line(line) for line in between):
            return comment['text']
    return ''


def normalize_direction(direction):
    if not direction:
        return None
    direction = direction.strip().lower().replace(' ', '').replace('-', '_')
    if direction in {'in', 'input'}:
        return 'input'
    if direction in {'out', 'output'}:
        return 'output'
    if direction in {'inout', 'in,out', 'out,in', 'inputoutput', 'input_output'}:
        return 'input_output'
    return direction


def parse_named_description(line):
    line = line.strip().lstrip('-*').strip()
    line = re.sub(r'^\[(?P<dir>in|out|in,out|out,in|inout)\]\s*', '', line, flags=re.IGNORECASE)
    match = re.match(r'(?P<name>[A-Za-z_]\w*)\s*(?:[-:]\s+|\s{2,})(?P<desc>.*)$', line)
    if match:
        return match.group('name'), match.group('desc').strip()
    match = re.match(r'(?P<name>[A-Za-z_]\w*)\s+(?P<desc>.+)$', line)
    if match:
        return match.group('name'), match.group('desc').strip()
    match = re.match(r'(?P<name>[A-Za-z_]\w*)$', line)
    if match:
        return match.group('name'), ''
    return None, line


def section_from_line(line):
    stripped = line.strip().strip('*').strip()
    if not stripped:
        return None, ''

    command = re.match(r'[@\\](?P<name>[A-Za-z_]+)\b\s*(?P<rest>.*)$', stripped)
    if command and command.group('name').lower() in SECTION_ALIASES:
        return SECTION_ALIASES[command.group('name').lower()], command.group('rest').strip()

    if ':' in stripped:
        head, rest = stripped.split(':', 1)
        key = normalize_space(head).lower()
        if key in SECTION_ALIASES:
            return SECTION_ALIASES[key], rest.strip()

    key = normalize_space(stripped).lower()
    if key in SECTION_ALIASES:
        return SECTION_ALIASES[key], ''
    return None, ''


def parse_doc(text):
    result = {
        'description': '',
        'params': {},
        'returns': '',
        'return_values': [],
        'hierarchy': {
            'lines': [],
            'symbols': [],
        },
    }
    if not text:
        return result

    section = 'description'
    description_lines = []
    return_lines = []
    hierarchy_lines = []

    def add_param(name, desc='', direction=None):
        if not name:
            return
        existing = result['params'].setdefault(name, {'direction': None, 'description': ''})
        if direction:
            existing['direction'] = normalize_direction(direction)
        if desc:
            existing['description'] = normalize_space((existing['description'] + ' ' + desc).strip())

    def consume_section_line(active_section, line):
        if not line:
            return
        if active_section in {'input', 'output', 'parameters'}:
            direction = 'input' if active_section == 'input' else 'output' if active_section == 'output' else None
            inline_direction = re.match(r'^\[(?P<dir>in|out|in,out|out,in|inout)\]\s*(?P<body>.*)$', line, re.IGNORECASE)
            if inline_direction:
                direction = inline_direction.group('dir')
                line = inline_direction.group('body').strip()
            name, desc = parse_named_description(line)
            add_param(name, desc, direction)
        elif active_section == 'returns':
            name, desc = parse_named_description(line)
            if name and (name.isupper() or name.startswith(('PRO_', 'Pro', 'e'))):
                result['return_values'].append({'value': name, 'description': desc})
            else:
                return_lines.append(line)
        elif active_section == 'hierarchy':
            hierarchy_lines.append(line)
        else:
            description_lines.append(line)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        param_match = re.match(
            r'[@\\]param(?:\s*\[(?P<dir>[^\]]+)\])?\s+(?P<name>[A-Za-z_]\w*)\s*(?P<desc>.*)$',
            line,
        )
        if param_match:
            add_param(param_match.group('name'), param_match.group('desc'), param_match.group('dir'))
            continue

        retval_match = re.match(r'[@\\]retval\s+(?P<value>\S+)\s*(?P<desc>.*)$', line)
        if retval_match:
            result['return_values'].append({
                'value': retval_match.group('value'),
                'description': retval_match.group('desc').strip(),
            })
            continue

        return_match = re.match(r'[@\\](?:return|returns)\b\s*(?P<desc>.*)$', line)
        if return_match:
            section = 'returns'
            if return_match.group('desc'):
                return_lines.append(return_match.group('desc').strip())
            continue

        section_name, rest = section_from_line(line)
        if section_name:
            section = section_name
            if rest:
                consume_section_line(section, rest)
            continue

        if line.startswith(('@file', '\\file', '@author', '\\author', '@date', '\\date')):
            continue

        consume_section_line(section, line)

    result['description'] = normalize_space(' '.join(description_lines))
    result['returns'] = normalize_space(' '.join(return_lines))
    result['hierarchy']['lines'] = hierarchy_lines

    symbols = []
    ignored = {
        'Class',
        'Hierarchy',
        'Base',
        'Derived',
        'Parent',
        'Child',
        'Inherits',
        'From',
        'The',
        'This',
    }
    for line in hierarchy_lines:
        for symbol in re.findall(r'[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*', line):
            if symbol not in ignored and symbol not in symbols:
                symbols.append(symbol)
    result['hierarchy']['symbols'] = symbols
    return result


def find_matching_delimiter(text, open_index, open_char='{', close_char='}'):
    depth = 0
    index = open_index
    quote = None
    escape = False

    while index < len(text):
        char = text[index]
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = None
            index += 1
            continue

        if char in {'"', "'"}:
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def find_next_statement_end(text, start):
    quote = None
    escape = False
    paren = 0
    bracket = 0
    brace = 0

    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = None
            continue

        if char in {'"', "'"}:
            quote = char
        elif char == '(':
            paren += 1
        elif char == ')':
            paren = max(0, paren - 1)
        elif char == '[':
            bracket += 1
        elif char == ']':
            bracket = max(0, bracket - 1)
        elif char == '{':
            brace += 1
        elif char == '}':
            brace = max(0, brace - 1)
        elif char == ';' and paren == 0 and bracket == 0 and brace == 0:
            return index
    return -1


def split_top_level(text, delimiter=','):
    parts = []
    start = 0
    quote = None
    escape = False
    paren = 0
    bracket = 0
    brace = 0
    angle = 0
    index = 0

    while index < len(text):
        char = text[index]
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = None
            index += 1
            continue

        if char in {'"', "'"}:
            quote = char
        elif char == '(':
            paren += 1
        elif char == ')':
            paren = max(0, paren - 1)
        elif char == '[':
            bracket += 1
        elif char == ']':
            bracket = max(0, bracket - 1)
        elif char == '{':
            brace += 1
        elif char == '}':
            brace = max(0, brace - 1)
        elif char == '<':
            previous_char = text[index - 1] if index else ''
            next_char = text[index + 1] if index + 1 < len(text) else ''
            if previous_char not in '<=' and next_char not in '<=':
                angle += 1
        elif char == '>':
            previous_char = text[index - 1] if index else ''
            next_char = text[index + 1] if index + 1 < len(text) else ''
            if angle and previous_char != '-' and next_char != '>':
                angle -= 1
        elif (
            char == delimiter
            and paren == 0
            and bracket == 0
            and brace == 0
            and angle == 0
        ):
            parts.append(text[start:index])
            start = index + 1
        index += 1

    parts.append(text[start:])
    return parts


def split_default_value(text):
    parts = split_top_level(text, delimiter='=')
    if len(parts) <= 1:
        return text.strip(), None
    return parts[0].strip(), '='.join(parts[1:]).strip()


def strip_attributes(text):
    text = re.sub(r'\[\[.*?\]\]', ' ', text)
    text = re.sub(r'__declspec\s*\([^)]*\)', ' ', text)
    text = re.sub(r'__attribute__\s*\(\([^)]*\)\)', ' ', text)
    text = re.sub(r'alignas\s*\([^)]*\)', ' ', text)
    return normalize_space(text)


def parse_aliases(trailer):
    aliases = []
    trailer = strip_attributes(trailer.strip().rstrip(';'))
    if not trailer:
        return aliases

    for piece in split_top_level(trailer):
        piece = re.sub(r'\[[^\]]*\]', ' ', piece)
        piece = piece.replace('*', ' ').replace('&', ' ')
        ids = [item for item in IDENT_RE.findall(piece) if item not in TYPE_NAME_STOPWORDS]
        if ids and ids[-1] not in aliases:
            aliases.append(ids[-1])
    return aliases


def find_top_level_colon(text):
    quote = None
    escape = False
    angle = 0
    paren = 0
    for index, char in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == '<':
            angle += 1
        elif char == '>':
            angle = max(0, angle - 1)
        elif char == '(':
            paren += 1
        elif char == ')':
            paren = max(0, paren - 1)
        elif char == ':' and angle == 0 and paren == 0:
            before = text[index - 1] if index else ''
            after = text[index + 1] if index + 1 < len(text) else ''
            if before != ':' and after != ':':
                return index
    return -1


def parse_base_classes(base_text):
    bases = []
    for raw_part in split_top_level(base_text):
        raw = normalize_space(raw_part)
        if not raw:
            continue
        access_match = re.search(r'\b(public|protected|private)\b', raw)
        access = access_match.group(1) if access_match else None
        name = re.sub(r'\b(public|protected|private|virtual)\b', ' ', raw)
        name = normalize_space(name)
        bases.append({
            'name': name,
            'access': access,
            'raw': raw,
        })
    return bases


def parse_type_header(kind, header, trailer):
    header = strip_attributes(header)
    aliases = parse_aliases(trailer)
    name = None
    bases = []
    scoped_enum = False
    underlying_type = None

    if kind == 'enum':
        match = re.search(r'\benum\s+(?:(class|struct)\s+)?(?P<rest>.*)$', header)
        rest = match.group('rest').strip() if match else ''
        scoped_enum = bool(match and match.group(1))
        colon = find_top_level_colon(rest)
        if colon >= 0:
            name_part = rest[:colon]
            underlying_type = normalize_space(rest[colon + 1:])
        else:
            name_part = rest
        ids = [item for item in IDENT_RE.findall(name_part) if item not in TYPE_NAME_STOPWORDS]
        name = ids[-1] if ids else None
    else:
        match = re.search(rf'\b{kind}\b(?P<rest>.*)$', header)
        rest = match.group('rest').strip() if match else ''
        colon = find_top_level_colon(rest)
        if colon >= 0:
            name_part = rest[:colon]
            bases = parse_base_classes(rest[colon + 1:])
        else:
            name_part = rest
        name_part = re.sub(r'\b(final|sealed|abstract)\b', ' ', name_part)
        ids = [item for item in IDENT_RE.findall(name_part) if item not in TYPE_NAME_STOPWORDS]
        name = ids[-1] if ids else None

    if not name and aliases:
        name = aliases[0]

    return {
        'name': name or '<anonymous>',
        'aliases': aliases,
        'base_classes': bases,
        'scoped_enum': scoped_enum,
        'underlying_type': underlying_type,
    }


def find_type_blocks(code, line_starts):
    blocks = []

    for match in TYPE_START_RE.finditer(code):
        start = match.start()
        kind = match.group('kind')
        open_brace = code.find('{', match.end())
        next_semicolon = code.find(';', match.end())
        if open_brace == -1 or (next_semicolon != -1 and next_semicolon < open_brace):
            continue

        close_brace = find_matching_delimiter(code, open_brace, '{', '}')
        if close_brace == -1:
            continue
        statement_end = find_next_statement_end(code, close_brace + 1)
        if statement_end == -1:
            continue

        header = code[start:open_brace]
        trailer = code[close_brace + 1:statement_end]
        parsed = parse_type_header(kind, header, trailer)
        block = {
            'index': len(blocks),
            'kind': kind,
            'name': parsed['name'],
            'aliases': parsed['aliases'],
            'base_classes': parsed['base_classes'],
            'scoped_enum': parsed['scoped_enum'],
            'underlying_type': parsed['underlying_type'],
            'start': start,
            'open_brace': open_brace,
            'close_brace': close_brace,
            'end': statement_end + 1,
            'line': line_for_offset(line_starts, start),
            'parent_index': None,
            'qualified_name': parsed['name'],
        }
        blocks.append(block)

    blocks.sort(key=lambda item: (item['start'], -(item['end'] - item['start'])))
    for index, block in enumerate(blocks):
        block['index'] = index

    for block in blocks:
        parent = None
        for candidate in blocks:
            if candidate is block:
                continue
            if candidate['open_brace'] < block['start'] and block['end'] <= candidate['close_brace']:
                if parent is None or candidate['start'] > parent['start']:
                    parent = candidate
        if parent:
            block['parent_index'] = parent['index']

    for block in blocks:
        names = [block['name']]
        parent_index = block['parent_index']
        while parent_index is not None:
            parent = blocks[parent_index]
            names.append(parent['name'])
            parent_index = parent['parent_index']
        block['qualified_name'] = '::'.join(reversed([name for name in names if name and name != '<anonymous>']))
    return blocks


def mask_spans(text, spans, base_offset=0):
    chars = list(text)
    for start, end in spans:
        relative_start = max(0, start - base_offset)
        relative_end = min(len(chars), end - base_offset)
        if relative_start >= relative_end:
            continue
        for index in range(relative_start, relative_end):
            if chars[index] != '\n':
                chars[index] = ' '
    return ''.join(chars)


def flatten_non_type_scopes(code):
    chars = list(code)
    scope_re = re.compile(r'\b(?:extern\s+"C(?:\+\+)?"|(?:inline\s+)?namespace(?:\s+[A-Za-z_]\w*)?)\s*\{')

    for match in scope_re.finditer(code):
        open_brace = code.rfind('{', match.start(), match.end())
        if open_brace == -1:
            continue
        close_brace = find_matching_delimiter(code, open_brace, '{', '}')
        if close_brace == -1:
            continue
        for index in range(match.start(), open_brace + 1):
            if chars[index] != '\n':
                chars[index] = ' '
        if chars[close_brace] != '\n':
            chars[close_brace] = ' '
    return ''.join(chars)


def split_declarations_and_definitions(text, base_offset=0):
    segments = []
    start = 0
    quote = None
    escape = False
    paren = 0
    bracket = 0
    index = 0

    while index < len(text):
        char = text[index]
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = None
            index += 1
            continue

        if char in {'"', "'"}:
            quote = char
        elif char == '(':
            paren += 1
        elif char == ')':
            paren = max(0, paren - 1)
        elif char == '[':
            bracket += 1
        elif char == ']':
            bracket = max(0, bracket - 1)
        elif char == '{' and paren == 0 and bracket == 0:
            prefix = text[start:index].strip()
            close = find_matching_delimiter(text, index, '{', '}')
            if close == -1:
                break
            if '(' in prefix and ')' in prefix:
                if prefix:
                    segments.append({
                        'text': prefix,
                        'start': base_offset + start,
                        'end': base_offset + close + 1,
                        'definition': True,
                    })
                start = close + 1
                index = close + 1
                continue
            index = close + 1
            continue
        elif char == ';' and paren == 0 and bracket == 0:
            chunk = text[start:index + 1].strip()
            if chunk:
                segments.append({
                    'text': chunk,
                    'start': base_offset + start,
                    'end': base_offset + index + 1,
                    'definition': False,
                })
            start = index + 1
        index += 1

    return segments


def first_meaningful_offset(segment_text, absolute_start):
    offset = 0
    for line in segment_text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            offset += len(line)
            continue
        stripped_without_access = re.sub(r'^(?:public|protected|private)\s*:\s*', '', stripped)
        if not stripped_without_access or stripped_without_access.startswith('#'):
            offset += len(line)
            continue
        column_shift = line.find(stripped_without_access)
        if column_shift == -1:
            column_shift = line.find(stripped)
        return absolute_start + offset + max(column_shift, 0)
    return absolute_start


def cleanup_declaration_text(text):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        lines.append(line)
    text = '\n'.join(lines)
    text = re.sub(r'\b(public|protected|private)\s*:\s*', ' ', text)
    text = re.sub(r'extern\s+"C(?:\+\+)?"\s*\{', ' ', text)
    text = re.sub(r'(?:inline\s+)?namespace\s+[A-Za-z_]\w*\s*\{', ' ', text)
    text = re.sub(r'\btemplate\s*<[^;{}]*>\s*', ' ', text)
    text = text.strip().rstrip(';').strip()
    if '{' in text:
        text = text.rsplit('{', 1)[-1].strip()
    text = strip_attributes(text)
    return text


def find_parameter_span(signature):
    candidates = []
    for index, char in enumerate(signature):
        if char != '(':
            continue
        close = find_matching_delimiter(signature, index, '(', ')')
        if close == -1:
            continue
        before = signature[:index].strip()
        if not before:
            continue
        if re.search(r'\(\s*\*\s*[A-Za-z_]\w*\s*\)\s*$', before):
            continue
        name = extract_function_name(before)
        if name:
            candidates.append((index, close, before, name))
    return candidates[-1] if candidates else None


def extract_function_name(prefix):
    prefix = prefix.strip()
    operator_match = re.search(r'(operator\s*(?:\(\)|\[\]|[^\s(]+))\s*$', prefix)
    if operator_match:
        return operator_match.group(1).strip()

    match = re.search(r'(?P<name>~?[A-Za-z_]\w*(?:::[~A-Za-z_]\w*)?)\s*$', prefix)
    if not match:
        return None
    name = match.group('name')
    simple_name = name.split('::')[-1].lstrip('~')
    if simple_name in CONTROL_NAMES:
        return None
    return name


def classify_special_method(name, return_type, context_name):
    simple_name = name.split('::')[-1]
    context_simple = context_name.split('::')[-1] if context_name else None
    if context_simple and simple_name == context_simple:
        return 'constructor', ''
    if context_simple and simple_name == f'~{context_simple}':
        return 'destructor', ''
    if not return_type and simple_name.startswith('~'):
        return 'destructor', ''
    return 'method', return_type


def parse_argument(raw_arg):
    raw = normalize_space(raw_arg)
    if not raw or raw == 'void':
        return None

    direction = None
    direction_match = re.match(r'^\[(?P<dir>in|out|in,out|out,in|inout)\]\s*(?P<body>.*)$', raw, re.IGNORECASE)
    if direction_match:
        direction = normalize_direction(direction_match.group('dir'))
        raw = direction_match.group('body').strip()

    no_default, default = split_default_value(raw)
    arg = no_default.strip()

    pointer_match = re.search(r'\(\s*\*\s*(?P<name>[A-Za-z_]\w*)\s*\)\s*\((?P<params>.*)\)', arg)
    if pointer_match:
        name = pointer_match.group('name')
        type_text = normalize_space(arg.replace(name, '', 1))
        return {
            'name': name,
            'type': type_text,
            'default': default,
            'direction': direction or 'input',
            'description': '',
            'raw': raw,
        }

    array_suffix = ''
    array_match = re.search(r'(?P<name>[A-Za-z_]\w*)\s*(?P<arrays>(?:\[[^\]]*\]\s*)+)$', arg)
    if array_match:
        name = array_match.group('name')
        array_suffix = normalize_space(array_match.group('arrays'))
        type_text = normalize_space(arg[:array_match.start('name')] + array_suffix)
    else:
        identifiers = list(IDENT_RE.finditer(arg))
        identifiers = [item for item in identifiers if item.group(0) not in {'const', 'volatile'}]
        if not identifiers:
            name = None
            type_text = arg
        elif len(identifiers) == 1 and identifiers[0].group(0) in ARGUMENT_TYPE_WORDS:
            name = None
            type_text = arg
        else:
            chosen = identifiers[-1]
            name = chosen.group(0)
            type_text = normalize_space((arg[:chosen.start()] + arg[chosen.end():]).strip())
            if not type_text:
                type_text = name
                name = None

    direction = direction or infer_argument_direction(name, type_text, raw)
    return {
        'name': name,
        'type': type_text,
        'default': default,
        'direction': direction,
        'description': '',
        'raw': raw,
    }


def infer_argument_direction(name, type_text, raw):
    raw_upper = raw.upper()
    if re.search(r'\b(IN_OUT|INOUT)\b', raw_upper):
        return 'input_output'
    if re.search(r'\b(OUT|OUTPUT|PRO_OUT)\b', raw_upper):
        return 'output'
    if re.search(r'\b(IN|INPUT|PRO_IN)\b', raw_upper):
        return 'input'

    lowered_name = (name or '').lower()
    pointer_like = '*' in raw or '&' in raw or '[]' in raw
    if pointer_like:
        if '**' in raw or re.search(r'(out|output|result|return|retval)', lowered_name):
            return 'output'
        if re.search(r'\bconst\b', type_text):
            return 'input'
        return 'input_output'
    return 'input'


def parse_function_signature(text, context_name=None):
    signature = cleanup_declaration_text(text)
    if not signature or '(' not in signature or ')' not in signature:
        return None
    if re.match(r'^(typedef|using|#|static_assert)\b', signature):
        return None

    span = find_parameter_span(signature)
    if not span:
        return None

    open_paren, close_paren, before, raw_name = span
    after = signature[close_paren + 1:].strip()
    params_text = signature[open_paren + 1:close_paren]
    simple_name = raw_name.split('::')[-1]
    qualified_hint = raw_name if '::' in raw_name else None
    return_type = before[:before.rfind(raw_name)].strip()
    return_type = normalize_space(return_type)

    if simple_name in CONTROL_NAMES:
        return None
    if re.match(r'^(if|for|while|switch|catch)\b', return_type):
        return None

    method_kind, return_type = classify_special_method(simple_name, return_type, context_name)
    qualifiers = parse_function_qualifiers(after)

    arguments = []
    for arg_text in split_top_level(params_text):
        argument = parse_argument(arg_text)
        if argument:
            arguments.append(argument)

    return {
        'name': simple_name,
        'qualified_hint': qualified_hint,
        'kind': method_kind,
        'return_type': return_type,
        'arguments': arguments,
        'qualifiers': qualifiers,
        'raw_signature': signature,
    }


def parse_function_qualifiers(after_params):
    qualifiers = []
    cleaned = after_params.strip()
    cleaned = re.sub(r'=\s*0\b', ' pure_virtual ', cleaned)
    for word in re.findall(r'[A-Za-z_]\w*', cleaned):
        if word in {'const', 'volatile', 'override', 'final', 'noexcept', 'pure_virtual'} and word not in qualifiers:
            qualifiers.append(word)
    return qualifiers


def apply_doc_to_arguments(arguments, doc):
    enriched = []
    for argument in arguments:
        argument = dict(argument)
        param_doc = doc['params'].get(argument.get('name') or '')
        if param_doc:
            if param_doc.get('direction'):
                argument['direction'] = param_doc['direction']
            argument['description'] = param_doc.get('description', '')
        enriched.append(argument)
    return enriched


def split_argument_directions(arguments):
    input_args = []
    output_args = []
    for argument in arguments:
        if argument['direction'] in {'input', 'input_output'}:
            input_args.append(argument)
        if argument['direction'] in {'output', 'input_output'}:
            output_args.append(argument)
    return input_args, output_args


def build_method_record(signature, doc, file_path, line, scope=None, access=None):
    arguments = apply_doc_to_arguments(signature['arguments'], doc)
    input_args, output_args = split_argument_directions(arguments)
    qualified_name = signature['qualified_hint'] or signature['name']
    if scope and '::' not in qualified_name:
        qualified_name = f'{scope}::{qualified_name}'

    record = {
        'name': signature['name'],
        'qualified_name': qualified_name,
        'scope': scope or 'global',
        'kind': signature['kind'],
        'file': file_path,
        'line': line,
        'description': doc['description'],
        'return_type': signature['return_type'],
        'return_value': {
            'type': signature['return_type'],
            'description': doc['returns'],
            'values': doc['return_values'],
        },
        'arguments': arguments,
        'input_arguments': input_args,
        'output_arguments': output_args,
        'qualifiers': signature['qualifiers'],
        'raw_signature': signature['raw_signature'],
    }
    if access:
        record['access'] = access
    return record


def parse_field_declaration(text):
    declaration = cleanup_declaration_text(text)
    if not declaration:
        return []
    if re.match(r'^(typedef|using|friend|static_assert|return|if|for|while|switch)\b', declaration):
        return []
    if '(' in declaration and ')' in declaration:
        return []

    declaration = declaration.rstrip(';').strip()
    parts = split_top_level(declaration)
    fields = []
    base_type = None

    for index, part in enumerate(parts):
        no_default, default = split_default_value(part.strip())
        bitfield = None
        if ':' in no_default and not re.search(r'::', no_default):
            before_bitfield, bitfield = no_default.rsplit(':', 1)
            no_default = before_bitfield.strip()
            bitfield = bitfield.strip()

        array_suffix = ''
        array_match = re.search(r'(?P<name>[A-Za-z_]\w*)\s*(?P<arrays>(?:\[[^\]]*\]\s*)+)$', no_default)
        if array_match:
            name = array_match.group('name')
            array_suffix = normalize_space(array_match.group('arrays'))
            type_text = normalize_space(no_default[:array_match.start('name')] + array_suffix)
        else:
            identifiers = list(IDENT_RE.finditer(no_default))
            if not identifiers:
                continue
            chosen = identifiers[-1]
            name = chosen.group(0)
            type_text = normalize_space((no_default[:chosen.start()] + no_default[chosen.end():]).strip())

        if index == 0:
            base_type = type_text
        elif base_type and not type_text:
            type_text = base_type
        elif base_type and type_text in {'*', '&'}:
            type_text = normalize_space(base_type + ' ' + type_text)

        if name in TYPE_NAME_STOPWORDS:
            continue
        fields.append({
            'name': name,
            'type': type_text,
            'default': default,
            'bitfield': bitfield,
            'description': '',
            'raw': normalize_space(part),
        })
    return fields


def build_access_markers(body_text, body_start_line, default_access):
    markers = [(body_start_line, default_access)]
    for offset, line in enumerate(body_text.splitlines()):
        for match in ACCESS_LABEL_RE.finditer(line):
            markers.append((body_start_line + offset, match.group(1)))
    return markers


def access_at_line(markers, line):
    access = markers[0][1]
    for marker_line, marker_access in markers:
        if marker_line <= line:
            access = marker_access
        else:
            break
    return access


def parse_integer_literal(token):
    token = token.strip()
    token = re.sub(r'(?i)(u|l|ul|lu|ull|llu)+$', '', token)
    if re.fullmatch(r'0[0-7]+', token):
        return int(token, 8)
    return int(token, 0)


def convert_c_integer_literals(expr):
    def replace(match):
        return str(parse_integer_literal(match.group(0)))

    return re.sub(r'\b(?:0[xX][0-9A-Fa-f]+|0[0-7]+|\d+)(?:[uUlL]+)?\b', replace, expr)


def convert_char_literals(expr):
    escapes = {
        r'\0': 0,
        r'\n': 10,
        r'\r': 13,
        r'\t': 9,
        r'\\': 92,
        r"\'": 39,
    }

    def replace(match):
        value = match.group(1)
        if value in escapes:
            return str(escapes[value])
        if len(value) == 1:
            return str(ord(value))
        return match.group(0)

    return re.sub(r"'(\\.|[^'])'", replace, expr)


class IntegerEvaluator(ast.NodeVisitor):
    BIN_OPS = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: int(a / b),
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.LShift: lambda a, b: a << b,
        ast.RShift: lambda a, b: a >> b,
        ast.BitOr: lambda a, b: a | b,
        ast.BitAnd: lambda a, b: a & b,
        ast.BitXor: lambda a, b: a ^ b,
    }
    UNARY_OPS = {
        ast.UAdd: lambda a: +a,
        ast.USub: lambda a: -a,
        ast.Invert: lambda a: ~a,
    }

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            return int(node.value)
        if isinstance(node.value, int):
            return node.value
        raise ValueError('not an integer')

    def visit_BinOp(self, node):
        op_type = type(node.op)
        if op_type not in self.BIN_OPS:
            raise ValueError('unsupported operator')
        return self.BIN_OPS[op_type](self.visit(node.left), self.visit(node.right))

    def visit_UnaryOp(self, node):
        op_type = type(node.op)
        if op_type not in self.UNARY_OPS:
            raise ValueError('unsupported unary operator')
        return self.UNARY_OPS[op_type](self.visit(node.operand))

    def generic_visit(self, node):
        raise ValueError(f'unsupported expression: {type(node).__name__}')


def safe_eval_int(expr, constants):
    if expr is None:
        return None
    expr = expr.strip()
    if not expr or '"' in expr:
        return None
    expr = re.sub(r'/\*.*?\*/', ' ', expr)
    expr = re.sub(r'//.*$', ' ', expr)
    expr = re.sub(r'\(\s*(?:unsigned|signed|long|short|int|char|size_t|[A-Za-z_]\w+_t)\s*\)', ' ', expr)
    expr = expr.replace('TRUE', '1').replace('FALSE', '0')
    expr = convert_char_literals(expr)
    try:
        expr = convert_c_integer_literals(expr)
    except ValueError:
        return None

    def replace_identifier(match):
        name = match.group(0)
        if name in constants and isinstance(constants[name], int):
            return str(constants[name])
        return name

    expr = IDENT_RE.sub(replace_identifier, expr)
    if re.search(r'[A-Za-z_]\w*', expr):
        return None

    try:
        parsed = ast.parse(expr, mode='eval')
        return IntegerEvaluator().visit(parsed)
    except Exception:
        return None


def extract_defines(code, comments, code_lines, file_path, constants):
    defines = []
    lines = code.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not re.match(r'\s*#\s*define\b', line):
            index += 1
            continue

        start_line = index + 1
        macro_lines = [line.rstrip()]
        while macro_lines[-1].endswith('\\') and index + 1 < len(lines):
            index += 1
            macro_lines[-1] = macro_lines[-1][:-1]
            macro_lines.append(lines[index].rstrip())

        macro_text = normalize_space(' '.join(macro_lines))
        match = re.match(r'#\s*define\s+(?P<name>[A-Za-z_]\w*)(?P<tail>.*)$', macro_text)
        if match:
            name = match.group('name')
            tail = match.group('tail')
            kind = 'object'
            args = []
            value = tail.strip()
            if tail.startswith('('):
                close = find_matching_delimiter(tail, 0, '(', ')')
                if close != -1:
                    kind = 'function'
                    args = [arg.strip() for arg in tail[1:close].split(',') if arg.strip()]
                    value = tail[close + 1:].strip()
            doc = parse_doc(nearest_comment_text(comments, code_lines, start_line))
            integer_value = safe_eval_int(value, constants)
            if integer_value is not None:
                constants[name] = integer_value
            defines.append({
                'name': name,
                'kind': kind,
                'file': file_path,
                'line': start_line,
                'description': doc['description'],
                'arguments': args,
                'value': value,
                'integer_value': integer_value,
            })

        index += 1
    return defines


def parse_enum_values(block, code, comments, code_lines, line_starts, constants):
    body = code[block['open_brace'] + 1:block['close_brace']]
    values = []
    previous_value = None
    local_constants = dict(constants)

    search_offset = block['open_brace'] + 1
    for item in split_top_level(body):
        raw = item.strip()
        if not raw:
            search_offset += len(item) + 1
            continue
        item_start = code.find(item, search_offset, block['close_brace'])
        if item_start == -1:
            item_start = search_offset
        line = line_for_offset(line_starts, item_start)

        match = re.match(r'(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?P<value>.*))?$', raw, re.DOTALL)
        if match:
            name = match.group('name')
            raw_value = normalize_space(match.group('value') or '')
            if raw_value:
                integer_value = safe_eval_int(raw_value, local_constants)
            elif previous_value is not None:
                integer_value = previous_value + 1
            else:
                integer_value = 0
            if integer_value is not None:
                previous_value = integer_value
                local_constants[name] = integer_value
                constants[name] = integer_value
            doc = parse_doc(nearest_comment_text(comments, code_lines, line, max_gap=1))
            values.append({
                'name': name,
                'raw_value': raw_value,
                'integer_value': integer_value,
                'description': doc['description'],
            })
        search_offset = item_start + len(item) + 1
    return values


def parse_class_or_struct_members(block, blocks, code, comments, code_lines, line_starts, file_path):
    body_start = block['open_brace'] + 1
    body_end = block['close_brace']
    body = code[body_start:body_end]
    child_spans = [
        (child['start'], child['end'])
        for child in blocks
        if child['parent_index'] == block['index']
    ]
    body_masked = mask_spans(body, child_spans, base_offset=body_start)
    body_start_line = line_for_offset(line_starts, body_start)
    default_access = 'private' if block['kind'] == 'class' else 'public'
    access_markers = build_access_markers(body, body_start_line, default_access)
    methods = []
    fields = []

    for segment in split_declarations_and_definitions(body_masked, base_offset=body_start):
        meaningful = first_meaningful_offset(segment['text'], segment['start'])
        line = line_for_offset(line_starts, meaningful)
        access = access_at_line(access_markers, line)
        doc = parse_doc(nearest_comment_text(comments, code_lines, line))

        signature = parse_function_signature(segment['text'], context_name=block['name'])
        if signature:
            methods.append(build_method_record(
                signature,
                doc,
                file_path,
                line,
                scope=block['qualified_name'],
                access=access,
            ))
            continue

        for field in parse_field_declaration(segment['text']):
            field['file'] = file_path
            field['line'] = line
            field['access'] = access
            if doc['description']:
                field['description'] = doc['description']
            fields.append(field)

    return methods, fields


def extract_global_methods(code, blocks, comments, code_lines, line_starts, file_path):
    type_spans = [(block['start'], block['end']) for block in blocks]
    masked = mask_spans(code, type_spans)
    masked = flatten_non_type_scopes(masked)
    methods = []
    seen = set()

    for segment in split_declarations_and_definitions(masked):
        meaningful = first_meaningful_offset(segment['text'], segment['start'])
        line = line_for_offset(line_starts, meaningful)
        doc = parse_doc(nearest_comment_text(comments, code_lines, line))
        signature = parse_function_signature(segment['text'])
        if not signature:
            continue
        key = (signature['qualified_hint'] or signature['name'], line)
        if key in seen:
            continue
        seen.add(key)
        methods.append(build_method_record(signature, doc, file_path, line))
    return methods


def build_type_records(blocks, code, comments, code_lines, line_starts, file_path, constants):
    classes = []
    structs = []
    unions = []
    enums = []

    children_by_parent = {}
    for block in blocks:
        children_by_parent.setdefault(block['parent_index'], []).append(block)

    for block in blocks:
        doc = parse_doc(nearest_comment_text(comments, code_lines, block['line']))
        nested_types = [
            {
                'kind': child['kind'],
                'name': child['name'],
                'qualified_name': child['qualified_name'],
            }
            for child in children_by_parent.get(block['index'], [])
        ]
        parent_scope = None
        if block['parent_index'] is not None:
            parent_scope = blocks[block['parent_index']]['qualified_name']

        if block['kind'] == 'enum':
            values = parse_enum_values(block, code, comments, code_lines, line_starts, constants)
            enums.append({
                'name': block['name'],
                'qualified_name': block['qualified_name'],
                'kind': 'enum',
                'file': file_path,
                'line': block['line'],
                'description': doc['description'],
                'scoped': block['scoped_enum'],
                'underlying_type': block['underlying_type'],
                'parent_scope': parent_scope,
                'aliases': block['aliases'],
                'values': values,
                'name_to_integer': {
                    value['name']: value['integer_value']
                    for value in values
                    if value['integer_value'] is not None
                },
            })
            continue

        methods, fields = parse_class_or_struct_members(
            block,
            blocks,
            code,
            comments,
            code_lines,
            line_starts,
            file_path,
        )
        record = {
            'name': block['name'],
            'qualified_name': block['qualified_name'],
            'kind': block['kind'],
            'file': file_path,
            'line': block['line'],
            'description': doc['description'],
            'aliases': block['aliases'],
            'hierarchy': {
                'parent_scope': parent_scope,
                'base_classes': block['base_classes'],
                'comment_hierarchy': doc['hierarchy'],
            },
            'fields': fields,
            'methods': methods,
            'nested_types': nested_types,
        }
        if block['kind'] == 'class':
            classes.append(record)
        elif block['kind'] == 'struct':
            structs.append(record)
        elif block['kind'] == 'union':
            unions.append(record)

    return classes, structs, unions, enums


def relative_file_path(path, root):
    try:
        return str(path.relative_to(root)).replace('\\', '/')
    except ValueError:
        return str(path).replace('\\', '/')


def extract_header_file(path, root):
    text, encoding = read_text(path)
    line_starts = build_line_starts(text)
    code, comments = strip_comments(text, line_starts)
    code_lines = code.splitlines()
    file_path = relative_file_path(path, root)
    constants = {}

    defines = extract_defines(code, comments, code_lines, file_path, constants)
    blocks = find_type_blocks(code, line_starts)
    classes, structs, unions, enums = build_type_records(
        blocks,
        code,
        comments,
        code_lines,
        line_starts,
        file_path,
        constants,
    )
    global_methods = extract_global_methods(code, blocks, comments, code_lines, line_starts, file_path)

    return {
        'path': file_path,
        'encoding': encoding,
        'counts': {
            'classes': len(classes),
            'structs': len(structs),
            'unions': len(unions),
            'enums': len(enums),
            'defines': len(defines),
            'global_methods': len(global_methods),
        },
        'classes': classes,
        'structs': structs,
        'unions': unions,
        'enums': enums,
        'defines': defines,
        'global_methods': global_methods,
    }


def merge_results(file_results, source_root):
    result = {
        'metadata': {
            'format_version': 1,
            'source_root': str(source_root),
            'extractor': 'headerfiles_extractor.py',
        },
        'summary': {
            'files': len(file_results),
            'classes': 0,
            'structs': 0,
            'unions': 0,
            'enums': 0,
            'defines': 0,
            'global_methods': 0,
            'class_methods': 0,
            'struct_methods': 0,
        },
        'files': [],
        'classes': [],
        'structs': [],
        'unions': [],
        'enums': [],
        'defines': [],
        'global_methods': [],
    }

    for file_result in file_results:
        result['files'].append({
            'path': file_result['path'],
            'encoding': file_result['encoding'],
            'counts': file_result['counts'],
        })
        for key in ('classes', 'structs', 'unions', 'enums', 'defines', 'global_methods'):
            result[key].extend(file_result[key])
            result['summary'][key] += len(file_result[key])
        result['summary']['class_methods'] += sum(len(item['methods']) for item in file_result['classes'])
        result['summary']['struct_methods'] += sum(len(item['methods']) for item in file_result['structs'])
    return result


def extract_headers(source, extensions=HEADER_EXTENSIONS, exclude_dirs=None):
    source = Path(source).resolve()
    exclude_dirs = set(exclude_dirs or [])
    root = source.parent if source.is_file() else source
    files = discover_header_files(source, extensions, exclude_dirs)
    file_results = [extract_header_file(path, root) for path in files]
    return merge_results(file_results, root)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Extract C/C++ header classes, structs, enums, defines, and functions into JSON.',
    )
    parser.add_argument('source', help='Header file or folder containing .h/.hpp files.')
    parser.add_argument(
        '-o',
        '--output',
        default='headerfiles_api.json',
        help='Output JSON file. Default: headerfiles_api.json',
    )
    parser.add_argument(
        '--extensions',
        nargs='+',
        default=list(HEADER_EXTENSIONS),
        help='Header extensions to scan. Default: .h .hh .hpp .hxx',
    )
    parser.add_argument(
        '--exclude-dir',
        action='append',
        default=[],
        help='Directory name to skip. Can be used multiple times.',
    )
    parser.add_argument(
        '--no-default-excludes',
        action='store_true',
        help='Do not skip common generated/dependency folders like venv and node_modules.',
    )
    parser.add_argument(
        '--stdout',
        action='store_true',
        help='Print JSON to stdout instead of writing --output.',
    )
    parser.add_argument(
        '--stats-only',
        action='store_true',
        help='Only print extraction counts; useful for a quick parser check.',
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    exclude_dirs = set(args.exclude_dir)
    if not args.no_default_excludes:
        exclude_dirs.update(DEFAULT_EXCLUDE_DIRS)

    result = extract_headers(args.source, args.extensions, exclude_dirs)

    if args.stats_only:
        print(json.dumps(result['summary'], indent=2))
        return 0

    if args.stdout:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    output_path = Path(args.output)
    if output_path.parent != Path('.'):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"Extracted {result['summary']['files']} header files to {output_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
