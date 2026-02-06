import json 
from typing import List, Dict, Any, Optional, Tuple 

from astrbot.api import logger, AstrBotConfig 
from astrbot.api.event import filter, AstrMessageEvent 
from astrbot.api.star import Context, Star, register 
import astrbot.api.message_components as Comp 
from astrbot.api.provider import ProviderRequest 

# 检查是否为 aiocqhttp 平台，因为合并转发是其特性 
try: 
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent 
    IS_AIOCQHTTP = True 
except ImportError: 
    IS_AIOCQHTTP = False 


@register("forward_reader", "EraAsh", "一个使用 LLM 分析合并转发消息内容的插件", "1.2.2", "https://github.com/EraAsh/astrbot_plugin_forward_reader") 
class ForwardReader(Star): 
    def __init__(self, context: Context, config: AstrBotConfig): 
        super().__init__(context) 
        self.config = config
        self.enable_direct_analysis = self.config.get("enable_direct_analysis", False) 
        self.enable_reply_analysis = self.config.get("enable_reply_analysis", False) 
    
    async def _extract_content_recursively(self, message_nodes: List[Dict[str, Any]], extracted_texts: list[str], image_urls: list[str], depth: int = 0):
        """
        核心递归解析器。遍历消息节点列表，提取文本、图片，并处理嵌套的 forward 结构。
        该函数不执行 API 调用，只进行结构解析。
        """
        indent = "  " * depth
        
        for message_node in message_nodes: 
            sender_name = message_node.get("sender", {}).get("nickname", "未知用户") 
            raw_content = message_node.get("message") or message_node.get("content", []) 

            # 解析消息内容链 (兼容字符串和列表格式)
            content_chain = [] 
            if isinstance(raw_content, str): 
                try: 
                    parsed_content = json.loads(raw_content) 
                    if isinstance(parsed_content, list): 
                        content_chain = parsed_content 
                except (json.JSONDecodeError, TypeError): 
                    # 无法解析为JSON的字符串内容，当作纯文本处理
                    content_chain = [{"type": "text", "data": {"text": raw_content}}] 
            elif isinstance(raw_content, list): 
                content_chain = raw_content 

            node_text_parts = [] 
            has_only_forward = False
            
            # 遍历消息段，处理文本、图片和嵌套转发
            if isinstance(content_chain, list):
                
                # 检查是否为纯粹的转发消息容器
                if len(content_chain) == 1 and content_chain[0].get("type") == "forward":
                    has_only_forward = True
                    
                for segment in content_chain: 
                    if isinstance(segment, dict): 
                        seg_type = segment.get("type") 
                        seg_data = segment.get("data", {}) 
                        
                        if seg_type == "text": 
                            text = seg_data.get("text", "") 
                            if text: 
                                node_text_parts.append(text) 
                        elif seg_type == "image": 
                            url = seg_data.get("url") 
                            if url: 
                                image_urls.append(url) 
                                node_text_parts.append("[图片]") 
                        
                        elif seg_type == "forward":
                            nested_content = seg_data.get("content")
                            if isinstance(nested_content, list):
                                await self._extract_content_recursively(nested_content, extracted_texts, image_urls, depth + 1)
                                
                            else:
                                node_text_parts.append("[转发消息内容缺失或格式错误]")

            
            # 格式化当前消息节点的内容
            full_node_text = "".join(node_text_parts).strip()
            
            if full_node_text and not has_only_forward: 
                extracted_texts.append(f"{indent}{sender_name}: {full_node_text}")
    
    async def _extract_forward_content(self, event: AiocqhttpMessageEvent, forward_id: str) -> Tuple[list[str], list[str]]:
        """
        从合并转发消息中提取内容，并启动递归解析。
        """
        client = event.bot 
        extracted_texts = [] 
        image_urls = []
        
        try: 
            # 1. 调用 API 获取转发消息详情
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id) 
        except Exception as e: 
            logger.warning(f"调用 get_forward_msg API 失败 (ID: {forward_id}): {e}") 
            return [], [] 

        if not forward_data or "messages" not in forward_data: 
            logger.debug(f"获取到的合并转发内容为空或结构异常 (ID: {forward_id})")
            return [], [] 

        # 2. 启动递归解析，处理所有内嵌层级
        await self._extract_content_recursively(forward_data["messages"], extracted_texts, image_urls, depth=0)
        
        return extracted_texts, image_urls 

    @filter.on_llm_request()
    async def modify_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        [唤醒模式]：当 LLM 请求被框架唤醒时触发。插件将聊天记录注入到现有请求末尾。
        此模式处理所有 is_at_or_wake_command = True 的情况。
        """
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent) or not event.is_at_or_wake_command:
            return
        
        forward_id: Optional[str] = None 
        reply_seg: Optional[Comp.Reply] = None 
        json_extracted_texts = [] 
        
        # --- 提取转发 ID / 内容 ---
        for seg in event.message_obj.message: 
            if isinstance(seg, Comp.Forward): 
                forward_id = seg.id 
                break
            elif isinstance(seg, Comp.Reply): 
                reply_seg = seg 
        
        if not forward_id and reply_seg:
            try: 
                client = event.bot 
                original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id)
                
                if original_msg and 'message' in original_msg: 
                    original_message_chain = original_msg['message'] 
                    
                    if isinstance(original_message_chain, list): 
                        for segment in original_message_chain: 
                            seg_type = segment.get("type")

                            if seg_type == "forward": 
                                forward_id = segment.get("data", {}).get("id") 
                                if forward_id: break
                            
                            elif seg_type == "json":
                                try:
                                    inner_data_str = segment.get("data", {}).get("data")
                                    if inner_data_str:
                                        inner_data_str = inner_data_str.replace("&#44;", ",")
                                        inner_json = json.loads(inner_data_str)
                                        if inner_json.get("app") == "com.tencent.multimsg" and inner_json.get("config", {}).get("forward") == 1:
                                            news_items = inner_json.get("meta", {}).get("detail", {}).get("news", [])
                                            for item in news_items:
                                                text_content = item.get("text")
                                                if text_content:
                                                    clean_text = text_content.strip().replace("[图片]", "").strip()
                                                    if clean_text: json_extracted_texts.append(clean_text)
                                            if json_extracted_texts: break
                                except (json.JSONDecodeError, TypeError, KeyError) as e:
                                    logger.debug(f"解析 JSON 消息内容失败: {e}")
                                    continue
                        
            except Exception as e: 
                logger.warning(f"获取被回复消息详情失败: {e}") 
        
        if forward_id or json_extracted_texts:
            image_urls = []
            try:
                if forward_id:
                    extracted_texts, image_urls = await self._extract_forward_content(event, forward_id)
                else:
                    extracted_texts = json_extracted_texts
            except ValueError as e:
                logger.warning(f"唤醒模式下内容提取失败，跳过注入: {e}")
                return 
            
            if not extracted_texts and not image_urls:
                return

            chat_records = "\n".join(extracted_texts)
            
            # 1. 确定用户问题：如果 req.prompt 为空（用户只 @Bot），则使用默认问题
            user_question = req.prompt.strip()
            if not user_question:
                 user_question = "请总结一下这个聊天记录"
            
            # 2. 构建上下文
            context_prompt = (
                f"\n\n用户是在吐槽以下聊天记录中的内容，请根据以下聊天记录内容来响应用户的吐槽。聊天记录如下：\n"
                f"--- 聊天记录开始 ---\n"
                f"{chat_records}\n"
                f"--- 聊天记录结束 ---"
            )
            
            # 3. 修改 ProviderRequest：注入到末尾
            req.prompt = user_question + context_prompt
            req.image_urls.extend(image_urls)
            
            logger.info(f"成功注入转发内容 ({len(extracted_texts)} 条文本, {len(image_urls)} 张图片) 到 LLM 请求末尾。")

    @filter.event_message_type(filter.EventMessageType.ALL) 
    async def on_any_message(self, event: AstrMessageEvent, *args, **kwargs): 
        """ 
        [自动模式] 监听所有消息，仅当配置开启且 LLM 未被唤醒时，手动触发 LLM 请求。
        """ 
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent): 
            return 

        is_bot_awaken = event.is_at_or_wake_command
        
        # 退出条件 1: 如果是唤醒消息，则交给 modify_llm_request 钩子处理
        if is_bot_awaken:
            return 
        
        # 退出条件 2: 如果两个配置都关闭，则退出
        if not self.enable_direct_analysis and not self.enable_reply_analysis:
            return

        # --- 提取转发 ID / 内容 ---
        forward_id: Optional[str] = None 
        reply_seg: Optional[Comp.Reply] = None 
        user_query: str = event.message_str.strip() 
        json_extracted_texts = []

        for seg in event.message_obj.message: 
            if isinstance(seg, Comp.Forward): 
                if self.enable_direct_analysis: # 仅检查自动配置
                    forward_id = seg.id 
                    if not user_query:
                        user_query = "请总结一下这个聊天记录" 
                    if forward_id: 
                        break
            elif isinstance(seg, Comp.Reply): 
                reply_seg = seg 
        
        if self.enable_reply_analysis and not forward_id and reply_seg:
            try: 
                client = event.bot 
                original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id)
                if original_msg and 'message' in original_msg: 
                    original_message_chain = original_msg['message'] 
                    if isinstance(original_message_chain, list): 
                        for segment in original_message_chain: 
                            seg_type = segment.get("type")
                            if seg_type == "forward": 
                                forward_id = segment.get("data", {}).get("id") 
                                if forward_id:
                                    if not user_query: user_query = "请总结一下这个聊天记录" 
                                    break
                            elif seg_type == "json":
                                try:
                                    inner_data_str = segment.get("data", {}).get("data")
                                    if inner_data_str:
                                        inner_data_str = inner_data_str.replace("&#44;", ",")
                                        inner_json = json.loads(inner_data_str)
                                        if inner_json.get("app") == "com.tencent.multimsg" and inner_json.get("config", {}).get("forward") == 1:
                                            news_items = inner_json.get("meta", {}).get("detail", {}).get("news", [])
                                            for item in news_items:
                                                text_content = item.get("text")
                                                if text_content:
                                                    clean_text = text_content.strip().replace("[图片]", "").strip()
                                                    if clean_text: json_extracted_texts.append(clean_text)
                                            if json_extracted_texts:
                                                if not user_query: user_query = "请总结一下这个聊天记录"
                                                break
                                except (json.JSONDecodeError, TypeError, KeyError):
                                    continue
            except Exception as e: 
                logger.warning(f"获取被回复消息详情失败: {e}") 
        
        # 只有在找到内容且有提问时才继续
        if (forward_id or json_extracted_texts) and user_query:
            try: 
                # 提取合并转发内容
                image_urls = []
                if forward_id:
                    extracted_texts, image_urls = await self._extract_forward_content(event, forward_id) 
                else:
                    extracted_texts = json_extracted_texts
                
                if not extracted_texts and not image_urls: 
                    yield event.plain_result("无法从合并转发消息中提取到任何有效内容。") 
                    return 
                
                # 自动模式下，发送“正在分析”消息
                await event.send(event.chain_result([Comp.Reply(id=event.message_obj.message_id), Comp.Plain("正在分析聊天记录，请稍候...")])) 

                # 构建用于LLM分析的最终提示词
                chat_records = "\n".join(extracted_texts) 
                final_prompt = ( 
                    f"这是用户的问题：'{user_query}'\n\n"
                    f"请根据以下聊天记录内容来回答用户的问题。聊天记录如下：\n"
                    f"--- 聊天记录开始 ---\n"
                    f"{chat_records}\n"
                    f"--- 聊天记录结束 ---"
                ) 

                logger.info(f"ForwardReader [自动模式]: 准备向LLM发送请求，Prompt长度: {len(final_prompt)}, 图片数量: {len(image_urls)}") 

                yield event.request_llm( 
                    prompt=final_prompt, 
                    image_urls=image_urls 
                ) 
                event.stop_event() 

            except Exception as e: 
                logger.error(f"分析转发消息失败: {e}") 
                yield event.plain_result(f"分析失败: {e}") 
        
    async def terminate(self): 
        pass

