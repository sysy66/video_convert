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
    """向 AI API 发送请求，返回解析后的 JSON 结果。"""
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
    """从 API 响应中提取最终文本内容，逐级回退到原始 JSON。"""
    return (
        result.get("choices", [{}])[0].get("message", {}).get("content", "")
        or result.get("error", {}).get("message", "")
        or json.dumps(result, ensure_ascii=False)
    )


def analyze(api_url, api_model, log_path, output_path, context=""):
    """读取日志、调用 AI API，并将分析结果写入输出文件。"""
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
    args = sys.argv[1:]
    api_url, api_model, log_path, output_path = args[:4]
    context = args[4] if len(args) > 4 else ""
    analyze(api_url, api_model, log_path, output_path, context)


if __name__ == "__main__":
    main()
