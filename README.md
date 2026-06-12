# CreoJSON API Extractor

This repository contains Python scripts to extract API documentation from several sources and write it into JSON format.

## What is included

- `crawler.py` - crawls HTML pages using Playwright and extracts structured content.
- `htmlfile_extractor.py` - extracts API documentation from local HTML files.
- `headerfiles_extractor.py` - extracts symbols from local C/C++ header files.
- `dll_extractor.py` - extracts DLL export metadata from Windows DLL files.
- `pdf_api_extractor.py` - extracts Creo Toolkit API documentation from a PDF file.
- `requirements.txt` - base Python dependencies.

## Prerequisites

- Python 3.8 or newer
- Git (optional, for repository checkout)
- Internet access for Playwright browser installation and crawling
- On Windows: `pywin32` for DLL extractor support

## Setup

1. Open a terminal in the repository root:

```powershell
cd E:\01Creo\CreoJSON
```

2. Create a virtual environment:

```powershell
python -m venv venv
```

3. Activate the virtual environment:

```powershell
.\
env\Scripts\Activate.ps1
```

If you use Command Prompt instead of PowerShell:

```cmd
venv\Scripts\activate.bat
```

4. Upgrade pip and install base requirements:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

5. Install Playwright browsers:

```powershell
python -m playwright install chromium
```

## Optional dependencies

Some scripts require extra packages that are not included in `requirements.txt`.

- `pdf_api_extractor.py` requires `PyMuPDF`:

```powershell
pip install PyMuPDF
```

- `dll_extractor.py` requires `pefile` and `pywin32`:

```powershell
pip install pefile pywin32
```

## Usage

### 1. Extract from HTML files

```powershell
python htmlfile_extractor.py <path-to-html-file-or-folder> -o jsonfile/htmlfile_api.json
```

Example:

```powershell
python htmlfile_extractor.py .\html_docs -o jsonfile/htmlfile_api.json
```

Useful flags:

- `--extensions .html .htm` - file extensions to scan
- `--exclude-dir <name>` - skip directories by name
- `--no-default-excludes` - include folders like `venv` and `node_modules`
- `--stdout` - print JSON to stdout instead of writing a file
- `--stats-only` - print only extraction counts

### 2. Extract from header files

```powershell
python headerfiles_extractor.py <path-to-header-file-or-folder> -o jsonfile/headerfiles_api.json
```

Example:

```powershell
python headerfiles_extractor.py .\headers -o jsonfile/headerfiles_api.json
```

Useful flags:

- `--extensions .h .hh .hpp .hxx`
- `--exclude-dir <name>`
- `--no-default-excludes`
- `--stdout`
- `--stats-only`

### 3. Extract from DLL files

```powershell
python dll_extractor.py <path-to-dll-file-or-folder> -o jsonfile/dll_api.json
```

Example:

```powershell
python dll_extractor.py .\dlls -o jsonfile/dll_api.json
```

Useful flags:

- `--extensions .dll .so .dylib .exe`
- `--exclude-dir <name>`
- `--no-default-excludes`
- `--stdout`
- `--stats-only`

> Note: DLL extraction works best on Windows. If `pefile` is missing, install it with `pip install pefile`.

### 4. Extract API from the PDF file

```powershell
python pdf_api_extractor.py --pdf tkuse12.pdf --output new_creo_api12.json
```

Example:

```powershell
python pdf_api_extractor.py --pdf .\tkuse12.pdf --output jsonfile/new_creo_api12.json
```

### 5. Run the crawler

```powershell
python crawler.py
```

The crawler currently uses a fixed `START_URL` and saves output as `SolidWorks_API.json`.

## Output files

Common output files in this repository include:

- `jsonfile/htmlfile_api.json`
- `jsonfile/headerfiles_api.json`
- `jsonfile/dll_api.json`
- `new_creo_api12.json`
- `SolidWorks_API.json`

You can change the output location by using the script-specific `--output` or `-o` option.

## Notes

- Always activate the virtual environment before running the scripts.
- If you install new Python packages, use the same `venv` environment.
- For Playwright, browser binaries must be installed separately with `python -m playwright install chromium`.
- If you need additional functionality, inspect each script's `argparse` section for all supported flags.

## Troubleshooting

- If `ImportError` occurs for `bs4`, run:

```powershell
pip install beautifulsoup4
```

- If `ImportError` occurs for `fitz`, run:

```powershell
pip install PyMuPDF
```

- If `ImportError` occurs for `pefile` or `win32api`, run:

```powershell
pip install pefile pywin32
```

- If the crawler fails, make sure network access is available and `playwright` is installed correctly.
