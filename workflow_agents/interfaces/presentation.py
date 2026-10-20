"""Concise chat presentation; complete evidence and reports remain on disk."""
import re


def details(title, content):
    return f':::details {title}\n{content}\n:::' if content else ''


def is_context_limit_error(error):
    text = str(error).lower()
    return any(marker in text for marker in ('maximum context length', 'context_length_exceeded',
        'context window', 'too many tokens', 'context length exceeded'))


def error_summary(error):
    text = str(error)
    if is_context_limit_error(text):
        limit = re.search(r'maximum context length is\s+(\d+)', text, re.I)
        requested = re.search(r'requested\s+(\d+)\s+tokens', text, re.I)
        counts = (f'本次输入 {int(requested[1]):,} tokens，上限 {int(limit[1]):,} tokens。'
                  if limit and requested else '')
        return '诊断输入证据超过模型上下文限制。' + counts + '请指定节点／事件，建立范围更小的调查任务；原样重试仍会超限。'
    return text.splitlines()[0]


def chat_history_summary(content):
    """Do not replay expandable raw output or legacy full reports into chat planning."""
    text = re.sub(r'^:::details[^\n]*\n[\s\S]*?^:::\s*$', '', content, flags=re.M)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.split(r'^#{1,6}\s*(?:冒溢初步诊断|诊断调查结果|诊断报告)', text, flags=re.M)[0]
    # Legacy assistant replies can contain hundreds of detailed evidence-scope lines.
    # Keep the human-facing result and next action; the controller supplies live bindings.
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if len(paragraphs) > 5:
        selected = paragraphs[:2] + [p for p in paragraphs[2:] if p.startswith(('下一步', '本轮只执行', '当前状态', '结果：', '要点：'))]
        text = '\n\n'.join(selected)
    return text.strip()
