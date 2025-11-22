import asyncio
import json
import os
import re
import shlex
import shutil
from typing import Optional
from urllib.parse import urljoin

import aiohttp

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.config import AstrBotConfig
from astrbot.core.utils.session_waiter import SessionController, session_waiter


class BiliDownloader(star.Star):
    """B站视频下载插件"""
    
    # 中文参数名到英文参数名的映射
    PARAM_MAPPING = {
        # 单个视频参数（大写）
        "<视频标题>": "<videoTitle>",
        "<BV号>": "<bvid>",
        "<AID>": "<aid>",
        "<CID>": "<cid>",
        "<清晰度>": "<dfn>",
        "<分辨率>": "<res>",
        "<帧率>": "<fps>",
        "<视频编码>": "<videoCodecs>",
        "<视频码率>": "<videoBandwidth>",
        "<音频编码>": "<audioCodecs>",
        "<音频码率>": "<audioBandwidth>",
        "<UP主名称>": "<ownerName>",
        "<UP主MID>": "<ownerMid>",
        "<发布时间>": "<publishDate>",
        "<API类型>": "<apiType>",
        # 分P视频额外参数（大写）
        "<分P序号>": "<pageNumber>",
        "<分P序号补零>": "<pageNumberWithZero>",
        "<分P标题>": "<pageTitle>",
        # 兼容小写
        "<bv号>": "<bvid>",
        "<aid>": "<aid>",
        "<cid>": "<cid>",
        "<up主名称>": "<ownerName>",
        "<up主mid>": "<ownerMid>",
        "<api类型>": "<apiType>",
        "<分p序号>": "<pageNumber>",
        "<分p序号补零>": "<pageNumberWithZero>",
        "<分p标题>": "<pageTitle>",
    }

    def __init__(self, context: star.Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context, config)
        
        # 获取配置：优先使用传入的config，否则从metadata获取
        if config:
            if isinstance(config, AstrBotConfig):
                self.config = dict(config)
            else:
                self.config = config
        else:
            # 从metadata获取配置
            plugin_metadata = self.context.get_registered_star("bilidownloader")
            if plugin_metadata and plugin_metadata.config:
                self.config = dict(plugin_metadata.config)
            else:
                # 使用默认配置
                self.config = {
                    "bbdown_path": "BBDown",
                    "download_path": "./downloads",
                    "cookie": "",
                    "classify_by_owner": True,
                    "default_options": {
                        "quality": "",
                        "download_danmaku": False,
                        "download_subtitle": True,
                    },
                    "naming": {
                        "single_video_pattern": "<视频标题>[<清晰度>]",
                        "multi_video_pattern": "<视频标题>/[P<分P序号补零>]<分P标题>[<清晰度>]",
                    }
                }
        
        # 初始化配置值
        self._update_config_values()
        
        # 确保下载目录存在
        os.makedirs(self.download_path, exist_ok=True)
        
        # 初始化权限配置（permissions在alist对象下）
        alist_config = self.config.get("alist", {})
        self.permissions = alist_config.get("permissions", {})
        self.open_groups = self.permissions.get("open_groups", [])
        # restricted_groups 可能是字符串（JSON格式）或字典
        restricted_groups_raw = self.permissions.get("restricted_groups", "{}")
        if isinstance(restricted_groups_raw, str):
            try:
                self.restricted_groups = json.loads(restricted_groups_raw) if restricted_groups_raw.strip() else {}
            except json.JSONDecodeError:
                logger.warning(f"restricted_groups JSON解析失败，使用空对象: {restricted_groups_raw}")
                self.restricted_groups = {}
        else:
            self.restricted_groups = restricted_groups_raw or {}

    async def initialize(self):
        """插件初始化时调用，重新加载配置"""
        # 重新从metadata获取配置（可能在WebUI中更新了）
        plugin_metadata = self.context.get_registered_star("bilidownloader")
        if plugin_metadata and plugin_metadata.config:
            self.config = dict(plugin_metadata.config)
            self._update_config_values()
            # 确保下载目录存在
            os.makedirs(self.download_path, exist_ok=True)
            # 重新加载权限配置（permissions在alist对象下）
            alist_config = self.config.get("alist", {})
            self.permissions = alist_config.get("permissions", {})
            self.open_groups = self.permissions.get("open_groups", [])
            # restricted_groups 可能是字符串（JSON格式）或字典
            restricted_groups_raw = self.permissions.get("restricted_groups", "{}")
            if isinstance(restricted_groups_raw, str):
                try:
                    self.restricted_groups = json.loads(restricted_groups_raw) if restricted_groups_raw.strip() else {}
                except json.JSONDecodeError:
                    logger.warning(f"restricted_groups JSON解析失败，使用空对象: {restricted_groups_raw}")
                    self.restricted_groups = {}
            else:
                self.restricted_groups = restricted_groups_raw or {}

    def _update_config_values(self):
        """更新配置值到实例变量"""
        self.bbdown_path = self.config.get("bbdown_path", "BBDown")
        self.download_path = self.config.get("download_path", "./downloads")

    def _parse_cookie(self, cookie_input: str) -> str:
        """解析不同格式的 cookie
        
        支持的格式：
        1. 浏览器格式: "name1=value1; name2=value2; name3=value3"
        2. Netscape 格式: "# Netscape HTTP Cookie File\n...\n.domain.com\tTRUE\t/\tFALSE\t1234567890\tname\tvalue"
        3. JSON 格式: '{"name1": "value1", "name2": "value2"}'
        4. 纯文本键值对: "name1=value1\nname2=value2"
        5. 已经是 BBDown 格式: "SESSDATA=xxx; DedeUserID=xxx"
        """
        cookie_input = cookie_input.strip()
        if not cookie_input:
            return ""
        
        # 如果已经是标准格式（包含 SESSDATA 等），直接返回
        if "SESSDATA=" in cookie_input or "DedeUserID=" in cookie_input:
            # 清理可能的换行和多余空格
            cookie_str = re.sub(r'\s+', ' ', cookie_input)
            cookie_str = cookie_str.replace('\n', ' ').replace('\r', ' ')
            return cookie_str.strip()
        
        # 尝试解析 Netscape 格式
        if cookie_input.startswith("#") or "\t" in cookie_input:
            cookies = {}
            for line in cookie_input.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name = parts[5]
                    value = parts[6]
                    cookies[name] = value
            return "; ".join([f"{k}={v}" for k, v in cookies.items()])
        
        # 尝试解析 JSON 格式
        if cookie_input.startswith("{") or cookie_input.startswith("["):
            try:
                cookie_obj = json.loads(cookie_input)
                if isinstance(cookie_obj, dict):
                    return "; ".join([f"{k}={v}" for k, v in cookie_obj.items()])
                elif isinstance(cookie_obj, list):
                    return "; ".join([f"{item.get('name', '')}={item.get('value', '')}" 
                                     for item in cookie_obj if isinstance(item, dict)])
            except json.JSONDecodeError:
                pass
        
        # 尝试解析纯文本键值对（多行）
        if "\n" in cookie_input:
            cookies = {}
            for line in cookie_input.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        cookies[parts[0].strip()] = parts[1].strip()
            if cookies:
                return "; ".join([f"{k}={v}" for k, v in cookies.items()])
        
        # 默认按浏览器格式处理（分号分隔）
        return cookie_input

    def _get_current_config(self) -> dict:
        """获取当前最新配置"""
        plugin_metadata = self.context.get_registered_star("bilidownloader")
        if plugin_metadata and plugin_metadata.config:
            return dict(plugin_metadata.config)
        return self.config

    def _convert_chinese_params(self, pattern: str) -> str:
        """将中文参数名转换为英文参数名
        
        Args:
            pattern: 包含中文或英文参数名的命名格式字符串
            
        Returns:
            转换后的命名格式字符串（英文参数名）
        """
        if not pattern:
            return pattern
        
        result = pattern
        # 按长度从长到短排序，避免短参数名被长参数名的一部分替换
        sorted_mapping = sorted(self.PARAM_MAPPING.items(), key=lambda x: len(x[0]), reverse=True)
        
        for chinese_param, english_param in sorted_mapping:
            result = result.replace(chinese_param, english_param)
        
        return result

    def _build_bbdown_command(self, url: str, cookie: Optional[str] = None, 
                              quality: Optional[str] = None, 
                              download_danmaku: bool = False,
                              download_subtitle: bool = True,
                              pages: Optional[str] = None) -> list:
        """构建 BBDown 命令"""
        # 获取最新配置
        current_config = self._get_current_config()
        current_bbdown_path = current_config.get("bbdown_path", "BBDown")
        current_download_path = current_config.get("download_path", "./downloads")
        
        cmd = [current_bbdown_path]
        
        # 添加 URL
        cmd.append(url)
        
        # 添加 Cookie
        cookie_to_use = cookie or current_config.get("cookie", "")
        if cookie_to_use:
            parsed_cookie = self._parse_cookie(cookie_to_use)
            if parsed_cookie:
                cmd.extend(["-c", parsed_cookie])
        
        # 添加清晰度
        quality_to_use = quality or current_config.get("default_options", {}).get("quality", "")
        if quality_to_use:
            cmd.extend(["-q", quality_to_use])
        
        # 添加弹幕下载
        if download_danmaku or current_config.get("default_options", {}).get("download_danmaku", False):
            cmd.append("--download-danmaku")
        
        # 添加字幕下载
        if download_subtitle and current_config.get("default_options", {}).get("download_subtitle", True):
            cmd.append("--download-subtitle")
        
        # 添加分P选择
        if pages:
            # pages可以是 "ALL", "1", "1-3", "1,2,3" 等格式
            if pages.upper() == "ALL":
                # BBDown默认下载全部，不需要添加参数
                pass
            else:
                cmd.extend(["-p", pages])
        
        # 添加文件命名格式（转换中文参数为英文）
        naming_config = current_config.get("naming", {})
        single_pattern = naming_config.get("single_video_pattern", "")
        multi_pattern = naming_config.get("multi_video_pattern", "")
        
        # 检查是否需要在命名格式中添加UP主文件夹分类
        classify_by_owner = current_config.get("classify_by_owner", True)
        
        if single_pattern:
            # 转换中文参数为英文参数
            single_pattern_en = self._convert_chinese_params(single_pattern)
            # 如果启用按UP主分类且格式中没有包含ownerName文件夹，则添加
            if classify_by_owner:
                # 检查格式中是否已经包含ownerName文件夹路径（以<ownerName>/开头或包含/<ownerName>/）
                # 注意：只检查文件夹路径，不检查文件名中的<ownerName>
                pattern_lower = single_pattern_en.lower()
                if not pattern_lower.startswith("<ownername>/") and "/<ownername>/" not in pattern_lower:
                    single_pattern_en = "<ownerName>/" + single_pattern_en
            cmd.extend(["--file-pattern", single_pattern_en])
        elif classify_by_owner:
            # 如果没有设置命名格式但启用了分类，使用默认格式
            cmd.extend(["--file-pattern", "<ownerName>/<videoTitle>[<dfn>]"])
        else:
            # 如果没有设置命名格式且不分类，使用简单格式
            cmd.extend(["--file-pattern", "<videoTitle>[<dfn>]"])
            
        if multi_pattern:
            # 转换中文参数为英文参数
            multi_pattern_en = self._convert_chinese_params(multi_pattern)
            # 如果启用按UP主分类且格式中没有包含ownerName文件夹，则添加
            if classify_by_owner:
                # 检查格式中是否已经包含ownerName文件夹路径
                pattern_lower = multi_pattern_en.lower()
                if not pattern_lower.startswith("<ownername>/") and "/<ownername>/" not in pattern_lower:
                    multi_pattern_en = "<ownerName>/" + multi_pattern_en
            cmd.extend(["--multi-file-pattern", multi_pattern_en])
        elif classify_by_owner:
            # 如果没有设置命名格式但启用了分类，使用默认格式
            cmd.extend(["--multi-file-pattern", "<ownerName>/<videoTitle>/[P<pageNumberWithZero>]<pageTitle>[<dfn>]"])
        else:
            # 如果没有设置命名格式且不分类，使用简单格式
            cmd.extend(["--multi-file-pattern", "<videoTitle>/[P<pageNumberWithZero>]<pageTitle>[<dfn>]"])
        
        # 添加下载路径（BBDown使用--work-dir参数）
        # 确保使用绝对路径，避免在不同平台下的路径解析问题
        abs_download_path = os.path.abspath(current_download_path)
        # 确保目录存在
        os.makedirs(abs_download_path, exist_ok=True)
        cmd.extend(["--work-dir", abs_download_path])
        
        logger.debug(f"BBDown下载路径: {abs_download_path}")
        
        return cmd

    def _extract_short_url(self, result: dict) -> Optional[str]:
        """从API响应中提取短链（支持多种响应格式）
        
        Args:
            result: API响应的JSON对象
        
        Returns:
            str: 短链，如果未找到则返回None
        """
        return (
            result.get("shorturl") or  # YOURLS格式
            result.get("short_url") or  # ShortLinks (FastAPI)格式
            result.get("url") or
            result.get("link") or
            result.get("data", {}).get("shorturl") or
            result.get("data", {}).get("short_url") or
            result.get("data", {}).get("url") or
            result.get("data", {}).get("shortUrl") or
            result.get("data", {}).get("link")
        )
    
    async def _shorten_url(self, url: str, shortener_config: Optional[dict] = None) -> Optional[str]:
        """将长链接转换为短链
        
        支持多种短链服务：
        - ShortLinks (FastAPI): 支持Header或Query参数认证
        - YOURLS: 需要API密钥，通过URL参数传递
        - Polr: 支持API密钥认证
        - Kutt: 支持API密钥认证
        - Shlink: 支持API密钥认证
        - 自定义服务: 根据配置灵活适配
        
        Args:
            url: 原始链接
            shortener_config: 短链服务配置
        
        Returns:
            str: 短链，如果转换失败则返回None（调用方会使用原链接）
        """
        if not shortener_config or not shortener_config.get("enabled", False):
            return None
        
        api_url = shortener_config.get("api_url", "")
        if not api_url:
            return None
        
        # 简单的URL格式验证
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            logger.warning(f"无效的URL格式，跳过短链转换: {url[:50]}")
            return None
        
        try:
            import aiohttp
            
            logger.debug(f"开始短链转换: API={api_url}, URL长度={len(url)}")
            
            # 构建请求头
            headers = {"Content-Type": "application/json"}
            
            # 获取API密钥和认证方式
            api_key = shortener_config.get("api_key", "")
            auth_method = shortener_config.get("auth_method", "header")  # header 或 query
            auth_header = shortener_config.get("auth_header", "X-API-Key")
            
            # 根据认证方式设置认证信息
            params = {}
            if api_key:
                if auth_method.lower() == "query":
                    # Query参数方式：添加到URL参数中
                    params["api_key"] = api_key
                    logger.debug(f"使用Query参数认证: api_key={api_key[:10]}...")
                else:
                    # Header方式（默认）：添加到请求头
                    headers[auth_header] = api_key
                    logger.debug(f"使用Header认证: {auth_header}={api_key[:10]}...")
            
            method = shortener_config.get("method", "POST").upper()
            logger.debug(f"请求方法: {method}")
            
            # 增加超时时间，避免Linux上网络延迟导致失败
            # total: 总超时时间（包括连接、发送、接收）
            # connect: 连接超时时间
            # 如果网络较慢，可以适当增加这些值
            timeout = aiohttp.ClientTimeout(total=15, connect=10)
            
            if method == "POST":
                # POST方式：请求体包含原始URL
                data_key = shortener_config.get("data_key", "url")
                data = {data_key: url}
                logger.debug(f"POST请求体: {data_key}={url[:100]}...")
                
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(api_url, json=data, headers=headers, params=params, timeout=timeout) as resp:
                            response_text = await resp.text()
                            logger.debug(f"短链API响应状态: {resp.status}, 响应长度: {len(response_text)}")
                            
                            if resp.status == 200:
                                try:
                                    result = await resp.json()
                                    logger.debug(f"短链API响应: {result}")
                                    short_url = self._extract_short_url(result)
                                    if short_url:
                                        logger.info(f"短链转换成功: {url[:50]}... -> {short_url}")
                                        return short_url
                                    else:
                                        logger.warning(f"短链API响应中未找到短链字段，响应内容: {result}")
                                except Exception as e:
                                    logger.warning(f"解析短链API响应失败: {e}, 响应文本: {response_text[:500]}")
                                    import traceback
                                    logger.debug(traceback.format_exc())
                            else:
                                logger.warning(f"短链API返回错误: HTTP {resp.status}, 响应: {response_text[:500]}")
                    except asyncio.TimeoutError:
                        logger.warning(f"短链API请求超时: {api_url}")
                    except aiohttp.ClientError as e:
                        logger.warning(f"短链API请求失败: {type(e).__name__}: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())
            else:
                # GET方式：URL作为参数
                params_key = shortener_config.get("params_key", "url")
                params[params_key] = url
                logger.debug(f"GET请求参数: {params_key}={url[:100]}...")
                
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.get(api_url, params=params, headers=headers, timeout=timeout) as resp:
                            response_text = await resp.text()
                            logger.debug(f"短链API响应状态: {resp.status}, 响应长度: {len(response_text)}")
                            
                            if resp.status == 200:
                                try:
                                    result = await resp.json()
                                    logger.debug(f"短链API响应: {result}")
                                    short_url = self._extract_short_url(result)
                                    if short_url:
                                        logger.info(f"短链转换成功: {url[:50]}... -> {short_url}")
                                        return short_url
                                    else:
                                        logger.warning(f"短链API响应中未找到短链字段，响应内容: {result}")
                                except Exception as e:
                                    logger.warning(f"解析短链API响应失败: {e}, 响应文本: {response_text[:500]}")
                                    import traceback
                                    logger.debug(traceback.format_exc())
                            else:
                                logger.warning(f"短链API返回错误: HTTP {resp.status}, 响应: {response_text[:500]}")
                    except aiohttp.ServerTimeoutError as e:
                        logger.warning(f"短链API服务器超时（总超时15秒）: {api_url}, 错误: {e}")
                        logger.warning(f"可能原因：网络延迟、服务器响应慢或网络连接问题")
                    except asyncio.TimeoutError:
                        logger.warning(f"短链API请求超时（总超时15秒）: {api_url}")
                        logger.warning(f"可能原因：网络延迟、服务器响应慢或网络连接问题")
                    except aiohttp.ClientConnectorError as e:
                        logger.warning(f"短链API连接失败: {api_url}, 错误: {e}")
                        logger.warning(f"可能原因：网络不通、DNS解析失败或服务器不可达")
                    except aiohttp.ClientError as e:
                        logger.warning(f"短链API请求失败: {type(e).__name__}: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())
        except Exception as e:
            logger.warning(f"短链转换失败: {type(e).__name__}: {e}")
            import traceback
            logger.debug(traceback.format_exc())
        
        return None
    
    async def _get_alist_download_link(self, base_url: str, file_path: str, password: str, local_file_path: Optional[str] = None) -> Optional[str]:
        """通过OpenList API获取文件的真实下载链接（使用密码方式）
        
        Args:
            base_url: OpenList访问地址
            file_path: 文件在OpenList中的路径（如 /bilibili/UP主名称/视频.mp4）
            password: 文件夹密码（必需）
            local_file_path: 本地文件路径（可选，用于检查文件是否存在）
        
        Returns:
            str: 文件的真实下载链接，失败返回None
        """
        try:
            # 如果提供了本地文件路径，先检查文件是否存在
            if local_file_path:
                if not os.path.exists(local_file_path):
                    logger.warning(f"[OpenList API] 本地文件不存在，跳过请求: {local_file_path}")
                    return None
                
                # 检查文件大小（如果文件大小为0，可能还在下载中）
                file_size = os.path.getsize(local_file_path)
                if file_size == 0:
                    logger.warning(f"[OpenList API] 文件大小为0，可能还在下载中，跳过请求: {local_file_path}")
                    return None
            
            api_url = f"{base_url}/api/fs/get"
            
            # 构建请求体（使用密码方式）
            data = {
                "path": file_path,
                "password": password
            }
            
            # 构建请求头（不使用token）
            headers = {"Content-Type": "application/json"}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    response_text = await resp.text()
                    
                    if resp.status != 200:
                        logger.error(f"OpenList API请求失败: HTTP {resp.status}")
                        logger.error(f"响应内容: {response_text}")
                        return None
                    
                    try:
                        result = await resp.json()
                    except Exception as e:
                        logger.error(f"解析API响应JSON失败: {e}")
                        logger.error(f"原始响应: {response_text}")
                        return None
                    
                    if result.get("code") != 200:
                        error_msg = result.get('message', '未知错误')
                        error_code = result.get('code', '未知')
                        logger.error(f"OpenList API返回错误: code={error_code}, message={error_msg}")
                        return None
                    
                    # 获取文件的真实下载链接
                    # 根据OpenList API文档，返回格式为: {"code": 200, "data": {"url": "直链"}}
                    file_data = result.get("data", {})
                    
                    if not file_data:
                        logger.error("API响应中没有data字段")
                        return None
                    
                    # 检查返回的是目录还是文件
                    if file_data.get("is_dir", False):
                        logger.error(f"返回的是目录而非文件: {file_path}")
                        return None
                    
                    # 根据API文档，获取data.url（这是API返回的直链）
                    direct_url = file_data.get("url") or file_data.get("raw_url")
                    if direct_url:
                        return direct_url
                    
                    logger.error("API响应中没有url字段")
                    return None
                        
        except Exception as e:
            logger.error(f"获取OpenList下载链接失败: {e}")
            return None
    
    async def _generate_alist_links_async(self, config: dict, video_title: str, page_info: list, selected_pages: Optional[str] = None) -> list:
        """生成OpenList下载链接（异步版本，使用OpenList API获取真实链接）
        
        工作原理：
        1. 扫描下载目录，通过文件名匹配找到对应的视频文件
        2. 根据配置的OpenList存储路径，构建文件在OpenList中的路径
        3. 如果有API Token，调用OpenList API获取真实的下载链接
        4. 如果没有API Token，使用路径拼接方式
        
        Args:
            config: 当前配置
            video_title: 视频标题（用于匹配文件）
            page_info: 分P信息列表（用于匹配文件）
            selected_pages: 用户选择的分P（用于匹配文件）
        
        Returns:
            list: 包含文件信息的列表，每个元素包含 name 和 url
        """
        alist_config = config.get("alist", {})
        if not alist_config.get("enabled", False):
            logger.debug("OpenList未启用")
            return []
        
        base_url = alist_config.get("base_url", "").rstrip("/")
        if not base_url:
            logger.warning("OpenList已启用但未配置base_url")
            return []
        
        # 获取文件夹密码（如果目录没有密码，可以留空）
        password = alist_config.get("password", "").strip()
        
        # 获取OpenList存储路径（例如：/bilibili）
        alist_storage_path = alist_config.get("alist_storage_path", "/bilibili").rstrip("/")
        if not alist_storage_path.startswith("/"):
            alist_storage_path = "/" + alist_storage_path
        
        download_path = config.get("download_path", "./downloads")
        
        try:
            import time
            # 获取下载目录的绝对路径
            abs_download_path = os.path.abspath(download_path)
            
            if not os.path.exists(abs_download_path):
                logger.warning(f"下载目录不存在: {abs_download_path}")
                return []
            
            # 查找视频文件（.mp4, .flv等）
            video_extensions = ['.mp4', '.flv', '.m4s', '.mkv']
            
            # 准备文件名匹配关键词
            match_keywords = []
            if video_title:
                # 提取视频标题的关键部分（去除特殊字符，用于匹配）
                title_clean = video_title.replace(" ", "").replace("-", "").replace("_", "")
                if len(title_clean) > 10:
                    match_keywords.append(title_clean[:20])  # 取前20个字符
                else:
                    match_keywords.append(title_clean)
            
            # 如果有分P信息，也加入匹配关键词
            if page_info:
                for page in page_info:
                    # 提取分P标题
                    if ":" in page:
                        page_title = page.split(":", 1)[1].strip()
                        if page_title:
                            page_title_clean = page_title.replace(" ", "").replace("-", "").replace("_", "")
                            if page_title_clean:
                                match_keywords.append(page_title_clean[:15])
            
            logger.info(f"文件名匹配关键词: {match_keywords}")
            
            # 递归查找所有视频文件
            def scan_directory(dir_path: str, base_dir: str) -> list:
                """递归扫描目录，通过文件名匹配找到对应的视频文件"""
                found_files = []
                try:
                    items = os.listdir(dir_path)
                    logger.debug(f"扫描目录 {dir_path}，找到 {len(items)} 个项目")
                    
                    for item in items:
                        item_path = os.path.join(dir_path, item)
                        
                        if os.path.isfile(item_path):
                            # 检查是否是视频文件
                            file_ext = os.path.splitext(item)[1].lower()
                            if file_ext in video_extensions:
                                # 通过文件名匹配
                                item_clean = item.replace(" ", "").replace("-", "").replace("_", "")
                                
                                # 如果有关键词，检查文件名是否包含关键词
                                # 如果没有关键词（可能是BBDown输出解析失败），则接受所有视频文件
                                if match_keywords:
                                    matched = any(keyword.lower() in item_clean.lower() for keyword in match_keywords if keyword)
                                    if not matched:
                                        logger.debug(f"文件名不匹配，跳过: {item}")
                                        continue
                                else:
                                    # 没有匹配关键词时，记录信息（可能是输出解析失败，但文件已下载）
                                    logger.info(f"没有匹配关键词，接受所有视频文件: {item}")
                                
                                logger.info(f"找到匹配的文件: {item}")
                                
                                # 计算相对于下载目录的路径
                                relative_path = os.path.relpath(item_path, base_dir)
                                # 转换为Alist路径格式（使用正斜杠）
                                alist_file_path = relative_path.replace("\\", "/")
                                # 组合完整的Alist路径
                                full_alist_path = f"{alist_storage_path}/{alist_file_path}"
                                
                                found_files.append({
                                    "name": item,
                                    "alist_path": full_alist_path,
                                    "path": item_path
                                })
                                logger.debug(f"  添加到列表: {item} -> {full_alist_path}")
                        elif os.path.isdir(item_path):
                            # 递归扫描子目录
                            logger.debug(f"进入子目录: {item_path}")
                            found_files.extend(scan_directory(item_path, base_dir))
                except Exception as e:
                    logger.error(f"扫描目录失败 {dir_path}: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                return found_files
            
            # 扫描下载目录
            logger.info(f"扫描下载目录: {abs_download_path}")
            
            files = scan_directory(abs_download_path, abs_download_path)
            
            if not files:
                logger.warning("未找到匹配的文件")
                return []
            
            # 最多处理10个文件
            files = files[:10]
            
            # 使用密码方式获取真实链接
            try:
                tasks = [self._get_alist_download_link(base_url, f["alist_path"], password, f["path"]) for f in files]
                links = await asyncio.gather(*tasks, return_exceptions=False)
            except Exception as e:
                logger.error(f"获取OpenList链接失败: {e}")
                import traceback
                logger.error(traceback.format_exc())
                links = [None] * len(files)
            
            # 组合结果（只包含成功获取链接的文件）
            result = []
            # 获取短链配置
            shortener_config = alist_config.get("shortener", {})
            
            # 准备需要转换的链接列表
            valid_links = []
            file_indices = []
            for i, file_info in enumerate(files):
                if i < len(links) and links[i] is not None:
                    valid_links.append(links[i])
                    file_indices.append(i)
            
            # 如果启用了短链服务，并行转换所有链接
            if shortener_config.get("enabled", False) and valid_links:
                logger.info(f"开始转换 {len(valid_links)} 个链接为短链...")
                try:
                    short_tasks = [self._shorten_url(url, shortener_config) for url in valid_links]
                    short_urls = await asyncio.gather(*short_tasks, return_exceptions=True)
                    # 记录转换结果
                    success_count = sum(1 for url in short_urls if url and not isinstance(url, Exception))
                    logger.info(f"短链转换完成: {success_count}/{len(valid_links)} 成功")
                    for i, (original, short) in enumerate(zip(valid_links, short_urls)):
                        if isinstance(short, Exception):
                            logger.warning(f"链接 {i+1} 转换失败: {short}")
                        elif not short:
                            logger.warning(f"链接 {i+1} 转换返回空: {original[:50]}...")
                except Exception as e:
                    logger.error(f"批量短链转换失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    short_urls = [None] * len(valid_links)
            else:
                short_urls = valid_links if not shortener_config.get("enabled", False) else [None] * len(valid_links)
            
            # 组合结果
            for idx, file_idx in enumerate(file_indices):
                file_info = files[file_idx]
                original_url = valid_links[idx]
                
                # 获取短链（如果转换失败或未启用，使用原链接）
                if shortener_config.get("enabled", False):
                    short_url = short_urls[idx] if not isinstance(short_urls[idx], Exception) and short_urls[idx] else original_url
                else:
                    short_url = original_url
                
                result.append({
                    "name": file_info["name"],
                    "url": short_url
                })
            
            logger.info(f"生成OpenList链接成功，共 {len(result)} 个")
            for link in result:
                logger.info(f"  - {link['name']}: {link['url']}")
            
            return result
            
        except Exception as e:
            logger.error(f"生成OpenList链接失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        return []
    
    def _decode_output(self, data: bytes) -> str:
        """尝试多种编码方式解码输出
        
        优先尝试GBK/GB2312（Windows中文系统常用），然后尝试UTF-8
        """
        if not data:
            return ""
        
        # 尝试的编码顺序：GBK -> GB2312 -> UTF-8 -> latin1（最后兜底）
        encodings = ["gbk", "gb2312", "utf-8", "latin1"]
        
        for encoding in encodings:
            try:
                decoded = data.decode(encoding, errors="strict")
                # 检查是否包含乱码（如果解码后大部分字符都是可打印的，认为成功）
                printable_ratio = sum(1 for c in decoded[:100] if c.isprintable() or c.isspace()) / min(len(decoded), 100)
                if printable_ratio > 0.7:  # 70%以上是可打印字符
                    return decoded
            except (UnicodeDecodeError, UnicodeError):
                continue
        
        # 如果所有编码都失败，使用errors="ignore"作为最后手段
        try:
            return data.decode("gbk", errors="ignore")
        except:
            return data.decode("utf-8", errors="ignore")

    async def _run_bbdown(self, cmd: list) -> tuple[int, str, str]:
        """运行 BBDown 命令"""
        try:
            # 检查BBDown是否存在
            bbdown_path = cmd[0] if cmd else "BBDown"
            if bbdown_path == "BBDown" or not os.path.isabs(bbdown_path):
                # 如果是相对路径或命令名，检查是否在PATH中
                import shutil
                if not shutil.which(bbdown_path):
                    error_msg = (
                        f"找不到BBDown可执行文件: {bbdown_path}\n"
                        f"请确保BBDown已安装并在PATH中，或使用 /bili-set bbdown_path <完整路径> 设置BBDown的完整路径\n"
                        f"例如: /bili-set bbdown_path /usr/local/bin/BBDown"
                    )
                    logger.error(error_msg)
                    return -1, "", error_msg
            
            logger.info(f"执行命令: {' '.join(shlex.quote(str(arg)) for arg in cmd)}")
            current_work_dir = os.getcwd()
            logger.debug(f"当前工作目录: {current_work_dir}")
            logger.debug(f"平台: {os.name}")  # 'nt' for Windows, 'posix' for Linux/Mac
            
            # 在Linux上，确保使用绝对路径，避免路径解析问题
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=current_work_dir,
                env=os.environ.copy()  # 显式传递环境变量，确保PATH等环境变量正确
            )
            stdout, stderr = await process.communicate()
            return_code = process.returncode or -1
            
            # 使用智能解码
            stdout_str = self._decode_output(stdout) if stdout else ""
            stderr_str = self._decode_output(stderr) if stderr else ""
            
            # 记录输出用于调试（限制长度）
            if stdout_str:
                logger.debug(f"BBDown stdout前1000字符: {stdout_str[:1000]}")
            if stderr_str:
                logger.debug(f"BBDown stderr前1000字符: {stderr_str[:1000]}")
            logger.debug(f"BBDown返回码: {return_code}")
            
            return return_code, stdout_str, stderr_str
        except FileNotFoundError as e:
            error_msg = (
                f"找不到BBDown可执行文件: {cmd[0] if cmd else 'BBDown'}\n"
                f"请确保BBDown已安装并在PATH中，或使用 /bili-set bbdown_path <完整路径> 设置BBDown的完整路径\n"
                f"例如: /bili-set bbdown_path /usr/local/bin/BBDown"
            )
            logger.error(error_msg)
            return -1, "", error_msg
        except Exception as e:
            logger.error(f"执行 BBDown 命令失败: {e}")
            return -1, "", str(e)

    @filter.command("bili-help", alias={"bilibili-help", "b站帮助", "B站帮助", "bili帮助"})
    async def show_help(self, event: AstrMessageEvent):
        """显示所有可用命令和帮助信息"""
        help_msg = """📚 B站视频下载器 - 命令帮助

【下载相关】
/bili <视频URL>
  下载B站视频
  示例: /bili https://www.bilibili.com/video/BV1qt4y1X7TW
  示例: /bili https://b23.tv/uKe83H7
  示例: /bili BV1qt4y1X7TW
  示例: /bili 【标题-哔哩哔哩】 https://b23.tv/xxx
  支持完整链接、短链（b23.tv）、BV号和移动端分享格式
  别名: /bilibili, /b站, /B站

【配置相关】
/bili-set <配置项> <值>
  设置插件配置
  配置项: bbdown_path, download_path, quality, danmaku, subtitle, single_pattern, multi_pattern
  示例: /bili-set download_path ./videos
  示例: /bili-set quality 1080P
  别名: /bilibili-set, /b站设置, /B站设置

/bili-config
  查看当前配置
  别名: /bilibili-config, /b站配置, /B站配置

【Cookie相关】
/bili-cookie <cookie字符串>
  设置B站Cookie
  支持多种格式：浏览器格式、Netscape格式、JSON格式、纯文本格式
  示例: /bili-cookie SESSDATA=xxx; DedeUserID=xxx
  别名: /bilibili-cookie, /b站cookie, /B站cookie

/bili-test-cookie [cookie字符串]
  测试Cookie是否有效
  不提供参数则测试当前配置的Cookie
  别名: /bilibili-test-cookie, /b站测试cookie, /B站测试cookie, /测试cookie

【命名格式相关】
/bili-naming
  查看文件命名格式可用参数
  别名: /bilibili-naming, /b站命名, /B站命名

【帮助】
/bili-help
  显示此帮助信息
  别名: /bilibili-help, /b站帮助, /B站帮助, /bili帮助

💡 提示：
- 使用 /bili-set 查看详细的配置项说明
- 使用 /bili-naming 查看命名格式参数列表
- 也可以在WebUI的插件配置页面进行设置
"""
        yield event.plain_result(help_msg)

    def _extract_url_from_text(self, text: str) -> Optional[str]:
        """从文本中提取B站URL
        
        支持从以下格式提取：
        - 【标题-哔哩哔哩】 https://b23.tv/xxx
        - 直接的URL
        - BV号
        
        Args:
            text: 可能包含URL的文本
            
        Returns:
            提取到的URL，如果没找到返回None
        """
        if not text:
            return None
        
        # 先去除首尾空白
        text = text.strip()
        
        # 1. 尝试匹配完整的HTTP/HTTPS链接（包括b23.tv短链和bilibili.com）
        # 匹配URL中常见的字符：字母、数字、-、_、/、?、=、&、%、#、.等
        url_pattern = r'https?://(?:b23\.tv|(?:www\.)?bilibili\.com)/[a-zA-Z0-9_/?=&%#.-]+'
        url_match = re.search(url_pattern, text)
        if url_match:
            return url_match.group(0)
        
        # 2. 尝试匹配BV号
        bv_pattern = r'BV[a-zA-Z0-9]+'
        bv_match = re.search(bv_pattern, text)
        if bv_match:
            return bv_match.group(0)
        
        # 3. 如果都没匹配到，检查是否是纯URL或BV号（没有其他字符）
        # 如果包含中文或特殊字符，返回None而不是原文本
        if len(text) < 100 and not any('\u4e00' <= c <= '\u9fff' for c in text):
            # 可能是纯URL或BV号，返回原文本
            return text
        else:
            # 包含中文或太长，肯定不是纯URL，返回None
            return None
    
    async def _resolve_b23_shortlink(self, url: str) -> Optional[str]:
        """解析B站短链（b23.tv）获取真实URL
        
        Args:
            url: B站短链URL（如 https://b23.tv/xxx）
            
        Returns:
            真实URL，如果解析失败返回None
        """
        if not url or "b23.tv" not in url:
            return None
        
        # 确保URL格式正确
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        
        try:
            async with aiohttp.ClientSession() as session:
                # 先尝试不跟随重定向，获取Location头
                async with session.get(
                    url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10, connect=5),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": "https://www.bilibili.com/"
                    }
                ) as resp:
                    # 检查重定向
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if location:
                            # 处理相对路径
                            if location.startswith("/"):
                                location = urljoin(url, location)
                            logger.debug(f"B站短链解析成功（重定向）: {url} -> {location}")
                            return location
                
                # 如果没有重定向头，尝试跟随重定向获取最终URL
                async with session.get(
                    url,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=10, connect=5),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": "https://www.bilibili.com/"
                    }
                ) as resp:
                    if resp.status == 200:
                        final_url = str(resp.url)
                        # 确保最终URL是B站链接
                        if "bilibili.com" in final_url:
                            logger.debug(f"B站短链解析成功（跟随重定向）: {url} -> {final_url}")
                            return final_url
                        else:
                            logger.warning(f"B站短链解析结果不是B站链接: {url} -> {final_url}")
        except asyncio.TimeoutError:
            logger.warning(f"B站短链解析超时: {url}")
        except aiohttp.ClientError as e:
            logger.warning(f"B站短链解析网络错误: {url}, 错误: {e}")
        except Exception as e:
            logger.warning(f"B站短链解析失败: {url}, 错误: {e}")
        
        return None
    
    def _extract_bv_from_url(self, url: str) -> Optional[str]:
        """从URL中提取BV号"""
        # 匹配BV号格式
        bv_match = re.search(r'BV[a-zA-Z0-9]+', url)
        if bv_match:
            return bv_match.group(0)
        # 如果URL中没有BV号（可能是动态链接如 t.bilibili.com），返回None
        # BBDown应该能处理这种链接，所以这里返回None是可以的
        return None
    
    async def _get_video_info_from_api(self, url: str) -> tuple[bool, str, list]:
        """通过B站API获取视频信息
        
        Returns:
            tuple: (是否成功, 视频标题, 分P列表)
        """
        try:
            # 提取BV号
            bv = self._extract_bv_from_url(url)
            if not bv:
                # 如果URL中没有BV号（可能是动态链接如 t.bilibili.com），
                # 直接返回失败，让BBDown来处理（BBDown支持这种链接）
                logger.info(f"URL中未包含BV号（可能是动态链接），将直接使用BBDown下载: {url}")
                return False, "", []
            
            # B站API：获取视频信息
            # 使用web接口，不需要登录
            api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
            
            # 添加请求头模拟浏览器，避免被反爬虫拦截
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.error(f"API请求失败: {resp.status}")
                        return False, "", []
                    
                    data = await resp.json()
                    
                    if data.get("code") != 0:
                        logger.error(f"API返回错误: {data.get('message', '未知错误')}")
                        return False, "", []
                    
                    video_data = data.get("data", {})
                    if not video_data:
                        logger.error("API返回数据为空")
                        return False, "", []
                    
                    # 提取视频标题
                    video_title = video_data.get("title", "")
                    
                    # 提取分P信息
                    pages = []
                    pages_data = video_data.get("pages", [])
                    
                    for page in pages_data:
                        pages.append({
                            "number": page.get("page", 0),
                            "cid": str(page.get("cid", "")),
                            "title": page.get("part", "")
                        })
                    
                    # 按分P序号排序
                    pages.sort(key=lambda x: x["number"])
                    
                    logger.info(f"通过API获取视频信息: 标题={video_title}, 分P数量={len(pages)}")
                    
                    return True, video_title, pages
                    
        except asyncio.TimeoutError:
            logger.error("获取视频信息超时")
            return False, "", []
        except Exception as e:
            logger.error(f"通过API获取视频信息失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False, "", []
    
    async def _get_video_info(self, url: str) -> tuple[bool, str, list]:
        """获取视频信息（优先使用B站API）
        
        Returns:
            tuple: (是否成功, 视频标题, 分P列表)
        """
        # 优先使用B站API获取信息（快速且可靠）
        success, title, pages = await self._get_video_info_from_api(url)
        
        if success and title:
            return success, title, pages
        
        # 如果API失败，返回失败（不再尝试BBDown，因为BBDown获取信息也会超时）
        logger.warning("API获取视频信息失败")
        return False, "", []

    def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str | None]:
        """检查用户是否有权限使用命令
        
        权限规则：
        1. 私聊：只有管理员可以使用，非管理员静默忽略（不回复）
        2. 群聊：
           - 只有在开放群组列表中的群组才能使用
           - 如果在受限群组配置中，只有配置的QQ号才能使用
           - 如果不在任何列表中，静默忽略（不回复）
           - 如果在受限群组配置中但用户不在允许列表中，静默忽略（不回复）
        
        Returns:
            tuple: (是否有权限, 错误消息)
            - (True, ""): 有权限，继续执行
            - (False, None): 没有权限，静默忽略（不回复）
        """
        # 获取群ID和用户ID
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        
        # 如果是私聊，只有管理员可以使用
        if not group_id:
            if event.is_admin():
                return True, ""
            else:
                # 私聊非管理员，静默忽略（不回复）
                return False, None
        
        # 转换为字符串进行比较
        group_id_str = str(group_id).strip()
        sender_id_str = str(sender_id).strip()
        
        # 调试日志
        logger.debug(f"权限检查: 群组ID={group_id_str}, 用户ID={sender_id_str}, 开放群组列表={self.open_groups}, 受限群组={self.restricted_groups}")
        
        # 检查是否在开放群组列表中
        if group_id_str in self.open_groups:
            # 检查是否在受限群组配置中（受限群组的优先级更高）
            if group_id_str in self.restricted_groups:
                allowed_users = self.restricted_groups[group_id_str]
                # 确保是列表格式
                if isinstance(allowed_users, list):
                    if sender_id_str in allowed_users:
                        return True, ""
                    else:
                        # 群组已配置，但用户没权限，静默忽略（不回复）
                        return False, None
                else:
                    # 如果不是列表格式，记录错误但允许使用（容错处理）
                    logger.warning(f"受限群组 {group_id_str} 的配置格式错误，应为列表")
                    return True, ""
            # 如果在开放群组列表中且不在受限群组配置中，所有人可用
            return True, ""
        
        # 如果不在开放群组列表中，静默忽略（不回复）
        return False, None
    
    @filter.command("bili", alias={"bilibili", "b站", "B站"})
    async def download_video(self, event: AstrMessageEvent):
        """下载B站视频
        
        用法: /bili <视频URL>
        示例: /bili https://www.bilibili.com/video/BV1qt4y1X7TW
        
        如果视频有多个分P，会提示选择下载全部或指定分P
        """
        # 检查权限
        has_permission, error_msg = self._check_permission(event)
        if not has_permission:
            # 如果error_msg为None，表示群组未配置，静默忽略（不回复）
            # 如果error_msg不为None，表示有配置但用户没权限，需要回复错误消息
            if error_msg:
                yield event.plain_result(error_msg)
            return
        
        # 从完整消息中提取URL（去除命令前缀）
        # 注意：event.message_str 已经去掉了斜杠，所以是 "bili xxx" 而不是 "/bili xxx"
        message = event.message_str.strip()
        
        # 移除命令前缀（bili, bilibili, b站, B站）
        url = ""
        for prefix in ["bili ", "bilibili ", "b站 ", "B站 "]:
            if message.startswith(prefix):
                url = message[len(prefix):].strip()
                break
        
        # 如果没有匹配到带空格的前缀，检查是否只有命令本身
        if not url:
            for prefix in ["bili", "bilibili", "b站", "B站"]:
                if message == prefix:
                    url = ""
                    break
        
        if not url:
            help_msg = """📚 B站视频下载器

用法: /bili <视频URL>

示例:
/bili https://www.bilibili.com/video/BV1qt4y1X7TW
/bili https://b23.tv/uKe83H7
/bili BV1qt4y1X7TW
/bili 【标题-哔哩哔哩】 https://b23.tv/xxx

💡 提示:
- 支持B站视频链接、短链（b23.tv）和BV号
- 支持直接粘贴移动端分享的内容
- 如果视频有多个分P，会提示选择下载
- 使用 /bili-help 查看完整帮助"""
            yield event.plain_result(help_msg)
            return
        
        # 从文本中提取URL（支持从移动端分享的内容中提取）
        extracted_url = self._extract_url_from_text(url)
        
        if extracted_url is None:
            # 提取失败，返回错误
            yield event.plain_result("❌ 无法从输入中提取有效的B站视频链接\n\n请使用以下格式之一：\n- https://www.bilibili.com/video/BV...\n- https://b23.tv/...\n- BV号")
            return
        elif extracted_url != url:
            logger.info(f"从文本中提取URL: {extracted_url}")
            url = extracted_url
        
        # 如果是B站短链（b23.tv），先解析获取真实URL
        if "b23.tv" in url:
            yield event.plain_result("正在解析短链...")
            resolved_url = await self._resolve_b23_shortlink(url)
            if resolved_url:
                logger.info(f"短链解析成功: {url} -> {resolved_url}")
                url = resolved_url
            else:
                yield event.plain_result("❌ 无法解析B站短链，请使用完整链接或BV号")
                return
        
        # 验证 URL
        if "bilibili.com" not in url and "BV" not in url and not url.startswith("BV"):
            yield event.plain_result("无效的B站视频URL")
            return
        
        yield event.plain_result("正在获取视频信息...")
        
        # 获取视频信息
        success, video_title, pages = await self._get_video_info(url)
        
        logger.info(f"获取视频信息结果: success={success}, title={video_title}, pages_count={len(pages)}")
        
        # 如果获取信息失败或没有标题，直接尝试下载
        if not success or not video_title:
            logger.warning("获取视频信息失败或标题为空，直接下载")
            yield event.plain_result("开始下载，请稍候...")
            current_config = self._get_current_config()
            classify_by_owner = current_config.get("classify_by_owner", True)
            cmd = self._build_bbdown_command(url)
            return_code, stdout, stderr = await self._run_bbdown(cmd)
        else:
            # 如果有多个分P，让用户选择
            if len(pages) > 1:
                logger.info(f"检测到 {len(pages)} 个分P，等待用户选择")
                # 显示分P列表
                pages_msg = f"📹 {video_title}\n\n发现 {len(pages)} 个分P：\n"
                for page in pages:
                    pages_msg += f"  P{page['number']}: {page['title']}\n"
                pages_msg += "\n请选择：\n"
                pages_msg += "  • 输入 'all' 或 '全部' - 下载全部分P\n"
                pages_msg += "  • 输入数字（如 1, 2, 3） - 下载指定分P\n"
                pages_msg += "  • 输入范围（如 1-3） - 下载指定范围的分P\n"
                pages_msg += "  • 输入多个数字（如 1,3,5） - 下载多个指定分P\n"
                pages_msg += "\n💡 30秒内未选择将自动下载全部"
                
                yield event.plain_result(pages_msg)
                
                # 等待用户选择
                @session_waiter(timeout=30)  # type: ignore
                async def wait_page_selection(controller: SessionController, user_event: AstrMessageEvent):
                    # 从事件中提取用户输入（使用message_str属性）
                    user_input = user_event.message_str.strip().lower()
                    
                    # 解析用户输入
                    selected_pages = None
                    
                    if user_input in ["all", "全部", "a", ""]:
                        selected_pages = "ALL"
                    elif "-" in user_input:
                        # 范围选择，如 1-3
                        try:
                            start, end = user_input.split("-", 1)
                            start = int(start.strip())
                            end = int(end.strip())
                            selected_pages = f"{start}-{end}"
                        except:
                            selected_pages = "ALL"
                    elif "," in user_input:
                        # 多个选择，如 1,2,3
                        try:
                            page_nums = [int(p.strip()) for p in user_input.split(",")]
                            selected_pages = ",".join(map(str, page_nums))
                        except:
                            selected_pages = "ALL"
                    else:
                        # 单个数字
                        try:
                            page_num = int(user_input)
                            if 1 <= page_num <= len(pages):
                                selected_pages = str(page_num)
                            else:
                                selected_pages = "ALL"
                        except:
                            selected_pages = "ALL"
                    
                    # 停止会话并返回结果
                    # 直接设置future的结果值，而不是调用stop()（stop()会设置None）
                    if not controller.future.done():
                        controller.future.set_result(selected_pages)
                    return selected_pages
                
                try:
                    # session_waiter装饰的函数调用时只需要传入event，装饰器会自动处理
                    selected_pages = await wait_page_selection(event)
                    logger.info(f"用户选择的分P: {selected_pages}")
                    if selected_pages is None:
                        selected_pages = "ALL"
                except Exception as e:
                    logger.warning(f"等待用户选择超时或出错: {e}")
                    selected_pages = "ALL"
                
                # 构建下载命令
                yield event.plain_result("开始下载，请稍候...")
                
                current_config = self._get_current_config()
                classify_by_owner = current_config.get("classify_by_owner", True)
                cmd = self._build_bbdown_command(url, pages=selected_pages)
                return_code, stdout, stderr = await self._run_bbdown(cmd)
                
                # 保存用户选择的分P信息到实例变量，用于后续输出
                self._last_selected_pages = selected_pages
            else:
                # 单个视频或单个分P，直接下载
                yield event.plain_result("开始下载，请稍候...")
                
                current_config = self._get_current_config()
                classify_by_owner = current_config.get("classify_by_owner", True)
                cmd = self._build_bbdown_command(url)
                return_code, stdout, stderr = await self._run_bbdown(cmd)
        
        # 合并输出用于分析
        output_combined = (stdout + "\n" + stderr).lower()
        output_original = stdout + "\n" + stderr
        
        # 记录BBDown输出用于调试
        logger.debug(f"BBDown返回码: {return_code}")
        logger.debug(f"BBDown stdout前500字符: {stdout[:500]}")
        if stderr:
            logger.debug(f"BBDown stderr前500字符: {stderr[:500]}")
        
        # 检查是否有明显的错误信息（更严格的错误判断，只检查真正的错误）
        error_keywords = [
            "unrecognized command", "unrecognized argument", 
            "command not found", "不是内部或外部命令",
            "error:", "failed to", "无法", "不能", "不支持"
        ]
        has_error_keyword = any(keyword.lower() in output_combined for keyword in error_keywords)
        
        # 检查是否有成功标志：视频信息、分P信息、UP主信息等（更宽松的判断）
        # 只要输出中有这些关键词，就认为BBDown至少成功解析了视频信息
        success_indicators = [
            "aid:", "cid:",  # 视频ID（关键标志）
            "视频:", "视频标题:", "title:",  # 视频标题
            "up主", "up主:", "owner", "space.bilibili.com",  # UP主信息
            "p1:", "p2:", "p3:", "分p", "page",  # 分P信息
            "获取aid", "获取视频", "视频信息",  # 获取信息的过程
            "下载", "保存", "saved", "completed", "完成", "成功",  # 完成标志
            "version", "bbdown", "bilibili downloader"  # BBDown版本信息（说明程序运行了）
        ]
        has_success_indicator = any(indicator.lower() in output_combined for indicator in success_indicators)
        
        # 检查是否有文件保存的路径信息
        has_file_path = any(keyword in output_original for keyword in [".mp4", ".flv", ".m4s", "保存至", "saved to", "文件"])
        
        # 检查是否有BBDown版本信息（说明程序至少启动了）
        has_bbdown_info = "bbdown version" in output_combined or "bilibili downloader" in output_combined
        
        # 检查是否有实际下载完成的标志（关键：检查是否有文件保存路径）
        has_download_complete = any(keyword in output_original.lower() for keyword in [
            "保存至", "saved to", "文件已保存", "下载完成", "download completed",
            "文件:", "file:", ".mp4", ".flv", ".m4s", ".mkv"
        ])
        
        # 判断是否成功：
        # 1. 返回码为0（标准成功）- 这是最可靠的判断，如果返回0通常表示成功
        # 2. 有成功标志且没有错误（有视频信息且没有明显错误）
        # 3. 有BBDown版本信息且有视频信息，且没有错误（说明程序运行并获取了信息）
        # 4. 有文件路径信息且没有错误
        # 5. 有下载完成标志（关键：必须有实际文件保存的迹象）
        # 注意：返回码为0是最可靠的判断，即使输出中没有特定关键词，也应该认为成功
        is_success = (
            return_code == 0 or  # 返回码为0是最可靠的判断
            (has_success_indicator and has_download_complete and not has_error_keyword) or
            (has_file_path and not has_error_keyword) or
            (has_bbdown_info and has_success_indicator and has_download_complete and not has_error_keyword)
        )
        
        # 如果没有下载完成的标志但返回码为0，记录信息（不是警告，因为返回码0通常表示成功）
        if not has_download_complete and return_code == 0:
            logger.info("BBDown返回码为0，但输出中未检测到文件保存关键词，将检查下载目录中的实际文件")
        
        if is_success:
            # 提取下载信息
            result_msg = "✅ 下载完成！\n"
            result_msg += "─" * 30 + "\n"
            
            # 提取关键信息：视频标题和分P信息
            video_title = ""
            page_info = []
            
            # 合并stdout和stderr来提取信息
            all_output = stdout + "\n" + stderr if stderr else stdout
            
            if all_output:
                lines = all_output.split("\n")
                for line in lines:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    
                    # 提取视频标题（格式：视频标题: xxx）
                    if "视频标题:" in line_stripped:
                        title_part = line_stripped.split("视频标题:", 1)[-1].strip()
                        if title_part:
                            video_title = title_part
                            continue
                    
                    # 提取分P信息（格式：P1: [cid] [标题] [时长]）
                    # 匹配格式: P1: [34047132747] [Mr.Taxi] [01m08s]
                    page_match = re.search(r'P(\d+):\s*\[([^\]]+)\]\s*\[([^\]]+)\]', line_stripped)
                    if page_match:
                        page_num = page_match.group(1)
                        page_title = page_match.group(3)  # 第三个方括号中是标题
                        page_info.append(f"P{page_num}: {page_title}")
            
            # 构建结果消息（只显示标题和实际下载的分P列表）
            if video_title:
                result_msg += f"📹 {video_title}\n"
            
            if page_info:
                # 如果用户选择了特定分P，只显示选中的分P
                # 检查是否有选中的分P信息（从外部作用域获取）
                selected_pages_info = getattr(self, '_last_selected_pages', None)
                if selected_pages_info and selected_pages_info.upper() != "ALL":
                    # 解析选中的分P
                    selected_pages_list = []
                    if "," in selected_pages_info:
                        # 多个分P，如 "1,2,3"
                        selected_pages_list = [int(p.strip()) for p in selected_pages_info.split(",")]
                    elif "-" in selected_pages_info:
                        # 范围，如 "1-3"
                        start, end = selected_pages_info.split("-", 1)
                        selected_pages_list = list(range(int(start.strip()), int(end.strip()) + 1))
                    else:
                        # 单个分P，如 "1"
                        try:
                            selected_pages_list = [int(selected_pages_info)]
                        except:
                            selected_pages_list = []
                    
                    # 只显示选中的分P
                    filtered_pages = [p for p in page_info if any(f"P{num}:" in p for num in selected_pages_list)]
                    if filtered_pages:
                        # 如果只有一个分P，简化显示
                        if len(filtered_pages) == 1:
                            result_msg += f"📌 {filtered_pages[0]}\n"
                        else:
                            result_msg += "📌 已下载分P：\n"
                            for page in filtered_pages:
                                result_msg += f"   • {page}\n"
                    else:
                        # 如果只有一个分P，简化显示
                        if len(page_info) == 1:
                            result_msg += f"📌 {page_info[0]}\n"
                        else:
                            result_msg += "📌 分P列表：\n"
                            for page in page_info:
                                result_msg += f"   • {page}\n"
                else:
                    # 下载全部，显示所有分P
                    # 如果只有一个分P，简化显示
                    if len(page_info) == 1:
                        result_msg += f"📌 {page_info[0]}\n"
                    else:
                        result_msg += "📌 分P列表：\n"
                        for page in page_info:
                            result_msg += f"   • {page}\n"
            elif not video_title:
                # 如果没有提取到信息，显示默认消息
                result_msg += "下载完成"
            
            # 生成OpenList下载链接（等待文件完全写入并稳定）
            import asyncio
            
            # 等待文件写入完成：检查文件大小是否稳定
            logger.info("等待文件写入完成...")
            await asyncio.sleep(2)  # 先等待2秒
            
            # 检查文件是否还在写入（文件大小是否稳定）
            download_path = current_config.get("download_path", "./downloads")
            abs_download_path = os.path.abspath(download_path)
            
            if os.path.exists(abs_download_path):
                # 检查文件大小是否稳定（连续3次检查，每次间隔1秒，大小不变）
                max_retries = 5
                stable_count = 0
                last_sizes = {}
                
                for retry in range(max_retries):
                    await asyncio.sleep(1)
                    # 扫描视频文件
                    video_extensions = ['.mp4', '.flv', '.m4s', '.mkv']
                    current_sizes = {}
                    
                    for root, dirs, files in os.walk(abs_download_path):
                        for file in files:
                            if any(file.lower().endswith(ext) for ext in video_extensions):
                                file_path = os.path.join(root, file)
                                try:
                                    file_size = os.path.getsize(file_path)
                                    # 检查所有视频文件的大小
                                    if file_size > 0:  # 只检查大小大于0的文件
                                        current_sizes[file_path] = file_size
                                except:
                                    pass
                    
                    # 检查文件大小是否稳定
                    if last_sizes:
                        all_stable = True
                        for file_path, current_size in current_sizes.items():
                            if file_path in last_sizes:
                                if current_size != last_sizes[file_path]:
                                    all_stable = False
                                    break
                        
                        if all_stable:
                            stable_count += 1
                            if stable_count >= 2:  # 连续2次大小不变，认为文件已稳定
                                logger.info(f"文件大小已稳定（检查了{retry+1}次）")
                                break
                    else:
                        stable_count += 1
                        if stable_count >= 2:
                            break
                    
                    last_sizes = current_sizes
                
                if stable_count < 2:
                    logger.warning("文件可能还在写入中，但将继续尝试生成链接")
            
            selected_pages_info = getattr(self, '_last_selected_pages', None)
            # 使用异步方式生成链接（因为需要调用OpenList API）
            # 通过文件名匹配找到对应的文件
            alist_links = await self._generate_alist_links_async(
                current_config, video_title, page_info, selected_pages_info
            )
            if alist_links:
                result_msg += "─" * 30 + "\n"
                result_msg += "📥 下载链接\n"
                result_msg += "─" * 30 + "\n"
                for i, link_info in enumerate(alist_links, 1):
                    # 如果只有一个链接，简化显示
                    if len(alist_links) == 1:
                        result_msg += f"🔗 {link_info['url']}\n"
                    else:
                        result_msg += f"【{i}】{link_info['name']}\n"
                        result_msg += f"   🔗 {link_info['url']}\n"
            else:
                logger.warning("未生成OpenList链接，可能原因：文件未找到或文件名不匹配")
            
            yield event.plain_result(result_msg.strip())
        else:
            # 失败情况
            error_msg = f"❌ 下载失败"
            if return_code != 0:
                error_msg += f" (返回码: {return_code})"
            error_msg += "\n\n"
            
            # 合并输出用于错误分析
            all_output = (stdout + "\n" + stderr).lower() if stderr else stdout.lower()
            all_output_original = stdout + "\n" + stderr if stderr else stdout
            
            # 检查是否有视频信息（用于判断是否成功获取到视频）
            # 更严格的判断：至少要有2个关键信息才认为获取到了视频
            video_info_keywords = ["aid:", "cid:", "视频标题:", "title:", "up主", "owner", "bvid:"]
            video_info_count = sum(1 for keyword in video_info_keywords if keyword in all_output)
            has_video_info = video_info_count >= 2
            
            # 检查是否是BBDown未找到的错误（最高优先级）
            if stderr and ("No such file or directory" in stderr or "找不到" in stderr or "command not found" in stderr.lower()):
                error_msg += "⚠️ BBDown未找到或无法执行\n\n"
                error_msg += "解决方案：\n"
                error_msg += "1. 确保BBDown已正确安装\n"
                error_msg += "2. 如果BBDown不在PATH中，请使用以下命令设置完整路径：\n"
                error_msg += "   /bili-set bbdown_path <BBDown的完整路径>\n"
                error_msg += "   例如: /bili-set bbdown_path /usr/local/bin/BBDown\n"
                error_msg += "   或: /bili-set bbdown_path /home/user/BBDown/BBDown\n\n"
            # 优先检查：如果返回码非0且没有视频信息，很可能是视频不存在（高优先级）
            elif return_code != 0 and not has_video_info:
                # 直接判断为视频不存在，简洁明了
                error_msg += "⚠️ 视频不存在或已被删除\n\n"
                error_msg += "💡 建议：\n"
                error_msg += "- 在浏览器中打开链接确认视频是否可访问\n"
                error_msg += "- 如果视频确实存在，可能需要登录，请使用 /bili-test-cookie 检查Cookie状态\n\n"
            # 检查输出中是否有明确的"视频不存在"相关错误
            elif any(keyword in all_output_original for keyword in [
                "视频不存在", "视频已删除", "视频已下架", "视频不可用", "视频无效",
                "not found", "不存在", "无法访问", "访问失败", "获取失败",
                "视频信息获取失败", "获取视频信息失败", "解析失败", "解析错误",
                "invalid video", "invalid url", "无效的视频", "无效的链接"
            ]):
                error_msg += "⚠️ 视频不存在或已被删除\n\n"
                error_msg += "💡 建议：\n"
                error_msg += "- 在浏览器中打开链接确认视频是否可访问\n"
                error_msg += "- 如果视频确实存在，可能需要登录，请使用 /bili-test-cookie 检查Cookie状态\n\n"
            # 检查是否是Cookie相关的错误（较低优先级）
            # 注意：只有当有完整视频信息（至少2个关键字段）但Cookie有明确问题时，才判断为Cookie错误
            # "检测账号登录"只是BBDown的常规输出，不代表Cookie有问题
            # 只有明确的错误信息（如"登录失败""cookie失效"）才判断为Cookie问题
            elif has_video_info and any(keyword in all_output for keyword in [
                "cookie失效", "cookie无效", "登录失败", "登录错误", "未登录",
                "需要登录", "请先登录", "认证失败", "unauthorized", "未授权",
                "账号异常", "账户异常", "登录状态失效"
            ]):
                error_msg += "⚠️ Cookie失效或需要登录\n\n"
                error_msg += "💡 建议：\n"
                error_msg += "- 使用 /bili-test-cookie 检查Cookie是否有效\n"
                error_msg += "- 如果Cookie失效，请使用 /bili-cookie 重新设置\n\n"
            # 其他未明确分类的错误（兜底处理）
            else:
                error_msg += "⚠️ 下载失败\n\n"
                error_msg += "💡 建议：\n"
                error_msg += "- 在浏览器中打开链接确认视频是否可访问\n"
                error_msg += "- 使用 /bili-test-cookie 检查Cookie状态\n"
                error_msg += "- 如果问题持续，请稍后重试\n\n"
            
            # 不再显示BBDown的原始输出信息，只显示用户友好的错误提示
            yield event.plain_result(error_msg.strip())

    async def _test_cookie(self, cookie: str) -> tuple[bool, str, dict]:
        """测试Cookie是否有效
        
        Returns:
            tuple: (是否有效, 消息, 用户信息字典)
        """
        try:
            parsed_cookie = self._parse_cookie(cookie)
            if not parsed_cookie:
                return False, "Cookie格式错误，无法解析", {}
            
            # 调用B站API获取用户信息
            url = "https://api.bilibili.com/x/space/myinfo"
            headers = {
                "Cookie": parsed_cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com/",
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return False, f"请求失败，状态码: {resp.status}", {}
                    
                    data = await resp.json()
                    code = data.get("code", -1)
                    
                    if code != 0:
                        message = data.get("message", "未知错误")
                        return False, f"Cookie无效: {message}", {}
                    
                    user_data = data.get("data", {})
                    if not user_data:
                        return False, "Cookie无效: 未获取到用户信息", {}
                    
                    return True, "Cookie有效", user_data
                    
        except asyncio.TimeoutError:
            return False, "请求超时，请检查网络连接", {}
        except Exception as e:
            logger.error(f"测试Cookie失败: {e}")
            return False, f"测试失败: {str(e)}", {}

    @filter.command("bili-test-cookie", alias={"bilibili-test-cookie", "b站测试cookie", "B站测试cookie", "测试cookie"})
    async def test_cookie(self, event: AstrMessageEvent, cookie: str = ""):
        """测试B站Cookie是否有效
        
        用法: /bili-test-cookie [cookie字符串]
        如果不提供cookie，则测试当前配置的cookie
        """
        # 获取要测试的cookie
        cookie_to_test = cookie
        if not cookie_to_test:
            # 从配置获取
            current_config = self._get_current_config()
            cookie_to_test = current_config.get("cookie", "")
            if not cookie_to_test:
                yield event.plain_result(
                    "请提供要测试的Cookie\n"
                    "用法: /bili-test-cookie <cookie字符串>\n"
                    "或者先设置Cookie后使用: /bili-test-cookie"
                )
                return
        
        yield event.plain_result("正在测试Cookie，请稍候...")
        
        # 测试cookie
        is_valid, message, user_data = await self._test_cookie(cookie_to_test)
        
        if is_valid:
            # 提取用户信息
            user_name = user_data.get("name", "未知")
            user_id = user_data.get("mid", "未知")
            level = user_data.get("level_info", {}).get("current_level", "未知")
            vip_status = user_data.get("vip", {}).get("status", 0)
            vip_type = user_data.get("vip", {}).get("type", 0)
            
            vip_text = "未开通" if vip_status == 0 else ("大会员" if vip_type == 2 else "年度大会员")
            
            result_msg = f"""✅ Cookie测试成功！

👤 用户信息：
用户名: {user_name}
用户ID: {user_id}
等级: LV{level}
会员状态: {vip_text}

💡 提示：此Cookie可以正常使用
"""
            yield event.plain_result(result_msg)
        else:
            result_msg = f"""❌ Cookie测试失败

{message}

💡 提示：
1. 请检查Cookie是否已过期
2. 请确认Cookie格式是否正确
3. 可以重新从浏览器复制Cookie
"""
            yield event.plain_result(result_msg)

    @filter.command("bili-cookie", alias={"bilibili-cookie", "b站cookie", "B站cookie"})
    async def set_cookie(self, event: AstrMessageEvent, cookie: str = ""):
        """设置B站Cookie
        
        用法: /bili-cookie <cookie字符串>
        支持多种格式：
        1. 浏览器格式: name1=value1; name2=value2
        2. Netscape格式: 从浏览器导出的cookie文件
        3. JSON格式: {"name": "value"}
        4. 纯文本格式: name=value (多行)
        """
        # 检查权限（设置Cookie需要权限）
        has_permission, error_msg = self._check_permission(event)
        if not has_permission:
            # 如果error_msg为None，表示群组未配置，静默忽略（不回复）
            # 如果error_msg不为None，表示有配置但用户没权限，需要回复错误消息
            if error_msg:
                yield event.plain_result(error_msg)
            return
        
        if not cookie:
            yield event.plain_result(
                "请提供Cookie\n"
                "用法: /bili-cookie <cookie字符串>\n"
                "支持多种格式，程序会自动识别"
            )
            return
        
        try:
            # 解析 cookie
            parsed_cookie = self._parse_cookie(cookie)
            
            if not parsed_cookie:
                yield event.plain_result("Cookie 解析失败，请检查格式")
                return
            
            # 保存到配置
            self.config["cookie"] = parsed_cookie
            
            # 如果config是AstrBotConfig类型，使用其save_config方法
            plugin_metadata = self.context.get_registered_star("bilidownloader")
            if plugin_metadata and plugin_metadata.config:
                plugin_metadata.config["cookie"] = parsed_cookie
                plugin_metadata.config.save_config()
                # 同步更新到self.config
                self.config = dict(plugin_metadata.config)
            else:
                # 否则手动保存（兼容旧方式）
                self._save_config()
            
            # 显示设置结果（隐藏敏感信息）
            display_cookie = parsed_cookie
            if len(display_cookie) > 100:
                display_cookie = display_cookie[:50] + "..." + display_cookie[-50:]
            # 隐藏敏感值
            display_cookie = re.sub(r'(SESSDATA|DedeUserID|bili_jct)=[^;]+', r'\1=***', display_cookie)
            
            yield event.plain_result(f"Cookie 设置成功！\n已解析格式: {display_cookie}")
        except Exception as e:
            logger.error(f"设置 Cookie 失败: {e}")
            yield event.plain_result(f"设置 Cookie 失败: {str(e)}")

    def _save_config_to_file(self, config_key: str, value):
        """保存配置到文件"""
        plugin_metadata = self.context.get_registered_star("bilidownloader")
        if plugin_metadata and plugin_metadata.config:
            # 处理嵌套配置
            if "." in config_key:
                keys = config_key.split(".")
                config = plugin_metadata.config
                for key in keys[:-1]:
                    if key not in config:
                        config[key] = {}
                    config = config[key]
                config[keys[-1]] = value
            else:
                plugin_metadata.config[config_key] = value
            
            plugin_metadata.config.save_config()
            # 同步更新到self.config
            self.config = dict(plugin_metadata.config)
            
            # 如果是路径相关配置，更新实例变量
            if config_key == "download_path":
                self.download_path = value
                os.makedirs(self.download_path, exist_ok=True)
            elif config_key == "bbdown_path":
                self.bbdown_path = value
        else:
            # 兼容旧方式
            if "." in config_key:
                keys = config_key.split(".")
                config = self.config
                for key in keys[:-1]:
                    if key not in config:
                        config[key] = {}
                    config = config[key]
                config[keys[-1]] = value
            else:
                self.config[config_key] = value
            self._save_config()

    @filter.command("bili-set", alias={"bilibili-set", "b站设置", "B站设置"})
    async def set_config(self, event: AstrMessageEvent, key: str = "", value: str = ""):
        """设置插件配置
        
        用法: /bili-set <配置项> <值>
        
        可用配置项：
        - bbdown_path: BBDown可执行文件路径
        - download_path: 下载保存路径
        - classify_by_owner: 按UP主名称分类文件夹（true/false 或 是/否）
        - quality: 默认清晰度（8K/4K/1080P60/1080P/720P60/720P/480P/360P，留空表示自动）
        - danmaku: 是否下载弹幕（true/false 或 是/否）
        - subtitle: 是否下载字幕（true/false 或 是/否）
        - single_pattern: 单个视频命名格式
        - multi_pattern: 分P视频命名格式
        
        示例：
        /bili-set download_path ./videos
        /bili-set quality 1080P
        /bili-set danmaku true
        /bili-set single_pattern <视频标题>[<清晰度>]
        """
        # 检查权限（设置配置需要权限）
        has_permission, error_msg = self._check_permission(event)
        if not has_permission:
            # 如果error_msg为None，表示群组未配置，静默忽略（不回复）
            # 如果error_msg不为None，表示有配置但用户没权限，需要回复错误消息
            if error_msg:
                yield event.plain_result(error_msg)
            return
        
        if not key:
            help_msg = """📝 设置插件配置

用法: /bili-set <配置项> <值>

可用配置项：
• bbdown_path - BBDown可执行文件路径
• download_path - 下载保存路径
• classify_by_owner - 按UP主名称分类文件夹（true/false 或 是/否）
• quality - 默认清晰度（8K/4K/1080P60/1080P/720P60/720P/480P/360P，留空表示自动）
• danmaku - 是否下载弹幕（true/false 或 是/否）
• subtitle - 是否下载字幕（true/false 或 是/否）
• single_pattern - 单个视频命名格式
• multi_pattern - 分P视频命名格式

示例：
/bili-set download_path ./videos
/bili-set quality 1080P
/bili-set danmaku true
/bili-set single_pattern <视频标题>[<清晰度>]

💡 提示：也可以使用WebUI配置页面进行设置
"""
            yield event.plain_result(help_msg)
            return
        
        if not value:
            yield event.plain_result(f"请提供配置值\n用法: /bili-set {key} <值>")
            return
        
        try:
            # 处理不同的配置项
            if key == "bbdown_path":
                self._save_config_to_file("bbdown_path", value)
                yield event.plain_result(f"✅ BBDown路径已设置为: {value}")
                
            elif key == "download_path":
                # 确保目录存在
                os.makedirs(value, exist_ok=True)
                self._save_config_to_file("download_path", value)
                yield event.plain_result(f"✅ 下载路径已设置为: {value}")
                
            elif key == "classify_by_owner":
                # 解析布尔值
                bool_value = value.lower() in ["true", "1", "yes", "是", "开启", "on"]
                self._save_config_to_file("classify_by_owner", bool_value)
                yield event.plain_result(f"✅ 按UP主分类已设置为: {'是' if bool_value else '否'}")
                
            elif key == "quality":
                # 验证清晰度值
                valid_qualities = ["8K", "4K", "1080P60", "1080P", "720P60", "720P", "480P", "360P", ""]
                if value not in valid_qualities:
                    yield event.plain_result(
                        f"❌ 无效的清晰度值: {value}\n"
                        f"可选值: {', '.join([q for q in valid_qualities if q])} 或留空（自动选择）"
                    )
                    return
                self._save_config_to_file("default_options.quality", value)
                quality_text = value if value else "自动选择"
                yield event.plain_result(f"✅ 默认清晰度已设置为: {quality_text}")
                
            elif key == "danmaku":
                # 解析布尔值
                bool_value = value.lower() in ["true", "1", "yes", "是", "开启", "on"]
                self._save_config_to_file("default_options.download_danmaku", bool_value)
                yield event.plain_result(f"✅ 下载弹幕已设置为: {'是' if bool_value else '否'}")
                
            elif key == "subtitle":
                # 解析布尔值
                bool_value = value.lower() in ["true", "1", "yes", "是", "开启", "on"]
                self._save_config_to_file("default_options.download_subtitle", bool_value)
                yield event.plain_result(f"✅ 下载字幕已设置为: {'是' if bool_value else '否'}")
                
            elif key == "single_pattern":
                self._save_config_to_file("naming.single_video_pattern", value)
                yield event.plain_result(f"✅ 单个视频命名格式已设置为:\n{value}")
                
            elif key == "multi_pattern":
                self._save_config_to_file("naming.multi_video_pattern", value)
                yield event.plain_result(f"✅ 分P视频命名格式已设置为:\n{value}")
                
            else:
                yield event.plain_result(
                    f"❌ 未知的配置项: {key}\n"
                    "使用 /bili-set 查看可用配置项"
                )
                
        except Exception as e:
            logger.error(f"设置配置失败: {e}")
            yield event.plain_result(f"❌ 设置失败: {str(e)}")

    @filter.command("bili-config", alias={"bilibili-config", "b站配置", "B站配置"})
    async def show_config(self, event: AstrMessageEvent):
        """查看当前配置"""
        # 重新从metadata获取最新配置
        plugin_metadata = self.context.get_registered_star("bilidownloader")
        if plugin_metadata and plugin_metadata.config:
            current_config = dict(plugin_metadata.config)
        else:
            current_config = self.config
        
        naming_config = current_config.get("naming", {})
        single_pattern = naming_config.get("single_video_pattern", "<videoTitle>[<dfn>]")
        multi_pattern = naming_config.get("multi_video_pattern", "<videoTitle>/[P<pageNumberWithZero>]<pageTitle>[<dfn>]")
        
        classify_by_owner = current_config.get('classify_by_owner', True)
        config_info = f"""当前配置：
下载路径: {current_config.get('download_path', './downloads')}
BBDown路径: {current_config.get('bbdown_path', 'BBDown')}
Cookie: {'已设置' if current_config.get('cookie') else '未设置'}
按UP主分类: {'是' if classify_by_owner else '否'}
默认清晰度: {current_config.get('default_options', {}).get('quality', '未设置') or '自动选择'}
下载弹幕: {'是' if current_config.get('default_options', {}).get('download_danmaku', False) else '否'}
下载字幕: {'是' if current_config.get('default_options', {}).get('download_subtitle', True) else '否'}

文件命名格式：
单个视频: {single_pattern}
分P视频: {multi_pattern}

💡 提示：可在WebUI的插件配置页面修改这些设置
"""
        yield event.plain_result(config_info)

    @filter.command("bili-naming", alias={"bilibili-naming", "b站命名", "B站命名"})
    async def show_naming_params(self, event: AstrMessageEvent):
        """查看文件命名可用参数"""
        params_info = """📝 文件命名格式可用参数（直接使用中文参数名即可）

【单个视频可用参数】
<视频标题>        - 视频的标题
<BV号>           - 视频的BV号（如：BV1234567890）
<AID>            - 视频的AID
<CID>            - 视频的CID
<清晰度>          - 视频清晰度（如：1080P、720P、4K等）
<分辨率>          - 视频分辨率（如：1920x1080）
<帧率>            - 视频帧率（如：30、60）
<视频编码>        - 视频编码格式（如：avc、hevc）
<视频码率>        - 视频码率
<音频编码>        - 音频编码格式
<音频码率>        - 音频码率
<UP主名称>        - 上传视频的UP主名字
<UP主MID>         - UP主的MID号
<发布时间>        - 视频发布时间（格式：2024-01-01_12-00-00）
<API类型>         - API类型（TV/APP/INTL/WEB）

【分P视频额外参数】
<分P序号>         - 分P序号（如：1、2、10）
<分P序号补零>     - 分P序号带前导零（如：01、02、10）
<分P标题>         - 每个分P的标题

📌 使用示例：

【单个视频】
格式：<视频标题>[<清晰度>]
结果：我的视频标题[1080P].mp4

格式：<UP主名称>-<视频标题>-<清晰度>
结果：张三-我的视频标题-1080P.mp4

格式：<视频标题>-<BV号>[<清晰度>]
结果：我的视频标题-BV1234567890[1080P].mp4

【分P视频】
格式：<视频标题>/[P<分P序号补零>]<分P标题>[<清晰度>]
结果：
  我的视频标题/[P01]第一集[1080P].mp4
  我的视频标题/[P02]第二集[1080P].mp4

格式：<UP主名称>-<视频标题>/P<分P序号>-<分P标题>
结果：
  张三-我的视频标题/P1-第一集.mp4
  张三-我的视频标题/P2-第二集.mp4

💡 提示：
- 直接使用中文参数名，系统会自动转换
- 可在WebUI的插件配置页面设置命名格式
- 参数名需要用尖括号 <> 包裹
"""
        yield event.plain_result(params_info)

    def _save_config(self):
        """保存配置文件（兼容方法，当config不是AstrBotConfig时使用）"""
        try:
            config_path = os.path.join("data", "config", "bilidownloader_config.json")
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
