"""身份适配器（M7）：开发期读请求头、生产期校验 JWT。

两者的取舍见各自模块的 docstring；**选择哪一个由组合根按 `AUTH_MODE` 决定**
（`app/api/deps.py`），业务代码只依赖 `app.ports.identity_provider.IdentityProvider`。
"""
