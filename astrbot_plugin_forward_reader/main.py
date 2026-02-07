"""
Forward Reader v2.0.1 - 智能合并转发消息分析插件
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
版本: 2.0.1
许可证: AGPL-3.0
"""

import json
import re
import random
import time
import hashlib
import asyncio
from enum import Enum, auto
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass, field
from collections import deque

import aiohttp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# MessageChain 可能在某些版本中导入方式不同，使用延迟导入
try:
    from astrbot.api.event import MessageChain
except ImportError:
    MessageChain = None  # 将在实际使用时再次尝试导入

# 平台适配检查
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False
    AiocqhttpMessageEvent = None  # 定义为 None 以避免后续代码报错


class AnalysisState(Enum):
    """分析状态机状态"""
    IDLE = auto()      # 空闲状态
    PENDING = auto()   # 等待触发（延迟中）
    ANALYZING = auto() # 分析中
    COMPLETED = auto() # 已完成


class AnalysisPriority(Enum):
    """分析优先级"""
    MANUAL = 2  # 手动指令 - 最高优先级，立即执行
    AUTO = 1    # 自动分析 - 低优先级，延迟触发


@dataclass
class MediaContent:
    """媒体内容数据结构"""
    type: str  # image, video, file
    url: str
    base64_data: Optional[str] = None
    size: int = 0  # 文件大小（字节）
    processed: bool = False  # 是否已预处理


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
    token_estimate: int = 0  # 预估Token数


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
        """总耗时（秒）"""
        return time.time() - self.start_time

    @property
    def llm_duration(self) -> Optional[float]:
        """LLM调用耗时（秒）"""
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
        """生成追踪ID"""
        self._trace_id_counter += 1
        timestamp = int(time.time() * 1000)
        return f"{timestamp:013x}-{self._trace_id_counter:04x}"

    def _format_log(self, level: str, trace_id: str, stage: str, message: str, extra: Dict = None) -> str:
        """格式化日志"""
        extra_str = ""
        if extra:
            extra_str = " | " + " ".join([f"{k}={v}" for k, v in extra.items()])
        return f"[{self.plugin_name}] [{level}] [{trace_id}] [{stage}] {message}{extra_str}"

    def debug(self, trace_id: str, stage: str, message: str, **kwargs):
        """DEBUG级别日志"""
        if self.enable_trace:
            logger.debug(self._format_log("DEBUG", trace_id, stage, message, kwargs))

    def info(self, trace_id: str, stage: str, message: str, **kwargs):
        """INFO级别日志"""
        logger.info(self._format_log("INFO", trace_id, stage, message, kwargs))

    def warning(self, trace_id: str, stage: str, message: str, **kwargs):
        """WARNING级别日志"""
        logger.warning(self._format_log("WARN", trace_id, stage, message, kwargs))

    def error(self, trace_id: str, stage: str, message: str, **kwargs):
        """ERROR级别日志"""
        logger.error(self._format_log("ERROR", trace_id, stage, message, kwargs))

    def performance(self, trace_id: str, metrics: PerformanceMetrics, **kwargs):
        """性能指标日志"""
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
        """生成消息哈希"""
        content = f"{forward_id}:{user_id}:{group_id or ''}"
        return hashlib.md5(content.encode()).hexdigest()

    async def is_duplicate(self, forward_id: str, user_id: str, group_id: Optional[str]) -> bool:
        """检查是否为重复消息"""
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
        """添加消息到缓存"""
        msg_hash = self._generate_hash(forward_id, user_id, group_id)
        async with self._lock:
            self._cache[msg_hash] = time.time() + self._ttl

    async def cleanup_expired(self):
        """清理过期缓存"""
        current_time = time.time()
        async with self._lock:
            expired_keys = [k for k, v in self._cache.items() if current_time > v]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    async def start_cleanup_task(self):
        """启动定期清理任务"""
        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(300)  # 每5分钟清理一次
                    count = await self.cleanup_expired()
                    if count > 0:
                        logger.debug(f"[ForwardReader] 清理 {count} 条过期缓存")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[ForwardReader] 缓存清理任务异常: {e}")

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def stop_cleanup_task(self):
        """停止清理任务"""
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
        """获取当前状态"""
        async with self._lock:
            return self._states.get(session_id, AnalysisState.IDLE)

    async def set_state(self, session_id: str, state: AnalysisState):
        """设置状态"""
        async with self._lock:
            self._states[session_id] = state

    async def can_start_analysis(self, session_id: str) -> bool:
        """检查是否可以开始新的分析"""
        async with self._lock:
            current_state = self._states.get(session_id, AnalysisState.IDLE)
            return current_state in [AnalysisState.IDLE, AnalysisState.COMPLETED]

    async def register_pending_task(self, session_id: str, task: asyncio.Task):
        """注册待处理任务"""
        async with self._lock:
            self._pending_tasks[session_id] = task

    async def cancel_pending_task(self, session_id: str):
        """取消待处理任务"""
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
        """添加任务到队列，返回是否成功添加"""
        async with self._lock:
            # 检查是否已有相同forward_id的任务
            for existing_task in self._queue:
                if existing_task.forward_id == task.forward_id:
                    # 如果新任务优先级更高，替换旧任务
                    if task.priority.value > existing_task.priority.value:
                        self._queue.remove(existing_task)
                        self._insert_by_priority(task)
                        return True
                    return False
            self._insert_by_priority(task)
            return True

    def _insert_by_priority(self, task: AnalysisTask):
        """按优先级插入任务"""
        inserted = False
        for i, existing in enumerate(self._queue):
            if task.priority.value > existing.priority.value:
                self._queue.insert(i, task)
                inserted = True
                break
        if not inserted:
            self._queue.append(task)

    async def get_next_task(self) -> Optional[AnalysisTask]:
        """获取下一个任务"""
        async with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    async def has_pending_task(self, forward_id: str) -> bool:
        """检查是否有待处理任务"""
        async with self._lock:
            return any(t.forward_id == forward_id for t in self._queue)


class MediaBatchProcessor:
    """多媒体批量预处理器"""

    def __init__(self, max_images: int = 5, max_videos: int = 2):
        self.max_images = max_images
        self.max_videos = max_videos
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取HTTP会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                connector=aiohttp.TCPConnector(limit=10, limit_per_host=5)
            )
        return self._session

    async def process_media_batch(
        self,
        media_list: List[MediaContent],
        trace_id: str,
        slogger: StructuredLogger
    ) -> Tuple[List[str], str]:
        """
        批量预处理媒体文件，返回图片URL列表和统一摘要
        """
        slogger.debug(trace_id, "media_preprocess", f"开始批量预处理 {len(media_list)} 个媒体文件")

        image_urls = []
        video_count = 0
        summary_parts = []

        for media in media_list:
            if media.type == "image" and len(image_urls) < self.max_images:
                try:
                    # 验证图片URL可访问
                    if await self._validate_url(media.url):
                        image_urls.append(media.url)
                        media.processed = True
                    else:
                        slogger.warning(trace_id, "media_preprocess", f"图片URL不可访问: {media.url[:50]}...")
                except Exception as e:
                    slogger.warning(trace_id, "media_preprocess", f"图片预处理失败: {e}")

            elif media.type == "video" and video_count < self.max_videos:
                video_count += 1
                summary_parts.append(f"[视频{video_count}]")

        # 生成统一摘要
        summary = ""
        if summary_parts:
            summary = f"包含 {len(image_urls)} 张图片和 {video_count} 个视频"
        elif image_urls:
            summary = f"包含 {len(image_urls)} 张图片"

        slogger.debug(trace_id, "media_preprocess", f"媒体预处理完成",
                     images=len(image_urls), videos=video_count)

        return image_urls, summary

    async def _validate_url(self, url: str) -> bool:
        """验证URL是否可访问"""
        try:
            session = await self._get_session()
            async with session.head(url, timeout=5) as response:
                return response.status == 200
        except Exception:
            return False

    async def close(self):
        """关闭HTTP会话"""
        if self._session and not self._session.closed:
            await self._session.close()


class TokenBudgetController:
    """Token预算控制器"""

    def __init__(self, token_budget: int = 4000, segment_size: int = 1500):
        self.token_budget = token_budget
        self.segment_size = segment_size

    def estimate_tokens(self, text: str, image_count: int = 0) -> int:
        """预估Token数（简化估算：中文字符1token，英文0.25token，图片1000token）"""
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        text_tokens = chinese_chars + int(other_chars * 0.25)
        image_tokens = image_count * 1000  # 多模态图片约1000token
        return text_tokens + image_tokens

    def should_segment(self, text: str, image_count: int = 0) -> bool:
        """判断是否需要分段"""
        if self.token_budget <= 0:
            return False
        return self.estimate_tokens(text, image_count) > self.token_budget

    def segment_content(self, contexts: List[ChatContext]) -> List[List[ChatContext]]:
        """将内容分段"""
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
        """生成分段摘要的统一提示词"""
        if len(segments) == 1:
            # 单段，直接生成
            return self._generate_single_segment_prompt(segments[0], user_query, intent_type)

        # 多段，先分段总结再统一
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
        """生成单段提示词"""
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
    "2.0.1",
    "https://github.com/EraAsh/astrbot_plugin_forward_reader"
)
class ForwardReader(Star):
    """
    智能合并转发消息阅读器 v2.0.1

    新增功能：
    1. 全链路结构化日志追踪系统
    2. 消息ID哈希缓存去重机制
    3. 分析状态机与互斥锁管理
    4. 自动分析延迟触发与优先级队列
    5. 多媒体批量预处理与统一摘要
    6. Token预算控制与分段摘要策略
    7. 多模态配置专区，支持vision_llm_provider枚举选择
    """

    # 自然语言触发关键词
    IMPLICIT_TRIGGERS = {
        "summary": ["总结一下", "什么内容", "聊了什么", "说啥了", "讲了什么", "怎么回事"],
        "analysis": ["怎么看", "什么意思", "分析一下", "评价一下", "有什么想法"],
        "query": ["谁说的", "有没有提到", "关于", "提到了吗"],
        "implicit": ["看看这个", "瞧瞧", "读一下", "翻译一下", "解释一下"]
    }

    # 真人对话风格模板
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

        # 初始化结构化日志
        log_level = self.logging_config.get("log_level", "INFO")
        enable_trace = self.logging_config.get("enable_trace_log", False)
        self.slogger = StructuredLogger("ForwardReader", log_level, enable_trace)

        # 初始化去重缓存
        dedup_ttl = self.performance_config.get("dedup_ttl", 3600)
        self.dedup_cache = MessageDeduplicationCache(dedup_ttl)

        # 初始化状态管理器
        self.state_manager = AnalysisStateManager()

        # 初始化优先级队列
        self.task_queue = PriorityTaskQueue()

        # 初始化媒体预处理器
        max_images = self.multimodal_config.get("max_images", 5)
        max_videos = self.multimodal_config.get("max_videos", 2)
        self.media_processor = MediaBatchProcessor(max_images, max_videos)

        # 初始化Token预算控制器
        token_budget = self.performance_config.get("token_budget", 4000)
        segment_size = self.performance_config.get("segment_size", 1500)
        self.token_controller = TokenBudgetController(token_budget, segment_size)

        # 启动后台任务
        self._background_tasks: Set[asyncio.Task] = set()
        self._running = True

        # 启动去重缓存清理任务
        asyncio.create_task(self.dedup_cache.start_cleanup_task())

        self.slogger.info("init", "plugin_initialized",
                         "ForwardReader v2.0.1 初始化完成",
                         dedup_ttl=dedup_ttl, token_budget=token_budget)

    def _load_config(self):
        """加载配置项"""
        # 基础配置
        self.enable_smart_detection = self.config.get("enable_smart_detection", True)
        self.enable_direct_analysis = self.config.get("enable_direct_analysis", False)
        self.enable_reply_analysis = self.config.get("enable_reply_analysis", True)
        self.enable_proactive_reply = self.config.get("enable_proactive_reply", False)
        self.sensitivity = self.config.get("sensitivity", 0.7)
        self.max_messages = self.config.get("max_messages", 200)
        self.show_thinking = self.config.get("show_thinking", True)

        # 多模态配置（新格式，兼容旧格式）
        self.multimodal_config = self.config.get("multimodal_config", {})
        # 向下兼容：如果新格式不存在，使用旧格式
        if not self.multimodal_config:
            self.multimodal_config = {
                "enable_multimodal": self.config.get("enable_multimodal", True),
                "max_images": self.config.get("max_images", 5),
                "vision_llm_provider": "auto",
                "max_videos": 2,
                "image_quality": "auto",
                "batch_process": True
            }

        # 性能配置
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

        # 日志配置
        self.logging_config = self.config.get("logging_config", {})
        if not self.logging_config:
            self.logging_config = {
                "log_level": "INFO",
                "enable_performance_log": True,
                "enable_trace_log": False
            }

        # 嵌套解析配置
        self.parse_nested_forward = self.config.get("parse_nested_forward", True)
        self.max_nested_depth = self.config.get("max_nested_depth", 3)

    def _is_implicit_query(self, message: str) -> Tuple[bool, str]:
        """智能判断消息是否为对合并转发的隐式查询"""
        if not message:
            return True, "implicit"

        message = message.lower().strip()

        for intent_type, keywords in self.IMPLICIT_TRIGGERS.items():
            for keyword in keywords:
                if keyword in message:
                    return True, intent_type

        question_patterns = [r"^谁", r"^什么", r"^怎么", r"^为什么", r"^哪里", r"^多少", r"^几"]
        for pattern in question_patterns:
            if re.search(pattern, message):
                return True, "query"

        return False, ""

    def _generate_natural_thinking(self, has_multimodal: bool = False) -> str:
        """生成自然的思考提示"""
        if has_multimodal:
            return random.choice(self.NATURAL_RESPONSES["thinking_with_image"])
        return random.choice(self.NATURAL_RESPONSES["thinking"])

    def _generate_vague_error_response(self) -> str:
        """生成模糊的错误响应"""
        return random.choice(self.NATURAL_RESPONSES["error_vague"])

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """智能消息处理器"""
        trace_id = self.slogger._generate_trace_id()

        # DEBUG级：消息接收
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
        reply_seg: Optional[Comp.Reply] = None
        user_message = event.message_str.strip() if event.message_str else ""

        # 解析消息链
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                forward_id = seg.id
            elif isinstance(seg, Comp.Reply):
                reply_seg = seg

        # INFO级：分析类型判定
        self.slogger.info(trace_id, "analysis_type",
                         f"消息类型判定",
                         has_forward=bool(forward_id),
                         has_reply=bool(reply_seg),
                         user_message=user_message[:50] if user_message else "")

        should_analyze = False
        intent_type = "implicit"
        is_direct_analysis = False
        priority = AnalysisPriority.AUTO

        # 场景A: 引用合并转发并提问（手动指令，最高优先级）
        if self.enable_reply_analysis and reply_seg and not forward_id:
            try:
                forward_id = await self._extract_forward_from_reply(event, reply_seg)
                if forward_id:
                    is_query, detected_intent = self._is_implicit_query(user_message)
                    if is_query or not user_message:
                        should_analyze = True
                        intent_type = detected_intent
                        priority = AnalysisPriority.MANUAL
                        self.slogger.info(trace_id, "analysis_type",
                                         "触发场景A：引用分析", intent=intent_type, priority="MANUAL")
            except Exception as e:
                self.slogger.warning(trace_id, "analysis_type", f"提取引用转发失败: {e}")
                return

        # 场景B: 直接发送合并转发（自动分析模式，延迟触发）
        elif self.enable_direct_analysis and forward_id:
            should_analyze = True
            is_direct_analysis = True
            priority = AnalysisPriority.AUTO
            is_query, intent_type = self._is_implicit_query(user_message)
            if not user_message:
                intent_type = "analysis"
            self.slogger.info(trace_id, "analysis_type",
                             "触发场景B：自动分析", intent=intent_type, priority="AUTO")

        # 场景C: 主动回复模式
        elif self.enable_proactive_reply and forward_id:
            is_at = any(isinstance(seg, Comp.At) for seg in event.message_obj.message)
            if is_at or event.is_at_or_wake_command:
                should_analyze = True
                intent_type = "summary"
                priority = AnalysisPriority.MANUAL
                self.slogger.info(trace_id, "analysis_type",
                                 "触发场景C：主动回复", priority="MANUAL")

        if not should_analyze or not forward_id:
            self.slogger.debug(trace_id, "analysis_type", "未触发分析")
            return

        # 检查去重
        if self.performance_config.get("enable_deduplication", True):
            is_dup = await self.dedup_cache.is_duplicate(
                forward_id,
                event.get_sender_id(),
                event.get_group_id()
            )
            if is_dup:
                self.slogger.info(trace_id, "deduplication", "消息已分析过，跳过")
                return

        # 创建分析任务
        task = AnalysisTask(
            task_id=trace_id,
            forward_id=forward_id,
            event=event,
            user_query=user_message,
            intent_type=intent_type,
            priority=priority,
            delay_seconds=self.performance_config.get("auto_analysis_delay", 2.0),
            is_direct_analysis=is_direct_analysis
        )

        # 添加到优先级队列
        added = await self.task_queue.add_task(task)
        if not added:
            self.slogger.info(trace_id, "task_queue", "任务已在队列中，跳过")
            return

        # 根据优先级决定处理方式
        if priority == AnalysisPriority.MANUAL:
            # 手动指令：立即执行
            self.slogger.info(trace_id, "task_execute", "立即执行手动任务")
            await self._execute_analysis_task(task)
        else:
            # 自动分析：延迟触发
            self.slogger.info(trace_id, "task_execute",
                             f"延迟触发自动任务", delay=task.delay_seconds)
            asyncio.create_task(self._delayed_analysis(task))

    async def _delayed_analysis(self, task: AnalysisTask):
        """延迟分析（用于自动分析模式）"""
        session_id = task.event.unified_msg_origin

        # 设置PENDING状态
        await self.state_manager.set_state(session_id, AnalysisState.PENDING)

        try:
            await asyncio.sleep(task.delay_seconds)

            # 检查状态是否仍为PENDING（可能被手动指令抢占）
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

        # 检查是否可以开始分析
        if not await self.state_manager.can_start_analysis(session_id):
            self.slogger.info(trace_id, "task_execute",
                             "当前会话正在分析中，加入队列等待")
            # 等待前一个分析完成
            for _ in range(30):  # 最多等待30秒
                await asyncio.sleep(1)
                if await self.state_manager.can_start_analysis(session_id):
                    break
            else:
                self.slogger.warning(trace_id, "task_execute", "等待超时，取消任务")
                return

        # 设置ANALYZING状态
        await self.state_manager.set_state(session_id, AnalysisState.ANALYZING)

        # 添加到去重缓存
        if self.performance_config.get("enable_deduplication", True):
            await self.dedup_cache.add(
                task.forward_id,
                task.event.get_sender_id(),
                task.event.get_group_id()
            )

        # 初始化性能指标
        metrics = PerformanceMetrics()

        try:
            self.slogger.info(trace_id, "analysis_start", "开始分析",
                             forward_id=task.forward_id[:20] if task.forward_id else "")

            # 准备分析
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

            # 更新性能指标
            metrics.image_count = len(result.image_urls)
            metrics.token_input = result.token_estimate

            # 发送思考提示
            if self.show_thinking:
                thinking_msg = self._generate_natural_thinking(result.has_multimodal)
                await self._send_natural_message(task.event, thinking_msg)

            # DEBUG级：多模态资源检测
            self.slogger.debug(trace_id, "multimodal_detect",
                              f"多模态检测完成",
                              has_multimodal=result.has_multimodal,
                              image_count=len(result.image_urls),
                              nested_count=result.nested_forward_count)

            # 批量预处理媒体
            image_urls_to_use = []
            if result.has_multimodal and self.multimodal_config.get("enable_multimodal", True):
                media_list = [MediaContent(type="image", url=url) for url in result.image_urls]
                image_urls_to_use, media_summary = await self.media_processor.process_media_batch(
                    media_list, trace_id, self.slogger
                )

                self.slogger.debug(trace_id, "media_preprocess",
                                  f"媒体预处理完成",
                                  processed_images=len(image_urls_to_use))

            # INFO级：LLM调用
            self.slogger.info(trace_id, "llm_request",
                             "调用LLM生成回应",
                             is_direct_analysis=task.is_direct_analysis,
                             image_count=len(image_urls_to_use),
                             token_estimate=result.token_estimate)

            metrics.llm_start_time = time.time()

            # 调用LLM
            if task.is_direct_analysis:
                yield task.event.request_llm(
                    prompt=result.content,
                    image_urls=image_urls_to_use,
                    contexts=[],
                    session_id=None
                )
            else:
                yield task.event.request_llm(
                    prompt=result.content,
                    image_urls=image_urls_to_use
                )

            metrics.llm_end_time = time.time()
            task.event.stop_event()

            # 记录性能指标
            if self.logging_config.get("enable_performance_log", True):
                self.slogger.performance(trace_id, metrics)

            self.slogger.info(trace_id, "analysis_complete", "分析完成",
                             duration=f"{metrics.total_duration:.2f}s")

        except Exception as e:
            self.slogger.error(trace_id, "analysis_error", f"分析过程异常: {e}")
        finally:
            # 设置COMPLETED状态
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
            # 提取内容
            contexts, image_urls, nested_count = await self._extract_forward_messages(
                event,
                forward_id,
                current_depth=0,
                trace_id=trace_id
            )

            if not contexts:
                return AnalysisResult(success=False, error_type="empty")

            total_text = "".join([ctx.content for ctx in contexts])
            has_multimodal = len(image_urls) > 0 or any(ctx.has_video or ctx.has_image for ctx in contexts)

            # 预估Token数
            token_estimate = self.token_controller.estimate_tokens(total_text, len(image_urls))

            self.slogger.debug(trace_id, "content_extract",
                              f"内容提取完成",
                              chat_count=len(contexts),
                              text_length=len(total_text),
                              image_count=len(image_urls),
                              token_estimate=token_estimate)

            if len(total_text.strip()) < 10 and not has_multimodal:
                self.slogger.debug(trace_id, "content_extract", "内容过少，可能无法解析")
                return AnalysisResult(success=False, error_type="unreadable")

            # 检查是否需要分段
            enable_segmented = self.performance_config.get("enable_segmented_summary", True)
            if enable_segmented and self.token_controller.should_segment(total_text, len(image_urls)):
                segments = self.token_controller.segment_content(contexts)
                prompt = self.token_controller.generate_segmented_summary_prompt(
                    segments, user_query, intent_type
                )
                self.slogger.info(trace_id, "segment_summary",
                                 f"启用分段摘要", segments=len(segments))
            else:
                # 单段处理
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
        forward_id: str,
        current_depth: int,
        trace_id: str
    ) -> Tuple[List[ChatContext], List[str], int]:
        """提取合并转发中的消息内容"""
        if current_depth > self.max_nested_depth:
            self.slogger.debug(trace_id, "extract_forward",
                              f"达到最大嵌套深度 {self.max_nested_depth}，停止递归")
            return [], [], 0

        client = event.bot

        try:
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
        except Exception as e:
            self.slogger.error(trace_id, "extract_forward", f"获取合并转发失败: {e}")
            raise ValueError("无法获取合并转发内容")

        if not forward_data or "messages" not in forward_data:
            raise ValueError("合并转发数据为空")

        contexts: List[ChatContext] = []
        image_urls: List[str] = []
        message_count = 0
        nested_forward_count = 0

        for node in forward_data["messages"]:
            if message_count >= self.max_messages:
                self.slogger.debug(trace_id, "extract_forward",
                                  f"达到最大消息数 {self.max_messages}")
                break

            sender_name = node.get("sender", {}).get("nickname", "未知用户")
            content_chain = node.get("content", [])

            ctx = ChatContext(sender=sender_name, content="")

            if isinstance(content_chain, str):
                ctx.content = content_chain
            elif isinstance(content_chain, list):
                text_parts = []
                for segment in content_chain:
                    if isinstance(segment, dict):
                        seg_type = segment.get("type")
                        seg_data = segment.get("data", {})

                        if seg_type == "text":
                            text_parts.append(seg_data.get("text", ""))
                        elif seg_type == "image":
                            url = seg_data.get("url", seg_data.get("file", ""))
                            if url:
                                ctx.has_image = True
                                ctx.media_list.append(MediaContent(type="image", url=url))
                                image_urls.append(url)
                            text_parts.append("[图片]")
                        elif seg_type == "video":
                            ctx.has_video = True
                            text_parts.append("[视频]")
                        elif seg_type == "file":
                            ctx.has_file = True
                            text_parts.append("[文件]")
                        elif seg_type == "forward" and self.parse_nested_forward:
                            # 嵌套合并转发
                            nested_id = seg_data.get("id")
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

    async def _extract_forward_from_reply(
        self,
        event: AiocqhttpMessageEvent,
        reply_seg: Comp.Reply
    ) -> Optional[str]:
        """从引用消息中提取合并转发ID"""
        try:
            client = event.bot
            original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id)

            if not original_msg or 'message' not in original_msg:
                return None

            message_chain = original_msg['message']
            if isinstance(message_chain, list):
                for segment in message_chain:
                    if isinstance(segment, dict) and segment.get("type") == "forward":
                        return segment.get("data", {}).get("id")

            if isinstance(message_chain, dict):
                if message_chain.get("type") == "forward":
                    return message_chain.get("data", {}).get("id")

        except Exception as e:
            self.slogger.debug("extract_reply", "extract_forward_from_reply", f"获取引用消息失败: {e}")

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

    async def terminate(self):
        """插件终止时资源释放"""
        self.slogger.info("terminate", "plugin_terminate", "插件正在停止，释放资源...")
        self._running = False

        # 停止去重缓存清理任务
        await self.dedup_cache.stop_cleanup_task()

        # 关闭媒体处理器
        await self.media_processor.close()

        # 取消所有后台任务
        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        self.slogger.info("terminate", "plugin_terminate", "资源释放完成")
