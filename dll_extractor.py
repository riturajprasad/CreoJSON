import argparse
import json
import re
import sys
from pathlib import Path
from collections import OrderedDict

try:
    import pefile
except ImportError:
    pefile = None

try:
    import win32api
    import win32con
except ImportError:
    win32api = None
    win32con = None


DLL_EXTENSIONS = ('.dll', '.so', '.dylib', '.exe')
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


def discover_dll_files(source, extensions, exclude_dirs):
    """Discover DLL files in source directory or return single file."""
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


def get_file_version_info(dll_path):
    """Extract version information from DLL."""
    version_info = {
        'file_version': None,
        'product_version': None,
        'company_name': None,
        'file_description': None,
        'product_name': None,
        'internal_name': None,
        'original_filename': None,
        'file_size': 0,
    }
    
    try:
        version_info['file_size'] = dll_path.stat().st_size
    except Exception:
        pass
    
    # Try using win32api (Windows only)
    if win32api:
        try:
            handle = win32api.LoadLibrary(str(dll_path))
            version_info['loaded'] = True
            win32api.FreeLibrary(handle)
        except Exception:
            pass
    
    return version_info


def extract_exports_pefile(dll_path):
    """Extract exported functions from DLL using pefile."""
    if not pefile:
        return [], []
    
    exports = []
    import_libs = []
    
    try:
        pe = pefile.PE(str(dll_path))
        
        # Extract imported libraries
        if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
            for imp in pe.DIRECTORY_ENTRY_IMPORT:
                lib_name = imp.dll.decode('utf-8', errors='ignore') if isinstance(imp.dll, bytes) else str(imp.dll)
                import_libs.append(lib_name)
        
        # Extract exported functions
        if hasattr(pe, 'DIRECTORY_ENTRY_EXPORT'):
            for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                func_name = exp.name.decode('utf-8', errors='ignore') if isinstance(exp.name, bytes) else str(exp.name)
                if func_name:
                    exports.append({
                        'name': func_name,
                        'ordinal': exp.ordinal,
                        'address': hex(exp.address) if exp.address else None,
                        'type': 'function',
                        'parameters': [],
                        'return_type': 'void',
                        'description': '',
                    })
        
        return exports, import_libs
    except Exception as e:
        return [], []


def extract_pe_sections(dll_path):
    """Extract PE section information."""
    if not pefile:
        return []
    
    sections = []
    try:
        pe = pefile.PE(str(dll_path))
        for section in pe.sections:
            section_name = section.Name.decode('utf-8', errors='ignore').rstrip('\x00')
            sections.append({
                'name': section_name,
                'virtual_address': hex(section.VirtualAddress),
                'virtual_size': section.Misc_VirtualSize,
                'raw_size': section.SizeOfRawData,
                'characteristics': hex(section.Characteristics),
            })
    except Exception:
        pass
    
    return sections


def extract_pe_headers(dll_path):
    """Extract PE header information."""
    if not pefile:
        return {}
    
    headers = {}
    try:
        pe = pefile.PE(str(dll_path))
        
        # DOS header
        if hasattr(pe, 'DOS_HEADER'):
            headers['dos_header'] = {
                'e_magic': hex(pe.DOS_HEADER.e_magic),
                'e_lfanew': hex(pe.DOS_HEADER.e_lfanew),
            }
        
        # File header
        if hasattr(pe, 'FILE_HEADER'):
            headers['file_header'] = {
                'machine': hex(pe.FILE_HEADER.Machine),
                'number_of_sections': pe.FILE_HEADER.NumberOfSections,
                'timestamp': pe.FILE_HEADER.TimeDateStamp,
                'characteristics': hex(pe.FILE_HEADER.Characteristics),
            }
        
        # Optional header
        if hasattr(pe, 'OPTIONAL_HEADER'):
            headers['optional_header'] = {
                'magic': hex(pe.OPTIONAL_HEADER.Magic),
                'subsystem': pe.OPTIONAL_HEADER.Subsystem,
                'image_base': hex(pe.OPTIONAL_HEADER.ImageBase),
                'size_of_image': pe.OPTIONAL_HEADER.SizeOfImage,
                'entry_point': hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint),
            }
    except Exception:
        pass
    
    return headers


def parse_function_signature_from_name(func_name):
    """Try to parse function signature from mangled/decorated name."""
    # C++ name mangling patterns (simplified)
    # This is a basic attempt to extract info from decorated names
    
    # Check for common calling conventions
    calling_convention = 'unknown'
    if func_name.startswith('_'):
        if func_name.startswith('__stdcall'):
            calling_convention = '__stdcall'
        elif func_name.startswith('__cdecl'):
            calling_convention = '__cdecl'
        elif func_name.startswith('__fastcall'):
            calling_convention = '__fastcall'
    
    return {
        'name': func_name,
        'calling_convention': calling_convention,
        'parameters': [],
        'return_type': 'unknown',
        'description': '',
    }


def extract_dll_file(path, root):
    """Extract API information from a single DLL file."""
    file_path = relative_file_path(path, root)
    
    # Get basic file info
    try:
        stat_info = path.stat()
        file_size = stat_info.st_size
    except Exception:
        file_size = 0
    
    # Extract version info
    version_info = get_file_version_info(path)
    
    # Extract exports and imports
    exports, import_libs = extract_exports_pefile(path)
    
    # Extract PE sections
    sections = extract_pe_sections(path)
    
    # Extract PE headers
    headers = extract_pe_headers(path)
    
    # Parse signatures from exported functions
    apis = []
    for export in exports:
        sig = parse_function_signature_from_name(export['name'])
        sig['ordinal'] = export['ordinal']
        sig['address'] = export['address']
        apis.append(sig)
    
    # Build return structure
    result = {
        'path': file_path,
        'file_size': file_size,
        'title': path.stem,
        'counts': {
            'exports': len(exports),
            'imports': len(import_libs),
            'sections': len(sections),
        },
        'version_info': version_info,
        'metadata': {
            'dll_name': path.name,
            'base_name': path.stem,
        },
        'structure': {
            'headers': headers,
            'sections': sections,
        },
        'imports': import_libs,
        'exports': exports,
        'apis': apis,
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
            'extractor': 'dll_extractor.py',
        },
        'summary': {
            'files': len(file_results),
            'total_exports': 0,
            'total_imports': 0,
            'total_sections': 0,
            'total_apis': 0,
            'total_file_size': 0,
        },
        'files': [],
        'imports_map': {},
        'exports_by_file': {},
        'all_exports': [],
        'all_apis': [],
    }
    
    for file_result in file_results:
        result['files'].append({
            'path': file_result['path'],
            'title': file_result['title'],
            'file_size': file_result['file_size'],
            'counts': file_result['counts'],
            'version_info': file_result['version_info'],
        })
        
        # Update summary counts
        result['summary']['total_exports'] += len(file_result['exports'])
        result['summary']['total_imports'] += len(file_result['imports'])
        result['summary']['total_sections'] += len(file_result['structure']['sections'])
        result['summary']['total_apis'] += len(file_result['apis'])
        result['summary']['total_file_size'] += file_result['file_size']
        
        # Build imports map
        for imp in file_result['imports']:
            if imp not in result['imports_map']:
                result['imports_map'][imp] = []
            result['imports_map'][imp].append(file_result['path'])
        
        # Collect exports by file
        result['exports_by_file'][file_result['path']] = file_result['exports']
        
        # Collect all exports with source file info
        for export in file_result['exports']:
            export_copy = dict(export)
            export_copy['source_file'] = file_result['path']
            result['all_exports'].append(export_copy)
        
        # Collect all APIs with source file info
        for api in file_result['apis']:
            api_copy = dict(api)
            api_copy['source_file'] = file_result['path']
            result['all_apis'].append(api_copy)
    
    return result


def extract_dll_files(source, extensions=DLL_EXTENSIONS, exclude_dirs=None):
    """Extract all DLL files from source."""
    source = Path(source).resolve()
    exclude_dirs = set(exclude_dirs or [])
    root = source.parent if source.is_file() else source
    files = discover_dll_files(source, extensions, exclude_dirs)
    
    if not files:
        print(f"Warning: No DLL files found in {source}")
        return merge_results([], root)
    
    file_results = []
    for path in files:
        try:
            file_result = extract_dll_file(path, root)
            file_results.append(file_result)
        except Exception as e:
            print(f"Warning: Failed to process {path}: {e}")
            continue
    
    return merge_results(file_results, root)


def parse_args(argv):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Extract exported APIs and metadata from DLL files into JSON format.',
    )
    parser.add_argument('source', help='DLL file or folder containing .dll files.')
    parser.add_argument(
        '-o',
        '--output',
        default='dll_api.json',
        help='Output JSON file. Default: dll_api.json',
    )
    parser.add_argument(
        '--extensions',
        nargs='+',
        default=list(DLL_EXTENSIONS),
        help='DLL extensions to scan. Default: .dll .so .dylib .exe',
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
    
    # Check if pefile is available
    if not pefile:
        print("Warning: pefile library not found. Install it with: pip install pefile")
        print("This extractor requires pefile to parse DLL files.")
        return 1
    
    result = extract_dll_files(args.source, args.extensions, exclude_dirs)
    
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
    print(f"Extracted {result['summary']['files']} DLL files to {output_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
