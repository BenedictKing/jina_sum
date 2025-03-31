# encoding:utf-8
import html
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import nest_asyncio
import newspaper
import requests
from bs4 import BeautifulSoup
from newspaper import Article
from requests_html import HTMLSession

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.utils import remove_markdown_symbol
from plugins import Event, EventAction, EventContext, Plugin

# 应用nest_asyncio以解决事件循环问题
try:
    nest_asyncio.apply()
except Exception as e:
    logger.warning(f"[JinaSum] 无法应用nest_asyncio: {str(e)}")


@plugins.register(
    name="JinaSum",
    desire_priority=20,
    hidden=False,
    desc="Sum url link content with newspaper3k and llm",
    version="2.4.1",
    author="BenedictKing",
)
class JinaSum(Plugin):
    """网页内容总结插件

    功能：
    1. 自动总结分享的网页内容
    2. 支持手动触发总结
    3. 支持群聊和单聊不同处理方式
    4. 支持黑名单群组配置
    """

    # 默认配置
    DEFAULT_CONFIG = {
        "max_words": 8000,
        "prompt": "我需要对下面引号内文档进行总结，总结输出包括以下三个部分：\n📖 一句话总结\n🔑 关键要点,用数字序号列出3-5个文章的核心内容\n🏷 标签: #xx #xx\n请使用emoji让你的表达更生动\n\n",
        "white_url_list": [],
        "black_url_list": [
            "https://support.weixin.qq.com",
            "https://channels-aladin.wxqcloud.qq.com",
            "https://www.wechat.com",
            "https://channels.weixin.qq.com",
            "https://docs.qq.com",
            "https://work.weixin.qq.com",
            "https://map.baidu.com",
            "https://map.qq.com",
            "https://y.qq.com",
            "https://music.163.com",
        ],
        "black_group_list": [],
        "auto_sum": True,
        "cache_timeout": 900,  # 缓存超时时间（15分钟）
        "openai_api_base": "https://api.openai.com/v1",
        "openai_api_key": "",
        "openai_model": "gpt-4o-2024-08-06",
        "qa_prompt": "根据以下文章内容回答问题：\n'''{content}'''\n\n问题：{question}\n要求：答案需准确简洁，引用原文内容需用引号标注",
        "qa_trigger": "问",
    }

    def __init__(self):
        """初始化插件配置"""
        try:
            super().__init__()

            # 合并默认配置和用户配置（用户配置覆盖默认）
            user_config = super().load_config() or {}
            self.config = {**self.DEFAULT_CONFIG, **user_config}  # 确保用户配置优先

            # 类型验证和转换
            self.max_words = int(self.config["max_words"])
            self.prompt = str(self.config["prompt"])
            self.cache_timeout = int(self.config["cache_timeout"])
            self.auto_sum = self.config["auto_sum"]

            # API配置处理
            self.openai_api_base = str(self.config["openai_api_base"]).rstrip("/")
            self.openai_api_key = str(self.config["openai_api_key"])
            self.openai_model = str(self.config["openai_model"])  # 保持变量名一致性

            # 列表类型配置处理
            self.white_url_list = list(map(str, self.config["white_url_list"]))
            self.black_url_list = list(map(str, self.config["black_url_list"]))
            self.black_group_list = list(map(str, self.config["black_group_list"]))

            # 问答相关配置
            self.qa_prompt = str(self.config["qa_prompt"])
            self.qa_trigger = str(self.config["qa_trigger"])  # 修复变量名拼写错误

            # 消息缓存
            self.pending_messages = {}  # 用于存储待处理的消息，格式: {chat_id: {"content": content, "timestamp": time.time()}}
            self.content_cache = {}  # 用于存储已处理的内容缓存，格式: {url: {"content": content, "timestamp": time.time()}}

            logger.info("[JinaSum] 初始化完成")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{str(e)}", exc_info=True)
            raise Exception("[JinaSum] 初始化失败")

    def on_handle_context(self, e_context: EventContext):
        """处理消息"""
        context = e_context["context"]
        logger.info(f"[JinaSum] 收到消息, 类型={context.type}, 内容长度={len(context.content)}")

        # 首先在日志中记录完整的消息内容，便于调试
        orig_content = context.content
        if len(orig_content) > 500:
            logger.info(f"[JinaSum] 消息内容(截断): {orig_content[:500]}...")
        else:
            logger.info(f"[JinaSum] 消息内容: {orig_content}")

        if context.type not in [ContextType.TEXT, ContextType.SHARING]:
            logger.info(f"[JinaSum] 消息类型不符合处理条件，跳过: {context.type}")
            return

        content = context.content
        # channel = e_context["channel"]
        msg = e_context["context"]["msg"]
        chat_id = msg.from_user_id
        is_group = msg.is_group

        # 打印前50个字符用于调试
        preview = content[:50] + "..." if len(content) > 50 else content
        logger.info(f"[JinaSum] 处理消息: {preview}, 类型={context.type}")

        # 检查内容是否为XML格式（哔哩哔哩等第三方分享卡片）
        if content.startswith("<?xml") or (content.startswith("<msg>") and "<appmsg" in content) or ("<appmsg" in content and "<url>" in content):
            logger.info("[JinaSum] 检测到XML格式分享卡片，尝试提取URL")
            try:
                # 处理可能的XML声明
                if content.startswith("<?xml"):
                    content = content[content.find("<msg>") :]

                # 如果不是完整的XML，尝试添加根节点
                if not content.startswith("<msg") and "<appmsg" in content:
                    content = f"<msg>{content}</msg>"

                # 对于一些可能格式不标准的XML，使用更宽松的解析方式
                try:
                    root = ET.fromstring(content)
                except ET.ParseError:
                    # 尝试用正则表达式提取URL

                    url_match = re.search(r"<url>(.*?)</url>", content)
                    if url_match:
                        extracted_url = url_match.group(1)
                        logger.info(f"[JinaSum] 通过正则表达式从XML中提取到URL: {extracted_url}")
                        content = extracted_url
                        context.type = ContextType.SHARING
                        context.content = extracted_url
                    else:
                        logger.error("[JinaSum] 无法通过正则表达式从XML中提取URL")
                        return
                else:
                    # XML解析成功
                    url_elem = root.find(".//url")
                    title_elem = root.find(".//title")

                    # 检查是否有appinfo节点，判断是否为B站等特殊应用
                    appinfo = root.find(".//appinfo")
                    app_name = None
                    if appinfo is not None and appinfo.find("appname") is not None:
                        app_name = appinfo.find("appname").text
                        logger.info(f"[JinaSum] 检测到APP分享: {app_name}")

                    logger.info(f"[JinaSum] XML解析结果: url_elem={url_elem is not None}, title_elem={title_elem is not None}, app_name={app_name}")

                    if url_elem is not None and url_elem.text:
                        # 提取到URL，将类型修改为SHARING
                        extracted_url = url_elem.text
                        logger.info(f"[JinaSum] 从XML中提取到URL: {extracted_url}")
                        content = extracted_url
                        context.type = ContextType.SHARING
                        context.content = extracted_url

                        # 对于B站视频链接，记录额外信息
                        if app_name and ("哔哩哔哩" in app_name or "bilibili" in app_name.lower() or "b站" in app_name):
                            logger.info("[JinaSum] 检测到B站视频分享")
                            # 可以在这里添加B站视频的特殊处理逻辑
                    else:
                        logger.error("[JinaSum] 无法从XML中提取URL")
                        return
            except Exception as e:
                logger.error(f"[JinaSum] 解析XML失败: {str(e)}", exc_info=True)
                return

        # 检查是否需要自动总结
        should_auto_sum = self.auto_sum
        if should_auto_sum and is_group and msg.from_user_nickname in self.black_group_list:
            should_auto_sum = False

        # 清理过期缓存
        self._clean_expired_cache()

        # 处理分享消息
        if context.type == ContextType.SHARING:
            logger.debug("[JinaSum] Processing SHARING message")
            if is_group:
                if should_auto_sum:
                    return self._process_summary(content, e_context, retry_count=0)
                else:
                    self.pending_messages[chat_id] = {"content": content, "timestamp": time.time()}
                    logger.debug(f"[JinaSum] Cached SHARING message: {content}, chat_id={chat_id}")
                    return
            else:  # 单聊消息直接处理
                return self._process_summary(content, e_context, retry_count=0)

        # 处理文本消息
        elif context.type == ContextType.TEXT:
            logger.debug("[JinaSum] Processing TEXT message")
            content = content.strip()

            # 移除可能的@信息
            if content.startswith("@"):
                parts = content.split(" ", 1)
                if len(parts) > 1:
                    content = parts[1].strip()
                else:
                    content = ""

            # 检查是否包含"总结"关键词（仅群聊需要）
            if is_group and "总结" in content:
                logger.debug(f"[JinaSum] Found summary trigger, pending_messages={self.pending_messages}")
                if chat_id in self.pending_messages:
                    cached_content = self.pending_messages[chat_id]["content"]
                    logger.debug(f"[JinaSum] Processing cached content: {cached_content}")
                    del self.pending_messages[chat_id]
                    return self._process_summary(cached_content, e_context, retry_count=0, skip_notice=False)

                # 检查是否是直接URL总结，移除"总结"并检查剩余内容是否为URL
                url = content.replace("总结", "").strip()
                if url and self._check_url(url):
                    logger.debug(f"[JinaSum] Processing direct URL: {url}")
                    return self._process_summary(url, e_context, retry_count=0, skip_notice=False)
                logger.debug("[JinaSum] No content to summarize")
                return

            # 处理"问xxx"格式的追问
            if content.startswith("问"):
                question = content[1:].strip()
                if question:
                    logger.debug(f"[JinaSum] Processing question: {question}")
                    return self._process_question(question, chat_id, e_context)
                else:
                    logger.debug("[JinaSum] Empty question, ignored")
                    return

            # 单聊中直接处理URL
            if not is_group:
                url = content.replace("总结", "").strip()
                if url and self._check_url(url):
                    logger.debug(f"[JinaSum] Processing direct URL: {url}")
                    return self._process_summary(url, e_context, retry_count=0)
                logger.debug("[JinaSum] No content to summarize")
                return

    def _clean_expired_cache(self):
        """清理过期的缓存"""
        current_time = time.time()
        # 清理待处理消息缓存
        expired_keys = [k for k, v in self.pending_messages.items() if current_time - v["timestamp"] > self.cache_timeout]
        for k in expired_keys:
            del self.pending_messages[k]

    def _extract_wechat_article(self, url, headers):
        """专门处理微信公众号文章

        Args:
            url: 微信文章URL
            headers: 请求头

        Returns:
            str: 提取的内容，失败返回None
        """
        try:
            # 添加必要的微信Cookie参数，减少被检测的可能性
            cookies = {
                "appmsglist_action_3941382959": "card",  # 一些随机的Cookie值
                "appmsglist_action_3941382968": "card",
                "pac_uid": f"{int(time.time())}_f{random.randint(10000, 99999)}",
                "rewardsn": "",
                "wxtokenkey": f"{random.randint(100000, 999999)}",
            }

            # 直接使用requests进行内容获取，有时比newspaper更有效
            session = requests.Session()
            response = session.get(url, headers=headers, cookies=cookies, timeout=20)
            response.raise_for_status()

            # 使用BeautifulSoup直接解析
            soup = BeautifulSoup(response.content, "html.parser")

            # 微信文章通常有这些特征
            title_elem = soup.select_one("#activity-name")
            author_elem = soup.select_one("#js_name") or soup.select_one("#js_profile_qrcode > div > strong")
            content_elem = soup.select_one("#js_content")

            if content_elem:
                # 移除无用元素
                for remove_elem in content_elem.select("script, style, svg"):
                    remove_elem.extract()

                # 尝试获取所有文本
                text_content = content_elem.get_text(separator="\n", strip=True)

                if text_content and len(text_content) > 200:  # 内容足够长
                    title = title_elem.get_text(strip=True) if title_elem else ""
                    author = author_elem.get_text(strip=True) if author_elem else "未知作者"

                    # 构建完整内容
                    full_content = ""
                    if title:
                        full_content += f"标题: {title}\n"
                    if author and author != "未知作者":
                        full_content += f"作者: {author}\n"
                    full_content += f"\n{text_content}"

                    logger.debug(f"[JinaSum] 成功通过直接请求提取微信文章内容，长度: {len(text_content)}")
                    return full_content
        except Exception as e:
            logger.error(f"[JinaSum] 直接请求提取微信文章失败: {str(e)}")

        return None

    def _extract_bilibili_video(self, url, title):
        """处理B站视频内容提取

        Args:
            url: B站视频URL
            title: 视频标题(如果已知)

        Returns:
            str: 提取的内容，通常只包含标题和提示信息
        """
        if title:
            return f"标题: {title}\n\n描述: 这是一个B站视频，无法获取完整内容。请直接观看视频。"
        else:
            return "这是一个B站视频链接。由于视频内容无法直接提取，请直接点击链接观看视频。"

    def _extract_with_newspaper(self, url, user_agent):
        """使用newspaper库提取文章内容

        Args:
            url: 文章URL
            user_agent: 使用的User-Agent

        Returns:
            str: 提取的内容，失败返回None
        """
        try:
            # 配置newspaper
            newspaper.Config().browser_user_agent = user_agent
            newspaper.Config().request_timeout = 30
            newspaper.Config().fetch_images = False  # 不下载图片以加快速度
            newspaper.Config().memoize_articles = False  # 避免缓存导致的问题

            # 创建Article对象但不立即下载
            article = Article(url, language="zh")

            # 手动下载
            try:
                # 构建更真实的请求头
                headers = {
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0",
                }

                session = requests.Session()
                response = session.get(url, headers=headers, timeout=30)
                response.raise_for_status()

                # 手动设置html内容
                article.html = response.text
                article.download_state = 2  # 表示下载完成
            except Exception as direct_dl_error:
                logger.error(f"[JinaSum] 尝试定制下载失败，回退到标准方法: {str(direct_dl_error)}")
                article.download()

            # 解析文章
            article.parse()

            # 尝试获取完整内容
            title = article.title
            authors = ", ".join(article.authors) if article.authors else "未知作者"
            publish_date = article.publish_date.strftime("%Y-%m-%d") if article.publish_date else "未知日期"
            content = article.text

            # 如果内容为空或过短，尝试直接从HTML获取
            if not content or len(content) < 500:
                logger.debug("[JinaSum] Article content too short, trying to extract from HTML directly")
                content = self._extract_from_html_directly(article.html)

            # 合成最终内容
            if title:
                full_content = f"标题: {title}\n"
                if authors and authors != "未知作者":
                    full_content += f"作者: {authors}\n"
                if publish_date and publish_date != "未知日期":
                    full_content += f"发布日期: {publish_date}\n"
                full_content += f"\n{content}"
            else:
                full_content = content

            if not full_content or len(full_content.strip()) < 50:
                logger.debug("[JinaSum] No content extracted by newspaper")
                return None

            logger.debug(f"[JinaSum] Successfully extracted content via newspaper, length: {len(full_content)}")
            return full_content
        except Exception as e:
            logger.error(f"[JinaSum] Error extracting content via newspaper: {str(e)}")
            return None

    def _extract_from_html_directly(self, html_content):
        """直接从HTML内容提取文本

        Args:
            html_content: 网页HTML内容

        Returns:
            str: 提取的文本内容
        """
        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # 移除脚本和样式元素
            for script in soup(["script", "style"]):
                script.extract()

            # 获取所有文本
            text = soup.get_text(separator="\n", strip=True)
            return text
        except Exception as bs_error:
            logger.error(f"[JinaSum] BeautifulSoup extraction failed: {str(bs_error)}")
            return ""

    def _resolve_b23_short_url(self, url):
        """解析B站短链接获取真实URL

        Args:
            url: B站短链接

        Returns:
            str: 解析后的真实URL，失败返回原始URL
        """
        try:
            logger.debug(f"[JinaSum] Resolving B站短链接: {url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "max-age=0",
                "Connection": "keep-alive",
            }
            response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
            if response.status_code == 200:
                real_url = response.url
                logger.debug(f"[JinaSum] B站短链接解析结果: {real_url}")
                return real_url
        except Exception as e:
            logger.error(f"[JinaSum] 解析B站短链接失败: {str(e)}")

        return url

    def _get_content_via_newspaper(self, url):
        """使用newspaper3k库提取文章内容，优化版本

        Args:
            url: 文章URL

        Returns:
            str: 文章内容,失败返回None
        """
        try:
            # 检查缓存是否存在且未过期
            if url in self.content_cache:
                cached_data = self.content_cache[url]
                if time.time() - cached_data["timestamp"] <= self.cache_timeout:
                    logger.debug(f"[JinaSum] 使用缓存内容，URL: {url}")
                    return cached_data["content"]
                else:
                    del self.content_cache[url]

            # 处理B站短链接
            if "b23.tv" in url:
                url = self._resolve_b23_short_url(url)

            # 随机选择一个User-Agent，模拟不同浏览器
            user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            ]
            selected_ua = random.choice(user_agents)

            # 构建更真实的请求头
            headers = {
                "User-Agent": selected_ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }

            # 设置一个随机的引荐来源，微信文章有时需要Referer
            referers = [
                "https://www.baidu.com/",
                "https://www.google.com/",
                "https://www.bing.com/",
                "https://mp.weixin.qq.com/",
                "https://weixin.qq.com/",
                "https://www.qq.com/",
            ]
            if random.random() > 0.3:  # 70%的概率添加Referer
                headers["Referer"] = random.choice(referers)

            # 为微信公众号文章添加特殊处理
            if "mp.weixin.qq.com" in url:
                wechat_content = self._extract_wechat_article(url, headers)
                if wechat_content:
                    return wechat_content
                # 如果特殊处理失败，会继续使用newspaper尝试

            # 使用newspaper提取内容
            extracted_content = self._extract_with_newspaper(url, selected_ua)
            if extracted_content:
                # 对于B站视频，特殊处理
                if "bilibili.com" in url or "b23.tv" in url:
                    title = None
                    if extracted_content.startswith("标题:"):
                        title_line = extracted_content.split("\n")[0]
                        title = title_line.replace("标题:", "").strip()

                    if title and not extracted_content or len(extracted_content.split("\n\n")[-1].strip()) < 100:
                        return self._extract_bilibili_video(url, title)

                return extracted_content

            # 尝试使用通用内容提取方法作为备用
            content = self._extract_content_general(url, headers)
            if content:
                return content

            # 所有方法都失败，提供特定网站的错误信息
            if "mp.weixin.qq.com" in url:
                return f"无法获取微信公众号文章内容。可能原因：\n1. 文章需要登录才能查看\n2. 文章已被删除\n3. 服务器被微信风控\n\n请尝试直接打开链接: {url}"

            # 通用失败消息
            return None

        except Exception as e:
            logger.error(f"[JinaSum] Error extracting content via newspaper: {str(e)}")

            # 尝试通用内容提取方法作为最后手段
            try:
                content = self._extract_content_general(url)
                if content:
                    return content
            except Exception:
                pass

            return None

    def _try_static_content_extraction(self, url, headers):
        """尝试静态提取网页内容

        Args:
            url: 网页URL
            headers: 请求头

        Returns:
            str 或 None: 提取的内容，失败返回None
        """
        try:
            # 创建会话对象
            session = requests.Session()

            # 设置基本cookies
            session.cookies.update(
                {
                    f"visit_id_{int(time.time())}": f"{random.randint(1000000, 9999999)}",
                    "has_visited": "1",
                }
            )

            # 发送请求获取页面
            logger.debug(f"[JinaSum] 通用提取方法正在请求: {url}")
            response = session.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            # 确保编码正确
            if response.encoding == "ISO-8859-1":
                response.encoding = response.apparent_encoding

            # 使用BeautifulSoup解析HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # 移除无用元素
            for element in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
                element.extract()

            # 获取标题和内容
            title = self._find_title(soup)
            content_element = self._find_best_content(soup)

            # 如果找到内容元素，提取并清理文本
            if content_element:
                # 移除内容中可能的广告或无关元素
                for ad in content_element.select('[class*="ad" i], [class*="banner" i], [id*="ad" i], [class*="recommend" i]'):
                    ad.extract()

                # 获取并清理文本
                content_text = content_element.get_text(separator="\n", strip=True)

                # 移除多余的空白行
                content_text = re.sub(r"\n{3,}", "\n\n", content_text)

                # 构建最终输出
                result = ""
                if title:
                    result += f"标题: {title}\n\n"

                result += content_text

                logger.debug(f"[JinaSum] 通用提取方法成功，提取内容长度: {len(result)}")
                return result

            return None
        except Exception as e:
            logger.debug(f"[JinaSum] 静态提取失败: {str(e)}")
            return None

    def _find_title(self, soup):
        """从BeautifulSoup对象中找到最佳标题

        Args:
            soup: BeautifulSoup对象

        Returns:
            str 或 None: 找到的标题，没找到返回None
        """
        # 尝试多种标题选择器
        title_candidates = [
            soup.select_one("h1"),  # 最常见的标题标签
            soup.select_one("title"),  # HTML标题
            soup.select_one(".title"),  # 常见的标题类
            soup.select_one(".article-title"),  # 常见的文章标题类
            soup.select_one(".post-title"),  # 博客标题
            soup.select_one('[class*="title" i]'),  # 包含title的类
        ]

        for candidate in title_candidates:
            if candidate and candidate.text.strip():
                return candidate.text.strip()

        return None

    def _find_best_content(self, soup):
        """查找网页中最佳内容元素

        Args:
            soup: BeautifulSoup对象

        Returns:
            BeautifulSoup元素 或 None: 找到的最佳内容元素，没找到返回None
        """
        # 查找可能的内容元素
        content_candidates = []

        # 1. 尝试找常见的内容容器
        content_selectors = [
            "article",
            "main",
            ".content",
            ".article",
            ".post-content",
            '[class*="content" i]',
            '[class*="article" i]',
            ".story",
            ".entry-content",
            ".post-body",
            "#content",
            "#article",
            ".body",
        ]

        for selector in content_selectors:
            elements = soup.select(selector)
            if elements:
                content_candidates.extend(elements)

        # 2. 如果没有找到明确的内容容器，寻找具有最多文本的div元素
        if not content_candidates:
            paragraphs = {}
            # 查找所有段落和div
            for elem in soup.find_all(["p", "div"]):
                text = elem.get_text(strip=True)
                # 只考虑有实际内容的元素
                if len(text) > 100:
                    paragraphs[elem] = len(text)

            # 找出文本最多的元素
            if paragraphs:
                max_elem = max(paragraphs.items(), key=lambda x: x[1])[0]
                # 如果是div，直接添加；如果是p，尝试找其父元素
                if max_elem.name == "div":
                    content_candidates.append(max_elem)
                else:
                    # 找包含多个段落的父元素
                    parent = max_elem.parent
                    if parent and len(parent.find_all("p")) > 3:
                        content_candidates.append(parent)
                    else:
                        content_candidates.append(max_elem)

        # 3. 简单算法来评分和选择最佳内容元素
        return self._score_content_elements(content_candidates)

    def _score_content_elements(self, content_candidates):
        """对内容候选元素进行评分，返回最佳内容元素

        Args:
            content_candidates: 内容候选元素列表

        Returns:
            BeautifulSoup元素 或 None: 找到的最佳内容元素，没找到返回None
        """
        best_content = None
        max_score = 0

        for element in content_candidates:
            # 计算文本长度
            text = element.get_text(strip=True)
            text_length = len(text)

            # 计算文本密度（文本长度/HTML长度）
            html_length = len(str(element))
            text_density = text_length / html_length if html_length > 0 else 0

            # 计算段落数量
            paragraphs = element.find_all("p")
            paragraph_count = len(paragraphs)

            # 检查是否有图片
            images = element.find_all("img")
            image_count = len(images)

            # 根据各种特征计算分数
            score = (
                text_length * 1.0  # 文本长度很重要
                + text_density * 100  # 文本密度很重要
                + paragraph_count * 30  # 段落数量也很重要
                + image_count * 10  # 图片不太重要，但也是一个指标
            )

            # 减分项：如果包含许多链接，可能是导航或侧边栏
            links = element.find_all("a")
            link_text_ratio = sum(len(a.get_text(strip=True)) for a in links) / text_length if text_length > 0 else 0
            if link_text_ratio > 0.5:  # 如果链接文本占比过高
                score *= 0.5

            # 更新最佳内容
            if score > max_score:
                max_score = score
                best_content = element

        return best_content

    def _extract_content_general(self, url, headers=None):
        """通用网页内容提取方法，支持静态和动态页面，优化版本

        首先尝试静态提取（更快、更轻量），如果失败或内容太少再尝试动态提取（更慢但更强大）

        Args:
            url: 网页URL
            headers: 可选的请求头，如果为None则使用默认

        Returns:
            str: 提取的内容，失败返回None
        """
        try:
            # 如果是百度文章链接，使用专门的处理方法
            if "md.mbd.baidu.com" in url or "mbd.baidu.com" in url:
                # 直接使用专门的百度文章提取方法
                content = self._extract_baidu_article(url)
                if content:
                    return content

            # 如果没有提供headers，创建一个默认的
            if not headers:
                headers = self._get_default_headers()

            # 添加随机延迟以避免被检测为爬虫
            time.sleep(random.uniform(0.5, 2))

            # 尝试静态提取内容
            static_content_result = self._try_static_content_extraction(url, headers)

            # 判断静态提取的内容质量
            content_is_good = False
            if static_content_result:
                # 内容长度检查
                if len(static_content_result) > 1000:
                    content_is_good = True
                # 结构检查 - 至少应该有多个段落
                elif static_content_result.count("\n\n") >= 3:
                    content_is_good = True

            # 如果静态提取内容质量不佳，尝试动态提取
            if not content_is_good:
                logger.debug("[JinaSum] 静态提取内容质量不佳，尝试动态提取")
                dynamic_content = self._extract_dynamic_content(url, headers)
                if dynamic_content:
                    logger.debug(f"[JinaSum] 动态提取成功，内容长度: {len(dynamic_content)}")
                    return dynamic_content

            return static_content_result

        except Exception as e:
            logger.error(f"[JinaSum] 通用内容提取方法失败: {str(e)}", exc_info=True)
            return None

    def _extract_dynamic_content(self, url, headers=None):
        """使用JavaScript渲染提取动态页面内容，优化版本

        Args:
            url: 网页URL
            headers: 可选的请求头

        Returns:
            str: 提取的内容，失败返回None
        """
        session = None
        try:
            logger.debug(f"[JinaSum] 开始动态提取内容: {url}")

            # 创建会话并设置超时
            session = HTMLSession()

            # 添加请求头
            req_headers = headers or self._get_default_headers()

            # 获取页面
            response = session.get(url, headers=req_headers, timeout=30)

            # 执行JavaScript (设置超时，防止无限等待)
            logger.debug("[JinaSum] 开始执行JavaScript")
            response.html.render(timeout=20, sleep=2)
            logger.debug("[JinaSum] JavaScript执行完成")

            # 处理渲染后的HTML
            rendered_html = response.html.html

            # 解析渲染后的HTML并提取内容
            content = self._extract_content_from_rendered_html(rendered_html)

            # 关闭会话
            if session:
                session.close()

            return content

        except Exception as e:
            logger.error(f"[JinaSum] 动态提取失败: {str(e)}", exc_info=True)
            # 确保会话被关闭
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            return None

    def _extract_content_from_rendered_html(self, rendered_html):
        """从渲染后的HTML中提取内容

        Args:
            rendered_html: 渲染后的HTML内容

        Returns:
            str: 提取的内容，失败返回None
        """
        try:
            # 使用BeautifulSoup解析渲染后的HTML
            soup = BeautifulSoup(rendered_html, "html.parser")

            # 清理无用元素
            for element in soup(["script", "style", "nav", "header", "footer", "aside"]):
                element.extract()

            # 查找标题
            title = self._find_title(soup)

            # 寻找主要内容
            main_content = self._find_dynamic_content(soup)

            # 从主要内容中提取文本
            if main_content:
                # 清理可能的广告或无关元素
                for ad in main_content.select('[class*="ad" i], [class*="banner" i], [id*="ad" i], [class*="recommend" i]'):
                    ad.extract()

                # 获取文本
                content_text = main_content.get_text(separator="\n", strip=True)
                content_text = re.sub(r"\n{3,}", "\n\n", content_text)  # 清理多余空行

                # 构建最终结果
                result = ""
                if title:
                    result += f"标题: {title}\n\n"
                result += content_text

                return result

            return None
        except Exception as e:
            logger.debug(f"[JinaSum] 解析渲染HTML失败: {str(e)}")
            return None

    def _find_dynamic_content(self, soup):
        """为动态渲染页面找到主要内容元素

        Args:
            soup: BeautifulSoup对象

        Returns:
            BeautifulSoup元素: 找到的内容元素，失败返回None
        """
        # 1. 尝试找主要内容容器
        main_selectors = ["article", "main", ".content", ".article", '[class*="content" i]', '[class*="article" i]', "#content", "#article"]

        for selector in main_selectors:
            elements = soup.select(selector)
            if elements:
                # 选择包含最多文本的元素
                return max(elements, key=lambda x: len(x.get_text()))

        # 2. 如果没找到，寻找文本最多的div
        paragraphs = {}
        for elem in soup.find_all(["div"]):
            text = elem.get_text(strip=True)
            if len(text) > 200:  # 只考虑长文本
                paragraphs[elem] = len(text)

        if paragraphs:
            return max(paragraphs.items(), key=lambda x: x[1])[0]

        # 3. 如果还是没找到，使用整个body
        return soup.body

    def _extract_baidu_article(self, url):
        """专门用于提取百度文章内容的方法，优化版本

        Args:
            url: 百度文章URL

        Returns:
            str: 提取的内容，失败返回None
        """
        try:
            logger.debug(f"[JinaSum] 尝试专门提取百度文章: {url}")

            # 提取文章ID
            article_id = self._extract_baidu_article_id(url)
            if not article_id:
                logger.error(f"[JinaSum] 无法从URL提取百度文章ID: {url}")
                return None

            logger.debug(f"[JinaSum] 提取到百度文章ID: {article_id}")

            # 初始化移动设备UA列表供后续使用
            self.mobile_user_agents = [
                "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
                "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36",
                "Mozilla/5.0 (iPad; CPU OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/94.0.4606.76 Mobile/15E148 Safari/604.1",
            ]

            # 构建多种URL尝试提取
            url_formats = [
                # 尝试直接访问原始URL
                url,
                # 尝试移动网页版格式1
                f"https://mbd.baidu.com/newspage/data/landingshare?context=%7B%22nid%22%3A%22news_{article_id}%22%2C%22sourceFrom%22%3A%22bjh%22%7D",
                # 尝试移动网页版格式2
                f"https://mbd.baidu.com/newspage/data/landingsuper?context=%7B%22nid%22%3A%22news_{article_id}%22%7D",
            ]

            # 依次尝试每个URL格式
            for target_url in url_formats:
                content = self._try_extract_baidu_url(target_url)
                if content:
                    return content

            # 所有尝试都失败，返回None
            logger.error("[JinaSum] 所有百度文章提取方法均失败")
            return None

        except Exception as e:
            logger.error(f"[JinaSum] 专门提取百度文章失败: {str(e)}")
            return None

    def _extract_baidu_article_id(self, url):
        """从百度文章URL中提取文章ID

        Args:
            url: 百度文章URL

        Returns:
            str: 文章ID，失败返回None
        """
        try:
            # 提取文章ID
            article_id = None
            parsed_url = urlparse(url)
            path_parts = parsed_url.path.split("/")

            # 例如 /r/1A1GKWoodMI
            if len(path_parts) > 1 and path_parts[-2] == "r":
                article_id = path_parts[-1]

            # 例如 ?r=1A1GKWoodMI
            if not article_id:
                query_params = parse_qs(parsed_url.query)
                if "r" in query_params:
                    article_id = query_params["r"][0]

            return article_id
        except Exception as e:
            logger.debug(f"[JinaSum] 提取百度文章ID失败: {str(e)}")
            return None

    def _try_extract_baidu_url(self, target_url):
        """尝试从单个百度文章URL中提取内容

        Args:
            target_url: 百度文章URL格式

        Returns:
            str: 成功提取的内容，失败返回None
        """
        try:
            logger.debug(f"[JinaSum] 尝试百度文章URL格式: {target_url}")

            # 构建请求头
            headers = {
                "User-Agent": random.choice(self.mobile_user_agents),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }

            # 发送请求
            response = requests.get(target_url, headers=headers, timeout=15, allow_redirects=True)
            response.raise_for_status()

            # 确保编码正确
            if response.encoding == "ISO-8859-1":
                response.encoding = response.apparent_encoding

            # 检查是否是JSON响应 - 某些百度API会返回JSON
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type or response.text.strip().startswith("{"):
                return self._extract_from_json(response.text)

            # 解析HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # 尝试提取JSON数据
            json_content = self._extract_from_script_json(soup)
            if json_content:
                return json_content

            # 尝试从HTML直接提取内容
            html_content = self._extract_from_baidu_html(soup)
            if html_content:
                return html_content

            return None
        except Exception as e:
            logger.debug(f"[JinaSum] 尝试URL {target_url} 失败: {str(e)}")
            return None

    def _extract_from_json(self, json_text):
        """从JSON响应中提取百度文章内容

        Args:
            json_text: JSON响应文本

        Returns:
            str: 提取的内容，失败返回None
        """
        try:
            data = json.loads(json_text)
            # 检查JSON数据中是否包含文章内容
            if data.get("data", {}).get("title") and (data.get("data", {}).get("content") or data.get("data", {}).get("html")):
                title = data["data"]["title"]
                content_html = data["data"].get("content", "") or data["data"].get("html", "")
                author = data["data"].get("author", "")
                publish_time = data["data"].get("publish_time", "")

                # 解析HTML内容
                content_soup = BeautifulSoup(content_html, "html.parser")

                # 移除脚本和样式
                for tag in content_soup(["script", "style"]):
                    tag.decompose()

                # 提取纯文本
                content_text = content_soup.get_text(separator="\n", strip=True)

                # 构建结果
                result = f"标题: {title}\n"
                if author:
                    result += f"作者: {author}\n"
                if publish_time:
                    result += f"时间: {publish_time}\n"

                result += f"\n{content_text}"

                logger.debug(f"[JinaSum] 成功通过JSON提取百度文章，长度: {len(result)}")
                return result
        except json.JSONDecodeError:
            return None
        except Exception as e:
            logger.debug(f"[JinaSum] 从JSON提取内容失败: {str(e)}")
            return None

    def _extract_from_script_json(self, soup):
        """从HTML中的脚本标签提取嵌入JSON数据

        Args:
            soup: BeautifulSoup解析的HTML

        Returns:
            str: 提取的内容，失败返回None
        """
        for script in soup.find_all("script"):
            script_text = script.string
            if not script_text or not ("content" in script_text or "article" in script_text):
                continue

            try:
                # 尝试找到JSON格式的数据
                json_start = script_text.find("{")
                json_end = script_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = script_text[json_start:json_end]
                    data = json.loads(json_str)

                    # 检查是否包含文章数据
                    article_data = None
                    if "article" in data:
                        article_data = data["article"]
                    elif "data" in data and "article" in data["data"]:
                        article_data = data["data"]["article"]

                    if article_data and "title" in article_data:
                        title = article_data.get("title", "")
                        content = article_data.get("content", "")
                        author = article_data.get("author", "")
                        publish_time = article_data.get("publish_time", "")

                        # 解析HTML内容
                        if content:
                            content_soup = BeautifulSoup(content, "html.parser")
                            content_text = content_soup.get_text(separator="\n", strip=True)

                            # 构建结果
                            result = f"标题: {title}\n"
                            if author:
                                result += f"作者: {author}\n"
                            if publish_time:
                                result += f"时间: {publish_time}\n"

                            result += f"\n{content_text}"

                            logger.debug(f"[JinaSum] 成功从嵌入JSON提取百度文章，长度: {len(result)}")
                            return result
            except Exception as e:
                logger.debug(f"[JinaSum] 从脚本提取JSON失败: {str(e)}")

        return None

    def _extract_from_baidu_html(self, soup):
        """从HTML直接提取百度文章内容

        Args:
            soup: BeautifulSoup解析的HTML

        Returns:
            str: 提取的内容，失败返回None
        """
        # 提取标题
        title = None
        for selector in [".article-title", ".title", "h1.title", "h1"]:
            title_elem = soup.select_one(selector)
            if title_elem and title_elem.text.strip():
                title = title_elem.text.strip()
                break

        # 如果没找到标题，尝试使用标题标签
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.text.strip()

        # 提取作者
        author = None
        for selector in [".author", ".writer", ".source", ".article-author"]:
            author_elem = soup.select_one(selector)
            if author_elem and author_elem.text.strip():
                author = author_elem.text.strip()
                break

        # 提取内容
        content = None
        for selector in [".article-content", ".article-detail", ".content", ".artcle", "#article"]:
            content_elem = soup.select_one(selector)
            if content_elem:
                # 移除无用元素
                for remove_elem in content_elem.select(".ad-banner, .recommend, .share-btn, script, style"):
                    remove_elem.extract()

                content_text = content_elem.get_text(separator="\n", strip=True)
                if len(content_text) > 200:  # 内容足够长
                    content = content_text
                    break

        # 如果没找到内容，尝试查找最长的段落集合
        if not content:
            max_paragraphs = []
            max_text_len = 0

            # 查找所有可能的内容容器
            for div in soup.find_all("div"):
                paragraphs = div.find_all("p")
                if len(paragraphs) >= 3:  # 至少有3个段落
                    text = "\n".join([p.get_text(strip=True) for p in paragraphs])
                    if len(text) > max_text_len:
                        max_text_len = len(text)
                        max_paragraphs = paragraphs

            # 如果找到足够长的段落集合
            if max_text_len > 200:
                content = "\n".join([p.get_text(strip=True) for p in max_paragraphs])

        # 如果找到内容，构建结果
        if content:
            result = ""
            if title:
                result += f"标题: {title}\n"
            if author:
                result += f"作者: {author}\n"
            result += f"\n{content}"

            logger.debug(f"[JinaSum] 成功通过HTML提取百度文章，长度: {len(result)}")
            return result

        return None

    def _get_default_headers(self):
        """获取默认请求头"""

        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
        ]
        selected_ua = random.choice(user_agents)

        return {
            "User-Agent": selected_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    def _process_summary(self, content: str, e_context: EventContext, retry_count: int = 0, skip_notice: bool = False):
        """处理总结请求，优化版本"""
        # 提前验证URL是否有效
        if not self._check_url(content):
            logger.debug(f"[JinaSum] {content} is not a valid url, skip")
            return

        # 显示正在处理的提示
        if retry_count == 0 and not skip_notice:
            logger.debug("[JinaSum] Processing URL: %s" % content)
            reply = Reply(ReplyType.TEXT, "🎉正在为您生成总结，请稍候...")
            channel = e_context["channel"]
            channel.send(reply, e_context["context"])

        try:
            # 获取网页内容
            target_url = html.unescape(content)
            target_url_content = self._get_web_content(target_url)

            # 检查返回的内容是否包含验证提示
            if target_url_content and target_url_content.startswith("⚠️"):
                # 这是一个验证提示，直接返回给用户
                logger.info(f"[JinaSum] 返回验证提示给用户: {target_url_content}")
                reply = Reply(ReplyType.INFO, target_url_content)
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            # 如果提取内容失败，尝试特殊处理
            if not target_url_content:
                # 对于B站视频，提供特殊处理
                if "bilibili.com" in target_url or "b23.tv" in target_url:
                    target_url_content = "这是一个B站视频链接。由于视频内容无法直接提取，请直接点击链接观看视频。"
                else:
                    raise ValueError("无法提取文章内容")

            # 清洗内容
            target_url_content = self._clean_content(target_url_content)
            # 将清洗后的内容存入缓存
            self.content_cache[target_url] = {"content": target_url_content, "timestamp": time.time()}

            # 限制内容长度
            target_url_content = target_url_content[: self.max_words]
            logger.debug(f"[JinaSum] Got content length: {len(target_url_content)}")

            # 构造提示词和内容
            sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"

            # 调用OpenAI API生成总结
            self._call_openai_api(sum_prompt, e_context)

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing summary: {str(e)}")
            self._handle_summary_error(content, e_context, retry_count, e)

    def _get_web_content(self, url):
        """从URL获取网页内容，处理可能的XML格式

        Args:
            url: 网页URL或XML内容

        Returns:
            str: 提取的网页内容，失败返回None
        """
        # 检查内容是否为XML格式（哔哩哔哩等第三方分享卡片）
        if url.startswith("<?xml") or (url.startswith("<msg>") and "<appmsg" in url) or ("<appmsg" in url and "<url>" in url):
            logger.info("[JinaSum] 检测到XML格式分享卡片，尝试提取URL")
            try:
                extracted_url = self._extract_url_from_xml(url)
                if extracted_url:
                    url = extracted_url
                else:
                    logger.error("[JinaSum] 无法从XML中提取URL")
                    return None
            except Exception as e:
                logger.error(f"[JinaSum] 解析XML失败: {str(e)}", exc_info=True)
                return None

        # 使用newspaper3k提取内容
        logger.debug(f"[JinaSum] 使用newspaper3k提取内容: {url}")
        return self._get_content_via_newspaper(url)

    def _extract_url_from_xml(self, xml_content):
        """从XML内容中提取URL

        Args:
            xml_content: XML格式的内容

        Returns:
            str: 提取的URL，失败返回None
        """
        try:
            # 处理可能的XML声明
            if xml_content.startswith("<?xml"):
                xml_content = xml_content[xml_content.find("<msg>") :]

            # 如果不是完整的XML，尝试添加根节点
            if not xml_content.startswith("<msg") and "<appmsg" in xml_content:
                xml_content = f"<msg>{xml_content}</msg>"

            # 尝试解析XML
            try:
                root = ET.fromstring(xml_content)
                url_elem = root.find(".//url")
                if url_elem is not None and url_elem.text:
                    extracted_url = url_elem.text
                    logger.info(f"[JinaSum] 从XML中提取到URL: {extracted_url}")
                    return extracted_url
            except ET.ParseError:
                # XML解析失败，尝试用正则表达式提取
                url_match = re.search(r"<url>(.*?)</url>", xml_content)
                if url_match:
                    extracted_url = url_match.group(1)
                    logger.info(f"[JinaSum] 通过正则表达式从XML中提取到URL: {extracted_url}")
                    return extracted_url

            return None
        except Exception as e:
            logger.error(f"[JinaSum] 提取URL失败: {str(e)}")
            return None

    def _call_openai_api(self, prompt, e_context):
        """调用OpenAI API生成内容总结

        Args:
            prompt: 发送给AI模型的提示词
            e_context: 事件上下文对象
        """
        try:
            # 构造完整请求参数
            openai_payload = {"model": self.openai_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": min(2000, self.max_words)}

            # 发送API请求
            response = requests.post(self._get_openai_chat_url(), headers={"Authorization": f"Bearer {self.openai_api_key}"}, json=openai_payload, timeout=30)
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]

            # 修改context内容
            e_context["context"].type = ContextType.TEXT
            answer = remove_markdown_symbol(answer)
            reply = Reply(ReplyType.TEXT, answer)

            # 设置回复并中断处理链
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            logger.debug(f"[JinaSum] 使用Bridge直接调用后台模型成功，回复类型={reply.type}，长度={len(reply.content) if reply.content else 0}")
            return True
        except Exception as e:
            logger.warning(f"[JinaSum] 直接调用后台失败: {str(e)}", exc_info=True)
            # 如果直接调用失败，回退到插件链的方式
            logger.debug("[JinaSum] 回退到使用插件链处理")
            e_context.action = EventAction.CONTINUE
            return False

    def _handle_summary_error(self, content, e_context, retry_count, exception=None):
        """处理总结过程中的错误

        Args:
            content: 原始URL内容
            e_context: 事件上下文
            retry_count: 当前重试次数
            exception: 捕获的异常
        """
        if retry_count < 3:
            logger.info(f"[JinaSum] Retrying {retry_count + 1}/3...")
            return self._process_summary(content, e_context, retry_count + 1, True)

        # 友好的错误提示
        error_msg = "抱歉，无法获取文章内容。可能是因为:\n"
        error_msg += "1. 文章需要登录或已过期\n"
        error_msg += "2. 文章有特殊的访问限制\n"
        error_msg += "3. 网络连接不稳定\n\n"
        error_msg += "建议您:\n"
        error_msg += "- 直接打开链接查看\n"
        error_msg += "- 稍后重试\n"
        error_msg += "- 尝试其他文章"

        reply = Reply(ReplyType.ERROR, error_msg)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def _process_question(self, question: str, chat_id: str, e_context: EventContext, retry_count: int = 0):
        """处理用户提问"""
        try:
            # 参数校验
            if not self.openai_api_key:
                raise ValueError("OpenAI API密钥未配置")
            if not question.strip():
                raise ValueError("问题内容为空")

            # 获取最近总结的内容
            recent_content = None
            recent_timestamp = 0

            # 遍历所有缓存找到最近总结的内容
            for url, cache_data in self.content_cache.items():
                if cache_data["timestamp"] > recent_timestamp:
                    recent_timestamp = cache_data["timestamp"]
                    recent_content = cache_data["content"]

            if not recent_content or time.time() - recent_timestamp > self.cache_timeout:
                logger.debug("[JinaSum] No valid content cache found or content expired")
                return  # 找不到相关文章，让后续插件处理问题

            if retry_count == 0:
                reply = Reply(ReplyType.TEXT, "🤔 正在思考您的问题，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            # 构建问答的 prompt
            qa_prompt = self.qa_prompt.format(
                content=recent_content[: self.max_words],  # 使用实例变量
                question=question.strip(),
            )

            # 构造完整请求参数
            openai_payload = {"model": self.openai_model, "messages": [{"role": "user", "content": qa_prompt}], "temperature": 0.7, "max_tokens": min(2000, self.max_words)}

            # 发送API请求
            response = requests.post(self._get_openai_chat_url(), headers={"Authorization": f"Bearer {self.openai_api_key}"}, json=openai_payload, timeout=30)
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]

            answer = remove_markdown_symbol(answer)
            reply = Reply(ReplyType.TEXT, answer)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing question: {str(e)}")
            if retry_count < 3:
                return self._process_question(question, chat_id, e_context, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"抱歉，处理您的问题时出错: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose, **kwargs):
        help_text = "网页内容总结插件:\n"
        help_text += "1. 发送「总结 网址」可以总结指定网页的内容\n"
        help_text += "2. 单聊时分享消息会自动总结\n"
        if self.auto_sum:
            help_text += "3. 群聊中分享消息默认自动总结"
            if self.black_group_list:
                help_text += "（部分群组需要发送含「总结」的消息触发）\n"
            else:
                help_text += "\n"
        else:
            help_text += "3. 群聊中收到分享消息后，发送包含「总结」的消息即可触发总结\n"
        help_text += f"4. 总结完成后5分钟内，可以发送「{self.qa_trigger}xxx」来询问文章相关问题\n"
        help_text += "注：群聊中的分享消息的总结请求需要在60秒内发出"
        return help_text

    def _get_openai_chat_url(self):
        return self.openai_api_base + "/chat/completions"

    def _get_openai_headers(self):
        """获取openai的header"""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.openai_api_key}",  # 直接使用实例变量
        }

    def _check_url(self, target_url: str):
        """增强URL检查"""
        stripped_url = target_url.strip()
        logger.debug(f"[JinaSum] 检查URL: {stripped_url}")

        # 协议头检查
        if not stripped_url.lower().startswith(("http://", "https://")):
            logger.debug("[JinaSum] URL协议头不合法")
            return False

        # 黑名单检查（不区分大小写）
        lower_url = stripped_url.lower()
        if any(black.lower() in lower_url for black in self.black_url_list):
            logger.debug("[JinaSum] URL在黑名单中")
            return False

        return True

    def _clean_content(self, content: str) -> str:
        """清洗内容，去除图片、链接、广告等无用信息

        Args:
            content: 原始内容

        Returns:
            str: 清洗后的内容
        """
        # 记录原始长度
        original_length = len(content)
        logger.debug(f"[JinaSum] Original content length: {original_length}")

        # 移除Markdown图片标签
        content = re.sub(r"!\[.*?\]\(.*?\)", "", content)
        content = re.sub(r"\[!\[.*?\]\(.*?\)", "", content)  # 嵌套图片标签

        # 移除图片描述 (通常在方括号或特定格式中)
        content = re.sub(r"\[图片\]|\[image\]|\[img\]|\[picture\]", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\[.*?图片.*?\]", "", content)

        # 移除阅读时间、字数等元数据
        content = re.sub(r"本文字数：\d+，阅读时长大约\d+分钟", "", content)
        content = re.sub(r"阅读时长[:：].*?分钟", "", content)
        content = re.sub(r"字数[:：]\d+", "", content)

        # 移除日期标记和时间戳
        content = re.sub(r"\d{4}[\.年/-]\d{1,2}[\.月/-]\d{1,2}[日号]?(\s+\d{1,2}:\d{1,2}(:\d{1,2})?)?", "", content)

        # 移除分隔线
        content = re.sub(r"\*\s*\*\s*\*", "", content)
        content = re.sub(r"-{3,}", "", content)
        content = re.sub(r"_{3,}", "", content)

        # 移除网页中常见的广告标记
        ad_patterns = [
            r"广告\s*[\.。]?",
            r"赞助内容",
            r"sponsored content",
            r"advertisement",
            r"promoted content",
            r"推广信息",
            r"\[广告\]",
            r"【广告】",
        ]
        for pattern in ad_patterns:
            content = re.sub(pattern, "", content, flags=re.IGNORECASE)

        # 移除URL链接和空的Markdown链接
        content = re.sub(r"https?://\S+", "", content)
        content = re.sub(r"www\.\S+", "", content)
        content = re.sub(r"\[\]\(.*?\)", "", content)  # 空链接引用 [](...)
        content = re.sub(r"\[.+?\]\(\s*\)", "", content)  # 有文本无链接 [text]()

        # 清理Markdown格式但保留文本内容
        content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)  # 移除加粗标记但保留内容
        content = re.sub(r"\*(.+?)\*", r"\1", content)  # 移除斜体标记但保留内容
        content = re.sub(r"`(.+?)`", r"\1", content)  # 移除代码标记但保留内容

        # 清理文章尾部的"微信编辑"和"推荐阅读"等无关内容
        content = re.sub(r"\*\*微信编辑\*\*.*?$", "", content, flags=re.MULTILINE)
        content = re.sub(r"\*\*推荐阅读\*\*.*?$", "", content, flags=re.MULTILINE | re.DOTALL)

        # 清理多余的空白字符
        content = re.sub(r"\n{3,}", "\n\n", content)  # 移除多余空行
        content = re.sub(r"\s{2,}", " ", content)  # 移除多余空格
        content = re.sub(r"^\s+", "", content, flags=re.MULTILINE)  # 移除行首空白
        content = re.sub(r"\s+$", "", content, flags=re.MULTILINE)  # 移除行尾空白

        # 记录清洗后长度
        cleaned_length = len(content)
        logger.debug(f"[JinaSum] Cleaned content length: {cleaned_length}, removed {original_length - cleaned_length} characters")

        return content
