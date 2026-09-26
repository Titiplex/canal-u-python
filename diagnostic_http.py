"""Petit essai réel sans navigateur et sans modifier SQLite."""
import time

from modules import crawling, parsing


def main():
    crawling.USE_BROWSER_FALLBACK = False
    try:
        for index in range(5):
            url = crawling.SEARCH_URL + str(index)
            start = time.monotonic()
            html = crawling.crawl(url)
            count = len(parsing.get_search_items(html, url))
            print(f"page={index} temps={time.monotonic() - start:.2f}s liens={count}", flush=True)
            if not count:
                raise ValueError("Page sans résultat parsable")
        print("5 pages obtenues sans navigateur ; la base SQLite est inchangée.")
        return 0
    except Exception as error:
        print(type(error).__name__, str(error))
        return 1
    finally:
        crawling.close()


if __name__ == "__main__":
    raise SystemExit(main())
