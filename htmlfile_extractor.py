import argparse
import json
import re
import sys
from pathlib import Path
from html.parser import HTMLParser

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


HTML_EXTENSIONS = ('.html', '.htm')
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


def normalize_space(text):
    """Normalize whitespace in text."""
    return re.sub(r'\s+', ' ', text or '').strip()


def read_text(path):
    """Read file with multiple encoding attempts."""
    for encoding in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    return path.read_text(errors='replace'), 'unknown'


def discover_html_files(source, extensions, exclude_dirs):
    """Discover HTML files in source directory or return single file."""
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


def extract_text_from_html(html_content):
    """Extract plain text from HTML content."""
    if not BeautifulSoup:
        # Fallback: basic HTML tag removal
        text = re.sub(r'<[^>]+>', ' ', html_content)
        return normalize_space(text)
    
    soup = BeautifulSoup(html_content, 'html.parser')
    # Remove script and style elements
    for script in soup(['script', 'style']):
        script.decompose()
    text = soup.get_text()
    return normalize_space(text)


def extract_headings(html_content):
    """Extract headings from HTML."""
    if not BeautifulSoup:
        headings = re.findall(r'<h[1-6][^>]*>([^<]+)</h[1-6]>', html_content, re.IGNORECASE)
        return [normalize_space(h) for h in headings]
    
    soup = BeautifulSoup(html_content, 'html.parser')
    headings = []
    for h in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        text = normalize_space(h.get_text())
        if text:
            headings.append(text)
    return headings


def extract_tables_data(html_content):
    """Extract data from HTML tables."""
    if not BeautifulSoup:
        return []
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        tables_data = []
        
        for table in soup.find_all('table'):
            rows = []
            # Get headers
            headers = []
            for th in table.find_all('th'):
                headers.append(normalize_space(th.get_text()))
            
            # Get rows
            for tr in table.find_all('tr'):
                cells = []
                for td in tr.find_all(['td', 'th']):
                    cells.append(normalize_space(td.get_text()))
                if cells:
                    rows.append(cells)
            
            if headers or rows:
                tables_data.append({
                    'headers': headers,
                    'rows': rows,
                })
        
        return tables_data
    except Exception:
        return []


def extract_code_blocks(html_content):
    """Extract code blocks from HTML."""
    if not BeautifulSoup:
        # Fallback: extract from <code> or <pre> tags
        codes = re.findall(r'<(?:code|pre)[^>]*>([^<]+)</(?:code|pre)>', html_content, re.IGNORECASE)
        return [normalize_space(c) for c in codes]
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        code_blocks = []
        
        for code in soup.find_all(['code', 'pre']):
            text = code.get_text()
            text = normalize_space(text)
            if text:
                code_blocks.append(text)
        
        return code_blocks
    except Exception:
        return []


def extract_paragraphs(html_content):
    """Extract paragraphs from HTML."""
    if not BeautifulSoup:
        # Fallback: extract from <p> tags
        paragraphs = re.findall(r'<p[^>]*>([^<]+(?:<[^/>]*>[^<]*)*)</p>', html_content, re.IGNORECASE)
        return [normalize_space(p) for p in paragraphs if normalize_space(p)]
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        paragraphs = []
        
        for p in soup.find_all('p'):
            text = normalize_space(p.get_text())
            if text:
                paragraphs.append(text)
        
        return paragraphs
    except Exception:
        return []


def extract_lists(html_content):
    """Extract lists from HTML."""
    if not BeautifulSoup:
        return []
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        lists_data = []
        
        for ul in soup.find_all(['ul', 'ol']):
            items = []
            for li in ul.find_all('li', recursive=False):
                text = normalize_space(li.get_text())
                if text:
                    items.append(text)
            if items:
                lists_data.append({
                    'type': ul.name,
                    'items': items,
                })
        
        return lists_data
    except Exception:
        return []


def parse_function_signature(text):
    """Parse function signature from text."""
    # Match patterns like: function_name(param1, param2) or void function_name(...)
    match = re.search(r'(?:[\w:<>*&\s]+\s+)?(\w+)\s*\(([^)]*)\)', text)
    if match:
        func_name = match.group(1)
        params_str = match.group(2)
        
        # Parse parameters
        params = []
        if params_str.strip() and params_str.lower() != 'void':
            for param in re.split(r',', params_str):
                param = normalize_space(param)
                if param:
                    # Try to extract type and name
                    parts = param.rsplit(None, 1)
                    if len(parts) == 2:
                        params.append({
                            'name': parts[1],
                            'type': parts[0],
                            'description': '',
                        })
                    else:
                        params.append({
                            'name': param,
                            'type': '',
                            'description': '',
                        })
        
        return {
            'name': func_name,
            'parameters': params,
            'return_type': '',
            'description': '',
        }
    return None


def extract_apis_from_content(html_content):
    """Extract API definitions from HTML content."""
    apis = []
    
    # Try to find structured API information
    code_blocks = extract_code_blocks(html_content)
    for code in code_blocks:
        func = parse_function_signature(code)
        if func:
            apis.append(func)
    
    # Extract from tables (common in API docs)
    tables = extract_tables_data(html_content)
    for table in tables:
        if table['headers']:
            # Check if this looks like a parameters/API table
            header_lower = [h.lower() for h in table['headers']]
            if any(keyword in header_lower for keyword in ['parameter', 'param', 'name', 'type']):
                apis.append({
                    'name': 'Table_Data',
                    'parameters': [],
                    'table': table,
                    'description': 'Parameters table extracted from HTML',
                })
    
    return apis


def extract_html_file(path, root):
    """Extract API information from a single HTML file."""
    text, encoding = read_text(path)
    file_path = relative_file_path(path, root)
    
    # Extract various components
    headings = extract_headings(text)
    paragraphs = extract_paragraphs(text)
    code_blocks = extract_code_blocks(text)
    tables = extract_tables_data(text)
    lists = extract_lists(text)
    apis = extract_apis_from_content(text)
    plain_text = extract_text_from_html(text)
    
    # Extract any API definitions and descriptions
    methods = []
    for heading in headings:
        # Try to match function-like names in headings
        if '(' in heading and ')' in heading:
            func = parse_function_signature(heading)
            if func:
                func['description'] = paragraphs[0] if paragraphs else heading
                methods.append(func)
    
    # Build return structure
    result = {
        'path': file_path,
        'encoding': encoding,
        'title': headings[0] if headings else 'Untitled',
        'counts': {
            'headings': len(headings),
            'paragraphs': len(paragraphs),
            'code_blocks': len(code_blocks),
            'tables': len(tables),
            'lists': len(lists),
            'apis': len(apis),
        },
        'metadata': {
            'headings': headings[:10],  # First 10 headings
            'summary': plain_text[:500] if plain_text else '',  # First 500 chars of plain text
        },
        'content': {
            'paragraphs': paragraphs,
            'code_blocks': code_blocks,
            'tables': tables,
            'lists': lists,
        },
        'apis': apis,
        'methods': methods,
    }
    
    return result


def relative_file_path(path, root):
    """Get relative file path."""
    try:
        return str(path.relative_to(root)).replace('\\', '/')
    except ValueError:
        return str(path).replace('\\', '/')


def merge_results(file_results, source_root):
    """Merge all file extraction results."""
    result = {
        'metadata': {
            'format_version': 1,
            'source_root': str(source_root),
            'extractor': 'htmlfile_extractor.py',
        },
        'summary': {
            'files': len(file_results),
            'total_headings': 0,
            'total_paragraphs': 0,
            'total_code_blocks': 0,
            'total_tables': 0,
            'total_lists': 0,
            'total_apis': 0,
        },
        'files': [],
        'apis': [],
        'all_content': {
            'headings': [],
            'paragraphs': [],
            'code_blocks': [],
            'tables': [],
            'lists': [],
        },
    }
    
    for file_result in file_results:
        result['files'].append({
            'path': file_result['path'],
            'encoding': file_result['encoding'],
            'title': file_result['title'],
            'counts': file_result['counts'],
        })
        
        # Update summary counts
        for key in ['headings', 'paragraphs', 'code_blocks', 'tables', 'lists', 'apis']:
            result['summary'][f'total_{key}'] += file_result['counts'].get(key, 0)
        
        # Collect all APIs
        for api in file_result['apis']:
            api['source_file'] = file_result['path']
            result['apis'].append(api)
        
        # Collect all content
        if file_result['content'].get('headings'):
            result['all_content']['headings'].extend([
                {'file': file_result['path'], 'text': h} 
                for h in file_result['content']['headings']
            ])
        if file_result['content'].get('paragraphs'):
            result['all_content']['paragraphs'].extend([
                {'file': file_result['path'], 'text': p} 
                for p in file_result['content']['paragraphs'][:5]  # Limit to first 5
            ])
        if file_result['content'].get('code_blocks'):
            result['all_content']['code_blocks'].extend([
                {'file': file_result['path'], 'code': c} 
                for c in file_result['content']['code_blocks']
            ])
        if file_result['content'].get('tables'):
            result['all_content']['tables'].extend([
                {'file': file_result['path'], 'table': t} 
                for t in file_result['content']['tables']
            ])
        if file_result['content'].get('lists'):
            result['all_content']['lists'].extend([
                {'file': file_result['path'], 'list': lst} 
                for lst in file_result['content']['lists']
            ])
    
    return result


def extract_html_files(source, extensions=HTML_EXTENSIONS, exclude_dirs=None):
    """Extract all HTML files from source."""
    source = Path(source).resolve()
    exclude_dirs = set(exclude_dirs or [])
    root = source.parent if source.is_file() else source
    files = discover_html_files(source, extensions, exclude_dirs)
    
    if not files:
        print(f"Warning: No HTML files found in {source}")
        return merge_results([], root)
    
    file_results = [extract_html_file(path, root) for path in files]
    return merge_results(file_results, root)


def parse_args(argv):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Extract API documentation from HTML files into JSON format.',
    )
    parser.add_argument('source', help='HTML file or folder containing .html/.htm files.')
    parser.add_argument(
        '-o',
        '--output',
        default='htmlfiles_api.json',
        help='Output JSON file. Default: htmlfiles_api.json',
    )
    parser.add_argument(
        '--extensions',
        nargs='+',
        default=list(HTML_EXTENSIONS),
        help='HTML extensions to scan. Default: .html .htm',
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
        help='Do not skip common generated/dependency folders.',
    )
    parser.add_argument(
        '--stdout',
        action='store_true',
        help='Print JSON to stdout instead of writing --output.',
    )
    parser.add_argument(
        '--stats-only',
        action='store_true',
        help='Only print extraction counts.',
    )
    return parser.parse_args(argv)


def main(argv=None):
    """Main entry point."""
    args = parse_args(argv or sys.argv[1:])
    exclude_dirs = set(args.exclude_dir)
    if not args.no_default_excludes:
        exclude_dirs.update(DEFAULT_EXCLUDE_DIRS)
    
    result = extract_html_files(args.source, args.extensions, exclude_dirs)
    
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
    print(f"Extracted {result['summary']['files']} HTML files to {output_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
