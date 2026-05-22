import json
import time
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


# ---------------- CONFIG ----------------
START_URL = "https://help.solidworks.com/2024/english/api/sldworksapiprogguide/Welcome.htm?id=0"
MAX_PAGES = 500
OUTPUT_FILE = "SolidWorks_API.json"
DELAY = 1  # seconds between requests


# ---------------- LINK EXTRACTOR ----------------
def extract_links(page, base_url):
    links = page.query_selector_all("a")
    urls = set()

    base_domain = urlparse(base_url).netloc

    for link in links:
        href = link.get_attribute("href")
        if not href:
            continue

        # Skip unwanted links
        if href.startswith("#") or "mailto:" in href or "javascript:" in href:
            continue

        full_url = urljoin(base_url, href)

        # Keep only same domain
        if urlparse(full_url).netloc == base_domain:
            urls.add(full_url)

    return urls


# ---------------- CONTENT SCRAPER ----------------
def scrape_content(html, url):
    soup = BeautifulSoup(html, "html.parser")

    # Title extraction
    title_tag = soup.find("h1")
    title = title_tag.text.strip() if title_tag else "No Title"

    sections = []
    current_section = {"heading": "Introduction", "content": []}

    for tag in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        if tag.name in ["h1", "h2", "h3"]:
            if current_section["content"]:
                sections.append(current_section)
            current_section = {
                "heading": tag.text.strip(),
                "content": []
            }
        else:
            text = tag.text.strip()
            if text:
                current_section["content"].append(text)

    if current_section["content"]:
        sections.append(current_section)

    return {
        "url": url,
        "title": title,
        "sections": sections
    }


# ---------------- MAIN CRAWLER ----------------
def crawl(start_url, max_pages):
    visited = set()
    to_visit = [start_url]
    all_data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        while to_visit and len(visited) < max_pages:
            url = to_visit.pop(0)

            if url in visited:
                continue

            print(f"[{len(visited)+1}] Crawling: {url}")

            try:
                page = browser.new_page()
                page.goto(url, timeout=60000)

                # IMPORTANT: wait for JS content
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(4000)

                html = page.content()

                # If empty → try iframe
                if len(html) < 1000:
                    for frame in page.frames:
                        try:
                            html = frame.content()
                            if len(html) > 1000:
                                break
                        except:
                            continue

                # ---- Extract content ----
                page_data = scrape_content(html, url)
                all_data.append(page_data)

                # ---- FIXED LINK EXTRACTION ----
                links = page.query_selector_all("a")
                new_links = set()

                for link in links:
                    href = link.get_attribute("href")
                    if not href:
                        continue

                    # HANDLE #page URLs
                    if "#page/" in href:
                        base = start_url.split("#")[0]
                        full_url = base + href
                        new_links.add(full_url)

                    # NORMAL LINKS
                    elif href.startswith("http"):
                        if "support.ptc.com" in href:
                            new_links.add(href)

                # Add to queue
                for link in new_links:
                    if link not in visited and link not in to_visit:
                        to_visit.append(link)

                visited.add(url)
                page.close()

                # Save progress
                with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(all_data, f, indent=2, ensure_ascii=False)

                time.sleep(DELAY)

            except Exception as e:
                print(f" Error on {url}: {e}")
                visited.add(url)
                continue

        browser.close()

    return all_data


# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    data = crawl(START_URL, MAX_PAGES)

    print("\n Crawling completed!")
    print(f"Total pages scraped: {len(data)}")
    print(f"Saved to: {OUTPUT_FILE}")