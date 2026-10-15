# 📋 每日日誌 - {{ current_date }}

## 🌤️ 今日天氣與情緒
- **天氣狀況**：{{ weather }}
- **今日情緒**：{{ mood }}

## 📝 工作記錄
{% if work_tasks %}
### 主要任務
{% for task in work_tasks %}
- {{ loop.index }}. [{{ "完成" if task.completed else "進行中" }}] {{ task.name }} - {{ task.progress }}
{% endfor %}
{% endif %}

{% if work_highlights %}
### 工作重點
{% for highlight in work_highlights %}
- {{ loop.index }}. {{ highlight }}
{% endfor %}
{% endif %}

{% if work_challenges %}
### 遇到的挑戰
{% for challenge in work_challenges %}
- {{ loop.index }}. {{ challenge }}
{% endfor %}
{% endif %}

{% if work_learnings %}
### 學習收穫
{% for learning in work_learnings %}
- {{ loop.index }}. {{ learning }}
{% endfor %}
{% endif %}

## 🌿 生活記錄
{% if health_activities %}
### 身心健康
{% for activity in health_activities %}
- {{ loop.index }}. {{ activity }}
{% endfor %}
{% endif %}

{% if social_interactions %}
### 人際關係
{% for interaction in social_interactions %}
- {{ loop.index }}. {{ interaction }}
{% endfor %}
{% endif %}

{% if family_events %}
### 家庭生活
{% for event in family_events %}
- {{ loop.index }}. {{ event }}
{% endfor %}
{% endif %}

## 📚 學習與成長
{% if learning_topics %}
### 知識吸收
{% for topic in learning_topics %}
- {{ loop.index }}. {{ topic }}
{% endfor %}
{% endif %}

{% if skill_practices %}
### 技能提升
{% for practice in skill_practices %}
- {{ loop.index }}. {{ practice }}
{% endfor %}
{% endif %}

## 💭 今日反思
{% if successes %}
### 成功之處
{% for success in successes %}
- {{ loop.index }}. {{ success }}
{% endfor %}
{% endif %}

{% if improvements %}
### 改進空間
{% for improvement in improvements %}
- {{ loop.index }}. {{ improvement }}
{% endfor %}
{% endif %}

{% if gratitude %}
### 感恩與收穫
{% for thing in gratitude %}
- {{ loop.index }}. {{ thing }}
{% endfor %}
{% endif %}

## 🎯 明日計劃
{% if tomorrow_goals %}
### 核心目標
{% for goal in tomorrow_goals %}
{{ loop.index }}. {{ goal }}
{% endfor %}
{% endif %}

{% if tomorrow_habits %}
### 儀式習慣
{% for habit in tomorrow_habits %}
{{ loop.index }}. {{ habit }}
{% endfor %}
{% endif %}

--- 
*由 AI 日報 W 自動生成*