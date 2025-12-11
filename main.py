import asyncio
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "newapi",
    "wanting0521",
    "从可配置的API 拉取用量数据，按固定时间跨度统计 RPM/TPM/TopN 模型，并在聊天中返回报告",
    "1.0.0",
)
class XiguaUsageReporter(Star):
    """
    一个 AstrBot 插件：
    - 通过可配置的 `base_url`、`Authorization`、`New-Api-User` 请求上游 API
    - 使用固定的时间跨度（分钟）对数据进行聚合计算
    - 输出总使用量、总请求数、总配额、平均 RPM/TPM，以及使用量 Top N 的模型
    - 可选将原始 JSON 响应保存到插件目录下的 `data.json`
    - 固定使用配置中的 `time_span_minutes`（默认 1500 分钟 = 25 小时）
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 本插件目录与数据文件路径
        self._plugin_dir: Path = Path(__file__).resolve().parent
        self.data_file_path: Path = self._plugin_dir / "data.json"
        # 基础请求配置（仅域名 + 可配置路径）
        self.base_domain: str = (
            config.get("base_domain")
            or config.get("base_url")  # 兼容旧字段
            or "https://new.xigua.wiki"
        ).strip()
        # 接口路径固定为本插件的默认值，配置文件不再提供路径项
        self.endpoint_path: str = "/api/data/"
        self.authorization: str = config.get("authorization", "").strip()
        self.new_api_user: str = config.get("new_api_user", "").strip()
        self.request_timeout: int = int(config.get("request_timeout", 15))

        # HTTP请求重试配置
        try:
            self.retry_max_attempts: int = int(config.get("retry_max_attempts", 5))
        except Exception:
            self.retry_max_attempts = 5
        try:
            self.retry_initial_delay_ms: int = int(config.get("retry_initial_delay_ms", 10000))
        except Exception:
            self.retry_initial_delay_ms = 10000

        # 统计与展示配置
        self.time_span_minutes_default: int = 1500
        self.show_top_models: bool = bool(config.get("show_top_models", True))
        try:
            self.top_n_models: int = int(config.get("top_n_models", 3))
        except Exception:
            self.top_n_models = 3
        self.save_raw_json: bool = True
        # 是否使用合并转发发送（允许通过配置开关）
        try:
            self.use_forward: bool = bool(config.get("use_forward", True))
        except Exception:
            self.use_forward = True
        self.log_verbose: bool = True
        self.max_log_body_chars: int = 500
        # 记录最近一次构造的时间窗，便于日志核对
        self._last_start_ts: int = 0
        self._last_end_ts: int = 0

        # 日志查询配置
        try:
            self.log_page_size: int = int(config.get("log_page_size", 20))
        except Exception:
            self.log_page_size = 20
        try:
            self.log_use_forward: bool = bool(config.get("log_use_forward", self.use_forward))
        except Exception:
            self.log_use_forward = self.use_forward
        try:
            self.user_use_forward: bool = bool(config.get("user_use_forward", False))
        except Exception:
            self.user_use_forward = False

        # 模板路径配置
        self._templates_dir: Path = self._plugin_dir / "templates"
        
        logger.info(
            f"已加载 [XiguaUsageReporter] v1.0.0，默认统计 {self.time_span_minutes_default} 分钟，Top{self.top_n_models} 模型。"
        )

    def _load_template(self, template_name: str) -> str:
        """加载HTML模板文件"""
        template_path = self._templates_dir / template_name
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"加载模板失败 {template_name}: {e}")
            return ""

    def _render_html_template(self, template: str, data: dict) -> str:
        """HTML模板渲染，使用 {{key}} 占位符格式"""
        import re
        result = template
        
        for key, value in data.items():
            # 统一使用双大括号格式 {{key}}
            placeholder = "{{" + key + "}}"
            result = result.replace(placeholder, str(value))
        
        # 检查是否还有未替换的占位符（排除 Jinja2 的 {% %} 语法）
        if remaining_placeholders := re.findall(r"\{\{[^}]+\}\}", result):
            logger.warning(
                f"未替换的占位符 ({len(remaining_placeholders)}个): {remaining_placeholders[:10]}"
            )
        
        return result

    def _render_jinja2_template(self, template_str: str, data: dict) -> str:
        """使用Jinja2渲染模板"""
        try:
            from jinja2 import Template
            template = Template(template_str)
            return template.render(**data)
        except ImportError:
            logger.warning("Jinja2 未安装，使用简单模板替换")
            return self._render_html_template(template_str, data)
        except Exception as e:
            logger.error(f"Jinja2 渲染失败: {e}")
            return self._render_html_template(template_str, data)

    async def _generate_image_report(self, html_content: str) -> Optional[str]:
        """使用AstrBot内置的HTML渲染服务生成图片"""
        try:
            # 图片生成选项
            image_options = {
                "full_page": True,
                "type": "jpeg",
                "quality": 95,
            }
            
            # 使用 Star 类继承的 html_render 方法
            image_url = await self.html_render(
                html_content,
                {},  # 空数据字典，因为数据已包含在HTML中
                True,  # return_url=True，返回URL
                image_options,
            )
            
            logger.info(f"图片生成成功: {image_url}")
            return image_url
            
        except Exception as e:
            logger.error(f"生成图片报告失败: {e}", exc_info=True)
            return None

    async def _http_get_json(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """使用标准库发起 GET 请求并解析 JSON，避免额外依赖。"""
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError

        if self.log_verbose:
            masked_headers = dict(headers)
            if "Authorization" in masked_headers:
                masked_headers["Authorization"] = self._mask_secret(masked_headers["Authorization"])
            if "New-Api-User" in masked_headers:
                masked_headers["New-Api-User"] = self._mask_secret(str(masked_headers["New-Api-User"]))
            logger.debug(f"HTTP GET 即将请求: url={url}, headers={masked_headers}")

        req = Request(url=url, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)

        def _do() -> Dict[str, Any]:
            with urlopen(req, timeout=self.request_timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = -1
                ct = None
                try:
                    ct = resp.headers.get("Content-Type")
                except Exception:
                    ct = None
                data = resp.read()
                body_len = len(data) if data else 0
                if self.log_verbose:
                    logger.debug(f"HTTP 响应: status={status}, content_type={ct}, body_len={body_len}")
                # 尝试解析 JSON
                text = None
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    pass
                try:
                    return json.loads(text if text is not None else data)
                except Exception as e:
                    if self.log_verbose:
                        snippet = (text or "")[: self.max_log_body_chars]
                        logger.debug(f"HTTP 响应非 JSON，解析失败: {e}; 片段: {snippet}")
                    return {"error": "non_json_response", "status": status, "content_type": ct}

        attempt = 0
        while attempt <= self.retry_max_attempts:
            if attempt > 0:
                # 计算退避时间（指数退避）
                delay_ms = self.retry_initial_delay_ms * (2 ** (attempt - 1))
                logger.info(f"HTTP 请求重试 #{attempt}/{self.retry_max_attempts}，等待 {delay_ms/1000:.2f}s...")
                await asyncio.sleep(delay_ms / 1000)
            
            try:
                result = await asyncio.to_thread(_do)
                if self.log_verbose:
                    if isinstance(result, dict):
                        logger.debug(f"HTTP 响应已解析为 JSON 对象，顶层键: {list(result.keys())[:20]}")
                    elif isinstance(result, list):
                        logger.debug(f"HTTP 响应已解析为 JSON 列表，长度: {len(result)}")
                    else:
                        logger.debug(f"HTTP 响应已解析为 JSON，类型: {type(result).__name__}")
                return result
            except HTTPError as e:
                # 对于 429 错误，进行重试
                if e.code == 429 and attempt < self.retry_max_attempts:
                    attempt += 1
                    logger.warning(f"请求频率过高 (429)，将在 {attempt} 次尝试中重试")
                    continue
                
                text = f"HTTP {e.code} {e.reason}"
                logger.error(f"请求失败: {text}")
                return {"error": text}
            except URLError as e:
                text = f"URL 错误: {e.reason}"
                logger.error(f"请求失败: {text}")
                return {"error": text}
            except Exception as e:
                text = f"请求异常: {e}"
                logger.error(text)
                return {"error": text}
                
        # 如果达到最大重试次数，返回错误
        return {"error": f"达到最大重试次数 ({self.retry_max_attempts})"}

    def _extract_records(self, payload: Any) -> List[Dict[str, Any]]:
        """更宽松地提取记录列表，兼容多种返回格式，并输出详细日志。"""
        try:
            ptype = type(payload).__name__
            if self.log_verbose:
                logger.debug(f"extract_records: 顶层类型={ptype}")
            # 直接是列表
            if isinstance(payload, list):
                if self.log_verbose:
                    logger.debug(f"extract_records: 使用顶层列表，len={len(payload)}")
                return payload  # type: ignore
            if not isinstance(payload, dict):
                return []
            # 常见：data 为列表
            data = payload.get("data")
            if isinstance(data, list):
                if self.log_verbose:
                    logger.debug(f"extract_records: 使用 data(list)，len={len(data)}")
                return data  # type: ignore
            # data 为对象，其中再包含 data/list
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, list):
                    if self.log_verbose:
                        logger.debug(f"extract_records: 使用 data.data(list)，len={len(inner)}")
                    return inner  # type: ignore
                inner = data.get("list")
                if isinstance(inner, list):
                    if self.log_verbose:
                        logger.debug(f"extract_records: 使用 data.list(list)，len={len(inner)}")
                    return inner  # type: ignore
            # 顶层 list
            lst = payload.get("list")
            if isinstance(lst, list):
                if self.log_verbose:
                    logger.debug(f"extract_records: 使用 list(list)，len={len(lst)}")
                return lst  # type: ignore
            if self.log_verbose:
                logger.debug("extract_records: 未在常见路径发现列表，返回空")
            return []
        except Exception as e:
            logger.warning(f"extract_records: 解析异常: {e}")
            return []

    def _analyze(self, records: List[Dict[str, Any]], start_timestamp: int, end_timestamp: int, time_span_minutes: int) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, int]]]]:
        """使用当前时刻回溯的固定窗口 [start_timestamp, end_timestamp] 进行统计；平均值以 time_span_minutes 为分母。"""
        if start_timestamp <= 0 or end_timestamp <= 0 or end_timestamp < start_timestamp:
            end_timestamp = int(time.time())
            start_timestamp = end_timestamp - (time_span_minutes * 60)

        total_tokens_used = 0
        total_requests = 0
        total_quota = 0
        total_use_time_ms = 0  # 总使用时间（毫秒）

        model_stats: Dict[str, Dict[str, int]] = {}

        for r in records:
            created_at = int(r.get("created_at", 0) or 0)
            if start_timestamp <= created_at <= end_timestamp:
                model_name = r.get("model_name")
                tokens_used = int(r.get("token_used", 0) or 0)
                count = int(r.get("count", 0) or 0)
                quota = int(r.get("quota", 0) or 0)
                use_time_ms = int(r.get("use_time", 0) or 0)  # 获取使用时间（毫秒）

                total_tokens_used += tokens_used
                total_requests += count
                total_quota += quota
                total_use_time_ms += use_time_ms

                if model_name:
                    entry = model_stats.setdefault(model_name, {"total_tokens": 0, "total_requests": 0, "total_quota": 0, "total_use_time_ms": 0})
                    entry["total_tokens"] += tokens_used
                    entry["total_requests"] += count
                    entry["total_quota"] += quota
                    entry["total_use_time_ms"] += use_time_ms

        minutes_for_avg = max(1, int(time_span_minutes))
        avg_rpm = (total_requests / minutes_for_avg) if minutes_for_avg > 0 else 0.0
        avg_tpm = (total_tokens_used / minutes_for_avg) if minutes_for_avg > 0 else 0.0
        # 计算平均用时（毫秒转秒）
        avg_use_time_s = (total_use_time_ms / total_requests) if total_requests > 0 else 0.0

        stats = {
            "time_span_minutes": time_span_minutes,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "total_tokens_used": total_tokens_used,
            "total_requests": total_requests,
            "total_quota": total_quota,
            "avg_rpm": avg_rpm,
            "avg_tpm": avg_tpm,
            "avg_use_time_s": avg_use_time_s,  # 平均用时（秒）
        }

        # 调用最多（按请求次数）排序
        sorted_models = sorted(model_stats.items(), key=lambda kv: kv[1]["total_requests"], reverse=True)
        return stats, sorted_models
        
    async def _fetch_user_stat(self, username: str, start_ts: int, end_ts: int) -> float:
        """调用/api/log/stat接口获取用户的准确消费额度数据"""
        try:
            # 构建查询参数
            params = {
                "type": 2,  # 固定值
                "username": username,
                "token_name": "",
                "model_name": "",
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "channel": "",
                "group": ""
            }
            
            # 构建完整的URL
            url = f"{self.base_domain}/api/log/stat?{urllib.parse.urlencode(params)}"
            
            # 发送请求并获取响应
            headers = self._build_headers()
            data = await self._http_get_json(url, headers)
            
            # 提取quota数据并进行处理
            if data and isinstance(data, dict) and "quota" in data:
                quota = int(data["quota"] or 0)
                # 计算消费额度：quota / 500000，保留两位小数
                consumption = round(quota / 500000, 2)
                logger.info(f"获取用户 [{username}] 的准确消费数据: {consumption}")
                return consumption
            else:
                logger.warning(f"无法从响应中提取用户 [{username}] 的quota数据: {data}")
                return -1.0
        except Exception as e:
            logger.error(f"调用/api/log/stat接口获取用户 [{username}] 数据失败: {str(e)}")
            return -1.0

    @staticmethod
    def _fmt_ts(ts: int) -> str:
        if not ts:
            return "-"
        try:
            tz = timezone(timedelta(hours=8), name="CST+8")
            return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S %Z")  # 格式化时间字符串
        except Exception:
            return str(ts)

    def _format_report(self, stats: Dict[str, Any], sorted_models: List[Tuple[str, Dict[str, int]]]) -> str:
        """将统计结果格式化为可读报告"""
        # 格式化时间范围
        start_time_str = self._fmt_ts(int(stats.get("start_timestamp", 0)))
        end_time_str = self._fmt_ts(int(stats.get("end_timestamp", 0)))
        span_minutes = float(stats.get("time_span_minutes", 0.0) or 0.0)
        
        # 构建报告头部
        lines = [
            "📊 --- 数据分析报告 ---",
            f"⏱️ 计算时间跨度: {int(span_minutes)} 分钟",
            f"📅 数据范围: {start_time_str} 至 {end_time_str}",
            f"🔢 总使用量 (tokens): {stats.get('total_tokens_used', 0):,}",
            f"📈 总请求次数: {stats.get('total_requests', 0):,}",
            # f"💳 总配额: {stats.get('total_quota', 0):,}",
            f"⚡ 平均 RPM: {float(stats.get('avg_rpm', 0.0)):.3f}",
            f"🚀 平均 TPM: {float(stats.get('avg_tpm', 0.0)):.3f}",
            f"⏱️ 平均用时: {float(stats.get('avg_use_time_s', 0.0)):.1f}s",
            "-------------------------",
        ]

        # 添加top模型信息
        if self.show_top_models and self.top_n_models > 0 and sorted_models:
            lines.append(f"🏆 调用最多的前 {self.top_n_models} 个模型：")
            span_minutes_float = max(1e-9, float(stats.get("time_span_minutes", 0.0) or 0.0))
            for model, s in sorted_models[: self.top_n_models]:
                avg_tpm_model = (s["total_tokens"] / span_minutes_float) if span_minutes_float > 0 else 0.0
                avg_rpm_model = (s["total_requests"] / span_minutes_float) if span_minutes_float > 0 else 0.0
                lines.append("")
                lines.append(f"🤖 模型: {model}")
                # 计算模型的平均用时（毫秒转秒）
                avg_use_time_model_s = (s.get('total_use_time_ms', 0) / s['total_requests'] / 1000) if s['total_requests'] > 0 else 0.0
                lines.append(f"  - 🔢 Token总和: {s['total_tokens']:,}")
                lines.append(f"  - 📈 请求总数: {s['total_requests']:,}")
                lines.append(f"  - 🚀 平均 TPM: {avg_tpm_model:.3f}")
                lines.append(f"  - ⚡ 平均 RPM: {avg_rpm_model:.3f}")
                lines.append(f"  - ⏱️ 平均用时: {avg_use_time_model_s:.3f}s")
                # lines.append(f"  - 💳 配额: {s['total_quota']:,}")
            # 注意：这里有一个bug，重复添加了最后一个模型的信息
            # lines.append("")
            # lines.append(f"模型: {model}")
            # lines.append(f"  - Token总和: {s['total_tokens']:,}")
            # lines.append(f"  - 请求总数: {s['total_requests']:,}")
            # lines.append(f"  - 平均 TPM: {avg_tpm_model:.3f}")
            # lines.append(f"  - 平均 RPM: {avg_rpm_model:.3f}")
            # lines.append(f"  - 配额: {s['total_quota']:,}")

        return "\n".join(lines)

    @staticmethod
    def _mask_secret(value: str, left: int = 4, right: int = 2) -> str:
        """掩码敏感信息，保留前后部分字符"""
        try:
            v = str(value)
            if len(v) <= left + right:
                return "*" * len(v)  # 如果字符串太短，全部掩码
            return v[:left] + "..." + v[-right:]  # 保留前后部分字符
        except Exception:
            return "***"

    def _build_forward_node(self, text: str) -> Any:
        """将文本包装为合并转发 Node。"""
        try:
            # 获取转发用户ID
            conf_uin = getattr(self, "forward_uin", None)
            if conf_uin is None and hasattr(self, "config"):
                conf_uin = self.config.get("forward_uin")  # type: ignore
            forward_uin = int(conf_uin) if conf_uin not in (None, "", 0) else 10000
        except Exception:
            forward_uin = 10000
        
        # 获取转发用户名
        forward_name = getattr(self, "forward_name", None) or (
            getattr(self, "config", {}).get("forward_name") if hasattr(self, "config") else None  # type: ignore
        ) or "小岚的虚拟意识工坊"
        
        # 创建消息节点
        return Comp.Node(
            uin=forward_uin,
            name=forward_name,
            content=[Comp.Plain(text)],
        )

    def _build_forward_nodes(self, text: str) -> List[Any]:
        """将长文本切分为多段，生成多个 Node。"""
        max_len = 900  # 每段最大长度
        parts: List[str] = []
        t = text or ""
        # 切分文本
        while t:
            parts.append(t[:max_len])
            t = t[max_len:]
        if not parts:
            parts = ["(空)"]
        # 为每段文本创建节点
        nodes = [self._build_forward_node(p) for p in parts]
        return nodes

    async def _save_raw_json(self, payload: Dict[str, Any]):
        """保存原始JSON响应到本地文件"""
        if not self.save_raw_json:
            return
        try:
            with open(self.data_file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)  # 保存JSON，保留非ASCII字符
            if self.log_verbose:
                try:
                    size = self.data_file_path.stat().st_size
                    logger.debug(f"已保存原始 JSON 到 {self.data_file_path} (size={size} bytes)")
                except Exception:
                    logger.debug(f"已保存原始 JSON 到 {self.data_file_path}")
        except Exception as e:
            logger.warning(f"保存 data.json 失败: {e}")

    async def _load_local_json(self) -> Dict[str, Any]:
        """从本地文件加载JSON数据"""
        try:
            if self.log_verbose:
                logger.debug(f"尝试从本地读取: {self.data_file_path} (exists={self.data_file_path.exists()})")
            with open(self.data_file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"读取 data.json 失败: {e}")
            return {}

    def _build_headers(self) -> Dict[str, str]:
        """构建HTTP请求头"""
        headers = {
            "Accept": "application/json",  # 接受JSON格式响应
        }
        if self.authorization:
            headers["Authorization"] = self.authorization  # 添加授权信息
        if self.new_api_user:
            headers["New-Api-User"] = self.new_api_user  # 添加用户信息
        return headers

    def _build_url(self, minutes: int) -> str:
        """构建带时间窗口的API请求URL"""
        path = self.endpoint_path or "/api/data/"
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_domain.rstrip("/") + path

        # 计算时间窗口 - 使用当前时间作为结束时间
        end_ts = int(time.time())
        start_ts = end_ts - minutes * 60
        self._last_start_ts = start_ts
        self._last_end_ts = end_ts

        # 追加开始/结束时间戳与默认粒度（固定为 username='', default_time='hour'）
        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(url)
            q = dict(parse_qsl(split.query, keep_blank_values=True))  # 解析现有查询参数
            q.update({
                "username": "",
                "start_timestamp": str(start_ts),
                "end_timestamp": str(end_ts),
                "default_time": "hour",
            })  # 添加新参数
            url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))  # 重新构建URL
        except Exception as e:
            if self.log_verbose:
                logger.debug(f"构造时间戳查询参数失败: {e}")
        
        if self.log_verbose:
            # 附带可读时间窗口
            try:
                cst_tz = timezone(timedelta(hours=8))
                def fmt(ts: int) -> str:
                    utc = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    cst = datetime.fromtimestamp(ts, cst_tz).strftime("%Y-%m-%d %H:%M:%S CST+8")
                    return f"{utc} | {cst}"
                win = f"start={start_ts}({fmt(start_ts)}) -> end={end_ts}({fmt(end_ts)})"
            except Exception:
                win = f"start={start_ts} -> end={end_ts}"
            logger.debug(
                f"构造 URL: domain={self.base_domain}, path={path}, url={url}, minutes={minutes}, window={win}"
            )
        return url

    def _build_url_with_range(self, start_ts: int, end_ts: int) -> str:
        """构建指定时间范围的API请求URL"""
        path = self.endpoint_path or "/api/data/"
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_domain.rstrip("/") + path

        self._last_start_ts = int(start_ts)
        self._last_end_ts = int(end_ts)

        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(url)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            q.update({
                "username": "",
                "start_timestamp": str(self._last_start_ts),
                "end_timestamp": str(self._last_end_ts),
                "default_time": "hour",
            })
            url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
        except Exception as e:
            if self.log_verbose:
                logger.debug(f"构造时间戳查询参数失败: {e}")

        if self.log_verbose:
            try:
                cst_tz = timezone(timedelta(hours=8))
                def fmt(ts: int) -> str:
                    utc = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    cst = datetime.fromtimestamp(ts, cst_tz).strftime("%Y-%m-%d %H:%M:%S CST+8")
                    return f"{utc} | {cst}"
                win = f"start={self._last_start_ts}({fmt(self._last_start_ts)}) -> end={self._last_end_ts}({fmt(self._last_end_ts)})"
            except Exception:
                win = f"start={self._last_start_ts} -> end={self._last_end_ts}"
            logger.debug(
                f"构造 URL(指定范围): domain={self.base_domain}, path={path}, url={url}, window={win}"
            )
        return url

    def _build_log_headers(self) -> Dict[str, str]:
        """构建日志查询的HTTP请求头"""
        # 与获取用量相同的鉴权逻辑
        return self._build_headers()

    def _build_log_url(self, params: Dict[str, Any]) -> str:
        """构建日志查询的URL"""
        base = self.base_domain.rstrip("/") + "/api/log/"
        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(base)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            # 合并传入查询参数
            for k, v in (params or {}).items():
                q[str(k)] = str(v)
            return urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
        except Exception:
            # 简单拼接
            try:
                from urllib.parse import urlencode
                return base.rstrip("?") + ("?" + urlencode(params or {}))
            except Exception:
                return base

    async def _fetch_logs(self, params: Dict[str, Any]) -> Any:
        """获取日志数据"""
        url = self._build_log_url(params)
        headers = self._build_log_headers()
        return await self._http_get_json(url, headers)

    @staticmethod
    def _mask_ip(ip: Any) -> str:
        """掩码IP地址，保留前两位"""
        try:
            s = str(ip or "")
            if not s:
                return "无IP信息"
            parts = s.split(".")
            if len(parts) >= 2:
                return f"{parts[0]}.{parts[1]}.x.x"  # 保留前两位，其余用x替换
            return s
        except Exception:
            return "无IP信息"

    @staticmethod
    def _format_log_type(t: Any) -> str:
        """格式化日志类型"""
        try:
            iv = int(t)
            if iv == 2:
                return "消费"
            if iv == 5:
                return "错误"
            return "其他"
        except Exception:
            return "其他"

    def _extract_log_items(self, payload: Any) -> List[Dict[str, Any]]:
        """从响应中提取日志项"""
        try:
            if isinstance(payload, list):
                return payload  # type: ignore
            if not isinstance(payload, dict):
                return []
            data = payload.get("data")
            if isinstance(data, dict):
                items = data.get("items") or data.get("list") or []
                if isinstance(items, list):
                    return items  # type: ignore
            # 顶层 items/list
            items = payload.get("items") or payload.get("list")
            if isinstance(items, list):
                return items  # type: ignore
            return []
        except Exception:
            return []

    def _format_log_item(self, item: Dict[str, Any]) -> str:
        """格式化单条日志项"""
        created_at = 0
        try:
            created_at = int(item.get("created_at", 0) or 0)
        except Exception:
            created_at = 0
        log_time = self._fmt_ts(created_at)  # 格式化日志时间
        log_type = self._format_log_type(item.get("type"))  # 格式化日志类型
        model = item.get("model_name") or "未知模型"  # 模型名称
        prompt_tokens = int(item.get("prompt_tokens", 0) or 0)  # 输入token数
        completion_tokens = int(item.get("completion_tokens", 0) or 0)  # 输出token数
        use_time = int(item.get("use_time", 0) or 0)  # 使用时间
        ip_masked = self._mask_ip(item.get("ip"))  # 掩码后的IP地址
        
        # 构建日志行
        lines = [
            f"🕒 {log_time}",
            f"📌 {log_type}",
            f"🤖 {model}",
            f"📥 输入: {prompt_tokens}",
            f"📤 输出: {completion_tokens}",
            f"⏱️ 耗时: {use_time}ms",
            f"🌐 IP: {ip_masked}",
        ]
        return "\n ".join(lines)

    def _build_user_search_url(self, params: Dict[str, Any]) -> str:
        """构建用户搜索查询URL"""
        base = self.base_domain.rstrip("/") + "/api/user/search"
        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(base)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            # 合并传入查询参数
            for k, v in (params or {}).items():
                q[str(k)] = str(v)
            return urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
        except Exception:
            # 简单拼接
            try:
                from urllib.parse import urlencode
                return base.rstrip("?") + ("?" + urlencode(params or {}))
            except Exception:
                return base

    async def _fetch_user_search(self, keyword: str) -> Any:
        """通过/api/user/search接口查询用户信息"""
        params = {
            "p": 1,  # 页码固定传1
            "page_size": 100,  # 页码大小固定传100
            "keyword": keyword,  # 搜索关键词
            "group": "",  # 分组名留空
        }
        url = self._build_user_search_url(params)
        headers = self._build_headers()
        return await self._http_get_json(url, headers)

    def _format_user_quota_info(self, payload: Any) -> str:
        """格式化用户额度信息"""
        # 解析响应数据
        if not isinstance(payload, dict):
            return "❌ 查询失败：响应数据格式错误"
            
        data = payload.get("data")
        if not isinstance(data, dict):
            return "❌ 查询失败：数据字段格式错误"
            
        items = data.get("items", [])
        if not isinstance(items, list) or len(items) == 0:
            # 未查询到用户
            return "⚠️ 您当前尚未注册或账号未绑定qq邮箱。\n请前往注册：https://xiaolanya.cn"
            
        # 取第一个用户信息
        user_info = items[0]
        if not isinstance(user_info, dict):
            return "❌ 查询失败：用户信息格式错误"
            
        # 提取所需信息并格式化
        username = str(user_info.get("username") or "未知用户")
        quota = int(user_info.get("quota", 0) or 0)
        used_quota = int(user_info.get("used_quota", 0) or 0)
        
        # 计算美元额度（quota/500000，保留两位小数）
        current_quota_usd = round(quota / 500000, 2)
        used_quota_usd = round(used_quota / 500000, 2)
        
        # 按照要求的格式返回
        lines = [
            f"👤 用户名：{username}",
            f"💳 当前剩余额度：${current_quota_usd:.2f}",
            f"📉 已消耗：${used_quota_usd:.2f}"
        ]
        
        # 当额度小于1000时添加充值提醒
        if quota < 1 * 500000:
            lines.append("")
            
        return "\n".join(lines)

    async def _fetch_payload(self, minutes: int, headers: Dict[str, str], start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> Any:
        """获取 payload：优先使用给定的 [start_ts, end_ts]；若无则按 minutes；若为空则回退用最新记录重拉。"""
        # 第一次：优先使用显式时间窗
        if start_ts is not None and end_ts is not None:
            url = self._build_url_with_range(int(start_ts), int(end_ts))
        else:
            url = self._build_url(minutes)
        payload = await self._http_get_json(url, headers)
        records = self._extract_records(payload)
        if records:
            return payload
        
        # 回退：不带时间窗获取一次，尝试拿到最新 created_at
        try:
            from urllib.parse import urlsplit, urlunsplit
            split = urlsplit(url)
            url_no_query = urlunsplit((split.scheme, split.netloc, split.path, "", split.fragment))
        except Exception:
            url_no_query = url
        
        # 获取不带时间窗的数据
        probe = await self._http_get_json(url_no_query, headers)
        probe_records = self._extract_records(probe)
        if not probe_records:
            return payload
        
        # 尝试基于最新记录重构时间窗
        try:
            latest = max(int(r.get("created_at", 0) or 0) for r in probe_records)
            if latest <= 0:
                return payload
            # 用最新记录时间作为 end，重新拉取
            self._last_end_ts = latest
            self._last_start_ts = latest - minutes * 60
            # 基于 latest 构造 URL
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(url_no_query)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            q.update({
                "username": "",
                "start_timestamp": str(self._last_start_ts),
                "end_timestamp": str(self._last_end_ts),
                "default_time": "hour",
            })
            url2 = urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
            if self.log_verbose:
                logger.debug(f"回退：基于最新记录 created_at={latest} 重构 URL 再次请求: {url2}")
            payload2 = await self._http_get_json(url2, headers)
            return payload2
        except Exception as e:
            if self.log_verbose:
                logger.debug(f"回退重拉失败: {e}")
            return payload

    @filter.command("tokens统计")
    async def handle_xigua_command(self, event: AstrMessageEvent):
        """命令：/tokens统计（固定 25 小时，或按配置 time_span_minutes）"""
        minutes = self.time_span_minutes_default
        # 实时窗口：以当前时刻+1小时为 end，向前回溯 minutes 分钟
        end_ts = int(time.time()) + 3600
        start_ts = end_ts - minutes * 60

        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return

        # 获取数据
        headers = self._build_headers()
        payload = await self._fetch_payload(minutes, headers, start_ts=start_ts, end_ts=end_ts)
        if self.log_verbose and isinstance(payload, dict):
            logger.debug(f"远端 payload 字段: keys={list(payload.keys())[:20]}, success={payload.get('success')}, message={payload.get('message')!r}")

        # 解析远端记录
        payload_records = self._extract_records(payload)

        # 检查是否有错误
        payload_error = None
        if isinstance(payload, dict) and payload.get("error"):
            payload_error = str(payload.get("error"))
            logger.warning(f"远端请求失败，将回退读取本地 data.json：{payload_error}")
        else:
            # 默认先保存到本地（仅在解析为 JSON 时有效）
            if isinstance(payload, (dict, list)):
                await self._save_raw_json(payload)
            elif self.log_verbose:
                logger.debug("远端响应非 JSON，跳过落盘，仅使用本地 data.json 回退")

        # 读取本地 data.json
        local_payload = await self._load_local_json()
        if self.log_verbose:
            if isinstance(local_payload, dict):
                logger.debug(f"本地 JSON 顶层键: {list(local_payload.keys())[:20]}")
            elif isinstance(local_payload, list):
                logger.debug(f"本地 JSON 顶层为列表，长度: {len(local_payload)}")
        local_records = self._extract_records(local_payload)
        if self.log_verbose:
            logger.debug(f"本地记录数量: {len(local_records)}; 远端记录数量: {len(payload_records)}")
            if not local_records and isinstance(local_payload, dict):
                logger.debug(f"本地 JSON data/list 为空，success={local_payload.get('success')}, message={local_payload.get('message')!r}")

        # 优先使用本次请求的最新记录，若无则回退本地
        records = payload_records if payload_records else local_records
        stats, sorted_models = self._analyze(records, start_ts, end_ts, minutes)
        if self.log_verbose:
            logger.debug(
                f"统计: tokens={stats.get('total_tokens_used')}, requests={stats.get('total_requests')}, "
                f"quota={stats.get('total_quota')}, avg_rpm={stats.get('avg_rpm')}, avg_tpm={stats.get('avg_tpm')}"
            )

        # 生成报告
        report = self._format_report(stats, sorted_models)
        # 若无数据，给出醒目提示
        if not records:
            report = "[提示] 获取的数据为空\n" + report

        # 发送报告
        if self.use_forward:
            try:
                nodes = self._build_forward_nodes(report)
                # 简单校验 forward_uin
                if nodes and getattr(nodes[0], "uin", None):
                    yield event.chain_result(nodes)
                    return
            except Exception:
                pass
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)


    @filter.command("logs")
    async def handle_query_logs_en(self, event: AstrMessageEvent):
        async for result in self._handle_query_logs(event):
            yield result

    async def _handle_query_logs(self, event: AstrMessageEvent):
        """处理日志查询请求"""
        # 默认：最近 24 小时、第一页、20 条、type=0
        end_ts = int(time.time())
        start_ts = end_ts - 86400  # 24小时 = 86400秒
        params = {
            "p": 1,
            "page_size": self.log_page_size,
            "type": 0,
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
        }
        
        # 通知用户正在查询
        yield event.plain_result("正在查询最近的20条日志，请稍候...")
        
        # 获取日志数据
        payload = await self._fetch_logs(params)
        items = self._extract_log_items(payload)
        if not items:
            yield event.plain_result("未获取到有效日志数据")
            return
        
        # 构造合并转发（将所有日志合并到单个合并转发消息中）
        title = "📊 最近20条API调用日志"
        texts: List[str] = [self._format_log_item(it) for it in items]
        combined = "\n\n".join([title] + texts + [f"✅ 共查询到 {len(items)} 条日志"])
        
        # 发送日志
        if self.log_use_forward:
            try:
                nodes: List[Any] = [self._build_forward_node(combined)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        
        # 回退为纯文本多段发送
        max_len = 900
        text = combined or "(空)"
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    @filter.command("查询额度")
    async def handle_user_self(self, event: AstrMessageEvent):
        """命令：/查询额度 通过/api/user/search接口获取查询当前用户额度信息，并转化为人民币和积分发送"""
        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return
        
        # 通知用户正在查询
        yield event.plain_result("正在查询您的额度信息，请稍候...")
        
        try:
            # 获取用户ID并构建邮箱关键词
            # 假设event.message_obj.sender.user_id可以获取到用户ID
            user_id = str(getattr(getattr(event, 'message_obj', None), 'sender', None).user_id or '')
            if not user_id:
                # 如果获取不到用户ID，返回错误信息
                yield event.plain_result("获取用户信息失败：无法获取用户ID")
                return
            
            # 构建邮箱关键词（用户ID后拼接@qq.com）
            keyword = f"{user_id}@qq.com"
            
            # 调用用户搜索接口
            payload = await self._fetch_user_search(keyword)
            
            # 格式化返回结果
            text = self._format_user_quota_info(payload)
            
            # 发送结果（不使用合并转发，直接发送纯文本）
            yield event.plain_result(text)
        except Exception as e:
            # 处理异常情况
            logger.error(f"查询额度时发生错误: {e}")
            yield event.plain_result(f"查询失败：{str(e)}")


    async def _fetch_user_self(self):
        """原有的获取用户信息方法，保留以避免报错"""
        # 返回空字典，让代码可以继续运行
        return {"error": "此方法已被废弃，请使用_fetch_user_search"}

    @filter.command("模型状态")
    async def handle_model_status(self, event: AstrMessageEvent):
        """命令：/模型状态 查询最近十五分钟各模型的成功率，输出图片报告"""
        # 计算时间窗口：最近15分钟
        end_ts = int(time.time())
        start_ts = end_ts - 30 * 60  # 15分钟 = 900秒
        
        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return
        
        # 通知用户正在查询
        yield event.plain_result("正在查询最近30分钟的模型状态数据，请稍候...")
        
        # 定义请求的页面数和每页大小
        max_pages = 10
        page_size = 100
        all_items = []
        
        # 构建基础查询参数
        base_params = {
            "page_size": page_size,  # 页码大小固定传100
            "type": 0,  # 数据类型传0，包含成功和失败
            "username": "",  # 用户名留空
            "token_name": "",  # 令牌名留空
            "model_name": "",  # 模型名留空
            "start_timestamp": start_ts,  # 起始时间戳
            "end_timestamp": end_ts,  # 结束时间戳
            "channel": "",  # 渠道名留空
            "group": "",  # 分组名留空
        }
        
        # 请求前五页数据
        for page in range(1, max_pages + 1):
            # 设置当前页码
            params = base_params.copy()
            params["p"] = page
            
            # 获取日志数据
            payload = await self._fetch_logs(params)
            items = self._extract_log_items(payload)
            
            if items:
                all_items.extend(items)
            
            # 如果当前页数据不足page_size，说明这是最后一页
            if len(items) < page_size:
                break
        
        items = all_items
        
        if not items:
            yield event.plain_result("未获取到有效日志数据")
            return
        
        # 按模型名称统计成功率和平均用时
        model_stats = {}
        for item in items:
            model_name = item.get("model_name") or "未知模型"
            log_type = item.get("type")
            use_time_ms = int(item.get("use_time", 0) or 0)  # 获取使用时间（毫秒）
            
            completion_tokens = int(item.get("completion_tokens", 0) or 0)  # 获取输出token数
            
            # 初始化模型统计
            if model_name not in model_stats:
                model_stats[model_name] = {
                    "total": 0,
                    "success": 0,
                    "failure": 0,
                    "total_use_time_ms": 0,  # 总使用时间（毫秒）
                    "success_empty_completion": 0
                }
            
            # 更新统计数据
            model_stats[model_name]["total"] += 1
            if log_type == 2:  # 成功
                model_stats[model_name]["success"] += 1
                model_stats[model_name]["total_use_time_ms"] += use_time_ms  # 只统计成功请求的用时
                if completion_tokens == 0:
                    model_stats[model_name]["success_empty_completion"] += 1
            elif log_type == 5:  # 失败
                model_stats[model_name]["failure"] += 1
        
        # 格式化时间范围
        start_time_str = self._fmt_ts(start_ts)
        end_time_str = self._fmt_ts(end_ts)
        time_range = f"{start_time_str} 至 {end_time_str}"
        total_requests = sum(stats['total'] for stats in model_stats.values())
        
        # 准备模型数据列表（按成功数降序排序）
        models_data = []
        for model_name, stats in sorted(model_stats.items(), key=lambda x: x[1]['success'], reverse=True):
            success_rate = (stats['success'] / stats['total']) * 100 if stats['total'] > 0 else 0
            empty_response_rate = (stats['success_empty_completion'] / stats['success']) * 100 if stats['success'] > 0 else 0.0
            avg_use_time_s = (stats['total_use_time_ms'] / stats['success']) if stats['success'] > 0 else 0.0
            
            models_data.append({
                "name": model_name,
                "total": stats['total'],
                "success": stats['success'],
                "failure": stats['failure'],
                "avg_time": round(avg_use_time_s, 1),
                "success_rate": round(success_rate, 1),
                "empty_rate": round(empty_response_rate, 1)
            })
        
        # 尝试生成图片报告
        try:
            # 加载HTML模板
            template_content = self._load_template("model_status.html")
            if template_content:
                # 准备渲染数据
                render_data = {
                    "time_range": time_range,
                    "total_requests": f"{total_requests:,}",
                    "models": models_data
                }
                
                # 使用Jinja2渲染模板
                html_content = self._render_jinja2_template(template_content, render_data)
                
                if html_content:
                    # 尝试生成图片
                    image_url = await self._generate_image_report(html_content)
                    
                    if image_url:
                        # 发送图片
                        yield event.chain_result([Comp.Image.fromURL(image_url)])
                        return
                    else:
                        logger.warning("图片生成失败，回退到文本模式")
                else:
                    logger.warning("HTML渲染失败，回退到文本模式")
            else:
                logger.warning("模板加载失败，回退到文本模式")
        except Exception as e:
            logger.error(f"图片报告生成异常: {e}", exc_info=True)
        
        # 回退到文本模式
        lines = [
            "🔍 --- 模型状态报告 ---",
            f"⏱️ 时间范围: {time_range}（15分钟）",
            f"📊 总请求数: {total_requests:,}",
            "-------------------------",
        ]
        
        # 添加各模型的成功率数据和平均用时
        for model in models_data:
            lines.append("")
            lines.append(f"🤖 模型: {model['name']}")
            lines.append(f"  - 📈 总请求: {model['total']:,}")
            lines.append(f"  - ✅ 成功: {model['success']:,}")
            lines.append(f"  - ❌ 失败: {model['failure']:,}")
            lines.append(f"  - 📉 成功率: {model['success_rate']}%")
            lines.append(f"  - 💨 空回率: {model['empty_rate']}%")
            lines.append(f"  - ⏱️ 平均用时: {model['avg_time']}s")
        
        report = "\n".join(lines)
        
        # 发送报告
        if self.log_use_forward:
            try:
                nodes = [self._build_forward_node(report)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    @filter.command("用户状态")
    async def handle_user_status(self, event: AstrMessageEvent):
        """命令：/用户状态 查询最近十五分钟各用户的成功率"""
        # 计算时间窗口：最近15分钟
        end_ts = int(time.time())
        start_ts = end_ts - 15 * 60  # 15分钟 = 900秒
        
        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return
        
        # 通知用户正在查询
        yield event.plain_result("正在查询最近15分钟的用户状态数据，请稍候...")
        
        # 定义请求的页面数和每页大小
        max_pages = 10
        page_size = 100
        all_items = []
        
        # 构建基础查询参数
        base_params = {
            "page_size": page_size,  # 页码大小固定传100
            "type": 0,  # 数据类型传0，包含成功和失败
            "username": "",  # 用户名留空
            "token_name": "",  # 令牌名留空
            "model_name": "",  # 模型名留空
            "start_timestamp": start_ts,  # 起始时间戳
            "end_timestamp": end_ts,  # 结束时间戳
            "channel": "",  # 渠道名留空
            "group": "",  # 分组名留空
        }
        
        # 请求前五页数据
        for page in range(1, max_pages + 1):
            # 设置当前页码
            params = base_params.copy()
            params["p"] = page
            
            # 获取日志数据
            payload = await self._fetch_logs(params)
            items = self._extract_log_items(payload)
            
            if items:
                all_items.extend(items)
            
            # 如果当前页数据不足page_size，说明这是最后一页
            if len(items) < page_size:
                break
        
        items = all_items
        
        if not items:
            yield event.plain_result("未获取到有效日志数据")
            return
        
        # 按用户名统计成功率
        user_stats = {}
        for item in items:
            username = item.get("username") or "未知用户"
            log_type = item.get("type")
            
            completion_tokens = int(item.get("completion_tokens", 0) or 0)
            
            # 初始化用户统计
            if username not in user_stats:
                user_stats[username] = {
                    "total": 0,
                    "success": 0,
                    "failure": 0,
                    "success_empty_completion": 0
                }
            
            # 更新统计数据
            user_stats[username]["total"] += 1
            if log_type == 2:  # 成功
                user_stats[username]["success"] += 1
                if completion_tokens == 0:
                    user_stats[username]["success_empty_completion"] += 1
            elif log_type == 5:  # 失败
                user_stats[username]["failure"] += 1
        
        # 格式化结果
        start_time_str = self._fmt_ts(start_ts)
        end_time_str = self._fmt_ts(end_ts)
        
        lines = [
            "🔍 --- 用户状态报告 ---",
            f"⏱️ 时间范围: {start_time_str} 至 {end_time_str}（15分钟）",
            f"📊 总请求数: {sum(stats['total'] for stats in user_stats.values()):,}",
            "-------------------------",
        ]
        
        # 添加各用户的成功率数据（按成功数降序排序）
        for username, stats in sorted(user_stats.items(), key=lambda x: x[1]['success'], reverse=True):
            success_rate = (stats['success'] / stats['total']) * 100 if stats['total'] > 0 else 0
            empty_response_rate = (stats['success_empty_completion'] / stats['success']) * 100 if stats['success'] > 0 else 0.0
            lines.append("")
            lines.append(f"👤 用户: {username}")
            lines.append(f"  - 📈 总请求: {stats['total']:,}")
            lines.append(f"  - ✅ 成功: {stats['success']:,}")
            lines.append(f"  - ❌ 失败: {stats['failure']:,}")
            lines.append(f"  - 📉 成功率: {success_rate:.2f}%")
            lines.append(f"  - 💨 空回率: {empty_response_rate:.2f}%")
        
        report = "\n".join(lines)
        
        # 发送报告 - 强制使用合并转发
        try:
            nodes = [self._build_forward_node(report)]
            yield event.chain_result(nodes)
            return
        except Exception:
            # 如果合并转发失败，回退为纯文本多段发送
            pass
        
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    @filter.command("消费查询")
    async def handle_daily_consumption(self, event: AstrMessageEvent):
        """命令：/消费查询 查询今天的所有人累计消耗，显示前10名"""
        # 计算今天的时间窗口（从0点到现在）
        now = datetime.now(timezone(timedelta(hours=8)))
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = int(start_of_day.timestamp())
        end_ts = int(time.time())
        
        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return
        
        # 通知用户正在查询
        yield event.plain_result("正在查询今天的消费数据，大约需要5-10分钟。请稍候...")
        
        # 定义请求的页面数和每页大小
        max_pages = 2000  # 增加页数以获取更多数据
        page_size = 100
        all_items = []
        
        # 构建基础查询参数
        base_params = {
            "page_size": page_size,  # 页码大小固定传100
            "type": 2,  # 只查询消费记录
            "username": "",  # 用户名留空
            "token_name": "",  # 令牌名留空
            "model_name": "",  # 模型名留空
            "start_timestamp": start_ts,  # 起始时间戳
            "end_timestamp": end_ts,  # 结束时间戳
            "channel": "",  # 渠道名留空
            "group": "",  # 分组名留空
        }
        
        # 请求多页数据
        for page in range(1, max_pages + 1):
            # 设置当前页码
            params = base_params.copy()
            params["p"] = page
            
            # 获取日志数据
            payload = await self._fetch_logs(params)
            items = self._extract_log_items(payload)
            
            if items:
                all_items.extend(items)
            
            # 如果当前页数据不足page_size，说明这是最后一页
            if len(items) < page_size:
                break
            
        
        items = all_items
        
        if not items:
            yield event.plain_result("未获取到有效消费数据")
            return
        
        # 按用户名统计消费额度（quota除以50万）
        user_stats = {}
        for item in items:
            username = item.get("username") or "未知用户"
            quota = int(item.get("quota", 0) or 0)
            # 计算消费额度：quota / 500000，保留两位小数
            consumption = round(quota / 500000, 2)
            
            # 初始化用户统计
            if username not in user_stats:
                user_stats[username] = {
                    "total_consumption": 0.0,
                    "count": 0
                }
            
            # 更新统计数据
            user_stats[username]["total_consumption"] += consumption
            user_stats[username]["count"] += 1
        
        # 格式化结果
        start_time_str = self._fmt_ts(start_ts)
        end_time_str = self._fmt_ts(end_ts)
        total_consumption = sum(stats['total_consumption'] for stats in user_stats.values())
        
        # 检查是否达到服务端数据限制
        has_limit_notice = len(items) >= 18000
        
        # 对前10名用户使用/api/log/stat接口重新查询额度，以获取准确数据
        top_users = sorted(user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10]
        updated_user_stats = dict(user_stats)
        
        # 通知用户正在获取准确数据
        yield event.plain_result("正在获取前10名用户的准确消费数据，请稍候...")
        
        # 逐个查询前10名用户的准确数据
        for username, _ in top_users:
            try:
                # 调用/api/log/stat接口获取准确的消费数据
                accurate_consumption = await self._fetch_user_stat(username, start_ts, end_ts)
                if accurate_consumption >= 0:
                    # 更新用户统计数据
                    updated_user_stats[username]["total_consumption"] = accurate_consumption
                    logger.info(f"已更新用户 [{username}] 的消费数据为: {accurate_consumption}")
            except Exception as e:
                logger.error(f"更新用户 [{username}] 的消费数据时出错: {str(e)}")
                # 出错时保持原有数据
                pass
        
        lines = [
            "💰 --- 今日消费报告 ---",
            f"⏱️ 时间范围: {start_time_str} 至 {end_time_str}",
            f"📊 总查询记录: {len(items):,} 条",
            # f"💵 总消费额度: ${total_consumption:.2f}",
        ]
        
            
        lines.append("-------------------------")
        
        # 添加消费前10的用户数据（按总消费额度降序排序）
        rank_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, (username, stats) in enumerate(sorted(updated_user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10], 1):
            rank_emoji = rank_emojis[i-1] if i-1 < len(rank_emojis) else f"{i}️⃣"  # 确保有足够的表情符号
            lines.append("")
            lines.append(f"{rank_emoji} 用户: {username}")
            lines.append(f"  - 💵 总消费额度: ${stats['total_consumption']:.2f}")
            lines.append(f"  - 📈 请求次数: {stats['count']:,}")
        
        report = "\n".join(lines)
        
        # 发送报告
        if self.user_use_forward:
            try:
                nodes = [self._build_forward_node(report)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    @filter.command("昨日消费")
    async def handle_yesterday_consumption(self, event: AstrMessageEvent):
        """命令：/昨日消费 查询昨天的所有人累计消耗，显示前10名"""
        # 计算昨天的时间窗口（从昨天0点到昨天24点）
        today = datetime.now(timezone(timedelta(hours=8)))
        yesterday = today - timedelta(days=1)
        start_of_yesterday = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_yesterday = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        start_ts = int(start_of_yesterday.timestamp())
        end_ts = int(end_of_yesterday.timestamp())
        
        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return
        
        # 通知用户正在查询
        yield event.plain_result("正在查询昨天的消费数据，大约需要5-15分钟。请稍候...")
        
        # 定义请求的页面数和每页大小
        max_pages = 2000  # 增加页数以获取更多数据
        page_size = 100
        all_items = []
        
        # 构建基础查询参数
        base_params = {
            "page_size": page_size,  # 页码大小固定传100
            "type": 2,  # 只查询消费记录
            "username": "",  # 用户名留空
            "token_name": "",  # 令牌名留空
            "model_name": "",  # 模型名留空
            "start_timestamp": start_ts,  # 起始时间戳
            "end_timestamp": end_ts,  # 结束时间戳
            "channel": "",  # 渠道名留空
            "group": "",  # 分组名留空
        }
        
        # 请求多页数据
        for page in range(1, max_pages + 1):
            # 设置当前页码
            params = base_params.copy()
            params["p"] = page
            
            # 获取日志数据
            payload = await self._fetch_logs(params)
            items = self._extract_log_items(payload)
            
            if items:
                all_items.extend(items)
            
            # 如果当前页数据不足page_size，说明这是最后一页
            if len(items) < page_size:
                break
            
        
        items = all_items
        
        if not items:
            yield event.plain_result("未获取到有效消费数据")
            return
        
        # 按用户名统计消费额度（quota除以50万）
        user_stats = {}
        for item in items:
            username = item.get("username") or "未知用户"
            quota = int(item.get("quota", 0) or 0)
            # 计算消费额度：quota / 500000，保留两位小数
            consumption = round(quota / 500000, 2)
            
            # 初始化用户统计
            if username not in user_stats:
                user_stats[username] = {
                    "total_consumption": 0.0,
                    "count": 0
                }
            
            # 更新统计数据
            user_stats[username]["total_consumption"] += consumption
            user_stats[username]["count"] += 1
        
        # 格式化结果
        start_time_str = self._fmt_ts(start_ts)
        end_time_str = self._fmt_ts(end_ts)
        total_consumption = sum(stats['total_consumption'] for stats in user_stats.values())
        
        # 检查是否达到服务端数据限制
        has_limit_notice = len(items) >= 18000
        
        # 对前10名用户使用/api/log/stat接口重新查询额度，以获取准确数据
        top_users = sorted(user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10]
        updated_user_stats = dict(user_stats)
        
        # 通知用户正在获取准确数据
        yield event.plain_result("正在获取前10名用户的准确消费数据，请稍候...")
        
        # 逐个查询前10名用户的准确数据
        for username, _ in top_users:
            try:
                # 调用/api/log/stat接口获取准确的消费数据
                accurate_consumption = await self._fetch_user_stat(username, start_ts, end_ts)
                if accurate_consumption >= 0:
                    # 更新用户统计数据
                    updated_user_stats[username]["total_consumption"] = accurate_consumption
                    logger.info(f"已更新用户 [{username}] 的消费数据为: {accurate_consumption}")
            except Exception as e:
                logger.error(f"更新用户 [{username}] 的消费数据时出错: {str(e)}")
                # 出错时保持原有数据
                pass
        
        lines = [
            "💰 --- 昨日消费报告 ---",
            f"⏱️ 时间范围: {start_time_str} 至 {end_time_str}",
            f"📊 总查询记录: {len(items):,} 条",
            # f"💵 总消费额度: ${total_consumption:.2f}",
        ]
        
        # 如果达到服务端限制，添加提示信息
        if has_limit_notice:
            lines.append("⚠️ 提示: 已达到服务端数据总量限制(18000条)，此为部分数据")
            
        lines.append("-------------------------")
        
        # 添加消费前10的用户数据（按总消费额度降序排序）
        rank_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, (username, stats) in enumerate(sorted(updated_user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10], 1):
            rank_emoji = rank_emojis[i-1] if i-1 < len(rank_emojis) else f"{i}️⃣"  # 确保有足够的表情符号
            lines.append("")
            lines.append(f"{rank_emoji} 用户: {username}")
            lines.append(f"  - 💵 总消费额度: ${stats['total_consumption']:.2f}")
            lines.append(f"  - 📈 请求次数: {stats['count']:,}")
        
        report = "\n".join(lines)
        
        # 发送报告
        if self.user_use_forward:
            try:
                nodes = [self._build_forward_node(report)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    @filter.command("消费汇总")
    async def handle_consumption_summary(self, event: AstrMessageEvent):
        """命令：/消费汇总 查询今日和昨日的消费数据并合并发送，显示前10名"""
        # 检查基础配置
        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return
        
        # 通知用户正在查询
        yield event.plain_result("正在查询消费汇总数据，请稍候...")
        
        # 获取今日数据
        today_report = await self._get_daily_consumption_report()
        
        # 获取昨日数据
        yesterday_report = await self._get_yesterday_consumption_report()
        
        # 合并报告
        combined_report = today_report + "\n\n" + yesterday_report
        
        # 发送合并报告
        if self.user_use_forward:
            try:
                nodes = [self._build_forward_node(combined_report)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = combined_report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)
            if text:  # 如果还有剩余内容，添加分隔符
                yield event.plain_result("=== 继续显示下一部分 ===")
    
    async def _get_daily_consumption_report(self):
        """获取今日消费报告文本"""
        # 计算今天的时间窗口（从0点到现在）
        now = datetime.now(timezone(timedelta(hours=8)))
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = int(start_of_day.timestamp())
        end_ts = int(time.time())
        
        # 定义请求的页面数和每页大小
        max_pages = 1000
        page_size = 100
        all_items = []
        
        # 构建基础查询参数
        base_params = {
            "page_size": page_size,
            "type": 2,
            "username": "",
            "token_name": "",
            "model_name": "",
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "channel": "",
            "group": "",
        }
        
        # 请求多页数据
        for page in range(1, max_pages + 1):
            params = base_params.copy()
            params["p"] = page
            payload = await self._fetch_logs(params)
            items = self._extract_log_items(payload)
            
            if items:
                all_items.extend(items)
            
            if len(items) < page_size:
                break
        
        items = all_items
        
        if not items:
            return "💰 --- 今日消费报告 ---(未获取到有效数据)"
        
        # 按用户名统计消费额度
        user_stats = {}
        for item in items:
            username = item.get("username") or "未知用户"
            quota = int(item.get("quota", 0) or 0)
            consumption = round(quota / 500000, 2)
            
            if username not in user_stats:
                user_stats[username] = {"total_consumption": 0.0, "count": 0}
            
            user_stats[username]["total_consumption"] += consumption
            user_stats[username]["count"] += 1
        
        # 格式化结果
        start_time_str = self._fmt_ts(start_ts)
        end_time_str = self._fmt_ts(end_ts)
        total_consumption = sum(stats['total_consumption'] for stats in user_stats.values())
        
        # 对前10名用户使用/api/log/stat接口重新查询额度，以获取准确数据
        top_users = sorted(user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10]
        updated_user_stats = dict(user_stats)
        
        # 逐个查询前10名用户的准确数据
        for username, _ in top_users:
            try:
                # 调用/api/log/stat接口获取准确的消费数据
                accurate_consumption = await self._fetch_user_stat(username, start_ts, end_ts)
                if accurate_consumption >= 0:
                    # 更新用户统计数据
                    updated_user_stats[username]["total_consumption"] = accurate_consumption
                    logger.info(f"已更新用户 [{username}] 的消费数据为: {accurate_consumption}")
            except Exception as e:
                logger.error(f"更新用户 [{username}] 的消费数据时出错: {str(e)}")
                # 出错时保持原有数据
                pass
        
        lines = [
            "💰 --- 今日消费报告 ---",
            f"⏱️ 时间范围: {start_time_str} 至 {end_time_str}",
            f"📊 总查询记录: {len(items):,} 条",
            # f"💵 总消费额度: ${total_consumption:.2f}",
            "-------------------------",
        ]
        
        # 添加消费前10的用户数据
        rank_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, (username, stats) in enumerate(sorted(updated_user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10], 1):
            rank_emoji = rank_emojis[i-1] if i-1 < len(rank_emojis) else f"{i}️⃣"
            lines.append("")
            lines.append(f"{rank_emoji} 用户: {username}")
            lines.append(f"  - 💵 总消费额度: ${stats['total_consumption']:.2f}")
            lines.append(f"  - 📈 请求次数: {stats['count']:,}")
        
        return "\n".join(lines)
    
    async def _get_yesterday_consumption_report(self):
        """获取昨日消费报告文本"""
        # 计算昨天的时间窗口
        today = datetime.now(timezone(timedelta(hours=8)))
        yesterday = today - timedelta(days=1)
        start_of_yesterday = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_yesterday = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        start_ts = int(start_of_yesterday.timestamp())
        end_ts = int(end_of_yesterday.timestamp())
        
        # 定义请求的页面数和每页大小
        max_pages = 1000
        page_size = 100
        all_items = []
        
        # 构建基础查询参数
        base_params = {
            "page_size": page_size,
            "type": 2,
            "username": "",
            "token_name": "",
            "model_name": "",
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "channel": "",
            "group": "",
        }
        
        # 请求多页数据
        for page in range(1, max_pages + 1):
            params = base_params.copy()
            params["p"] = page
            payload = await self._fetch_logs(params)
            items = self._extract_log_items(payload)
            
            if items:
                all_items.extend(items)
            
            if len(items) < page_size:
                break
        
        items = all_items
        
        if not items:
            return "💰 --- 昨日消费报告 ---(未获取到有效数据)"
        
        # 按用户名统计消费额度
        user_stats = {}
        for item in items:
            username = item.get("username") or "未知用户"
            quota = int(item.get("quota", 0) or 0)
            consumption = round(quota / 500000, 2)
            
            if username not in user_stats:
                user_stats[username] = {"total_consumption": 0.0, "count": 0}
            
            user_stats[username]["total_consumption"] += consumption
            user_stats[username]["count"] += 1
        
        # 格式化结果
        start_time_str = self._fmt_ts(start_ts)
        end_time_str = self._fmt_ts(end_ts)
        total_consumption = sum(stats['total_consumption'] for stats in user_stats.values())
        
        # 对前10名用户使用/api/log/stat接口重新查询额度，以获取准确数据
        top_users = sorted(user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10]
        updated_user_stats = dict(user_stats)
        
        # 逐个查询前10名用户的准确数据
        for username, _ in top_users:
            try:
                # 调用/api/log/stat接口获取准确的消费数据
                accurate_consumption = await self._fetch_user_stat(username, start_ts, end_ts)
                if accurate_consumption >= 0:
                    # 更新用户统计数据
                    updated_user_stats[username]["total_consumption"] = accurate_consumption
                    logger.info(f"已更新用户 [{username}] 的消费数据为: {accurate_consumption}")
            except Exception as e:
                logger.error(f"更新用户 [{username}] 的消费数据时出错: {str(e)}")
                # 出错时保持原有数据
                pass
        
        lines = [
            "💰 --- 昨日消费报告 ---",
            f"⏱️ 时间范围: {start_time_str} 至 {end_time_str}",
            f"📊 总查询记录: {len(items):,} 条",
            # f"💵 总消费额度: ${total_consumption:.2f}",
            "-------------------------",
        ]
        
        # 添加消费前10的用户数据
        rank_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, (username, stats) in enumerate(sorted(updated_user_stats.items(), key=lambda x: x[1]['total_consumption'], reverse=True)[:10], 1):
            rank_emoji = rank_emojis[i-1] if i-1 < len(rank_emojis) else f"{i}️⃣"
            lines.append("")
            lines.append(f"{rank_emoji} 用户: {username}")
            lines.append(f"  - 💵 总消费额度: ${stats['total_consumption']:.2f}")
            lines.append(f"  - 📈 请求次数: {stats['count']:,}")
        
        return "\n".join(lines)

    async def terminate(self):
        """插件终止时的清理工作"""
        logger.info("已卸载 [XiguaUsageReporter] 插件。")
