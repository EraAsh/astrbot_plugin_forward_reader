"""
Forward Reader v2.0.2 - 智能合并转发消息分析插件
核心设计理念：
- 完全模拟真人对话，无任何技术痕迹
- 智能感知用户意图，无需显式指令
- 优雅处理异常情况，静默失败或自然跳过
- 支持多模态内容（图片/视频）智能识别
- 多层嵌套合并消息智能处理
- 自动分析模式下不携带之前上下文
- 全链路结构化日志追踪
- 消息ID哈希缓存去重
- 分析状态机管理
- 延迟触发与优先级队列
- Token预算控制与分段摘要

作者: EraAsh
版本: 2.0.2
许可证: AGPL-3.0
"""

import json
import re
import random
import time
import hashlib
import asyncio
from enum import Enum, auto
from typing import List, Dict, Optional, Tuple, Set, Any
from urllib.parse import urlparse
from dataclasses import dataclass, field
from collections import deque

import aiohttp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 平台适配检查
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False
    AiocqhttpMessageEvent = None


class AnalysisState(Enum):
    """分析状态机状态"""
    IDLE = auto()
    PENDING = auto()
    ANALYZING = auto()
    COMPLETED = auto()


class AnalysisPriority(Enum):
    """分析优先级"""
    MANUAL = 2
    AUTO = 1


@dataclass
class MediaContent:
    """媒体内容数据结构"""
    type: str
    url: str
    base64_data: Optional[str] = None
    size: int = 0
    processed: bool = False


@dataclass
class ChatContext:
    """聊天记录上下文数据结构"""
    sender: str
    content: str
    has_image: bool = False
    has_video: bool = False
    has_file: bool = False
    media_list: List[MediaContent] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """分析结果数据结构"""
    success: bool
    content: str = ""
    error_type: Optional[str] = None
    chat_count: int = 0
    image_urls: List[str] = field(default_factory=list)
    has_multimodal: bool = False
    nested_forward_count: int = 0
    token_estimate: int = 0


@dataclass
class AnalysisTask:
    """分析任务数据结构"""
    task_id: str
    forward_id: str
    event: AstrMessageEvent
    user_query: str
    intent_type: str
    priority: AnalysisPriority
    state: AnalysisState = AnalysisState.IDLE
    created_at: float = field(default_factory=time.time)
    delay_seconds: float = 2.0
    is_direct_analysis: bool = False
    forward_payload: Optional[Dict[str, Any]] = None


@dataclass
class PerformanceMetrics:
    """性能指标数据结构"""
    start_time: float = field(default_factory=time.time)
    llm_start_time: Optional[float] = None
    llm_end_time: Optional[float] = None
    token_input: int = 0
    token_output: int = 0
    image_count: int = 0
    video_count: int = 0

    @property
    def total_duration(self) -> float:
        return time.time() - self.start_time

    @property
    def llm_duration(self) -> Optional[float]:
        if self.llm_start_time and self.llm_end_time:
            return self.llm_end_time - self.llm_start_time
        return None


class StructuredLogger:
    """结构化日志追踪器"""

    def __init__(self, plugin_name: str, log_level: str = "INFO", enable_trace: bool = False):
        self.plugin_name = plugin_name
        self.log_level = log_level.upper()
        self.enable_trace = enable_trace
        self._trace_id_counter = 0

    def _generate_trace_id(self) -> str:
        self._trace_id_counter += 1
        timestamp = int(time.time() * 1000)
        return f"{timestamp:013x}-{self._trace_id_counter:04x}"

    def _format_log(self, level: str, trace_id: str, stage: str, message: str, extra: Dict = None) -> str:
        extra_str = ""
        if extra:
            extra_str = " | " + " ".join([f"{k}={v}" for k, v in extra.items()])
        return f"[{self.plugin_name}] [{level}] [{trace_id}] [{stage}] {message}{extra_str}"

    def debug(self, trace_id: str, stage: str, message: str, **kwargs):
        if self.enable_trace:
            logger.debug(self._format_log("DEBUG", trace_id, stage, message, kwargs))

    def info(self, trace_id: str, stage: str, message: str, **kwargs):
        logger.info(self._format_log("INFO", trace_id, stage, message, kwargs))

    def warning(self, trace_id: str, stage: str, message: str, **kwargs):
        logger.warning(self._format_log("WARN", trace_id, stage, message, kwargs))

    def error(self, trace_id: str, stage: str, message: str, **kwargs):
        logger.error(self._format_log("ERROR", trace_id, stage, message, kwargs))

    def performance(self, trace_id: str, metrics: PerformanceMetrics, **kwargs):
        llm_duration = metrics.llm_duration
        llm_duration_str = f"{llm_duration:.2f}s" if llm_duration else "N/A"
        logger.info(self._format_log(
            "PERF", trace_id, "performance",
            f"总耗时={metrics.total_duration:.2f}s LLM耗时={llm_duration_str} "
            f"Token输入={metrics.token_input} Token输出={metrics.token_output} "
            f"图片={metrics.image_count} 视频={metrics.video_count}",
            kwargs
        ))


class MessageDeduplicationCache:
    """消息去重缓存管理器"""

    def __init__(self, ttl_seconds: int = 3600):
        self._cache: Dict[str, float] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    def _generate_hash(self, forward_id: str, user_id: str, group_id: Optional[str]) -> str:
        content = f"{forward_id}:{user_id}:{group_id or ''}"
        return hashlib.md5(content.encode()).hexdigest()

    async def is_duplicate(self, forward_id: str, user_id: str, group_id: Optional[str]) -> bool:
        msg_hash = self._generate_hash(forward_id, user_id, group_id)
        async with self._lock:
            if msg_hash in self._cache:
                expiry_time = self._cache[msg_hash]
                if time.time() < expiry_time:
                    return True
                else:
                    del self._cache[msg_hash]
            return False

    async def add(self, forward_id: str, user_id: str, group_id: Optional[str]):
        msg_hash = self._generate_hash(forward_id, user_id, group_id)
        async with self._lock:
            self._cache[msg_hash] = time.time() + self._ttl

    async def cleanup_expired(self):
        current_time = time.time()
        async with self._lock:
            expired_keys = [k for k, v in self._cache.items() if current_time > v]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    async def start_cleanup_task(self):
        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(300)
                    count = await self.cleanup_expired()
                    if count > 0:
                        logger.debug(f"[ForwardReader] 清理 {count} 条过期缓存")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[ForwardReader] 缓存清理任务异常: {e}")

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def stop_cleanup_task(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None


class AnalysisStateManager:
    """分析状态管理器"""

    def __init__(self):
        self._states: Dict[str, AnalysisState] = {}
        self._lock = asyncio.Lock()
        self._pending_tasks: Dict[str, asyncio.Task] = {}

    async def get_state(self, session_id: str) -> AnalysisState:
        async with self._lock:
            return self._states.get(session_id, AnalysisState.IDLE)

    async def set_state(self, session_id: str, state: AnalysisState):
        async with self._lock:
            self._states[session_id] = state

    async def can_start_analysis(self, session_id: str) -> bool:
        async with self._lock:
            current_state = self._states.get(session_id, AnalysisState.IDLE)
            return current_state in [AnalysisState.IDLE, AnalysisState.COMPLETED]

    async def register_pending_task(self, session_id: str, task: asyncio.Task):
        async with self._lock:
            self._pending_tasks[session_id] = task

    async def cancel_pending_task(self, session_id: str):
        async with self._lock:
            if session_id in self._pending_tasks:
                task = self._pending_tasks[session_id]
                if not task.done():
                    task.cancel()
                del self._pending_tasks[session_id]


class PriorityTaskQueue:
    """优先级任务队列"""

    def __init__(self):
        self._queue: deque = deque()
        self._lock = asyncio.Lock()
        self._processing = False
        self._current_task: Optional[AnalysisTask] = None

    async def add_task(self, task: AnalysisTask) -> bool:
        async with self._lock:
            for existing_task in self._queue:
                if existing_task.forward_id == task.forward_id:
                    if task.priority.value > existing_task.priority.value:
                        self._queue.remove(existing_task)
                        self._insert_by_priority(task)
                        return True
                    return False
            self._insert_by_priority(task)
            return True

    def _insert_by_priority(self, task: AnalysisTask):
        inserted = False
        for i, existing in enumerate(self._queue):
            if task.priority.value > existing.priority.value:
                self._queue.insert(i, task)
                inserted = True
                break
        if not inserted:
            self._queue.append(task)

    async def get_next_task(self) -> Optional[AnalysisTask]:
        async with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    async def has_pending_task(self, forward_id: str) -> bool:
        async with self._lock:
            return any(t.forward_id == forward_id for t in self._queue)


class MediaBatchProcessor:
    """多媒体批量预处理器"""

    def __init__(
        self,
        max_images: int = 5,
        max_videos: int = 2,
        request_timeout: float = 10.0,
        retry_times: int = 2
    ):
        self.max_images = max_images
        self.max_videos = max_videos
        self.request_timeout = request_timeout
        self.retry_times = retry_times
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=max(5.0, self.request_timeout)),
                connector=aiohttp.TCPConnector(limit=12, limit_per_host=6, ssl=False)
            )
        return self._session

    def _is_http_url(self, url: str) -> bool:
        if not url:
            return False
        return url.startswith("http://") or url.startswith("https://")

    def _is_qq_multimedia_url(self, url: str) -> bool:
        try:
            host = (urlparse(url).netloc or "").lower()
            return "multimedia.nt.qq.com.cn" in host
        except Exception:
            return False

    def _build_request_headers(self, url: str) -> Dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://nt.qq.com/"
        }
        try:
            host = (urlparse(url).netloc or "").lower()
            if host:
                headers["Host"] = host
        except Exception:
            pass
        return headers

    async def _probe_image_url(self, url: str) -> Tuple[bool, str]:
        if not self._is_http_url(url):
            return False, "non_http_url"

        session = await self._get_session()
        headers = self._build_request_headers(url)

        # 关键修复：QQ 临时资源常拒绝 HEAD，优先使用 GET + Range 探测
        last_error = ""
        for attempt in range(1, self.retry_times + 2):
            try:
                req_headers = dict(headers)
                req_headers["Range"] = "bytes=0-1023"
                async with session.get(url, headers=req_headers, timeout=self.request_timeout, allow_redirects=True) as response:
                    if response.status in (200, 206):
                        ctype = (response.headers.get("Content-Type") or "").lower()
                        if "image" in ctype or ctype == "":
                            return True, "ok"
                        return False, f"unexpected_content_type:{ctype}"
                    if response.status in (401, 403):
                        if self._is_qq_multimedia_url(url):
                            return True, f"qq_signed_url_status_{response.status}_keep"
                        return False, f"http_{response.status}"
                    if response.status == 404:
                        return False, "http_404"
                    last_error = f"http_{response.status}"
            except Exception as e:
                last_error = f"exception:{type(e).__name__}:{e}"
                await asyncio.sleep(0.15 * attempt)

        # QQ 链接在本机探测失败时不直接丢弃，交由上游模型再尝试拉取
        if self._is_qq_multimedia_url(url):
            return True, f"qq_probe_failed_keep:{last_error}"

        return False, last_error or "probe_failed"

    async def process_media_batch(
        self,
        media_list: List[MediaContent],
        trace_id: str,
        slogger: StructuredLogger
    ) -> Tuple[List[str], str]:
        slogger.debug(trace_id, "media_preprocess", f"开始批量预处理 {len(media_list)} 个媒体文件")

        image_urls: List[str] = []
        video_count = 0
        failed_images: List[str] = []
        seen_urls: Set[str] = set()

        for media in media_list:
            if media.type == "image" and len(image_urls) < self.max_images:
                url = (media.url or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    ok, reason = await self._probe_image_url(url)
                    if ok:
                        image_urls.append(url)
                        media.processed = True
                        slogger.debug(trace_id, "media_preprocess", "图片可用",
                                     reason=reason, preview=url[:64])
                    else:
                        failed_images.append(f"{url[:80]} | {reason}")
                        slogger.warning(trace_id, "media_preprocess", "图片URL不可用",
                                       reason=reason, preview=url[:80])
                except Exception as e:
                    failed_images.append(f"{url[:80]} | exception:{e}")
                    slogger.warning(trace_id, "media_preprocess", f"图片预处理失败: {e}")

            elif media.type == "video" and video_count < self.max_videos:
                video_count += 1

        summary_parts = []
        if image_urls:
            summary_parts.append(f"可用图片 {len(image_urls)} 张")
        if failed_images:
            summary_parts.append(f"不可用图片 {len(failed_images)} 张")
        if video_count > 0:
            summary_parts.append(f"视频 {video_count} 个")

        summary = "，".join(summary_parts)
        slogger.info(trace_id, "media_preprocess", "媒体预处理完成",
                    available_images=len(image_urls),
                    failed_images=len(failed_images),
                    videos=video_count)

        if failed_images:
            slogger.debug(trace_id, "media_preprocess", "不可用图片详情",
                         details=" || ".join(failed_images[:6]))

        return image_urls, summary

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class TokenBudgetController:
    """Token预算控制器"""

    def __init__(self, token_budget: int = 4000, segment_size: int = 1500):
        self.token_budget = token_budget
        self.segment_size = segment_size

    def estimate_tokens(self, text: str, image_count: int = 0) -> int:
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        text_tokens = chinese_chars + int(other_chars * 0.25)
        image_tokens = image_count * 1000
        return text_tokens + image_tokens

    def should_segment(self, text: str, image_count: int = 0) -> bool:
        if self.token_budget <= 0:
            return False
        return self.estimate_tokens(text, image_count) > self.token_budget

    def segment_content(self, contexts: List[ChatContext]) -> List[List[ChatContext]]:
        if not self.token_budget or self.token_budget <= 0:
            return [contexts]

        segments = []
        current_segment = []
        current_tokens = 0

        for ctx in contexts:
            ctx_text = f"{ctx.sender}: {ctx.content}"
            ctx_tokens = self.estimate_tokens(ctx_text, len(ctx.media_list))

            if current_tokens + ctx_tokens > self.token_budget and current_segment:
                segments.append(current_segment)
                current_segment = [ctx]
                current_tokens = ctx_tokens
            else:
                current_segment.append(ctx)
                current_tokens += ctx_tokens

        if current_segment:
            segments.append(current_segment)

        return segments

    def generate_segmented_summary_prompt(
        self,
        segments: List[List[ChatContext]],
        user_query: str,
        intent_type: str
    ) -> str:
        if len(segments) == 1:
            return self._generate_single_segment_prompt(segments[0], user_query, intent_type)

        system_instruction = (
            "这是一段较长的聊天记录，将被分成多个部分进行分析。"
            "请先分别理解每个部分的内容，最后给出整体的总结或回答。"
        )

        all_content = []
        for i, segment in enumerate(segments, 1):
            segment_lines = []
            for ctx in segment:
                line = f"{ctx.sender}: {ctx.content}"
                if ctx.has_image:
                    line += " [图片]"
                if ctx.has_video:
                    line += " [视频]"
                segment_lines.append(line)
            all_content.append(f"--- 第{i}部分 ---\n" + "\n".join(segment_lines))

        full_content = "\n\n".join(all_content)

        prompt = f"""{system_instruction}

{full_content}

用户说："{user_query if user_query else '你怎么看？'}"

请综合以上所有内容，给出自然、口语化的回应。直接像朋友聊天一样回应，不要加"根据聊天记录"之类的前缀。"""

        return prompt

    def _generate_single_segment_prompt(
        self,
        contexts: List[ChatContext],
        user_query: str,
        intent_type: str
    ) -> str:
        chat_lines = []
        for ctx in contexts:
            line = f"{ctx.sender}: {ctx.content}"
            if ctx.has_image:
                line += " [图片]"
            if ctx.has_video:
                line += " [视频]"
            if ctx.has_file:
                line += " [文件]"
            chat_lines.append(line)

        chat_content = "\n".join(chat_lines)

        if intent_type == "summary":
            system_instruction = (
                "你正在查看一段聊天记录。请用自然、口语化的方式总结这段对话的核心内容，"
                "就像朋友之间转述聊天内容一样。"
            )
        elif intent_type == "analysis":
            system_instruction = (
                "你正在查看一段聊天记录。请基于这段对话给出你的看法或分析，"
                "用朋友间聊天的口吻，自然、随意一些。"
            )
        elif intent_type == "query":
            system_instruction = (
                "你正在查看一段聊天记录。请根据对话内容回答用户的问题，"
                "用自然、口语化的方式回应。"
            )
        else:
            system_instruction = (
                "你正在查看一段聊天记录。请自然地回应，可以总结内容、"
                "表达看法，或者回答可能的问题。用朋友间聊天的随意口吻。"
            )

        prompt = f"""{system_instruction}

{chat_content}

用户说："{user_query if user_query else '你怎么看？'}"

请直接给出自然、口语化的回应，不要加"根据聊天记录"之类的前缀。"""

        return prompt


@register(
    "forward_reader",
    "EraAsh",
    "智能感知合并转发消息，支持多模态分析，全链路日志追踪，消息去重与性能优化",
    "2.0.2",
    "https://github.com/EraAsh/astrbot_plugin_forward_reader"
)
class ForwardReader(Star):
    """
    智能合并转发消息阅读器 v2.0.2

    修复内容：
    1. 修复配置系统，移除冗余的vision_llm_provider字段
    2. 新增专用LLM工具函数解析合并转发消息
    3. 优化消息处理逻辑，确保完整处理链路正常
    4. 修复多模态内容提取和结构化数据提取
    """

    EXPLICIT_COMMAND_PATTERNS = {
        "summary": ["总结合并转发", "总结转发", "总结聊天记录"],
        "analysis": ["分析合并转发", "分析转发", "解读合并转发", "解读转发"],
        "query": ["查询合并转发", "查询转发"]
    }

    NATURAL_INTENT_PATTERNS = {
        "summary": [
            r"(总结|概括|归纳).*(合并转发|转发消息|聊天记录|这段对话|该消息)",
            r"(这段|这个|该)(合并转发|转发消息|聊天记录).*(讲了什么|说了什么|主要内容)",
            r"(这段|这个|该).*(内容|消息).*(讲了什么|说了什么|主要内容)"
        ],
        "analysis": [
            r"(分析|解读|评价|看看|看下|看一看).*(合并转发|转发消息|聊天记录|这段对话|该消息)",
            r"(这段|这个|该)(合并转发|转发消息|聊天记录).*(怎么看|什么意思|怎么理解)",
            r"(分析|解读|看看|看下).*(这个|这段|该).*(内容|消息)",
            r"(看看|看下|看一眼).*(发的|里面|这个|这段).*(啥|什么|内容)"
        ],
        "query": [
            r"(谁|有没有|是否).*(在|于)?.*(合并转发|转发消息|聊天记录|这段对话).*(说|提到)",
            r"(合并转发|转发消息|聊天记录).*(里|中).*(谁|什么|哪里|何时|为什么|怎么)"
        ]
    }

    NATURAL_RESPONSES = {
        "thinking": [
            "让我看看这段对话...",
            "稍等，我浏览一下聊天记录...",
            "正在看这些内容...",
            "让我了解一下这段对话的背景..."
        ],
        "thinking_with_image": [
            "让我看看这段对话和图片...",
            "稍等，我浏览一下聊天记录和图片...",
            "正在看这些内容，包括里面的图片..."
        ],
        "error_vague": [
            "这段内容有点复杂，不太好概括呢。",
            "这些内容有点零散，不太好总结。",
            "抱歉，这部分内容我理解起来有点困难。"
        ],
        "error_multimodal": [
            "这里有些图片/视频我没法看清，只能告诉你文字部分的内容。",
            "图片内容我看不太清楚，不过文字部分我可以帮你分析。"
        ]
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()

        log_level = self.logging_config.get("log_level", "INFO")
        enable_trace = self.logging_config.get("enable_trace_log", False)
        self.slogger = StructuredLogger("ForwardReader", log_level, enable_trace)

        dedup_ttl = self.performance_config.get("dedup_ttl", 3600)
        self.dedup_cache = MessageDeduplicationCache(dedup_ttl)

        self.state_manager = AnalysisStateManager()
        self.task_queue = PriorityTaskQueue()

        max_images = self.multimodal_config.get("max_images", 5)
        max_videos = self.multimodal_config.get("max_videos", 2)
        media_timeout = float(self.multimodal_config.get("media_request_timeout", 10.0) or 10.0)
        media_retry = int(self.multimodal_config.get("media_retry_times", 2) or 2)
        self.media_processor = MediaBatchProcessor(max_images, max_videos, media_timeout, media_retry)

        token_budget = self.performance_config.get("token_budget", 4000)
        segment_size = self.performance_config.get("segment_size", 1500)
        self.token_controller = TokenBudgetController(token_budget, segment_size)

        self._background_tasks: Set[asyncio.Task] = set()
        self._running = True

        asyncio.create_task(self.dedup_cache.start_cleanup_task())

        self.slogger.info("init", "plugin_initialized",
                         "ForwardReader v2.0.2 初始化完成",
                         dedup_ttl=dedup_ttl, token_budget=token_budget)

    def _load_config(self):
        """加载配置项"""
        self.enable_smart_detection = self.config.get("enable_smart_detection", True)
        self.enable_direct_analysis = self.config.get("enable_direct_analysis", False)
        self.enable_reply_analysis = self.config.get("enable_reply_analysis", True)
        self.enable_proactive_reply = self.config.get("enable_proactive_reply", False)
        self.sensitivity = self.config.get("sensitivity", 0.7)
        self.max_messages = self.config.get("max_messages", 200)
        self.show_thinking = self.config.get("show_thinking", True)

        self.multimodal_config = self.config.get("multimodal_config", {})
        if not self.multimodal_config:
            self.multimodal_config = {
                "enable_multimodal": self.config.get("enable_multimodal", True),
                "vision_provider_id": "",
                "max_images": self.config.get("max_images", 5),
                "max_videos": 2,
                "image_quality": "auto",
                "batch_process": True,
                "media_request_timeout": 10.0,
                "media_retry_times": 2
            }

        self.performance_config = self.config.get("performance_config", {})
        if not self.performance_config:
            self.performance_config = {
                "enable_deduplication": True,
                "dedup_ttl": 3600,
                "auto_analysis_delay": 2.0,
                "token_budget": 4000,
                "enable_segmented_summary": True,
                "segment_size": 1500
            }

        self.logging_config = self.config.get("logging_config", {})
        if not self.logging_config:
            self.logging_config = {
                "log_level": "INFO",
                "enable_performance_log": True,
                "enable_trace_log": False
            }

        self.parse_nested_forward = self.config.get("parse_nested_forward", True)
        self.max_nested_depth = self.config.get("max_nested_depth", 3)

    def _normalize_user_message(self, message: str) -> str:
        """归一化用户文本，便于做触发判定。"""
        if not message:
            return ""
        text = message.strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def _detect_explicit_command(self, message: str) -> Tuple[bool, str, str]:
        """检测显式命令触发。返回: (是否命令, 意图, 命令后的参数文本)"""
        normalized = self._normalize_user_message(message)
        if not normalized:
            return False, "", ""

        # 兼容 /分析合并转发 xxx、分析合并转发: xxx、/forward_reader xxx
        command_match = re.match(
            r"^/?(分析合并转发|分析转发|解读合并转发|解读转发|总结合并转发|总结转发|总结聊天记录|查询合并转发|查询转发|forward_reader|fr)\b[:：]?\s*(.*)$",
            normalized,
            flags=re.IGNORECASE
        )
        if not command_match:
            return False, "", ""

        command_word = command_match.group(1).lower()
        command_args = command_match.group(2).strip()

        if "总结" in command_word:
            return True, "summary", command_args
        if "查询" in command_word:
            return True, "query", command_args
        return True, "analysis", command_args

    def _detect_natural_intent(self, message: str) -> Tuple[bool, str]:
        """检测自然语言意图（严格模式，仅在明确提及目标消息时触发）。"""
        normalized = self._normalize_user_message(message)
        if not normalized:
            return False, ""

        for intent_type, patterns in self.NATURAL_INTENT_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, normalized, flags=re.IGNORECASE):
                    return True, intent_type

        return False, ""

    def _is_brief_analysis_request(self, message: str) -> bool:
        """在已锁定目标消息（如引用/直接转发）时，识别简短分析请求。"""
        normalized = self._normalize_user_message(message)
        if not normalized:
            return False

        brief_keywords = [
            "分析这个", "分析下", "分析一下", "解读一下", "看下", "看看这个", "看看", "看一眼",
            "这个内容", "这个消息", "这段内容", "这段消息", "发的啥", "发了啥", "说了啥", "啥意思"
        ]
        return any(k in normalized for k in brief_keywords)

    def _is_negative_intent(self, message: str) -> bool:
        """识别明确取消/拒绝分析的表达。"""
        normalized = self._normalize_user_message(message)
        if not normalized:
            return False

        negative_keywords = ["不用分析", "别分析", "不需要分析", "无需分析", "先别看", "不用看"]
        return any(k in normalized for k in negative_keywords)

    def _build_inline_forward_id(self, payload: Dict[str, Any]) -> str:
        """为内联合并转发生成稳定ID，便于去重。"""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return f"inline_{digest}"

    def _generate_natural_thinking(self, has_multimodal: bool = False) -> str:
        """生成自然的思考提示"""
        if has_multimodal:
            return random.choice(self.NATURAL_RESPONSES["thinking_with_image"])
        return random.choice(self.NATURAL_RESPONSES["thinking"])

    def _generate_vague_error_response(self) -> str:
        """生成模糊的错误响应"""
        return random.choice(self.NATURAL_RESPONSES["error_vague"])

    @filter.llm_tool(name="parse_forward_message")
    async def parse_forward_message_tool(
        self,
        event: AstrMessageEvent,
        forward_id: str
    ) -> str:
        '''
        解析合并转发消息内容，提取结构化数据（包括文本、图片及多媒体信息）。
        当需要分析、总结或理解合并转发的聊天记录时调用此工具。

        Args:
            forward_id(string): 合并转发消息的唯一标识ID

        Returns:
            包含聊天记录结构化数据的JSON字符串，格式如下：
            {
                "messages": [
                    {
                        "sender": "发送者昵称",
                        "content": "消息内容",
                        "has_image": false,
                        "has_video": false,
                        "has_file": false
                    }
                ],
                "image_urls": ["图片URL列表"],
                "total_messages": 消息总数,
                "has_multimodal": 是否包含多媒体
            }
        '''
        trace_id = self.slogger._generate_trace_id()
        self.slogger.info(trace_id, "llm_tool_call", "LLM工具被调用",
                         forward_id=forward_id[:20] if forward_id else "")

        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps({"error": "该功能仅在QQ平台可用"}, ensure_ascii=False)

        try:
            contexts, image_urls, nested_count = await self._extract_forward_messages(
                event, forward_id, current_depth=0, trace_id=trace_id
            )

            messages_data = []
            for ctx in contexts:
                messages_data.append({
                    "sender": ctx.sender,
                    "content": ctx.content,
                    "has_image": ctx.has_image,
                    "has_video": ctx.has_video,
                    "has_file": ctx.has_file
                })

            result = {
                "messages": messages_data,
                "image_urls": image_urls,
                "total_messages": len(contexts),
                "has_multimodal": len(image_urls) > 0 or any(
                    ctx.has_video or ctx.has_image for ctx in contexts
                ),
                "nested_forward_count": nested_count
            }

            self.slogger.info(trace_id, "llm_tool_complete", "工具执行完成",
                            messages=len(contexts), images=len(image_urls))

            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            self.slogger.error(trace_id, "llm_tool_error", f"工具执行失败: {e}")
            return json.dumps({"error": f"解析失败: {str(e)}"}, ensure_ascii=False)

    @filter.llm_tool(name="analyze_forward_message")
    async def analyze_forward_message_tool(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        user_query: str = "请分析这段聊天记录"
    ) -> str:
        '''
        分析合并转发消息的内容（包括文本、图片及多媒体信息），提取结构化数据并生成内容理解。
        当用户需要分析、总结或理解一段合并转发的聊天记录时调用此工具。

        Args:
            forward_id(string): 合并转发消息的唯一标识ID
            user_query(string): 用户的具体问题或分析要求，如"总结一下这段对话"、"他们聊了什么"等

        Returns:
            分析结果的文本内容
        '''
        trace_id = self.slogger._generate_trace_id()
        self.slogger.info(trace_id, "llm_tool_call", "LLM工具被调用",
                         forward_id=forward_id[:20] if forward_id else "",
                         query=user_query[:50])

        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return "抱歉，该功能仅在QQ平台可用。"

        try:
            task = AnalysisTask(
                task_id=f"llm_tool_{trace_id}",
                forward_id=forward_id,
                event=event,
                user_query=user_query,
                intent_type="llm_tool",
                priority=AnalysisPriority.MANUAL,
                is_direct_analysis=False
            )

            result = await self._execute_analysis(task, trace_id)
            return result if result else "分析完成"

        except Exception as e:
            self.slogger.error(trace_id, "llm_tool_error", f"工具执行失败: {e}")
            return "分析合并转发消息时出现了一些问题，请稍后重试。"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """智能消息处理器"""
        trace_id = self.slogger._generate_trace_id()

        self.slogger.debug(trace_id, "msg_receive", "收到消息事件",
                          platform=event.get_platform_name(),
                          user=event.get_sender_id(),
                          group=event.get_group_id() or "private",
                          content_preview=event.message_str[:50] if event.message_str else "")

        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            self.slogger.debug(trace_id, "msg_receive", "非aiocqhttp平台，跳过")
            return

        if not self.enable_smart_detection:
            self.slogger.debug(trace_id, "msg_receive", "智能检测已禁用")
            return

        forward_id: Optional[str] = None
        forward_payload: Optional[Dict[str, Any]] = None
        reply_seg: Optional[Comp.Reply] = None
        user_message = event.message_str.strip() if event.message_str else ""

        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                seg_data = getattr(seg, "data", {}) or {}
                forward_id = (
                    getattr(seg, "id", None)
                    or getattr(seg, "resid", None)
                    or seg_data.get("id")
                    or seg_data.get("resid")
                    or seg_data.get("forward_id")
                )
                if isinstance(seg_data, dict) and isinstance(seg_data.get("messages"), list):
                    forward_payload = {"messages": seg_data.get("messages", [])}
                if forward_id:
                    forward_id = str(forward_id)
            elif isinstance(seg, Comp.Reply):
                reply_seg = seg

        has_explicit_command, explicit_intent, command_args = self._detect_explicit_command(user_message)
        has_natural_intent, natural_intent = self._detect_natural_intent(user_message)

        self.slogger.info(trace_id, "analysis_type", "消息类型判定",
                         has_forward=bool(forward_id or forward_payload),
                         has_reply=bool(reply_seg),
                         explicit_command=has_explicit_command,
                         natural_intent=has_natural_intent,
                         user_message=user_message[:60] if user_message else "")

        should_analyze = False
        intent_type = "analysis"
        is_direct_analysis = False
        priority = AnalysisPriority.MANUAL
        trigger_reason = ""

        # 场景A：引用消息（最推荐）
        if self.enable_reply_analysis and reply_seg:
            try:
                reply_forward_id, reply_forward_payload = await self._extract_forward_from_reply(event, reply_seg)
                if reply_forward_id or reply_forward_payload:
                    forward_id = reply_forward_id or forward_id
                    forward_payload = reply_forward_payload or forward_payload

                    if self._is_negative_intent(user_message):
                        self.slogger.info(trace_id, "analysis_gate", "检测到取消分析意图，按用户要求跳过")
                        return

                    # 规则：引用目标合并转发本身即为显式触发；意图词用于细化类型
                    should_analyze = True
                    if has_explicit_command:
                        intent_type = explicit_intent
                        trigger_reason = "reply_command"
                    elif has_natural_intent:
                        intent_type = natural_intent
                        trigger_reason = "reply_natural_intent"
                    elif self._is_brief_analysis_request(user_message):
                        intent_type = "analysis"
                        trigger_reason = "reply_brief_intent"
                    elif not user_message:
                        intent_type = "summary"
                        trigger_reason = "reply_only"
                    else:
                        intent_type = "analysis"
                        trigger_reason = "reply_default"
                    priority = AnalysisPriority.MANUAL
            except Exception as e:
                self.slogger.warning(trace_id, "analysis_type", f"提取引用转发失败: {e}")
                return

        # 场景B：直接发送合并转发（必须显式命令或明确自然语言意图）
        elif self.enable_direct_analysis and (forward_id or forward_payload):
            if has_explicit_command or has_natural_intent or self._is_brief_analysis_request(user_message):
                should_analyze = True
                is_direct_analysis = True
                if has_explicit_command:
                    intent_type = explicit_intent
                elif has_natural_intent:
                    intent_type = natural_intent
                else:
                    intent_type = "analysis"
                trigger_reason = "direct_with_intent"
                priority = AnalysisPriority.MANUAL
            else:
                self.slogger.info(
                    trace_id,
                    "analysis_gate",
                    "检测到合并转发但无明确分析指令，静默跳过"
                )

        # 场景C：主动回复模式（仍要求自然语言意图，避免@即触发）
        elif self.enable_proactive_reply and (forward_id or forward_payload):
            is_at = any(isinstance(seg, Comp.At) for seg in event.message_obj.message)
            if (is_at or event.is_at_or_wake_command) and (has_explicit_command or has_natural_intent):
                should_analyze = True
                intent_type = explicit_intent if has_explicit_command else natural_intent
                trigger_reason = "proactive_with_intent"
                priority = AnalysisPriority.MANUAL

        # 显式命令但没有目标合并转发：拒绝触发并记录原因
        if has_explicit_command and not (forward_id or forward_payload):
            self.slogger.info(trace_id, "analysis_gate", "检测到显式命令，但未找到目标合并转发，跳过")
            return

        if forward_payload and not forward_id:
            forward_id = self._build_inline_forward_id(forward_payload)

        if not should_analyze or not (forward_id or forward_payload):
            self.slogger.debug(trace_id, "analysis_type", "未触发分析")
            return

        self.slogger.info(trace_id, "analysis_gate", "通过触发门控",
                         reason=trigger_reason,
                         intent=intent_type,
                         direct=is_direct_analysis)

        if self.performance_config.get("enable_deduplication", True):
            is_dup = await self.dedup_cache.is_duplicate(
                forward_id,
                event.get_sender_id(),
                event.get_group_id()
            )
            if is_dup:
                self.slogger.info(trace_id, "deduplication", "消息已分析过，跳过")
                return

        task = AnalysisTask(
            task_id=trace_id,
            forward_id=forward_id,
            event=event,
            user_query=command_args or user_message,
            intent_type=intent_type,
            priority=priority,
            delay_seconds=self.performance_config.get("auto_analysis_delay", 2.0),
            is_direct_analysis=is_direct_analysis,
            forward_payload=forward_payload
        )

        added = await self.task_queue.add_task(task)
        if not added:
            self.slogger.info(trace_id, "task_queue", "任务已在队列中，跳过")
            return

        if priority == AnalysisPriority.MANUAL:
            self.slogger.info(trace_id, "task_execute", "立即执行手动任务")
            await self._execute_analysis_task(task)
        else:
            self.slogger.info(trace_id, "task_execute",
                            "延迟触发自动任务", delay=task.delay_seconds)
            asyncio.create_task(self._delayed_analysis(task))

    async def _delayed_analysis(self, task: AnalysisTask):
        """延迟分析（用于自动分析模式）"""
        session_id = task.event.unified_msg_origin

        await self.state_manager.set_state(session_id, AnalysisState.PENDING)

        try:
            await asyncio.sleep(task.delay_seconds)

            current_state = await self.state_manager.get_state(session_id)
            if current_state != AnalysisState.PENDING:
                self.slogger.info(task.task_id, "delayed_analysis",
                                "状态已变更，取消延迟任务", new_state=current_state.name)
                return

            await self._execute_analysis_task(task)
        except asyncio.CancelledError:
            self.slogger.debug(task.task_id, "delayed_analysis", "延迟任务被取消")
        except Exception as e:
            self.slogger.error(task.task_id, "delayed_analysis", f"延迟任务异常: {e}")

    async def _execute_analysis_task(self, task: AnalysisTask):
        """执行分析任务"""
        session_id = task.event.unified_msg_origin
        trace_id = task.task_id

        if not await self.state_manager.can_start_analysis(session_id):
            self.slogger.info(trace_id, "task_execute",
                            "当前会话正在分析中，加入队列等待")
            for _ in range(30):
                await asyncio.sleep(1)
                if await self.state_manager.can_start_analysis(session_id):
                    break
            else:
                self.slogger.warning(trace_id, "task_execute", "等待超时，取消任务")
                return

        await self.state_manager.set_state(session_id, AnalysisState.ANALYZING)

        if self.performance_config.get("enable_deduplication", True):
            await self.dedup_cache.add(
                task.forward_id,
                task.event.get_sender_id(),
                task.event.get_group_id()
            )

        metrics = PerformanceMetrics()

        try:
            self.slogger.info(trace_id, "analysis_start", "开始分析",
                            forward_id=task.forward_id[:20] if task.forward_id else "")

            setattr(task.event, "_forward_payload_override", task.forward_payload)
            result = await self._prepare_analysis(
                task.event,
                task.forward_id,
                task.user_query,
                task.intent_type,
                task.is_direct_analysis,
                trace_id
            )

            if not result.success:
                await self._handle_analysis_error(task.event, result, trace_id)
                return

            metrics.image_count = len(result.image_urls)
            metrics.token_input = result.token_estimate

            if self.show_thinking:
                thinking_msg = self._generate_natural_thinking(result.has_multimodal)
                await self._send_natural_message(task.event, thinking_msg)

            self.slogger.debug(trace_id, "multimodal_detect", "多模态检测完成",
                             has_multimodal=result.has_multimodal,
                             image_count=len(result.image_urls),
                             nested_count=result.nested_forward_count)

            image_urls_to_use: List[str] = []
            media_summary = ""
            if result.has_multimodal and self.multimodal_config.get("enable_multimodal", True):
                media_list = [MediaContent(type="image", url=url) for url in result.image_urls]
                image_urls_to_use, media_summary = await self.media_processor.process_media_batch(
                    media_list, trace_id, self.slogger
                )

            self.slogger.debug(trace_id, "media_preprocess", "媒体预处理完成",
                             processed_images=len(image_urls_to_use),
                             raw_images=len(result.image_urls),
                             summary=media_summary)

            llm_prompt = result.content
            if result.has_multimodal and self.multimodal_config.get("enable_multimodal", True):
                if len(image_urls_to_use) == 0 and len(result.image_urls) > 0:
                    llm_prompt += (
                        "\n\n[多模态回退说明] 聊天记录中包含图片，但当前环境无法直接读取这些图片链接。"
                        "请重点参考上下文中的[图片]标记及周边文本进行分析，不要臆造图片细节。"
                    )
                    self.slogger.warning(trace_id, "llm_request", "图片全部不可用，降级为文本+图片占位语义")
                elif len(image_urls_to_use) < len(result.image_urls):
                    llm_prompt += (
                        "\n\n[多模态部分回退说明] 仅部分图片可访问，请结合可访问图片和文本上下文进行分析。"
                    )

            self.slogger.info(trace_id, "llm_request", "调用LLM生成回应",
                            is_direct_analysis=task.is_direct_analysis,
                            image_count=len(image_urls_to_use),
                            raw_image_count=len(result.image_urls),
                            token_estimate=result.token_estimate)

            metrics.llm_start_time = time.time()

            manual_provider_id = str(self.multimodal_config.get("vision_provider_id", "")).strip()
            if manual_provider_id:
                manual_result = await self._request_llm_with_selected_provider(
                    provider_id=manual_provider_id,
                    prompt=llm_prompt,
                    image_urls=image_urls_to_use,
                    trace_id=trace_id
                )
                if manual_result:
                    metrics.llm_end_time = time.time()
                    await self._send_natural_message(task.event, manual_result)
                    history_ok = await self._append_assistant_text_to_history(
                        task.event,
                        manual_result,
                        trace_id=trace_id,
                        source="manual_provider"
                    )
                    self.slogger.info(
                        trace_id,
                        "context_write",
                        "手动提供商结果写回状态",
                        send_success=True,
                        history_written=history_ok
                    )
                    task.event.stop_event()

                    if self.logging_config.get("enable_performance_log", True):
                        self.slogger.performance(trace_id, metrics)

                    self.slogger.info(
                        trace_id,
                        "analysis_complete",
                        "分析完成（已使用手动指定多模态提供商）",
                        provider_id=manual_provider_id,
                        duration=f"{metrics.total_duration:.2f}s"
                    )
                    return

                self.slogger.warning(
                    trace_id,
                    "llm_request",
                    "手动指定多模态提供商调用失败，已回退到默认提供商",
                    provider_id=manual_provider_id
                )

            track_info = {
                "trace_id": trace_id,
                "origin": task.event.unified_msg_origin,
                "time": time.time()
            }
            setattr(task.event, "_forward_reader_context_track", track_info)

            if task.is_direct_analysis:
                await task.event.request_llm(
                    prompt=llm_prompt,
                    image_urls=image_urls_to_use,
                    contexts=[],
                    session_id=None
                )
            else:
                await task.event.request_llm(
                    prompt=llm_prompt,
                    image_urls=image_urls_to_use
                )

            metrics.llm_end_time = time.time()
            self.slogger.info(trace_id, "context_write", "已提交到框架LLM流程，等待on_llm_response回填上下文")
            task.event.stop_event()

            if self.logging_config.get("enable_performance_log", True):
                self.slogger.performance(trace_id, metrics)

            self.slogger.info(trace_id, "analysis_complete", "分析完成",
                            duration=f"{metrics.total_duration:.2f}s")

        except Exception as e:
            self.slogger.error(trace_id, "analysis_error", f"分析过程异常: {e}")
        finally:
            await self.state_manager.set_state(session_id, AnalysisState.COMPLETED)

    async def _execute_analysis(self, task: AnalysisTask, trace_id: str) -> str:
        """执行分析并返回结果（用于LLM工具）"""
        session_id = task.event.unified_msg_origin

        await self.state_manager.set_state(session_id, AnalysisState.ANALYZING)

        try:
            setattr(task.event, "_forward_payload_override", task.forward_payload)
            result = await self._prepare_analysis(
                task.event,
                task.forward_id,
                task.user_query,
                task.intent_type,
                task.is_direct_analysis,
                trace_id
            )

            if not result.success:
                return "无法解析该合并转发消息。"

            image_urls_to_use: List[str] = []
            if result.has_multimodal and self.multimodal_config.get("enable_multimodal", True):
                media_list = [MediaContent(type="image", url=url) for url in result.image_urls]
                image_urls_to_use, _ = await self.media_processor.process_media_batch(
                    media_list, trace_id, self.slogger
                )

            llm_prompt = result.content
            if result.has_multimodal and len(result.image_urls) > 0 and len(image_urls_to_use) == 0:
                llm_prompt += (
                    "\n\n[多模态回退说明] 检测到图片但当前环境不可访问图片链接，"
                    "请基于文本与[图片]占位符谨慎分析，不要编造图片细节。"
                )

            provider = self._get_provider_by_id(
                str(self.multimodal_config.get("vision_provider_id", "")).strip()
            )
            if not provider:
                provider = self.context.get_using_provider()
            if not provider:
                return "LLM提供商未配置，无法进行分析。"

            response = await provider.text_chat(
                prompt=llm_prompt,
                session_id=None,
                contexts=[],
                image_urls=image_urls_to_use,
                func_tool=None,
                system_prompt="你是一个专业的聊天记录分析助手。"
            )

            if response and response.role == "assistant" and response.completion_text:
                completion = response.completion_text.strip()
                if completion:
                    return completion
            return "分析完成，但未能获取有效结果。"

        except Exception as e:
            self.slogger.error(trace_id, "analysis_error", f"分析过程异常: {e}")
            return f"分析过程中出现错误: {str(e)}"
        finally:
            await self.state_manager.set_state(session_id, AnalysisState.COMPLETED)

    async def _prepare_analysis(
        self,
        event: AiocqhttpMessageEvent,
        forward_id: str,
        user_query: str,
        intent_type: str,
        is_direct_analysis: bool,
        trace_id: str
    ) -> AnalysisResult:
        """准备分析内容"""
        try:
            contexts, image_urls, nested_count = await self._extract_forward_messages(
                event,
                forward_id,
                current_depth=0,
                trace_id=trace_id,
                forward_payload=getattr(event, "_forward_payload_override", None)
            )

            if not contexts:
                return AnalysisResult(success=False, error_type="empty")

            total_text = "\n".join([ctx.content for ctx in contexts])
            has_multimodal = len(image_urls) > 0 or any(
                ctx.has_video or ctx.has_image for ctx in contexts
            )

            token_estimate = self.token_controller.estimate_tokens(total_text, len(image_urls))

            self.slogger.debug(trace_id, "content_extract", "内容提取完成",
                             chat_count=len(contexts),
                             text_length=len(total_text),
                             image_count=len(image_urls),
                             token_estimate=token_estimate)

            if len(total_text.strip()) < 10 and not has_multimodal:
                self.slogger.debug(trace_id, "content_extract", "内容过少，可能无法解析")
                return AnalysisResult(success=False, error_type="unreadable")

            enable_segmented = self.performance_config.get("enable_segmented_summary", True)
            if enable_segmented and self.token_controller.should_segment(total_text, len(image_urls)):
                segments = self.token_controller.segment_content(contexts)
                prompt = self.token_controller.generate_segmented_summary_prompt(
                    segments, user_query, intent_type
                )
                self.slogger.info(trace_id, "segment_summary", "启用分段摘要", segments=len(segments))
            else:
                prompt = self._generate_analysis_prompt(
                    contexts, user_query, intent_type, has_multimodal, is_direct_analysis
                )

            return AnalysisResult(
                success=True,
                content=prompt,
                chat_count=len(contexts),
                image_urls=image_urls,
                has_multimodal=has_multimodal,
                nested_forward_count=nested_count,
                token_estimate=token_estimate
            )

        except json.JSONDecodeError as e:
            self.slogger.warning(trace_id, "content_extract", f"JSON解析失败: {e}")
            return AnalysisResult(success=False, error_type="unreadable")
        except ValueError as e:
            self.slogger.warning(trace_id, "content_extract", f"内容无效: {e}")
            return AnalysisResult(success=False, error_type="unreadable")
        except Exception as e:
            self.slogger.error(trace_id, "content_extract", f"准备分析失败: {e}")
            return AnalysisResult(success=False, error_type="unknown")

    async def _extract_forward_messages(
        self,
        event: AiocqhttpMessageEvent,
        forward_id: Optional[str],
        current_depth: int,
        trace_id: str,
        forward_payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[ChatContext], List[str], int]:
        """提取合并转发中的消息内容（支持ID拉取与内联payload两种来源）。"""
        if current_depth > self.max_nested_depth:
            self.slogger.debug(trace_id, "extract_forward",
                             f"达到最大嵌套深度 {self.max_nested_depth}，停止递归")
            return [], [], 0

        messages: List[dict] = []

        # 优先使用内联数据，避免因 id 缺失导致无法解析
        if isinstance(forward_payload, dict):
            messages = self._extract_messages_from_forward_data(forward_payload)
            if messages:
                self.slogger.debug(trace_id, "extract_forward", "使用内联合并转发数据解析",
                                 current_depth=current_depth, message_count=len(messages))

        # 内联数据不可用时，回退到 get_forward_msg
        if not messages:
            if not forward_id:
                self.slogger.error(trace_id, "extract_forward", "缺少forward_id且无内联数据")
                raise ValueError("无法获取合并转发内容：缺少可用ID")

            client = event.bot
            try:
                forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
                messages = self._extract_messages_from_forward_data(forward_data)
                self.slogger.debug(trace_id, "extract_forward", "通过API获取合并转发成功",
                                 current_depth=current_depth, message_count=len(messages))
            except Exception as e:
                self.slogger.error(trace_id, "extract_forward", f"获取合并转发失败: {e}")
                raise ValueError("无法获取合并转发内容")

        if not messages:
            raise ValueError("合并转发数据为空")

        contexts: List[ChatContext] = []
        image_urls: List[str] = []
        message_count = 0
        nested_forward_count = 0

        for node in messages:
            if message_count >= self.max_messages:
                self.slogger.debug(trace_id, "extract_forward",
                                 f"达到最大消息数 {self.max_messages}")
                break

            sender_obj = node.get("sender", {}) if isinstance(node, dict) else {}
            sender_name = (
                sender_obj.get("nickname")
                or node.get("nickname")
                or node.get("name")
                or "未知用户"
            ) if isinstance(node, dict) else "未知用户"

            content_chain = []
            if isinstance(node, dict):
                content_chain = (
                    node.get("content")
                    or node.get("message")
                    or node.get("raw_message")
                    or []
                )

            ctx = ChatContext(sender=sender_name, content="")

            if isinstance(content_chain, str):
                ctx.content = content_chain
            else:
                if isinstance(content_chain, dict):
                    content_chain = [content_chain]
                elif not isinstance(content_chain, list):
                    content_chain = [content_chain] if content_chain else []

                text_parts = []
                for segment in content_chain:
                    seg_type = None
                    seg_data = {}

                    if isinstance(segment, str):
                        text_parts.append(segment)
                        continue

                    if isinstance(segment, dict):
                        seg_type = segment.get("type")
                        seg_data = segment.get("data", {}) or {}
                    else:
                        seg_type = getattr(segment, "type", None)
                        seg_data = getattr(segment, "data", {}) or {}

                    if seg_type in ("text", "plain"):
                        text_parts.append(seg_data.get("text", ""))
                    elif seg_type == "image":
                        url = self._extract_image_url_from_segment_data(seg_data)
                        if url:
                            ctx.has_image = True
                            ctx.media_list.append(MediaContent(type="image", url=url))
                            image_urls.append(url)
                            text_parts.append("[图片]")
                        else:
                            ctx.has_image = True
                            text_parts.append("[图片:链接缺失]")
                    elif seg_type == "video":
                        ctx.has_video = True
                        text_parts.append("[视频]")
                    elif seg_type == "file":
                        ctx.has_file = True
                        text_parts.append("[文件]")
                    elif seg_type == "forward" and self.parse_nested_forward:
                        nested_id = self._extract_forward_id_from_segment_data(seg_data)
                        if nested_id and current_depth < self.max_nested_depth:
                            nested_forward_count += 1
                            self.slogger.debug(trace_id, "extract_forward",
                                             f"发现嵌套转发，深度 {current_depth + 1}")
                            try:
                                nested_contexts, nested_images, _ = await self._extract_forward_messages(
                                    event, nested_id, current_depth + 1, trace_id
                                )
                                contexts.extend(nested_contexts)
                                image_urls.extend(nested_images)
                            except Exception as e:
                                self.slogger.warning(trace_id, "extract_forward",
                                                   f"嵌套解析失败: {e}")

                ctx.content = "".join(text_parts).strip()

            if ctx.content or ctx.media_list:
                contexts.append(ctx)
                message_count += 1

        return contexts, image_urls, nested_forward_count

    def _extract_image_url_from_segment_data(self, seg_data: dict) -> str:
        """从 image 段 data 中兼容提取 URL。"""
        if not isinstance(seg_data, dict):
            return ""

        # 常见字段优先级：url > source_url > src > file(若为http)
        for key in ("url", "source_url", "src", "origin", "origin_url"):
            value = seg_data.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()

        file_value = seg_data.get("file")
        if isinstance(file_value, str) and file_value.strip().startswith(("http://", "https://")):
            return file_value.strip()

        return ""

    def _extract_forward_id_from_segment_data(self, seg_data: dict) -> Optional[str]:
        """从 forward 段 data 中兼容提取 forward_id（id/resid/forward_id）。"""
        if not isinstance(seg_data, dict):
            return None

        for key in ("id", "resid", "forward_id"):
            value = seg_data.get(key)
            if value:
                return str(value)

        return None

    def _extract_messages_from_forward_data(self, forward_data: Any) -> List[dict]:
        """兼容不同协议端返回结构，提取 messages 列表。"""
        if isinstance(forward_data, list):
            return [x for x in forward_data if isinstance(x, dict)]

        if not isinstance(forward_data, dict):
            return []

        messages = forward_data.get("messages")
        if isinstance(messages, list):
            return [x for x in messages if isinstance(x, dict)]

        # 某些实现中 message 字段直接就是节点列表
        message_list = forward_data.get("message")
        if isinstance(message_list, list):
            return [x for x in message_list if isinstance(x, dict)]

        data_obj = forward_data.get("data")
        if isinstance(data_obj, dict):
            messages = data_obj.get("messages")
            if isinstance(messages, list):
                return [x for x in messages if isinstance(x, dict)]

            message_list = data_obj.get("message")
            if isinstance(message_list, list):
                return [x for x in message_list if isinstance(x, dict)]

        return []

    def _extract_forward_id_from_message_obj(self, message_obj) -> Optional[str]:
        """从 get_msg 返回结构中递归提取 forward_id。"""
        if isinstance(message_obj, list):
            for item in message_obj:
                found = self._extract_forward_id_from_message_obj(item)
                if found:
                    return found
            return None

        if not isinstance(message_obj, dict):
            return None

        seg_type = message_obj.get("type")
        if seg_type == "forward":
            seg_data = message_obj.get("data", {})
            found = self._extract_forward_id_from_segment_data(seg_data)
            if found:
                return found
            # 兼容某些实现把字段直接放在 segment 根级
            found = self._extract_forward_id_from_segment_data(message_obj)
            if found:
                return found

        for key in ("message", "messages", "content", "data"):
            child = message_obj.get(key)
            found = self._extract_forward_id_from_message_obj(child)
            if found:
                return found

        return None

    def _extract_forward_payload_from_message_obj(self, message_obj: Any) -> Optional[Dict[str, Any]]:
        """从 message 对象中递归提取内联合并转发 payload（messages）。"""
        if isinstance(message_obj, list):
            for item in message_obj:
                found = self._extract_forward_payload_from_message_obj(item)
                if found:
                    return found
            return None

        if not isinstance(message_obj, dict):
            return None

        seg_type = message_obj.get("type")
        if seg_type == "forward":
            seg_data = message_obj.get("data", {})
            if isinstance(seg_data, dict) and isinstance(seg_data.get("messages"), list):
                return {"messages": seg_data.get("messages", [])}
            if isinstance(message_obj.get("messages"), list):
                return {"messages": message_obj.get("messages", [])}

        for key in ("message", "messages", "content", "data"):
            child = message_obj.get(key)
            found = self._extract_forward_payload_from_message_obj(child)
            if found:
                return found

        return None

    def _extract_reply_message_id(self, reply_seg: Comp.Reply) -> Optional[str]:
        """兼容不同实现提取 Reply 指向的 message_id。"""
        candidate_values = [
            getattr(reply_seg, "id", None),
            getattr(reply_seg, "message_id", None),
            getattr(reply_seg, "msg_id", None),
        ]

        seg_data = getattr(reply_seg, "data", {}) or {}
        if isinstance(seg_data, dict):
            candidate_values.extend([
                seg_data.get("id"),
                seg_data.get("message_id"),
                seg_data.get("msg_id"),
            ])

        for value in candidate_values:
            if value is None:
                continue
            value_str = str(value).strip()
            if value_str:
                return value_str

        return None

    async def _extract_forward_from_reply(
        self,
        event: AiocqhttpMessageEvent,
        reply_seg: Comp.Reply
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """从引用消息中提取合并转发ID或内联payload。"""
        reply_message_id = self._extract_reply_message_id(reply_seg)
        if not reply_message_id:
            self.slogger.info("extract_reply", "extract_forward_from_reply", "Reply段缺少可用message_id")
            return None, None

        try:
            client = event.bot

            # 某些端要求 int message_id，优先尝试 int，失败后回退原始字符串
            original_msg = None
            call_errors: List[str] = []

            try:
                original_msg = await client.api.call_action('get_msg', message_id=int(reply_message_id))
            except Exception as e_int:
                call_errors.append(f"int_id失败: {e_int}")

            if not original_msg:
                try:
                    original_msg = await client.api.call_action('get_msg', message_id=reply_message_id)
                except Exception as e_str:
                    call_errors.append(f"str_id失败: {e_str}")

            if not original_msg or 'message' not in original_msg:
                self.slogger.info(
                    "extract_reply",
                    "extract_forward_from_reply",
                    "引用消息获取成功但未含message字段或为空",
                    message_id=reply_message_id,
                    errors=" | ".join(call_errors) if call_errors else ""
                )
                return None, None

            message_obj = original_msg.get("message")
            found_id = self._extract_forward_id_from_message_obj(message_obj)
            found_payload = self._extract_forward_payload_from_message_obj(message_obj)

            if not found_id and not found_payload:
                self.slogger.info(
                    "extract_reply",
                    "extract_forward_from_reply",
                    "引用目标不是合并转发，跳过",
                    message_id=reply_message_id
                )

            return found_id, found_payload

        except Exception as e:
            self.slogger.warning(
                "extract_reply",
                "extract_forward_from_reply",
                f"获取引用消息失败: {e}",
                message_id=reply_message_id
            )

        return None, None

    def _get_provider_by_id(self, provider_id: str):
        """按 provider_id 获取提供商实例，找不到则返回 None。"""
        if not provider_id:
            return None

        provider_mgr = getattr(self.context, "provider_manager", None)
        provider = None

        try:
            if provider_mgr and hasattr(provider_mgr, "inst_map"):
                provider = provider_mgr.inst_map.get(provider_id)

            if not provider and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None

        return provider

    async def _request_llm_with_selected_provider(
        self,
        provider_id: str,
        prompt: str,
        image_urls: List[str],
        trace_id: str
    ) -> Optional[str]:
        """使用手动指定的提供商调用多模态分析，失败返回 None。"""
        provider = self._get_provider_by_id(provider_id)
        if not provider:
            self.slogger.warning(
                trace_id,
                "llm_request",
                "未找到手动指定的多模态提供商",
                provider_id=provider_id
            )
            return None

        try:
            response = await provider.text_chat(
                prompt=prompt,
                session_id=None,
                contexts=[],
                image_urls=image_urls,
                func_tool=None,
                system_prompt="你是一个专业的聊天记录分析助手。"
            )
            if response and response.role == "assistant" and response.completion_text:
                return response.completion_text
        except Exception as e:
            self.slogger.warning(
                trace_id,
                "llm_request",
                f"手动提供商调用异常: {e}",
                provider_id=provider_id
            )

        return None

    def _generate_analysis_prompt(
        self,
        contexts: List[ChatContext],
        user_query: str,
        intent_type: str,
        has_multimodal: bool = False,
        is_direct_analysis: bool = False
    ) -> str:
        """生成分析提示词"""
        chat_lines = []
        for ctx in contexts:
            line = f"{ctx.sender}: {ctx.content}"
            chat_lines.append(line)

        chat_content = "\n".join(chat_lines)

        if intent_type == "summary":
            system_instruction = (
                "你正在查看一段聊天记录。请用自然、口语化的方式总结这段对话的核心内容，"
                "就像朋友之间转述聊天内容一样。"
            )
        elif intent_type == "analysis":
            system_instruction = (
                "你正在查看一段聊天记录。请基于这段对话给出你的看法或分析，"
                "用朋友间聊天的口吻，自然、随意一些。"
            )
        elif intent_type == "query":
            system_instruction = (
                "你正在查看一段聊天记录。请根据对话内容回答用户的问题，"
                "用自然、口语化的方式回应。"
            )
        else:
            system_instruction = (
                "你正在查看一段聊天记录。请自然地回应，可以总结内容、"
                "表达看法，或者回答可能的问题。用朋友间聊天的随意口吻。"
            )

        if is_direct_analysis:
            system_instruction += (
                "\n\n注意：请只针对这段聊天记录的内容进行分析和评价，"
                "不要参考或提及任何之前的对话内容。"
            )

        if has_multimodal:
            system_instruction += (
                "\n\n注意：聊天记录中包含图片内容，请结合图片和文字进行分析。"
            )

        prompt = f"""{system_instruction}

{chat_content}

用户说："{user_query if user_query else '你怎么看？'}"

请直接给出自然、口语化的回应，不要加"根据聊天记录"之类的前缀。"""

        return prompt

    async def _handle_analysis_error(
        self,
        event: AstrMessageEvent,
        result: AnalysisResult,
        trace_id: str
    ):
        """处理分析错误"""
        if result.error_type == "unreadable":
            self.slogger.debug(trace_id, "error_handle", "无法解析，静默处理")
        elif result.error_type == "empty":
            await self._send_natural_message(event, "这段聊天记录好像没什么内容呢。")
            event.stop_event()
        elif result.error_type == "multimodal_only":
            await self._send_natural_message(
                event,
                random.choice(self.NATURAL_RESPONSES["error_multimodal"])
            )
            event.stop_event()
        else:
            await self._send_natural_message(
                event,
                self._generate_vague_error_response()
            )
            event.stop_event()

    async def _send_natural_message(self, event: AstrMessageEvent, message: str):
        """发送自然语言消息"""
        try:
            from astrbot.api.event import MessageChain
            msg = MessageChain().message(message)
            await self.context.send_message(event.unified_msg_origin, msg)
        except Exception as e:
            self.slogger.error("send_msg", "send_natural_message", f"发送消息失败: {e}")

    async def _append_assistant_text_to_history(
        self,
        event: AstrMessageEvent,
        text: str,
        trace_id: str,
        source: str
    ) -> bool:
        """将助手最终文本幂等写入会话历史，保证分析后可追问。"""
        cleaned = (text or "").strip()
        if not cleaned:
            self.slogger.warning(trace_id, "context_write", "文本为空，跳过写回", source=source)
            return False

        cm = getattr(self.context, "conversation_manager", None)
        if not cm:
            self.slogger.warning(trace_id, "context_write", "conversation_manager 不可用", source=source)
            return False

        uid = event.unified_msg_origin
        cid = None

        try:
            req = getattr(event, "get_extra", None) and event.get_extra("provider_request")
            if req and getattr(req, "conversation", None) and getattr(req.conversation, "cid", None):
                cid = req.conversation.cid
        except Exception:
            cid = None

        if not cid:
            try:
                cid = await cm.get_curr_conversation_id(uid)
            except Exception:
                cid = None

        if not cid:
            self.slogger.warning(trace_id, "context_write", "仅发送成功但未入上下文：无可用会话ID", source=source)
            return False

        try:
            conv = await cm.get_conversation(uid, cid, create_if_not_exists=False)
            if not conv:
                self.slogger.warning(trace_id, "context_write", "仅发送成功但未入上下文：会话未就绪", source=source, cid=cid)
                return False

            msgs = []
            try:
                msgs = json.loads(conv.history) if getattr(conv, "history", "") else []
            except Exception:
                msgs = []

            if msgs:
                last = msgs[-1]
                if isinstance(last, dict) and last.get("role") == "assistant" and (last.get("content") or "").strip() == cleaned:
                    self.slogger.info(trace_id, "context_write", "已在上下文中（去重命中）", source=source, cid=cid)
                    return True

            msgs.append({"role": "assistant", "content": cleaned})
            await cm.update_conversation(uid, cid, history=msgs)
            self.slogger.info(trace_id, "context_write", "已写入上下文/记忆可读", source=source, cid=cid)
            return True

        except Exception as e:
            self.slogger.error(trace_id, "context_write", f"写回上下文失败: {e}", source=source)
            return False

    @filter.on_llm_response(priority=1)
    async def _on_llm_response_record_context(self, event: AstrMessageEvent, response):
        """兜底：框架LLM完成后，补写分析结论到会话历史。"""
        try:
            track = getattr(event, "_forward_reader_context_track", None)
            if not isinstance(track, dict):
                return

            trace_id = str(track.get("trace_id") or "context_hook")
            track_origin = str(track.get("origin") or "")
            track_time = float(track.get("time") or 0)

            # 防止事件对象复用造成脏标记干扰后续普通对话
            if track_origin and track_origin != event.unified_msg_origin:
                self.slogger.info(trace_id, "context_write", "检测到跨会话脏标记，已忽略")
                return
            if track_time and (time.time() - track_time) > 30:
                self.slogger.info(trace_id, "context_write", "检测到过期追踪标记，已忽略")
                return

            completion_text = ""
            try:
                completion_text = getattr(response, "completion_text", "") or ""
            except Exception:
                completion_text = ""

            if not completion_text:
                try:
                    rc = getattr(response, "result_chain", None)
                    if rc and hasattr(rc, "chain"):
                        parts = []
                        for comp in rc.chain:
                            text = getattr(comp, "text", None)
                            if isinstance(text, str) and text.strip():
                                parts.append(text.strip())
                        completion_text = "\n".join(parts).strip()
                except Exception:
                    completion_text = ""

            if not completion_text:
                self.slogger.warning(trace_id, "context_write", "on_llm_response 无文本，跳过回填")
                return

            ok = await self._append_assistant_text_to_history(
                event,
                completion_text,
                trace_id=trace_id,
                source="llm_response_hook"
            )

            self.slogger.info(
                trace_id,
                "context_write",
                "on_llm_response 回填完成",
                history_written=ok
            )
        except Exception as e:
            # 绝不影响主LLM回复链路
            self.slogger.error("context_hook", "context_write", f"on_llm_response补写异常(已忽略): {e}")
        finally:
            try:
                if hasattr(event, "_forward_reader_context_track"):
                    delattr(event, "_forward_reader_context_track")
            except Exception:
                pass

    async def terminate(self):
        """插件终止时资源释放"""
        self.slogger.info("terminate", "plugin_terminate", "插件正在停止，释放资源...")
        self._running = False

        await self.dedup_cache.stop_cleanup_task()
        await self.media_processor.close()

        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        self.slogger.info("terminate", "plugin_terminate", "资源释放完成")
