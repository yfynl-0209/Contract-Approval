"""全局配置：从 .env 读取，缺省值保证零配置可运行。

**所有配置项都带合理默认值**，且默认可在"无数据库、无模型 Key"的环境下启动 ——
这是"三层降级"要求的基础。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 用 __file__ 推导而非 os.getcwd()，保证从任何目录启动都定位到同一份 .env 与 data/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",  # 绝对路径，避免相对"当前工作目录"去找 .env
        env_file_encoding="utf-8",
        # .env 中可能有本类未声明的字段（临时调试项），安静忽略而不是启动即报错
        extra="ignore",
    )

    # ---- 服务地址 ----
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # ---- mock 审批系统（独立进程，端口 8001）----
    mock_approval_base_url: str = "http://127.0.0.1:8001"
    mock_approval_token: str = "demo-token"

    # ---- 数据库与存储 ----
    db_url: str = "sqlite:///./data/app.db"
    storage_root: str = "./storage"

    # ---- 审批接入 ----
    # 去重唯一键 = approval_provider + tenant_id + instance_id
    approval_provider: str = "mock"
    # v1 固定默认租户；字段先留，避免接入第二个企业时全库迁移
    tenant_id: str = "default"
    gateway_timeout_seconds: float = 10.0
    # 同一窗口内的重复拉取合流为一个作业，跨窗口视为新的一次拉取
    pull_window_minutes: int = 1

    # ---- 附件校验 ----
    attachment_max_bytes: int = 20 * 1024 * 1024  # 20 MB
    #: 允许的附件类型。⚠️ 三项是**需求**（§4.9 修-17）：图片扫描件同样是合同，
    #: 只收 PDF 会把它判成 `ATTACHMENT_TYPE_NOT_ALLOWED` ——
    #: 而"手机拍的合同照片"恰好是这类系统最常见的一类输入。
    #: 图片在解析侧被**规范化为单页虚拟 PDF**，坐标系与 PDF 完全一致（§4.9 修-24）。
    attachment_allowed_types: str = "application/pdf,image/png,image/jpeg"

    # ---- 解析（M4；运行期只有 app/adapters/parse/ 使用 PyMuPDF）----
    # 页数与渲染像素上限是**内存保护**：一份 500 页扫描件按 200dpi 渲染有数十亿像素，
    # 进程会被 OOM 杀掉 —— 而"进程被杀"连错误码都留不下，排查时只剩"任务卡住了"。
    # 因此在**分配之前**按页面尺寸推算并拒绝。
    parse_max_pages: int = 300
    parse_max_render_pixels: int = 60_000_000
    parse_render_dpi: int = 200
    parse_ocr_min_confidence: float = 0.6
    # 执行时限（秒）：PyMuPDF 与 ONNX Runtime 都不能从外部中断，
    # 没有时限时一次卡死会让 Worker **永远阻塞**，而且不留任何错误码。
    # ⚠️ 超时只是"不再等待"，那个调用仍在后台跑 —— 反复超时是"输入有问题
    #    或超时值太小"的信号，见 `app/deadline.py`。
    parse_render_timeout_seconds: float = 30.0
    parse_ocr_timeout_seconds: float = 60.0

    # ---- 对象存储（M3 用 local；M9 增 minio，端口语义不变）----
    storage_backend: str = "local"
    # MinIO / S3 兼容端点（storage_backend=minio 时必填）。
    # ⚠️ endpoint 只写 host[:port]，不带 scheme —— scheme 由 `minio_secure` 决定
    minio_endpoint: str = "127.0.0.1:59000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "contract-objects"
    minio_secure: bool = False
    # presign 的有效期（秒）。给短不给长：泄露的签名 URL 在过期前是免鉴权通道
    presign_expires_seconds: int = 600

    # ---- 规则评价（M5）----
    #: 本项目运行币种。金额类字段的数值比较**只有在该币种下才有意义**
    #: （见 `app/rules/fields.py` 的 `MONEY_FIELDS`）。
    #: 放在配置里而不是写死在匹配器里：换一个市场只改配置，不改判断逻辑。
    default_currency: str = "CNY"

    # ---- LLM（三层降级：留空即纯规则模式）----
    # 首选 OpenAI 兼容接口 → 无 Key 时字段抽取退回正则 → 解析失败则 blocked
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = 30
    #: 单次送进模型的正文上限（字符）。**超限时拒绝判断**，不截断 ——
    #: 截断会让"判断不完整"与"判断为否"在下游变得无法区分（见 app/rules/llm_judge.py）。
    llm_max_input_chars: int = 20_000

    # ---- harness（回写门禁）----
    # True：整体风险为 high 时必须人工确认才允许回写。"AI 不代替人工审批"的执行开关。
    high_risk_require_manual: bool = True
    writeback_max_retry: int = 2

    # ---- 身份与授权（M7）----
    #: 部署环境。**`production` 会触发启动期的身份配置校验**（见 `app/auth.py`）：
    #: 生产环境选了开发期身份来源 → **拒绝启动**，而不是降级为匿名访问。
    env: str = "development"
    #: `dev` = 读显式请求头（**无条件信任**，仅开发期）；`jwt` = 校验 JWT。
    auth_mode: str = "dev"
    #: 静态公钥（PEM）。与 `jwt_jwks_url` 二选一，同时给出时**以静态公钥为准**。
    jwt_public_key: str = ""
    #: JWKS 端点。用于密钥轮换；不可达时返回 **503 而非 401**（判断不了 ≠ 无效）。
    jwt_jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    #: ⚠️ **验签算法白名单，必须来自配置**。取自令牌的 `alg` 头等于让攻击者选门锁。
    jwt_algorithms: str = "RS256"
    # 声明名可配：企业 IdP 的字段名并不统一
    jwt_subject_claim: str = "sub"
    jwt_name_claim: str = "name"
    jwt_roles_claim: str = "roles"
    jwt_tenant_claim: str = "tenant_id"
    #: 允许的时钟偏移（秒）。给的是服务器间的时间同步误差，
    #: 不是"令牌过期后还能宽限多久"—— 调大它等于放宽有效期。
    jwt_leeway_seconds: int = 0

    # ---- 派生属性 ----
    @property
    def storage_path(self) -> Path:
        """存储根目录（绝对路径；相对路径同样锚定到项目根）。"""
        path = Path(self.storage_root)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def allowed_attachment_types(self) -> tuple[str, ...]:
        """附件类型白名单（统一转小写，避免响应头大小写差异误判）。"""
        return tuple(
            item.strip().lower()
            for item in self.attachment_allowed_types.split(",")
            if item.strip()
        )

    @property
    def llm_enabled(self) -> bool:
        """是否具备调用大模型的条件（三项配置**全部**非空才算可用）。"""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def jwt_algorithm_list(self) -> tuple[str, ...]:
        """验签算法白名单（逗号分隔，去空、转大写）。"""
        return tuple(
            item.strip().upper()
            for item in self.jwt_algorithms.split(",")
            if item.strip()
        )

    #: 低/中风险完整结果是否允许**未确认**自动回写（M6 Fixed Decision 4）。
    #: 默认 False；高风险 / needs_review **永远**要求有效确认，不受此开关影响。
    auto_writeback_enabled: bool = False


# 模块级单例：全项目共用同一份配置
settings = Settings()
