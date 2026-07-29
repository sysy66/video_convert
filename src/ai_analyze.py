import json
import os
import sys
import urllib.error
import urllib.request

args = sys.argv[1:]
api_url, api_model, log_path, output_path = args[:4]
context = args[4] if len(args) > 4 else ""

with open(log_path) as f:
    log_content = f.read()

system_prompt = os.environ.get(
    "AI_SYSTEM_PROMPT",
    "You are a professional video transcoding quality analyst. "
    "Analyze the QSV AV1 transcoding pipeline log and provide a structured report covering:\n"
    "1. Summary: files processed, accepted, rejected, errors\n"
    "2. Per-file results: input/output size, ratio, VMAF score, encoding time\n"
    "3. Anomalies: warnings (QSV SW FALLBACK, REJECT), errors, unexpected behavior\n"
    "4. Performance: encoding speed, bottlenecks, encoder settings assessment\n"
    "5. Recommendations: config changes to improve quality, speed, or reliability\n"
    "Format the output in clean Markdown. All content must be in Simplified Chinese (简体中文).",
)

user_parts = []
if context:
    user_parts.append(f"本次运行配置：{context}")
user_parts.append("以下是转码日志：\n\n```\n" + log_content + "\n```")
user_content = "\n\n".join(user_parts)

payload = {
    "model": api_model,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ],
    "max_tokens": 4096,
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 64,
}

req = urllib.request.Request(
    api_url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read())
except (urllib.error.URLError, urllib.error.HTTPError) as e:
    result = {"error": str(e)}

content = (
    result.get("choices", [{}])[0]
    .get("message", {})
    .get("content", "")
    or result.get("error", {}).get("message", "")
    or json.dumps(result, ensure_ascii=False)
)

with open(output_path, "w") as f:
    f.write(content)
