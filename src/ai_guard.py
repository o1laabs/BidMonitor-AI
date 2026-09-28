"""AIGuard — 招投标项目相关性判断。

改动说明（相对上游 zhiqianzheng/BidMonitor-AI）：
  1. base_url 不再硬编码第三方中转，改为配置项 + 环境变量，默认值是中立的占位符
  2. api_key 支持从环境变量读取，不强制写进配置文件
  3. 协议判断改为「显式配置 api_format」，不再靠域名猜测（原实现按域名判断，
     但请求头/响应解析始终写死 OpenAI 格式，导致 Claude 分支是永不生效的死代码）
  4. 支持 Anthropic 原生协议（x-api-key + /v1/messages + content[].text）
  5. 失败时不再静默 return True —— 由 fail_open 显式控制，且计入统计
  6. 增加调用统计，便于判断 AI 层到底生效了几成
"""

import json
import logging
import os

DEFAULT_BASE_URL = "https://api.openai.com/v1/chat/completions"

# 内置的两套协议。用 api_format 显式指定，不再靠域名猜。
FORMAT_OPENAI = "openai"
FORMAT_ANTHROPIC = "anthropic"


class AIGuard:
    def __init__(self, config=None, log_callback=None):
        self.logger = logging.getLogger("AIGuard")
        self.log_callback = log_callback
        self.stats = {"total": 0, "relevant": 0, "irrelevant": 0,
                      "error": 0, "skipped": 0}
        self.update_config(config)

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)
        self.logger.info(message)

    def update_config(self, config):
        if not config:
            self.enabled = False
            return

        # --- 凭据：优先环境变量，其次配置文件 ---
        self.api_key = (
            os.environ.get("BIDMONITOR_AI_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or config.get("api_key", "")
            or ""
        )

        # --- 端点：优先环境变量，其次配置文件，最后中立默认值 ---
        self.base_url = (
            os.environ.get("BIDMONITOR_AI_BASE_URL")
            or config.get("base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/")

        self.model = config.get("model", "gpt-4o-mini")

        # --- 协议：显式声明，不再猜 ---
        # 默认按端点猜一次，但用户可用 api_format 覆盖
        fmt = config.get("api_format")
        if not fmt:
            fmt = FORMAT_ANTHROPIC if "anthropic.com" in self.base_url else FORMAT_OPENAI
        self.api_format = fmt

        self.enabled = config.get("enable", False)

        # --- 失败策略：显式 ---
        # fail_open=True  → 出错时放行（宁可多推，原上游行为）
        # fail_open=False → 出错时拦下（宁可少推，适合对准确性要求高的场景）
        self.fail_open = config.get("fail_open", True)

        self.custom_prompt = config.get("prompt", "")
        self.max_tokens = int(config.get("max_tokens", 512))
        self.timeout = int(config.get("timeout", 120))

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------
    def _build_request(self, system_prompt, user_content):
        """按 api_format 返回 (url, headers, payload)——两套协议各自正确。"""
        if self.api_format == FORMAT_ANTHROPIC:
            url = self.base_url
            # Anthropic 原生端点需要 /v1/messages 路径
            if not url.endswith("/messages"):
                url = url + "/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,              # 注意：不是 Bearer
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": self.model,
                "system": system_prompt,               # system 是顶层参数
                "messages": [{"role": "user", "content": user_content}],
                "temperature": 0.1,
                "max_tokens": self.max_tokens,         # Anthropic 必填
            }
        else:
            url = self.base_url
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.1,
                "max_tokens": self.max_tokens,
            }
        return url, headers, payload

    def _extract_text(self, result):
        """按 api_format 从响应里取正文——两套协议字段不同。"""
        if self.api_format == FORMAT_ANTHROPIC:
            blocks = result.get("content") or []
            parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            return "".join(parts)
        return (result.get("choices") or [{}])[0].get("message", {}).get("content", "")

    # ------------------------------------------------------------------
    # 主逻辑
    # ------------------------------------------------------------------
    def check_relevance(self, title, content="", raise_on_error=False):
        """返回 (is_relevant: bool, reason: str)"""
        self.stats["total"] += 1

        if not self.enabled:
            self.stats["skipped"] += 1
            return self._on_failure("AI未启用")
        if not self.api_key:
            self.stats["skipped"] += 1
            return self._on_failure("AI未配置Key")

        self.log(f"[AI分析] 开始分析: {title[:40]}...")

        system_prompt = self.custom_prompt or (
            "你是一个专业的招投标项目筛选专家。请判断该项目是否值得关注。\n"
            "只输出JSON，不要任何解释："
            '{"relevant": true/false, "reason": "50字以内的判断理由"}'
        )

        user_content = f"项目标题: {title}\n项目内容: {content[:800]}"
        url, headers, payload = self._build_request(system_prompt, user_content)

        self.log(f"[AI分析] 协议={self.api_format} 模型={self.model} 端点={self.base_url}")
        self.log("[AI分析] 请求端点: " + url)

        try:
            import time
            import requests
        except ImportError:
            self.stats["error"] += 1
            return self._on_failure("请安装 requests 库: pip install requests")

        max_retries, retry_delay = 3, 2
        last_err = ""

        for attempt in range(max_retries):
            try:
                self.log("[AI分析] 正在等待AI响应...")
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=self.timeout)

                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    self.log(f"[AI分析] API返回错误: HTTP {resp.status_code}")
                    # 4xx 是配置问题，重试无意义
                    if 400 <= resp.status_code < 500:
                        break
                    raise Exception(last_err)

                ai_content = self._extract_text(resp.json())
                self.log("[AI分析] 收到AI响应")

                if not ai_content.strip():
                    # reasoning 模型可能把预算全烧在思维链上，content 为空
                    last_err = "模型返回空内容（可尝试调大 max_tokens）"
                    self.log(f"[AI分析] {last_err}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    break

                relevant, reason = self._parse_verdict(ai_content)
                self.stats["relevant" if relevant else "irrelevant"] += 1
                self.log(("[AI判定] 相关 - " if relevant else "[AI判定] 不相关 - ") + reason)
                return relevant, reason

            except Exception as e:
                last_err = str(e)
                is_network = "Connection" in type(e).__name__ or "Timeout" in type(e).__name__
                if is_network and attempt < max_retries - 1:
                    self.log(f"[AI分析] 网络异常，{retry_delay}秒后重试 ({attempt+1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                self.log(f"[AI分析] 请求失败: {last_err[:120]}")
                self.logger.error(f"AI请求失败: {last_err}")
                break

        self.stats["error"] += 1
        if raise_on_error:
            raise RuntimeError(last_err or "AI请求失败")
        return self._on_failure(last_err or "AI请求异常")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _parse_verdict(self, ai_content):
        """从模型输出里解析 JSON 结论，容忍 markdown 包裹。"""
        text = ai_content.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        elif "{" in text and "}" in text:
            text = text[text.find("{"): text.rfind("}") + 1]

        try:
            data = json.loads(text)
            return bool(data.get("relevant", False)), str(data.get("reason", "AI未提供理由"))
        except (json.JSONDecodeError, AttributeError):
            self.log("[AI分析] 返回非标准JSON，降级为文本判断")
            low = ai_content.lower()
            relevant = '"relevant": true' in low or "'relevant': true" in low
            return relevant, ai_content[:80]

    def _on_failure(self, reason):
        """统一失败处理：由 fail_open 显式决定放行还是拦下。"""
        if self.fail_open:
            self.log(f"[AI降级] {reason} → 放行（fail_open=true）")
            return True, reason
        self.log(f"[AI降级] {reason} → 拦下（fail_open=false）")
        return False, reason

    def get_stats(self):
        """返回调用统计，便于判断 AI 层实际生效比例。"""
        s = dict(self.stats)
        done = s["relevant"] + s["irrelevant"]
        s["ai_decision_rate"] = round(done / s["total"], 3) if s["total"] else 0.0
        return s
