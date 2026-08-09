"""
AI 日志分析模块。读取转码日志，构造 LLM API 请求负载并调用远程模型，
从返回结果中抽取结构化的质量分析报告。

AI log-analysis module. Reads transcode logs, builds the LLM API request payload,
calls a remote model, and extracts a structured quality report from the response.
"""

import json
import os
import sys
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "You are a professional video transcoding quality analyst. "
    "Analyze the QSV AV1 transcoding pipeline log and provide a structured report covering:\n"
    "1. Summary: files processed, accepted, rejected, errors\n"
    "2. Per-file results: input/output size, ratio, VMAF score, encoding time\n"
    "3. Anomalies: warnings (QSV SW FALLBACK, REJECT), errors, unexpected behavior\n"
    "4. Performance: encoding speed, bottlenecks, encoder settings assessment\n"
    "5. Recommendations: config changes to improve quality, speed, or reliability\n"
    "Format the output in clean Markdown. All content must be in Simplified Chinese (简体中文)."
)

DEFAULT_MAX_TOKENS = 4096
REQUEST_TIMEOUT = 180


def build_payload(api_model, log_content, context=""):
    """构造 LLM Chat Completions 请求负载：系统提示 + 用户提供的日志内容。

    Build an LLM Chat Completions request payload: the system prompt plus the
    user-provided log content.

    :param api_model: 模型名称 (Model name)
    :param log_content: 转码日志文本 (Transcode log text)
    :param context: 可选的运行配置说明 (Optional run-config description)
    :return: 请求负载字典 (Request payload dict)
    """
    user_parts = []
    if context:
        user_parts.append(f"本次运行配置：{context}")
    user_parts.append("以下是转码日志：\n\n```\n" + log_content + "\n```")
    user_content = "\n\n".join(user_parts)
    return {
        "model": api_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": DEFAULT_MAX_TOKENS,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
    }


def send_request(api_url, payload, timeout=REQUEST_TIMEOUT):
    """
    向 AI API 发送 POST 请求；成功时返回解析后的 JSON，网络/HTTP 错误时返回 {"error": ...}。

    Send a POST request to the AI API; return the parsed JSON on success, or an
    {"error": ...} dict on URL/HTTP errors.
    """
    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return {"error": str(e)}


def extract_content(result):
    """
    从 API 响应中提取最终文本内容：依次尝试 message.content、error.message，
    最后回退到整个响应的 JSON 序列化。

    Extract the final text from an API response: tries message.content, then
    error.message, and finally falls back to the serialized response JSON.
    """
    return (
        result.get("choices", [{}])[0].get("message", {}).get("content", "")
        or result.get("error", {}).get("message", "")
        or json.dumps(result, ensure_ascii=False)
    )


def analyze(api_url, api_model, log_path, output_path, context=""):
    """
    读取日志文件、构建并发送 AI 请求（可用 AI_SYSTEM_PROMPT 环境变量覆盖系统提示），
    将提取的分析文本写入输出文件并返回。

    Read the log file, build and send the AI request (the AI_SYSTEM_PROMPT env var
    can override the system prompt), write the extracted analysis text to the
    output file, and return it.
    """
    with open(log_path) as f:
        log_content = f.read()

    system_prompt = os.environ.get("AI_SYSTEM_PROMPT", SYSTEM_PROMPT)

    payload = build_payload(api_model, log_content, context)
    payload["messages"][0]["content"] = system_prompt

    result = send_request(api_url, payload)
    content = extract_content(result)

    with open(output_path, "w") as f:
        f.write(content)
    return content


def main():
    """脚本入口：解析命令行参数并调用 analyze 执行日志分析。

    Script entry point: parse CLI arguments and run the log analysis.
    """
    args = sys.argv[1:]
    api_url, api_model, log_path, output_path = args[:4]
    context = args[4] if len(args) > 4 else ""
    analyze(api_url, api_model, log_path, output_path, context)


if __name__ == "__main__":
    main()
