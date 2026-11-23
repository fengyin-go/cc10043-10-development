"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import receipt_guard, resource

# 回执守卫是固定前缀路由，先注册以保证其 /api/receipt-guard/* 路径优先匹配。
ROUTERS = [receipt_guard, resource]
