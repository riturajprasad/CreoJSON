"""
Creo Toolkit HTML documentation extractor.

Converts the local Creo Toolkit online_help HTML files into a structured
JSON database covering functions, objects, categories, and user-guide topics.

Usage:
    python htmlfile_extractor.py <online_help_root> [-o output.json]
    python htmlfile_extractor.py "C:/PTC/Creo 12.4.1.0/Common Files/protoolkit/online_help"

Output sections:
    metadata     – extraction info
    summary      – file / entry counts
    categories   – API category index (Objects + Functions per category)
    objects      – ProXxx object descriptions, inheritance, function lists
    functions    – full function signatures with parameters and return codes
    user_guide   – user-guide topic titles and content
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    print("ERROR: BeautifulSoup4 is required.  pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

HTML_EXTENSIONS = ('.html', '.htm')
DEFAULT_EXCLUDE_DIRS = {'.git', '__pycache__', 'node_modules', 'css', 'scripts',
                        'images', 'wwhelp', 'connect'}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '')).strip()


def read_html(path: Path) -> str:
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors='replace')


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace('\\', '/')
    except ValueError:
        return str(path).replace('\\', '/')


def soup_text(tag) -> str:
    return normalize(tag.get_text()) if tag else ''


# ──────────────────────────────────────────────────────────────────────────────
# Page-type detection
# ──────────────────────────────────────────────────────────────────────────────

PAGE_FUNCTION = 'function'
PAGE_OBJECT   = 'object'
PAGE_CATEGORY = 'category'
PAGE_USERGUIDE = 'user_guide'
PAGE_OTHER    = 'other'


def detect_page_type(soup: BeautifulSoup, rel_p: str) -> str:
    # Path-based routing takes priority — user_guide pages have arbitrary titles.
    if 'user_guide' in rel_p or 'get_started' in rel_p:
        return PAGE_USERGUIDE

    title_tag = soup.find('title')
    title = normalize(title_tag.get_text()) if title_tag else ''

    if 'api/dita' in rel_p:
        if title.startswith('Function ') or title.startswith('Callback '):
            return PAGE_FUNCTION
        if title.startswith('Object '):
            return PAGE_OBJECT
        if soup.find(class_='Heading_1'):
            return PAGE_CATEGORY
        return PAGE_OTHER

    # Non-dita API pages at the creo_toolkit root level
    if title.startswith('Function ') or title.startswith('Callback '):
        return PAGE_FUNCTION
    if title.startswith('Object '):
        return PAGE_OBJECT

    return PAGE_OTHER


# ──────────────────────────────────────────────────────────────────────────────
# Breadcrumb parser  →  {'category': str, 'object': str}
# ──────────────────────────────────────────────────────────────────────────────

def parse_breadcrumb(soup: BeautifulSoup) -> dict:
    crumbs_div = soup.find(class_='ww_skin_breadcrumbs')
    if not crumbs_div:
        return {'category': '', 'object': ''}
    links = [normalize(a.get_text()) for a in crumbs_div.find_all('a')]
    # links: ['API Documentation', '<category>', '<object>']  (1–3 items)
    result = {'category': '', 'object': ''}
    if len(links) >= 2:
        result['category'] = links[1]
    if len(links) >= 3:
        result['object'] = links[2].replace('Object ', '')
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Synopsis / parameter parser  (function pages)
# ──────────────────────────────────────────────────────────────────────────────

def _find_section(soup: BeautifulSoup, section_name: str):
    """Return the next sibling element after a Section_Title whose text matches."""
    for div in soup.find_all(class_='Section_Title'):
        if normalize(div.get_text()) == section_name:
            return div
    return None


def parse_include_header(soup: BeautifulSoup) -> str:
    pre = soup.find(class_='Preformatted')
    if not pre:
        return ''
    text = normalize(pre.get_text())
    m = re.search(r'#include\s*[<"]([^>"]+)[>"]', text)
    return m.group(1) if m else text


def parse_synopsis_table(soup: BeautifulSoup) -> dict:
    """
    Parse the synopsis noborders table for return type, function name,
    and parameters.

    Table structure:
      Row 0: [return_type] [function_name]
      Row 1: []            [(  param_type param_name  /* (Dir) desc */  ...  )]
    """
    synopsis_div = _find_section(soup, 'Synopsis')
    if not synopsis_div:
        return {'return_type': '', 'name': '', 'parameters': []}

    # Find the next table sibling after the synopsis section title
    table = None
    for sibling in synopsis_div.next_siblings:
        if isinstance(sibling, Tag):
            t = sibling.find('table')
            if t:
                table = t
                break
            if sibling.name == 'table':
                table = sibling
                break

    if not table:
        return {'return_type': '', 'name': '', 'parameters': []}

    rows = table.find_all('tr')
    if not rows:
        return {'return_type': '', 'name': '', 'parameters': []}

    # Row 0: return type (col 0) and function name (col 1)
    cells_row0 = rows[0].find_all('td')
    return_type = normalize(cells_row0[0].get_text()) if len(cells_row0) > 0 else ''
    func_name   = normalize(cells_row0[1].get_text()) if len(cells_row0) > 1 else ''

    # Row 1 col 1: all Table_Cell divs form the parameter block
    parameters = []
    if len(rows) > 1:
        cells_row1 = rows[1].find_all('td')
        param_cell = cells_row1[1] if len(cells_row1) > 1 else (cells_row1[0] if cells_row1 else None)
        if param_cell:
            divs = [normalize(d.get_text()) for d in param_cell.find_all(class_='Table_Cell')]
            parameters = _parse_param_divs(divs)

    return {'return_type': return_type, 'name': func_name, 'parameters': parameters}


def _parse_param_divs(divs: list) -> list:
    """
    Parse a flat list of Table_Cell texts into parameter dicts.

    Pattern per parameter (after opening '('):
        <type_and_name>        e.g. 'ProMdl mdl'  or 'wchar_t* name'
        /* (In)                direction token
        description text
        */
    """
    params = []
    i = 0
    # Skip leading '('
    while i < len(divs) and divs[i] in ('(', ''):
        i += 1

    while i < len(divs):
        token = divs[i]
        if token in (')', ''):
            i += 1
            continue

        # A parameter declaration line: does NOT start with '/*'
        if token.startswith('/*') or token.startswith('*/'):
            i += 1
            continue

        decl = token
        direction = ''
        description = ''
        i += 1

        # Next should be /* (Dir)
        if i < len(divs) and divs[i].startswith('/*'):
            dir_text = divs[i]
            m = re.search(r'\(([^)]+)\)', dir_text)
            direction = m.group(1).strip() if m else ''
            i += 1

        # Collect description lines until '*/'
        desc_parts = []
        while i < len(divs) and not divs[i].startswith('*/'):
            if divs[i]:
                desc_parts.append(divs[i])
            i += 1
        description = ' '.join(desc_parts)
        if i < len(divs) and divs[i].startswith('*/'):
            i += 1

        param_type, param_name = _split_type_name(decl)
        if param_type or param_name:
            params.append({
                'type': param_type,
                'name': param_name,
                'direction': direction,
                'description': description,
            })

    return params


def _split_type_name(decl: str) -> tuple:
    """Split 'ProMdl mdl' → ('ProMdl', 'mdl'), 'wchar_t* name' → ('wchar_t*', 'name')."""
    parts = decl.rsplit(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    if len(parts) == 1:
        # Could be just a type with no variable name (e.g. 'void')
        return parts[0].strip(), ''
    return '', ''


# ──────────────────────────────────────────────────────────────────────────────
# Returns / error-code parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_returns(soup: BeautifulSoup) -> list:
    """
    Returns section: noborders table where col 0 = error code, col 1 = message.
    """
    returns_div = _find_section(soup, 'Returns')
    if not returns_div:
        return []

    table = None
    for sibling in returns_div.next_siblings:
        if isinstance(sibling, Tag):
            t = sibling.find('table')
            if t:
                table = t
                break
            if sibling.name == 'table':
                table = sibling
                break

    if not table:
        return []

    results = []
    for row in table.find_all('tr'):
        cells = row.find_all('td')
        if len(cells) >= 2:
            code = normalize(cells[0].get_text())
            msg  = normalize(cells[1].get_text())
            if code:
                results.append({'code': code, 'description': msg})
    return results


# ──────────────────────────────────────────────────────────────────────────────
# OTK replacement parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_otk_replacement(soup: BeautifulSoup) -> str:
    """Extract 'Replacement in Object TOOLKIT' value if present."""
    for div in soup.find_all(class_='Section_Title'):
        if 'Replacement' in normalize(div.get_text()):
            # Find next table
            for sib in div.next_siblings:
                if isinstance(sib, Tag):
                    t = sib.find('table')
                    if t:
                        rows = t.find_all('tr')
                        if rows:
                            cells = rows[0].find_all('td')
                            if len(cells) >= 2:
                                return normalize(cells[1].get_text())
                        break
    return ''


# ──────────────────────────────────────────────────────────────────────────────
# User guide reference parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_user_guide_refs(soup: BeautifulSoup) -> list:
    refs_div = _find_section(soup, 'User Guide References')
    if not refs_div:
        return []
    refs = []
    for sib in refs_div.next_siblings:
        if isinstance(sib, Tag):
            text = normalize(sib.get_text())
            if not text:
                continue
            cls = sib.get('class', [])
            if isinstance(cls, list):
                cls = ' '.join(cls)
            if 'Definition_Term' in cls or 'Body' in cls:
                refs.append(text)
            elif 'Section_Title' in cls or 'Figure' in cls:
                break
    return refs


# ──────────────────────────────────────────────────────────────────────────────
# Body-text extractor (description, first Body div after a Section_Title)
# ──────────────────────────────────────────────────────────────────────────────

def get_section_body(soup: BeautifulSoup, section_name: str) -> str:
    sec = _find_section(soup, section_name)
    if not sec:
        return ''
    for sib in sec.next_siblings:
        if isinstance(sib, Tag):
            cls = sib.get('class', [])
            if isinstance(cls, list):
                cls = ' '.join(cls)
            if 'Body' in cls or 'Section_Body' in cls:
                return normalize(sib.get_text())
            if 'Section_Title' in cls or 'Figure' in cls:
                break
    return ''


# ──────────────────────────────────────────────────────────────────────────────
# Page-type specific parsers
# ──────────────────────────────────────────────────────────────────────────────

def parse_function_page(soup: BeautifulSoup, rel_p: str) -> dict:
    title_tag = soup.find('title')
    raw_title = normalize(title_tag.get_text()) if title_tag else ''
    func_kind = 'Callback' if raw_title.startswith('Callback') else 'Function'
    name = re.sub(r'^(Function|Callback)\s+', '', raw_title).strip()

    breadcrumb = parse_breadcrumb(soup)
    description = get_section_body(soup, 'Description')
    include_header = parse_include_header(soup)
    synopsis = parse_synopsis_table(soup)
    returns = parse_returns(soup)
    otk = parse_otk_replacement(soup)
    ug_refs = parse_user_guide_refs(soup)

    return {
        'kind': func_kind,
        'name': name or synopsis.get('name', ''),
        'source_file': rel_p,
        'category': breadcrumb['category'],
        'object': breadcrumb['object'],
        'description': description,
        'include_header': include_header,
        'return_type': synopsis.get('return_type', ''),
        'parameters': synopsis.get('parameters', []),
        'returns': returns,
        'otk_replacement': otk,
        'user_guide_refs': ug_refs,
    }


def parse_object_page(soup: BeautifulSoup, rel_p: str) -> dict:
    title_tag = soup.find('title')
    name = normalize(title_tag.get_text()).replace('Object ', '').strip() if title_tag else ''
    breadcrumb = parse_breadcrumb(soup)

    # Description: first Body div after Heading_2
    description = ''
    h2 = soup.find(class_='Heading_2')
    if h2:
        for sib in h2.next_siblings:
            if isinstance(sib, Tag):
                cls = ' '.join(sib.get('class', []))
                if 'Body' in cls:
                    text = normalize(sib.get_text())
                    if text and not text.startswith('•'):
                        description = text
                        break
                if 'Heading' in cls:
                    break

    def get_links_after(section_title: str) -> list:
        sec = _find_section(soup, section_title)
        if not sec:
            return []
        links = []
        for sib in sec.next_siblings:
            if isinstance(sib, Tag):
                cls = ' '.join(sib.get('class', []))
                if 'Body' in cls:
                    for a in sib.find_all('a', title=True):
                        title = a.get('title', '')
                        clean = re.sub(r'^(Object|Function|Callback)\s+', '', title).strip()
                        if clean:
                            links.append(clean)
                if 'Section_Title' in cls or 'Figure' in cls:
                    break
        return links

    superobjects = get_links_after('Superobjects:')
    attribute_of = get_links_after('This object is an attribute of the following objects:')

    # Functions: find Section_Title containing 'Functions:'
    own_functions = []
    inherited_functions = []
    for div in soup.find_all(class_='Section_Title'):
        text = normalize(div.get_text())
        if 'Functions:' in text and 'inherited' not in text.lower():
            for sib in div.next_siblings:
                if isinstance(sib, Tag):
                    cls = ' '.join(sib.get('class', []))
                    if 'Body' in cls:
                        for a in sib.find_all('a', title=True):
                            t = normalize(a.get('title', ''))
                            n = re.sub(r'^(Function|Callback)\s+', '', t).strip()
                            if n:
                                own_functions.append(n)
                    if 'Section_Title' in cls or 'Figure' in cls:
                        break
        elif 'inherited' in text.lower() and 'Functions' in text:
            for sib in div.next_siblings:
                if isinstance(sib, Tag):
                    cls = ' '.join(sib.get('class', []))
                    if 'Body' in cls:
                        for a in sib.find_all('a', title=True):
                            t = normalize(a.get('title', ''))
                            n = re.sub(r'^(Function|Callback)\s+', '', t).strip()
                            if n:
                                inherited_functions.append(n)
                    if 'Section_Title' in cls or 'Figure' in cls:
                        break

    return {
        'name': name,
        'source_file': rel_p,
        'category': breadcrumb['category'],
        'description': description,
        'superobjects': superobjects,
        'attribute_of': attribute_of,
        'functions': own_functions,
        'inherited_functions': inherited_functions,
    }


def parse_category_page(soup: BeautifulSoup, rel_p: str) -> dict:
    title_tag = soup.find('title')
    name = normalize(title_tag.get_text()) if title_tag else ''

    functions = []
    objects = []
    for div in soup.find_all(class_='Section_Title'):
        section = normalize(div.get_text())
        target = None
        if section == 'Functions':
            target = functions
        elif section == 'Objects':
            target = objects
        else:
            continue
        for sib in div.next_siblings:
            if isinstance(sib, Tag):
                cls = ' '.join(sib.get('class', []))
                if 'List_1' in cls or 'Body' in cls:
                    for a in sib.find_all('a', title=True):
                        t = normalize(a.get('title', ''))
                        n = re.sub(r'^(Function|Callback|Object)\s+', '', t).strip()
                        if n:
                            target.append(n)
                if 'Section_Title' in cls or 'Heading' in cls:
                    break

    return {
        'name': name,
        'source_file': rel_p,
        'functions': functions,
        'objects': objects,
    }


def parse_userguide_page(soup: BeautifulSoup, rel_p: str) -> dict:
    title_tag = soup.find('title')
    title = normalize(title_tag.get_text()) if title_tag else ''

    # Remove boilerplate elements
    for tag in soup(['script', 'style', 'header', 'footer', 'nav']):
        tag.decompose()

    content_div = soup.find(id='page_content')
    if not content_div:
        content_div = soup.find(id='page_content_container') or soup.body

    headings = []
    paragraphs = []
    code_blocks = []

    if content_div:
        for tag in content_div.find_all(True):
            cls = ' '.join(tag.get('class', []))
            text = normalize(tag.get_text())
            if not text:
                continue
            if any(c in cls for c in ('Heading_1', 'Heading_2', 'Heading_3')):
                headings.append(text)
            elif 'Preformatted' in cls or tag.name in ('pre', 'code'):
                code_blocks.append(text)
            elif 'Body' in cls and tag.name == 'div':
                paragraphs.append(text)

    return {
        'title': title,
        'source_file': rel_p,
        'headings': headings[:20],
        'paragraphs': paragraphs[:30],
        'code_blocks': code_blocks[:20],
    }


# ──────────────────────────────────────────────────────────────────────────────
# File discovery
# ──────────────────────────────────────────────────────────────────────────────

def discover_html_files(source: Path, exclude_dirs: set) -> list:
    if source.is_file():
        return [source]
    files = []
    for path in source.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in HTML_EXTENSIONS:
            continue
        if any(part in exclude_dirs for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


# ──────────────────────────────────────────────────────────────────────────────
# Main extractor
# ──────────────────────────────────────────────────────────────────────────────

def extract_all(source: Path, exclude_dirs: set, verbose: bool = False) -> dict:
    root = source if source.is_dir() else source.parent
    files = discover_html_files(source, exclude_dirs)

    categories  = []
    objects     = []
    functions   = []
    user_guide  = []
    skipped     = 0

    total = len(files)
    for i, path in enumerate(files):
        rp = rel_path(path, root)
        if verbose and i % 500 == 0:
            print(f"  [{i}/{total}] {rp}", flush=True)

        try:
            html = read_html(path)
            soup = BeautifulSoup(html, 'html.parser')
            page_type = detect_page_type(soup, rp)

            if page_type == PAGE_FUNCTION:
                functions.append(parse_function_page(soup, rp))
            elif page_type == PAGE_OBJECT:
                objects.append(parse_object_page(soup, rp))
            elif page_type == PAGE_CATEGORY:
                categories.append(parse_category_page(soup, rp))
            elif page_type == PAGE_USERGUIDE:
                user_guide.append(parse_userguide_page(soup, rp))
            else:
                skipped += 1
        except Exception as exc:
            if verbose:
                print(f"  WARNING: {rp}: {exc}", file=sys.stderr)
            skipped += 1

    # Sort for stable output
    functions.sort(key=lambda x: x['name'])
    objects.sort(key=lambda x: x['name'])
    categories.sort(key=lambda x: x['name'])
    user_guide.sort(key=lambda x: x['title'])

    return {
        'metadata': {
            'format_version': 2,
            'source_root': str(root),
            'extractor': 'htmlfile_extractor.py',
        },
        'summary': {
            'total_files_scanned': total,
            'functions': len(functions),
            'objects': len(objects),
            'categories': len(categories),
            'user_guide': len(user_guide),
            'skipped': skipped,
        },
        'categories': categories,
        'objects': objects,
        'functions': functions,
        'user_guide': user_guide,
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv):
    p = argparse.ArgumentParser(
        description='Extract Creo Toolkit HTML documentation into JSON.',
    )
    p.add_argument('source',
                   help='Path to the Creo online_help root directory (or a single HTML file).')
    p.add_argument('-o', '--output', default='creo_toolkit_api.json',
                   help='Output JSON file. Default: creo_toolkit_api.json')
    p.add_argument('--exclude-dir', action='append', default=[],
                   metavar='DIR', help='Directory name to skip (repeatable).')
    p.add_argument('--no-default-excludes', action='store_true',
                   help='Do not apply the built-in exclude list.')
    p.add_argument('--stdout', action='store_true',
                   help='Print JSON to stdout.')
    p.add_argument('--stats-only', action='store_true',
                   help='Print summary counts only.')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Print progress while scanning.')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    source = Path(args.source).resolve()

    exclude = set(args.exclude_dir)
    if not args.no_default_excludes:
        exclude.update(DEFAULT_EXCLUDE_DIRS)

    print(f"Scanning {source} …", flush=True)
    result = extract_all(source, exclude, verbose=args.verbose)

    if args.stats_only:
        print(json.dumps(result['summary'], indent=2))
        return 0

    payload = json.dumps(result, indent=2, ensure_ascii=False)

    if args.stdout:
        print(payload)
        return 0

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding='utf-8')
    s = result['summary']
    print(f"Done: {s['functions']} functions, {s['objects']} objects, "
          f"{s['categories']} categories, {s['user_guide']} user-guide pages -> {out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
