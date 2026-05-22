import argparse
from collections import defaultdict
import json
import re

import fitz  # PyMuPDF

PDF_FILE = "tkuse11.pdf"
OUTPUT_FILE = "new_creo_api.json"

FUNCTION_NAME_RE = re.compile(r'\bPro[A-Z][A-Za-z0-9_]*(?:\(\))?')
SECTION_HEADINGS = {
    'DESCRIPTION': 'description',
    'PARAMETERS': 'parameters',
    'INPUTS': 'parameters',
    'OUTPUTS': 'returns',
    'RETURNS': 'returns',
    'RETURN': 'returns',
    'EXAMPLE': 'example',
}
NOISE_LINE_RE = re.compile(r'^(?:page\s*\d+|\d+\s+creo|creo\s+toolkit|user\s*\'s?\s*guide|\d+)$', re.IGNORECASE)
SYMBOL_RE = re.compile(r'[\u2000-\u206F\u2E00-\u2E7F\u25A0-\u25FF\u00A0]')


def title_key(text):
    return normalize_line(text).casefold()


def normalize_line(text):
    text = text.strip()
    text = SYMBOL_RE.sub(' ', text)
    text = text.replace('\ufeff', ' ')
    text = re.sub(r'^(Pro[A-Z][A-Za-z0-9_]*)(?:\(\))?\s+\1\(\)', r'\1()', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def build_outline_entries(doc):
    entries = []
    stack = []

    for index, (level, title, page_number) in enumerate(doc.get_toc(simple=True)):
        title = normalize_line(title)
        if not title or page_number < 1:
            continue

        while len(stack) >= level:
            stack.pop()
        stack.append(title)

        entries.append({
            'index': index,
            'level': level,
            'title': title,
            'page': page_number,
            'access_chain': stack.copy(),
        })

    return entries


def build_page_outline_state(entries, page_count):
    page_default_chains = {}
    page_start_indexes = {}
    previous_page_index = -1
    latest_chain = []
    next_previous = 0
    next_latest = 0

    for page_number in range(1, page_count + 1):
        while next_previous < len(entries) and entries[next_previous]['page'] < page_number:
            previous_page_index = entries[next_previous]['index']
            next_previous += 1
        page_start_indexes[page_number] = previous_page_index

        while next_latest < len(entries) and entries[next_latest]['page'] <= page_number:
            latest_chain = entries[next_latest]['access_chain']
            next_latest += 1
        page_default_chains[page_number] = latest_chain.copy()

    return page_default_chains, page_start_indexes


class OutlineTracker:
    def __init__(self, doc):
        self.entries = build_outline_entries(doc)
        self.entries_by_page_and_title = defaultdict(list)
        for entry in self.entries:
            self.entries_by_page_and_title[(entry['page'], title_key(entry['title']))].append(entry)

        self.page_default_chains, self.page_start_indexes = build_page_outline_state(
            self.entries,
            doc.page_count,
        )
        self.current_page = 1
        self.current_index = -1
        self.current_access_chain = []
        self.recent_lines = []

    def start_page(self, page_number):
        self.current_page = page_number
        self.current_index = self.page_start_indexes.get(page_number, -1)
        self.current_access_chain = self.page_default_chains.get(page_number, []).copy()
        self.recent_lines = []

    def observe(self, text):
        self.recent_lines.append(text)
        self.recent_lines = self.recent_lines[-3:]

        for count in range(len(self.recent_lines), 0, -1):
            candidate = title_key(' '.join(self.recent_lines[-count:]))
            matches = self.entries_by_page_and_title.get((self.current_page, candidate), [])
            next_match = next(
                (entry for entry in matches if entry['index'] > self.current_index),
                None,
            )
            if next_match:
                self.current_index = next_match['index']
                self.current_access_chain = next_match['access_chain'].copy()
                break

        return self.current_access_chain.copy()


def is_noise_line(text):
    if not text:
        return True
    if NOISE_LINE_RE.match(text):
        return True
    text_lower = text.lower()
    if 'creo' in text_lower and 'toolkit' in text_lower:
        return True
    if text.isdigit() and len(text) < 5:
        return True
    return False


def extract_function_names(text):
    return [match.group(0).rstrip('()') for match in FUNCTION_NAME_RE.finditer(text)]


def get_section_heading(text):
    normalized = text.strip()
    if normalized.lower() == 'new function':
        return 'ignore'
    key = normalized.upper().split(':')[0].strip()
    return SECTION_HEADINGS.get(key)


def is_new_function_start(text, prev_text=''):
    names = extract_function_names(text)
    if not names:
        return False

    lower = text.lower()
    if 'the function' in lower or 'use the function' in lower:
        return True
    if 'function' in lower and text.strip().startswith('pro'):
        return True

    stripped = text.strip()
    if re.fullmatch(r'(?:Pro[A-Z][A-Za-z0-9_]*\(\))(?:\s*(?:and|,|&)?\s*Pro[A-Z][A-Za-z0-9_]*\(\))*', stripped):
        return True

    if stripped.startswith(names[0] + '()'):
        remainder = stripped[len(names[0] + '()'):].strip()
        if not remainder:
            return True
        if remainder[0].isupper() or remainder.startswith('function'):
            return True
        if prev_text and not prev_text.rstrip().endswith(('.', '?', '!', ':', ';')):
            return False
        return True

    return False


def append_current_api(apis, current, current_names):
    for name in current_names:
        apis.append({
            'function': name,
            'description': current['description'].strip(),
            'parameters': current['parameters'],
            'returns': current['returns'].strip(),
            'example': current['example'].strip(),
            'page': current['page'],
            'access_chain': current['access_chain'],
        })


def extract_api_from_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    outline_tracker = OutlineTracker(doc)
    lines = []

    for page_number, page in enumerate(doc, start=1):
        outline_tracker.start_page(page_number)
        raw_text = page.get_text('text')
        for line in raw_text.splitlines():
            normalized = normalize_line(line)
            if not normalized:
                continue
            access_chain = outline_tracker.observe(normalized)
            if is_noise_line(normalized):
                continue
            lines.append({
                'text': normalized,
                'page': page_number,
                'access_chain': access_chain,
            })

    apis = []
    current = None
    current_names = []
    current_section = 'description'
    prev_line = ''

    for item in lines:
        line = item['text']
        heading = get_section_heading(line)
        if heading == 'ignore':
            prev_line = line
            continue
        if heading:
            current_section = heading
            prev_line = line
            continue

        if current and is_new_function_start(line, prev_line):
            append_current_api(apis, current, current_names)
            current = None
            current_names = []

        if not current and is_new_function_start(line, prev_line):
            names = extract_function_names(line)
            current_names = names
            current = {
                'description': '',
                'parameters': [],
                'returns': '',
                'example': '',
                'page': item['page'],
                'access_chain': item['access_chain'],
            }
            current_section = 'description'
            current['description'] += line + ' '
            prev_line = line
            continue

        if current:
            if current_section == 'description':
                current['description'] += line + ' '
            elif current_section == 'returns':
                current['returns'] += line + ' '
            elif current_section == 'example':
                current['example'] += line + '\n'
            elif current_section == 'parameters':
                parts = re.split(r'\s+-\s+|:\s+|\s{2,}', line, maxsplit=1)
                if len(parts) == 2:
                    name, description = parts[0].strip(), parts[1].strip()
                else:
                    tokens = line.split(None, 1)
                    name = tokens[0]
                    description = tokens[1].strip() if len(tokens) == 2 else ''
                current['parameters'].append({'name': name, 'description': description})

        prev_line = line

    if current:
        append_current_api(apis, current, current_names)

    return apis


def main():
    parser = argparse.ArgumentParser(description='Extract Creo TOOLKIT API documentation from PDF into JSON.')
    parser.add_argument('--pdf', default=PDF_FILE, help='Path to the PDF file.')
    parser.add_argument('--output', default=OUTPUT_FILE, help='Path to the output JSON file.')
    args = parser.parse_args()

    apis = extract_api_from_pdf(args.pdf)

    with open(args.output, 'w', encoding='utf-8') as output_file:
        json.dump(apis, output_file, indent=2, ensure_ascii=False)

    print(f'Extracted {len(apis)} APIs to {args.output}')


if __name__ == '__main__':
    main()
