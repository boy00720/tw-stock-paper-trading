import sys
sys.path.append(".")
from utils.news_fetcher import NewsFetcher
from utils.news_processor import NewsProcessor
from scripts.news_crawler import NewsCrawler

crawler = NewsCrawler()
raw_news = crawler.fetcher.fetch_all_news()
processed_news = crawler.processor.process_news(raw_news)
categorized = crawler._categorize_news(processed_news)

economy_news = categorized.get('economy', [])
print(f"Total economy news: {len(economy_news)}")
print("---")
print(crawler.processor.format_category_news(economy_news[:2], 'economy', 0))
