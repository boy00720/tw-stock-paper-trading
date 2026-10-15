# 📰 今日新聞摘要

## 📊 新聞統計
- **總新聞數量**：{{news_count}} 條
- **更新時間**：{{update_time}}

## 🌍 國際新聞
{% for news in international_news %}
### {{ loop.index }}. {{ news.title }}
- **來源**：{{ news.source }}
- **類別**：{{ news.category }}
- **摘要在**：{{ news.summary }}
- [閱讀原文]({{ news.url }})

{% endfor %}

## 🇨🇳 國內新聞
{% for news in china_news %}
### {{ loop.index }}. {{ news.title }}
- **來源**：{{ news.source }}
- **類別**：{{ news.category }}
- **摘要在**：{{ news.summary }}
- [閱讀原文]({{ news.url }})

{% endfor %}

## 🇹🇼 台灣新聞
{% for news in taiwan_news %}
### {{ loop.index }}. {{ news.title }}
- **來源**：{{ news.source }}
- **類別**：{{ news.category }}
- **摘要在**：{{ news.summary }}
- [閱讀原文]({{ news.url }})

{% endfor %}

## 🔍 熱點分析
{% if hot_topics %}
### 今日熱點
{% for topic in hot_topics %}
- **{{ topic.name }}**：{{ topic.description }}
{% endfor %}
{% endif %}

## 💡 冷知識彩蛋
{% if fun_facts %}
### 今日冷知識
{% for fact in fun_facts %}
- {{ fact }}
{% endfor %}
{% endif %}

--- 
*由 AI 新聞資訊 W 自動整理發送*